"""Capture et reassemblage.

On ne code pas de sniffer : on delegue a tshark et on ne lit que des champs.
Ce module transforme des lignes tshark en payloads de trames, en respectant les
deux regles de reassemblage :

* concatener par flux TCP et dans l'ordre, jamais paquet par paquet ;
* garder les deux sens d'une connexion dans des tampons separes.

Il connait tshark, pas les octets du jeu : le decoupage en trames est delegue a
`wire.FrameReader`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from . import wire
from .wire import Frame, FrameReader, FrameStats

__all__ = [
    "TsharkNotFound",
    "find_tshark",
    "Interface",
    "list_interfaces",
    "probe_interfaces",
    "Payload",
    "CaptureSource",
    "LiveSource",
    "PcapSource",
    "DirSource",
    "iter_payloads",
    "discover_ports",
    "autodetect",
    "Detection",
    "Attempt",
    "DiscoveryReport",
    "PortScorer",
    "PortScore",
    "WEB_PORTS",
]

WEB_PORTS = (80, 443, 8080, 8443, 5228)

_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Wireshark\tshark.exe",
    r"C:\Program Files (x86)\Wireshark\tshark.exe",
)


class TsharkNotFound(RuntimeError):
    pass


def find_tshark(explicit: str | None = None) -> str:
    """Localise tshark. Sous Windows il est rarement dans le PATH."""
    if explicit:
        if Path(explicit).exists():
            return explicit
        raise TsharkNotFound(f"tshark introuvable a {explicit}")
    found = shutil.which("tshark")
    if found:
        return found
    for cand in _WINDOWS_CANDIDATES:
        if Path(cand).exists():
            return cand
    raise TsharkNotFound(
        "tshark introuvable. Installe Wireshark (qui fournit Npcap, requis sous "
        "Windows) ou passe --tshark <chemin>."
    )


_IFACE_RE = re.compile(r"^(\d+)\.\s+(\S+)(?:\s+\((.*)\))?\s*$")

#: Interfaces virtuelles qui ne portent jamais de trafic de jeu.
_SKIP_PROBE = ("etwdump", "ciscodump", "randpktdump", "sshdump", "udpdump", "dpauxmon")


@dataclass(frozen=True)
class Interface:
    number: str
    device: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.device} ({self.name})" if self.name else self.device


def list_interfaces(tshark: str | None = None) -> list[Interface]:
    exe = find_tshark(tshark)
    proc = subprocess.run([exe, "-D"], capture_output=True, text=True, errors="replace")
    out: list[Interface] = []
    for line in proc.stdout.splitlines():
        m = _IFACE_RE.match(line.strip())
        if m:
            out.append(Interface(m.group(1), m.group(2), m.group(3) or ""))
    return out


def probe_interfaces(
    seconds: int = 8, tshark: str | None = None
) -> list[tuple[Interface, int, str]]:
    """Compte les paquets vus sur chaque interface, en parallele.

    Choisir une interface a l'oeil est une devinette : une machine a souvent
    du Wi-Fi et de l'Ethernet declares alors qu'un seul porte du trafic.
    Compter est une preuve, et ca coute quelques secondes.
    """
    exe = find_tshark(tshark)
    targets = [
        i for i in list_interfaces(tshark)
        if not any(s in i.device.lower() for s in _SKIP_PROBE)
    ]

    def count(iface: Interface) -> tuple[Interface, int, str]:
        proc = subprocess.run(
            [exe, "-i", iface.device, "-n", "-a", f"duration:{seconds}",
             "-q", "-z", "io,stat,0"],
            capture_output=True, text=True, errors="replace",
        )
        m = re.search(r"(\d+) packets? captured", proc.stdout + proc.stderr)
        if m:
            return iface, int(m.group(1)), ""
        tail = (proc.stderr.strip().splitlines() or [""])[-1]
        return iface, -1, tail[:120]

    with ThreadPoolExecutor(max_workers=min(8, len(targets) or 1)) as pool:
        results = list(pool.map(count, targets))
    return sorted(results, key=lambda r: r[1], reverse=True)


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payload:
    """Un payload de trame, pret a decoder."""

    data: bytes
    stream: str          # cle de sens de connexion : "<tcp.stream>/<srcport>"
    seq: int
    flag: int
    compressed: bool
    origin: str = ""     # nom de fichier quand la source est un repertoire

    @property
    def label(self) -> str:
        return self.origin or f"{self.stream}#{self.seq}"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class CaptureSource:
    """Interface commune. `payloads()` rend des `Payload` dans l'ordre."""

    description: str = "source"

    def payloads(self) -> Iterator[Payload]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def stats(self) -> FrameStats:
        return FrameStats()


class _TsharkSource(CaptureSource):
    """Base des sources tshark : lance le process, reassemble, decoupe."""

    def __init__(self, tshark: str | None = None) -> None:
        self._tshark = find_tshark(tshark)
        self._stats = FrameStats()
        self._readers: dict[str, FrameReader] = {}
        self.packets = 0
        self._proc: subprocess.Popen[str] | None = None
        self._stopped = False

    def stop(self) -> None:
        """Arrete la capture en cours. Ce qui a ete recu est traite normalement.

        Le flush de fin de flux tourne quand meme : les trames deja completes
        dans les tampons ne sont pas perdues.
        """
        self._stopped = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    @property
    def stats(self) -> FrameStats:
        return self._stats

    def _argv(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def payloads(self) -> Iterator[Payload]:
        argv = self._argv()
        if self._stopped:
            return
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
        self._proc = proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                for payload in self._on_line(line):
                    yield payload
        except KeyboardInterrupt:
            proc.terminate()
            print("\n[capture interrompue -- traitement de ce qui a ete recu]",
                  file=sys.stderr)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            rc = proc.poll()
            if rc is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            err = ""
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read() or ""
                finally:
                    proc.stderr.close()
            if proc.returncode not in (0, None) and self.packets == 0 and err.strip():
                print(f"[tshark] {err.strip()}", file=sys.stderr)
        yield from self._flush()

    # -- reassemblage -----------------------------------------------------
    def _on_line(self, line: str) -> Iterator[Payload]:
        parsed = _parse_line(line)
        if parsed is None:
            return
        stream, srcport, data = parsed
        if not data:
            return
        self.packets += 1
        # Une connexion porte deux flux independants : la cle inclut le port
        # source, sinon on entrelace deux sequences de trames et on desynchronise
        # les deux.
        key = f"{stream}/{srcport}"
        reader = self._readers.get(key)
        if reader is None:
            reader = self._readers[key] = FrameReader(key)
        for frame in reader.feed(data):
            yield _to_payload(frame, key)

    def _flush(self) -> Iterator[Payload]:
        for key, reader in self._readers.items():
            for frame in reader.flush():
                yield _to_payload(frame, key)
            self._stats.merge(reader.stats)


def _to_payload(frame: Frame, stream: str) -> Payload:
    return Payload(
        data=frame.payload,
        stream=stream,
        seq=frame.seq,
        flag=frame.flag,
        compressed=frame.compressed,
    )


def _parse_line(line: str) -> tuple[str, str, bytes] | None:
    """tcp.stream <TAB> tcp.srcport <TAB> tcp.payload (hexa)."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    stream, srcport, payload_field = parts[0], parts[1], parts[2]
    chunks: list[bytes] = []
    # -E occurrence=a joint les occurrences multiples par ','. Certaines
    # versions de tshark separent les octets par ':'.
    for occ in payload_field.split(","):
        hexstr = occ.replace(":", "").replace(" ", "").strip()
        if not hexstr:
            continue
        if len(hexstr) % 2:
            hexstr = hexstr[:-1]
        try:
            chunks.append(bytes.fromhex(hexstr))
        except ValueError:
            continue
    if not chunks:
        return None
    return stream, srcport, b"".join(chunks)


_FIELDS = ["-e", "tcp.stream", "-e", "tcp.srcport", "-e", "tcp.payload"]
_FORMAT = ["-T", "fields", "-E", "separator=/t", "-E", "occurrence=a"]


class LiveSource(_TsharkSource):
    def __init__(
        self,
        iface: str,
        port: int,
        duration: int | None = None,
        tshark: str | None = None,
        snaplen: int = 0,
    ) -> None:
        super().__init__(tshark)
        self.iface = iface
        self.port = port
        self.duration = duration
        self.snaplen = snaplen
        self.description = f"live iface={iface} port={port}"

    def _argv(self) -> list[str]:
        flt = (
            f"tcp.port == {self.port} && tcp.len > 0 "
            f"&& !tcp.analysis.retransmission"
        )
        argv = [self._tshark, "-i", self.iface, "-n", "-l", "-Y", flt]
        if self.duration:
            argv += ["-a", f"duration:{self.duration}"]
        if self.snaplen:
            argv += ["-s", str(self.snaplen)]
        return argv + _FORMAT + _FIELDS


class PcapSource(_TsharkSource):
    def __init__(self, path: str | Path, port: int | None = None,
                 tshark: str | None = None) -> None:
        super().__init__(tshark)
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.port = port
        self.description = f"pcap {self.path.name}" + (f" port={port}" if port else "")

    def _argv(self) -> list[str]:
        flt = "tcp.len > 0 && !tcp.analysis.retransmission"
        if self.port:
            flt = f"tcp.port == {self.port} && " + flt
        return [self._tshark, "-r", str(self.path), "-n", "-Y", flt] + _FORMAT + _FIELDS


class DirSource(CaptureSource):
    """Re-ingere un repertoire de payloads sauvegardes par `inspect --save`.

    Sans ca il faudrait recapturer a chaque iteration.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.description = f"dir {self.path}"

    def payloads(self) -> Iterator[Payload]:
        files = sorted(p for p in self.path.rglob("*.bin") if p.is_file())
        for i, fp in enumerate(files):
            data = fp.read_bytes()
            if not data:
                continue
            rel = fp.relative_to(self.path).as_posix()
            yield Payload(
                data=data,
                stream=fp.parent.name,
                seq=i,
                flag=-1,
                compressed=False,
                origin=rel,
            )


def iter_payloads(source: CaptureSource) -> Iterator[Payload]:
    return source.payloads()


# ---------------------------------------------------------------------------
# Decouverte de port
# ---------------------------------------------------------------------------
#
# Le port du jeu change a chaque session. On n'essaie pas de deviner : on
# ecoute tous les ports TCP non-web et on SCORE chaque port en lui appliquant
# le vrai decodeur de trames. Si les octets se decoupent en trames valides,
# c'est le bon canal. Une plage de ports ou un nom de processus serait une
# devinette ; faire tourner le decodeur est une preuve.


@dataclass
class PortScore:
    port: int
    packets: int = 0
    bytes_seen: int = 0
    frames: int = 0
    decoded: int = 0
    exact: int = 0
    commands: set[str] = field(default_factory=set)
    stats: FrameStats = field(default_factory=FrameStats)
    #: Prefixe hexa des premiers payloads, pour diagnostic (opt-in a l'affichage).
    hex_samples: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Nombre de trames dont le payload decode jusqu'au dernier octet."""
        return self.exact + 0.25 * self.decoded + 0.05 * self.frames

    def framing_note(self) -> str:
        """Pourquoi ce port n'a rien rendu -- la question que pose un score nul.

        La magie zstd est le signal le plus fort : elle ne survient pas par
        hasard. La voir sans rien decoder veut dire "bon protocole, detail
        different" ; ne pas la voir veut dire "pas ce protocole du tout".
        """
        s = self.stats
        bits = [f"brutes={s.raw_frames}", f"zstd={s.zstd_frames}"]
        if s.resyncs:
            bits.append(f"resync={s.resyncs} ({s.resync_bytes_skipped} o sautes)")
        if s.zstd_errors:
            bits.append(f"erreurs zstd={s.zstd_errors}")
        if s.size_mismatches:
            bits.append(f"taille decomp incoherente={s.size_mismatches}")
        if s.zero_length_headers:
            bits.append(f"len=0={s.zero_length_headers}")
        if s.flag_counts:
            flags = ",".join(f"0x{f:02x}x{n}" for f, n in
                             sorted(s.flag_counts.items(), key=lambda kv: -kv[1])[:4])
            bits.append(f"flags={flags}")
        return "  ".join(bits)

    def verdict(self) -> str:
        if self.exact:
            return "canal du jeu"
        if self.stats.zstd_frames or self.stats.zstd_errors:
            return "magie zstd vue : protocole proche, detail different"
        if self.stats.resync_bytes_skipped > self.bytes_seen * 0.5:
            return "le decoupage en trames ne prend pas : autre protocole"
        return "aucune trame exploitable"


@dataclass
class DiscoveryReport:
    """Resultat de `discover`, avec de quoi distinguer les causes d'un echec.

    Sans ces compteurs, "aucun port candidat" est indiscernable de "tshark a
    echoue", "l'interface est morte" et "tout le trafic est sur 443".
    """

    scores: list[PortScore] = field(default_factory=list)
    total_packets: int = 0
    web_packets: int = 0
    web_ports: set[int] = field(default_factory=set)
    #: Echec de la phase de CAPTURE : rien n'a pu etre observe.
    tshark_error: str = ""
    #: Incidents non bloquants (relecture partielle, fichier tronque...). Ils
    #: n'invalident pas un scoring qui a produit des resultats : ce qui a ete
    #: decode l'a ete pour de bon.
    warnings: list[str] = field(default_factory=list)
    pcap_path: str = ""
    pcap_packets: int = -1

    def diagnosis(self, keep_web: bool) -> str | None:
        """Ce qui explique une liste vide, quand elle l'est.

        Ne rend jamais un verdict d'echec quand des ports ont ete scores : un
        incident de relecture est un avertissement, pas une infirmation des
        octets deja decodes.
        """
        if self.tshark_error and not self.scores:
            return f"tshark a echoue : {self.tshark_error}"
        if self.total_packets == 0:
            return (
                "aucun paquet TCP avec charge utile sur cette interface. Ce n'est"
                " pas un probleme de port : l'interface elle-meme ne voit rien."
                " Lance `lwvs ifaces --probe` pour trouver celle qui porte le"
                " trafic."
            )
        if not self.scores:
            if self.web_packets:
                ports = ", ".join(str(p) for p in sorted(self.web_ports))
                return (
                    f"{self.total_packets} paquet(s) vus, mais tous sur des ports web"
                    f" ({ports}), exclus du scoring. Si le jeu parle en TLS-like sur"
                    " 443, relance avec --keep-web : le trafic n'est pas chiffre, seul"
                    " le port l'est."
                )
            return "des paquets ont ete vus mais aucun port n'a produit de trame."
        if not any(s.exact for s in self.scores):
            return (
                "des ports ont ete vus mais aucun ne produit de trame decodable."
                " Le jeu tournait-il, et l'ecran VS etait-il ouvert ?"
            )
        return None


class PortScorer:
    """Score chaque port en lui appliquant le VRAI decodeur de trames.

    Testable sans tshark : `feed()` prend des octets deja extraits.
    """

    def __init__(self, exclude: Sequence[int] = WEB_PORTS, keep_web: bool = False) -> None:
        self.exclude = frozenset() if keep_web else frozenset(exclude)
        self.report = DiscoveryReport()
        self._scores: dict[int, PortScore] = {}
        self._readers: dict[tuple[int, str], FrameReader] = {}

    def _bucket(self, port: int) -> PortScore:
        sc = self._scores.get(port)
        if sc is None:
            sc = self._scores[port] = PortScore(port)
        return sc

    def feed(self, srcport: int, dstport: int, stream: str, data: bytes) -> None:
        self.report.total_packets += 1
        # Le port "du jeu" est le plus bas des deux : le port serveur.
        port = min(srcport, dstport)
        if port in self.exclude:
            self.report.web_packets += 1
            self.report.web_ports.add(port)
            return
        sc = self._bucket(port)
        sc.packets += 1
        sc.bytes_seen += len(data)
        key = (port, f"{stream}/{srcport}")
        reader = self._readers.get(key)
        if reader is None:
            reader = self._readers[key] = FrameReader(str(key))
            # Premiers octets de chaque sens : c'est la ou se lit le framing.
            if len(sc.hex_samples) < 4:
                sc.hex_samples.append(f"debut {key[1]}: {data[:48].hex(' ')}")
        self._consume(port, reader.feed(data))

    def _consume(self, port: int, frames: Iterable[Frame]) -> None:
        from .messages import command_of  # tardif : capture ne connait pas le domaine

        sc = self._bucket(port)
        for frame in frames:
            sc.frames += 1
            try:
                root = wire.decode_payload(frame.payload)
            except (wire.RootDecodeError, wire.DecodeError) as exc:
                if len(sc.hex_samples) < 8:
                    kind = "zstd" if frame.compressed else "brut"
                    sc.hex_samples.append(
                        f"trame {kind} #{frame.seq} ({len(frame.payload)} o) non"
                        f" decodee: {frame.payload[:48].hex(' ')}"
                        f"  [{type(exc).__name__}]"
                    )
                continue
            sc.decoded += 1
            if root.exact:
                sc.exact += 1
            cmd = command_of(root.value)
            if cmd:
                sc.commands.add(cmd)

    def leader(self) -> PortScore | None:
        """Le port en tete A CET INSTANT, sans clore le scoring.

        `finish()` vide les tampons de reassemblage : l'appeler juste pour
        regarder ou on en est couperait les trames en cours. La detection a
        besoin de savoir "est-ce deja prouve ?" a chaque paquet, d'ou cette
        vue en lecture seule.
        """
        live = [s for s in self._scores.values() if s.packets]
        if not live:
            return None
        return max(live, key=lambda s: (s.exact, s.score, s.bytes_seen))

    def finish(self, top: int = 10) -> DiscoveryReport:
        for (port, _), reader in self._readers.items():
            self._consume(port, reader.flush())
            self._bucket(port).stats.merge(reader.stats)
        ranked = sorted(self._scores.values(),
                        key=lambda s: (s.score, s.bytes_seen), reverse=True)
        self.report.scores = [s for s in ranked if s.packets][:top]
        return self.report


def _meaningful_stderr(err: str) -> str:
    noise = ("Capturing on", "packets captured", "packet captured", "packets dropped")
    lines = [ln for ln in err.strip().splitlines()
             if ln.strip() and not any(n in ln for n in noise)]
    return " / ".join(lines)[:300]


#: Champs du scoring : ceux de la capture, plus le port destination -- le port
#: "du jeu" est le plus bas des deux, il faut donc les deux.
_SCORE_FIELDS = ["-e", "tcp.stream", "-e", "tcp.srcport", "-e", "tcp.dstport",
                 "-e", "tcp.payload"]
_SCORE_FILTER = "tcp.len > 0 && !tcp.analysis.retransmission"


def _feed_line(scorer: "PortScorer", line: str) -> bool:
    """Une ligne `stream/srcport/dstport/payload` -> scorer.

    Rend True si la ligne etait bien un paquet. Un seul endroit sait decouper
    cette ligne : la relecture d'un pcap et la detection en direct lisent le
    meme format, et diverger les ferait scorer differemment.
    """
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 4:
        return False
    stream, srcport, dstport, payload_field = parts[0], parts[1], parts[2], parts[3]
    parsed = _parse_line("\t".join((stream, srcport, payload_field)))
    if parsed is None:
        return True                       # paquet vu, mais rien d'exploitable
    try:
        sp, dp = int(srcport), int(dstport)
    except ValueError:
        return True
    scorer.feed(sp, dp, stream, parsed[2])
    return True


def feed_pcap(
    scorer: "PortScorer", path: str | Path, tshark: str | None = None
) -> tuple[int, str]:
    """Rejoue un pcap dans un scorer. Rend (paquets lus, erreur eventuelle)."""
    exe = find_tshark(tshark)
    argv = [exe, "-r", str(path), "-n", "-Y", _SCORE_FILTER]
    argv += ["-T", "fields", "-E", "separator=/t", "-E", "occurrence=a"]
    argv += _SCORE_FIELDS
    proc = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    read = 0
    for line in proc.stdout.splitlines():
        if _feed_line(scorer, line):
            read += 1
    return read, _meaningful_stderr(proc.stderr)


def discover_ports(
    iface: str,
    duration: int = 25,
    tshark: str | None = None,
    exclude: Sequence[int] = WEB_PORTS,
    top: int = 10,
    keep_web: bool = False,
    pcap_out: str | Path | None = None,
) -> DiscoveryReport:
    """Ecoute tout, decoupe par port, score en decodant reellement.

    Deux phases : tshark ne peut pas ecrire un pcap et extraire des champs dans
    le meme passage. On capture d'abord, on rejoue ensuite. La capture brute est
    conservee (`pcap_out`), ce qui evite de recapturer a chaque hypothese.

    Le trafic web n'est PAS ecarte a la capture mais au scoring : c'est ce qui
    permet de dire "tout est sur 443" au lieu de "aucun trafic".
    """
    exe = find_tshark(tshark)
    scorer = PortScorer(exclude=exclude, keep_web=keep_web)

    target = Path(pcap_out) if pcap_out else None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
    else:
        fd, name = tempfile.mkstemp(suffix=".pcapng", prefix="lwvs-discover-")
        os.close(fd)
        tmp = target = Path(name)

    try:
        cap = subprocess.run(
            [exe, "-i", iface, "-n", "-f", "tcp", "-a", f"duration:{duration}",
             "-w", str(target)],
            capture_output=True, text=True, errors="replace",
        )
        err = _meaningful_stderr(cap.stderr)
        if err:
            scorer.report.tshark_error = err
            return scorer.finish(top=top)
        packets, replay_err = feed_pcap(scorer, target, tshark=tshark)
        scorer.report.pcap_packets = packets
        if replay_err:
            # Un pcapng tronque se lit jusqu'au bloc fautif : les paquets deja
            # lus sont valides. C'est un avertissement, pas un echec.
            scorer.report.warnings.append(
                f"relecture partielle du pcap ({packets} paquet(s) lus) : {replay_err}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass

    report = scorer.finish(top=top)
    if pcap_out:
        report.pcap_path = str(target)
    return report


# ---------------------------------------------------------------------------
# Detection : interface ET port, en une seule ecoute
# ---------------------------------------------------------------------------
#
# Chercher l'interface puis le port etait deux attentes a la file : 8 s de
# comptage de paquets, puis 25 s de capture-vers-pcap-puis-relecture. Or les
# deux questions se repondent avec le MEME signal -- une trame du jeu qui
# decode jusqu'a son dernier octet prouve a la fois l'interface et le port.
#
# Donc : on ecoute toutes les interfaces en parallele, on score en direct (pas
# de pcap intermediaire, donc pas de deuxieme passe), et on s'arrete a la
# preuve au lieu d'aller au bout du chronometre. Le jeu envoie des battements
# de coeur en continu : la preuve arrive en general en une poignee de secondes.
# La duree passee n'est plus un temps d'attente, c'est un plafond.


@dataclass
class Attempt:
    """Ce qu'une interface a donne pendant la detection."""

    iface: Interface
    report: DiscoveryReport
    error: str = ""

    @property
    def best(self) -> PortScore | None:
        return self.report.scores[0] if self.report.scores else None


@dataclass
class Detection:
    iface: Interface | None = None
    port: int | None = None
    #: Rapport de l'interface retenue.
    report: DiscoveryReport | None = None
    #: Toutes les interfaces, la meilleure d'abord -- de quoi expliquer un echec.
    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0
    #: Panne globale (tshark absent, aucune interface declaree).
    error: str = ""

    @property
    def found(self) -> bool:
        return self.iface is not None and self.port is not None

    @property
    def packets(self) -> int:
        return sum(a.report.total_packets for a in self.attempts)

    def diagnosis(self) -> str | None:
        """Ce qui explique une detection sans resultat, quand elle l'est.

        Les trois pannes ne se reparent pas au meme endroit : pas de droits de
        capture (rien nulle part), le jeu ferme (du trafic mais aucune trame),
        tout en TLS (du trafic, mais sur des ports web ecartes).
        """
        if self.error:
            return self.error
        if self.found:
            return None
        if not self.attempts:
            return "no capture interface is available."
        if not self.packets:
            note = next((a.error for a in self.attempts if a.error), "")
            return (
                "no interface sees any TCP packet"
                + (f" ({note})" if note else "")
                + ". Is Npcap installed, and does this session have capture"
                " rights?"
            )
        scored = any(a.report.scores for a in self.attempts)
        web = sum(a.report.web_packets for a in self.attempts)
        if not scored and web:
            ports = sorted({p for a in self.attempts for p in a.report.web_ports})
            return (
                f"{self.packets} packet(s) seen, but all on web ports"
                f" ({', '.join(str(p) for p in ports)}), excluded from scoring. If"
                " the game speaks TLS-like on 443, rerun keeping web ports: the"
                " traffic is not encrypted, only the port is."
            )
        where = self.iface.name or self.iface.device if self.iface else "the interface"
        return (
            f"traffic does flow on {where}, but no port produced a game frame."
            " Was the game running during detection?"
        )


def _score_stream(
    lines: Iterable[str], scorer: PortScorer, stop: threading.Event, min_exact: int
) -> None:
    """Score un flux de lignes tshark et leve `stop` des que c'est prouve.

    Le seuil n'est pas 1 : une trame exacte isolee peut sortir d'un protocole
    voisin qui se decoupe par hasard. Deux sur le meme port, non.
    """
    for line in lines:
        if stop.is_set():
            return
        if not _feed_line(scorer, line):
            continue
        top = scorer.leader()
        if top is not None and top.exact >= min_exact:
            stop.set()
            return


def _watch_iface(
    exe: str,
    iface: Interface,
    seconds: int,
    scorer: PortScorer,
    stop: threading.Event,
    procs: list[subprocess.Popen],
    min_exact: int,
) -> str:
    """Ecoute une interface en direct. Rend l'erreur tshark eventuelle.

    `-a duration` borne le process meme si personne ne le termine : un tshark
    orphelin qui capture tout le TCP d'un poste n'est pas acceptable.
    """
    argv = [exe, "-i", iface.device, "-n", "-l", "-f", "tcp",
            "-a", f"duration:{seconds}", "-Y", _SCORE_FILTER]
    argv += ["-T", "fields", "-E", "separator=/t", "-E", "occurrence=a"]
    argv += _SCORE_FIELDS
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", bufsize=1,
        )
    except OSError as exc:
        return str(exc)[:120]
    procs.append(proc)                     # list.append est atomique
    err = ""
    try:
        assert proc.stdout is not None
        _score_stream(proc.stdout, scorer, stop, min_exact)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            if proc.stderr is not None:
                err = proc.stderr.read() or ""
        except Exception:
            pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
    return _meaningful_stderr(err)


def autodetect(
    seconds: int = 20,
    tshark: str | None = None,
    exclude: Sequence[int] = WEB_PORTS,
    keep_web: bool = False,
    min_exact: int = 2,
    prefer: str = "",
    ifaces: Sequence[Interface] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> Detection:
    """Trouve interface ET port en une passe, arretee des la premiere preuve.

    `seconds` est un plafond, pas une duree : des qu'un port montre `min_exact`
    trames decodees jusqu'au dernier octet, tout s'arrete. `prefer` (device
    d'une interface memorisee) ne sert qu'a departager une egalite.
    """
    started = time.monotonic()
    exe = find_tshark(tshark)
    targets = list(ifaces) if ifaces is not None else [
        i for i in list_interfaces(tshark)
        if not any(s in i.device.lower() for s in _SKIP_PROBE)
    ]
    if not targets:
        return Detection(error="no capture interface is available.")

    stop = threading.Event()
    scorers = {i.device: PortScorer(exclude=exclude, keep_web=keep_web)
               for i in targets}
    procs: list[subprocess.Popen] = []
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_watch_iface, exe, i, seconds, scorers[i.device], stop,
                        procs, min_exact): i
            for i in targets
        }
        deadline = started + seconds + 2
        last_tick = 0.0
        while not stop.wait(0.2):
            now = time.monotonic()
            if now >= deadline or all(f.done() for f in futures):
                break
            if on_progress and now - last_tick >= 1.0:
                last_tick = now
                seen = sum(s.report.total_packets for s in scorers.values())
                tops = [s.leader() for s in scorers.values()]
                best = max((t.exact for t in tops if t), default=0)
                on_progress(max(0, int(deadline - now)), seen, best)
        # Une interface bloquee sur une lecture ne se debloque qu'a la mort de
        # son tshark : couper les process EST la facon de joindre les threads.
        stop.set()
        for proc in list(procs):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        for future, iface in futures.items():
            try:
                errors[iface.device] = future.result(timeout=10) or ""
            except Exception as exc:
                errors[iface.device] = f"{type(exc).__name__}: {exc}"

    attempts = []
    for iface in targets:
        report = scorers[iface.device].finish()
        report.tshark_error = errors.get(iface.device, "")
        attempts.append(Attempt(iface, report, errors.get(iface.device, "")))

    def rank(a: Attempt) -> tuple[int, float, int, int]:
        best = a.best
        return (
            best.exact if best else 0,
            best.score if best else 0.0,
            a.report.total_packets,
            1 if prefer and a.iface.device == prefer else 0,
        )

    attempts.sort(key=rank, reverse=True)
    found = Detection(attempts=attempts, seconds=time.monotonic() - started)
    head = attempts[0]
    if head.report.total_packets or head.report.scores:
        found.iface = head.iface
        found.report = head.report
        if head.best is not None and head.best.exact:
            found.port = head.best.port
    return found
