"""Passe de decodage partagee : source -> payloads -> objets racine.

`ingest`, `inspect` et `probe` passent tous par ici, pour que le garde-fou
"part des payloads qui decodent jusqu'a leur dernier octet exactement" soit
mesure de la meme facon partout.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import wire
from .capture import CaptureSource, Payload

__all__ = ["Decoded", "DecodeStats", "decode_source", "PayloadSaver"]


@dataclass(frozen=True)
class Decoded:
    payload: Payload
    root: wire.RootDecode | None
    error: Exception | None

    @property
    def ok(self) -> bool:
        return self.root is not None


@dataclass
class DecodeStats:
    payloads: int = 0
    decoded: int = 0
    exact: int = 0
    failed: int = 0
    #: Compte des payloads qui se cadrent mais ne decodent pas, GROUPE PAR
    #: OCTET DE TYPE FAUTIF. Un tiers d'une vraie capture peut etre illisible,
    #: et un message illisible est invisible pour tous les filtres.
    failures_by_tag: Counter = field(default_factory=Counter)
    failures_other: int = 0
    #: Tous les offsets racine retenus, exacts ou non (diagnostic).
    root_offsets: Counter = field(default_factory=Counter)
    #: Offsets ou la capture ancre REELLEMENT ses messages : releves sur les
    #: seuls payloads qui consomment tout. Un payload qui decode en laissant une
    #: queue n'a pas revele un ancrage, il a revele une racine bidon. Le sondeur
    #: se restreint a ceux-ci.
    exact_offsets: Counter = field(default_factory=Counter)
    trailing_bytes: Counter = field(default_factory=Counter)

    @property
    def exact_ratio(self) -> float:
        return self.exact / self.decoded if self.decoded else 0.0

    def anchor_offsets(self) -> list[int]:
        return sorted(self.exact_offsets)

    def guardrail(self) -> str:
        """Message du garde-fou de §1.6, a afficher systematiquement."""
        if not self.decoded:
            return "garde-fou: aucun payload decode."
        pct = 100.0 * self.exact_ratio
        verdict = "OK" if pct >= 99.0 else "ALERTE"
        msg = (
            f"garde-fou [{verdict}]: {self.exact}/{self.decoded} payloads decodent "
            f"jusqu'au dernier octet exactement ({pct:.1f} %)"
        )
        if pct < 99.0:
            msg += (
                "\n  -> la regle de racine gloutonne masque les echecs : tant qu'un"
                "\n     octet de type de l'enveloppe est refuse, l'enveloppe ne parse"
                "\n     pas et la map du dessous est rendue silencieusement."
                "\n     Tu ne decodes pas ce que tu crois. Lance `lwvs probe`."
            )
        return msg


def decode_source(
    source: CaptureSource,
    stats: DecodeStats | None = None,
    saver: "PayloadSaver | None" = None,
    table: wire.TypeTable = wire.DEFAULT_TABLE,
) -> Iterator[Decoded]:
    """Decode chaque payload de `source` et met a jour `stats`.

    `table` n'est pas la table de production sauf pour le sondeur, qui y injecte
    des hypotheses. Rien ne s'ecrit jamais dans `wire.DEFAULT_TABLE`.
    """
    st = stats if stats is not None else DecodeStats()
    for payload in source.payloads():
        st.payloads += 1
        try:
            root = wire.decode_payload(payload.data, table)
        except wire.RootDecodeError as exc:
            st.failed += 1
            tags = exc.unknown_tags
            if tags:
                for tag in tags:
                    st.failures_by_tag[tag] += 1
            else:
                st.failures_other += 1
            if saver is not None:
                saver.save(payload, ok=False, error=exc)
            yield Decoded(payload=payload, root=None, error=exc)
            continue
        except wire.DecodeError as exc:  # pragma: no cover - filet
            st.failed += 1
            st.failures_other += 1
            if saver is not None:
                saver.save(payload, ok=False, error=exc)
            yield Decoded(payload=payload, root=None, error=exc)
            continue
        st.decoded += 1
        st.root_offsets[root.root_offset] += 1
        if root.exact:
            st.exact += 1
            st.exact_offsets[root.root_offset] += 1
        else:
            st.trailing_bytes[root.trailing] += 1
        if saver is not None:
            saver.save(payload, ok=True, error=None)
        yield Decoded(payload=payload, root=root, error=None)


class PayloadSaver:
    """Ecrit les payloads bruts sur disque, y compris ceux qui n'ont pas decode.

    Les echecs vont dans `failed/`, QUEL QUE SOIT LE FILTRE : un message
    illisible ne peut pas matcher un filtre de cle, et ce sont justement
    ceux-la qu'il faut garder.

    Attention vie privee : ces fichiers contiennent des donnees joueur brutes.
    Le repertoire de capture est dans le .gitignore du projet.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "ok").mkdir(parents=True, exist_ok=True)
        (self.root / "failed").mkdir(parents=True, exist_ok=True)
        self._manifest = (self.root / "manifest.tsv").open("w", encoding="utf-8", newline="")
        self._manifest.write("file\tstream\tseq\tflag\tcompressed\tbytes\tstatus\tdetail\n")
        self._n = 0

    def save(self, payload: Payload, ok: bool, error: Exception | None) -> Path:
        self._n += 1
        sub = "ok" if ok else "failed"
        safe_stream = payload.stream.replace("/", "-").replace("\\", "-")
        name = f"{self._n:06d}_{safe_stream}_{payload.seq:05d}.bin"
        path = self.root / sub / name
        path.write_bytes(payload.data)
        # Le detail cite un offset et du hexa, jamais du contenu decode.
        detail = str(error).replace("\t", " ").replace("\n", " ") if error else ""
        self._manifest.write(
            f"{sub}/{name}\t{payload.stream}\t{payload.seq}\t"
            f"{payload.flag if payload.flag >= 0 else ''}\t"
            f"{int(payload.compressed)}\t{len(payload.data)}\t{sub}\t{detail}\n"
        )
        return path

    def close(self) -> None:
        try:
            self._manifest.close()
        except Exception:
            pass

    def __enter__(self) -> "PayloadSaver":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
