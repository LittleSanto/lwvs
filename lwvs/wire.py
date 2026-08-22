"""Couche octets du protocole Last War: Survival.

CE MODULE EST LE SEUL A CONNAITRE LES OCTETS.

Il contient trois choses, et rien d'autre du domaine :

* la couche trame (decoupage du flux TCP en messages, decompression zstd) ;
* le decodeur de serialisation (les octets de type) ;
* l'encodeur, volontairement colle au decodeur pour qu'ils ne puissent pas
  diverger (cf. regles transverses du cahier des charges).

Une mise a jour du jeu doit casser ce fichier et rien d'autre.

Vie privee : aucune fonction d'ici ne logue de contenu. Les erreurs citent un
offset et du hexa de contexte, jamais une valeur decodee.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, NamedTuple, Sequence

import zstandard

__all__ = [
    "ZSTD_MAGIC",
    "KNOWN_FLAGS",
    "FLAG_RAW",
    "FLAG_ZSTD",
    "Frame",
    "FrameReader",
    "FrameStats",
    "TAG_BOOL",
    "TAG_INT8",
    "TAG_INT16",
    "TAG_INT32",
    "TAG_INT64",
    "TAG_STRING",
    "TAG_BYTES",
    "TAG_ARRAY",
    "TAG_MAP",
    "KNOWN_TAGS",
    "UNKNOWN_TAGS",
    "TAG_DOUBLE",
    "TAG_NAMES",
    "DecodeError",
    "UnknownTypeByte",
    "TruncatedValue",
    "RootDecodeError",
    "RootDecode",
    "Fixed",
    "LenPrefixed",
    "Counted",
    "TypeTable",
    "DEFAULT_TABLE",
    "decode_value_at",
    "decode_payload",
    "hex_context",
    "Tagged",
    "i8",
    "i16",
    "i32",
    "i64",
    "raw_bytes",
    "encode_value",
    "encode_frame",
    "encode_stream",
]


# ---------------------------------------------------------------------------
# Couche trame -- CONFIRME
# ---------------------------------------------------------------------------
#
#   brut        [flag u8][len u16 BE][payload : len octets]
#               -> avancer 3 + len
#
#   compresse   [flag u8][len u16 BE][taille_decomp u32 BE][trame zstd : len]
#               -> avancer 7 + len
#
# PIEGE PRINCIPAL : le `len` d'une trame compressee ne couvre QUE la trame
# zstd, pas les 4 octets de taille decompressee. Avancer de 3 + len decode
# parfaitement la premiere trame puis transforme tout le reste en bouillie.
#
# Le discriminant compresse/brut n'est PAS le flag mais la magie zstd : le flag
# n'est connu que pour deux valeurs, la magie est auto-evidente. On sonde donc
# +7 avant +3.

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

FLAG_RAW = 0x80
FLAG_ZSTD = 0xB0
KNOWN_FLAGS = frozenset({FLAG_RAW, FLAG_ZSTD})

_HEADER_MIN = 3
_PROBE_MIN = 11  # 1 (flag) + 2 (len) + 4 (taille decomp) + 4 (magie)
_COMPACT_THRESHOLD = 1 << 16


class _NeedMore(Exception):
    """Pas assez d'octets pour trancher : attendre le segment suivant."""


class Frame(NamedTuple):
    """Une trame reassemblee, decompressee si besoin."""

    payload: bytes
    flag: int
    compressed: bool
    wire_len: int          # le champ `len` tel qu'il etait sur le fil
    declared_size: int | None   # taille decompressee annoncee (compresse seul)
    offset: int            # offset de debut dans le tampon continu du flux
    seq: int               # numero de trame dans ce sens de connexion


@dataclass
class FrameStats:
    bytes_fed: int = 0
    frames: int = 0
    raw_frames: int = 0
    zstd_frames: int = 0
    resyncs: int = 0
    resync_bytes_skipped: int = 0
    zstd_errors: int = 0
    size_mismatches: int = 0
    zero_length_headers: int = 0
    truncated_tail: int = 0
    flag_counts: dict[int, int] = field(default_factory=dict)

    def merge(self, other: "FrameStats") -> None:
        self.bytes_fed += other.bytes_fed
        self.frames += other.frames
        self.raw_frames += other.raw_frames
        self.zstd_frames += other.zstd_frames
        self.resyncs += other.resyncs
        self.resync_bytes_skipped += other.resync_bytes_skipped
        self.zstd_errors += other.zstd_errors
        self.size_mismatches += other.size_mismatches
        self.zero_length_headers += other.zero_length_headers
        self.truncated_tail += other.truncated_tail
        for flag, n in other.flag_counts.items():
            self.flag_counts[flag] = self.flag_counts.get(flag, 0) + n


class _Header(NamedTuple):
    flag: int
    compressed: bool
    wire_len: int
    body_start: int
    frame_end: int
    declared_size: int | None


class FrameReader:
    """Decoupe un flux TCP continu en trames.

    Un FrameReader par SENS de connexion. Une connexion porte deux flux
    independants ; les fusionner entrelace deux sequences de trames et
    desynchronise les deux.

    Les charges utiles doivent etre concatenees par flux ET dans l'ordre : un
    message traverse plusieurs segments TCP, decouper paquet par paquet ne
    marche jamais.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.stats = FrameStats()
        self._buf = bytearray()
        self._off = 0
        self._base = 0          # offset absolu du debut de _buf dans le flux
        self._seq = 0
        self._scanned_to = 0    # borne de rebalayage deja exploree (resync)
        self._counted_bad = -1  # offset d'un echec deja compte (pas de doublon)

    # -- entree -----------------------------------------------------------
    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        self.stats.bytes_fed += len(data)
        return self._drain(final=False)

    def flush(self) -> list[Frame]:
        """Fin de flux : traite ce qui reste, y compris les cas ambigus."""
        return self._drain(final=True)

    # -- moteur -----------------------------------------------------------
    def _drain(self, final: bool) -> list[Frame]:
        out: list[Frame] = []
        while self._off < len(self._buf):
            try:
                hdr = self._header(self._off, final=final, strict=False)
            except _NeedMore:
                break
            if hdr is None:
                if not self._resync(final):
                    break
                continue
            if hdr.frame_end > len(self._buf):
                if final:
                    self.stats.truncated_tail += 1
                    self._off = len(self._buf)
                break
            frame = self._materialize(hdr)
            if frame is None:
                if not self._resync(final):
                    break
                continue
            out.append(frame)
            self._off = hdr.frame_end
            self._scanned_to = self._off
        self._compact()
        return out

    def _header(self, off: int, final: bool, strict: bool) -> _Header | None:
        buf = self._buf
        avail = len(buf) - off
        if avail < _HEADER_MIN:
            if final:
                return None
            raise _NeedMore
        flag = buf[off]
        wire_len = int.from_bytes(buf[off + 1 : off + 3], "big")
        if wire_len == 0:
            if not strict:
                self.stats.zero_length_headers += 1
            return None
        if avail < _PROBE_MIN and not final:
            # On ne peut pas encore trancher compresse/brut : la magie zstd
            # tombe a +7. Attendre plutot que deviner.
            raise _NeedMore
        if buf[off + 7 : off + 11] == ZSTD_MAGIC:
            declared = int.from_bytes(buf[off + 3 : off + 7], "big")
            return _Header(flag, True, wire_len, off + 7, off + 7 + wire_len, declared)
        if strict and flag not in KNOWN_FLAGS:
            return None
        return _Header(flag, False, wire_len, off + 3, off + 3 + wire_len, None)

    def _materialize(self, hdr: _Header) -> Frame | None:
        body = bytes(self._buf[hdr.body_start : hdr.frame_end])
        # Un echec ici est definitif (on a tous les octets de la trame), mais le
        # drain peut repasser sur le meme offset si la resync attend des donnees.
        # On ne compte l'echec qu'une fois.
        already_counted = self._off == self._counted_bad
        if hdr.compressed:
            try:
                # decompressobj() et non decompress() : ce dernier exige une
                # taille de contenu dans l'en-tete zstd, qui n'est pas garantie.
                payload = zstandard.ZstdDecompressor().decompressobj().decompress(body)
            except zstandard.ZstdError:
                if not already_counted:
                    self.stats.zstd_errors += 1
                    self._counted_bad = self._off
                return None
            if hdr.declared_size is not None and len(payload) != hdr.declared_size:
                # Signature exacte du bug "3 + len" : on a lu a cote.
                if not already_counted:
                    self.stats.size_mismatches += 1
                    self._counted_bad = self._off
                return None
            self.stats.zstd_frames += 1
        else:
            payload = body
            self.stats.raw_frames += 1
        self.stats.frames += 1
        self.stats.flag_counts[hdr.flag] = self.stats.flag_counts.get(hdr.flag, 0) + 1
        frame = Frame(
            payload=payload,
            flag=hdr.flag,
            compressed=hdr.compressed,
            wire_len=hdr.wire_len,
            declared_size=hdr.declared_size,
            offset=self._base + self._off,
            seq=self._seq,
        )
        self._seq += 1
        return frame

    def _resync(self, final: bool) -> bool:
        """Rebalaye vers l'avant jusqu'a une frontiere plausible.

        Lookahead d'une trame : des octets aleatoires produisent souvent un
        en-tete valide, rarement deux d'affilee.
        """
        start = max(self._off + 1, self._scanned_to)
        limit = len(self._buf) - _HEADER_MIN
        for i in range(start, limit + 1):
            try:
                hdr = self._header(i, final=final, strict=True)
            except _NeedMore:
                self._scanned_to = i
                return False
            if hdr is None:
                continue
            if hdr.frame_end > len(self._buf):
                if final:
                    continue
                self._scanned_to = i
                return False
            if hdr.frame_end == len(self._buf):
                if not final:
                    self._scanned_to = i
                    return False
            else:
                try:
                    nxt = self._header(hdr.frame_end, final=final, strict=True)
                except _NeedMore:
                    self._scanned_to = i
                    return False
                if nxt is None:
                    continue
            skipped = i - self._off
            self.stats.resyncs += 1
            self.stats.resync_bytes_skipped += skipped
            self._off = i
            self._scanned_to = i
            return True
        if final:
            self.stats.resyncs += 1
            self.stats.resync_bytes_skipped += len(self._buf) - self._off
            self._off = len(self._buf)
        else:
            self._scanned_to = max(start, limit)
        return False

    def _compact(self) -> None:
        if self._off >= _COMPACT_THRESHOLD:
            del self._buf[: self._off]
            self._base += self._off
            self._scanned_to = max(0, self._scanned_to - self._off)
            if self._counted_bad >= 0:
                self._counted_bad -= self._off
            self._off = 0


# ---------------------------------------------------------------------------
# Serialisation -- CONFIRME
# ---------------------------------------------------------------------------
#
# Big-endian partout. Chaque valeur commence par un octet de type.
#
#   0x01 bool    1 octet 00/01
#   0x02 int8    1 octet
#   0x03 int16   2 octets
#   0x04 int32   4 octets
#   0x05 int64   8 octets
#   0x08 string  u16 longueur + UTF-8
#   0x0A bytes   u32 longueur + octets opaques
#   0x11 array   u16 nombre + N valeurs
#   0x12 map     u16 nombre + N paires (cle, valeur)
#
# Les CLES de map n'ont PAS d'octet de type : juste u16 longueur + le nom.
#
# 0x0A utilise un u32 la ou les strings utilisent un u16. Son unique porteur
# observe est une cle `_proto` contenant du protobuf embarque : on le garde en
# bytes bruts, le decoder comme du texte le massacrerait.
#
# INCONNUS : 0x06, 0x07, 0x0C, 0x0D. Vus dans du vrai trafic, layout non
# etabli. On NE DEVINE PAS leur largeur : une largeur devinee corrompt
# silencieusement tous les champs suivants, ce qui est bien pire qu'un arret
# net. Le decodage s'arrete proprement en rapportant offset et hexa.

TAG_BOOL = 0x01
TAG_INT8 = 0x02
TAG_INT16 = 0x03
TAG_INT32 = 0x04
TAG_INT64 = 0x05
#: Double IEEE 754 big-endian. ETABLI le 20/08/2026 sur `captures/thp`, apres
#: avoir bloque `rank.get.preview` (94 % du message non lu). Quatre preuves :
#:
#: 1. `probe --dir captures/thp --tag 0x07` propose `fixed:8`, sans ex aequo ;
#: 2. relu sur le hex, `41 3e 95 9c 22 e0 16 51` sur une cle `stageScore` vaut
#:    2 004 380,14 en double -- et 4,7e18 en int64, ce qui n'est pas un score ;
#: 3. sous cette hypothese, plus AUCUN payload des 4 captures n'est bloque par
#:    0x07 : il est franchi et le decodage continue jusqu'a 0x06 / 0x0C ;
#: 4. falsification : fixed:2, fixed:4 et fixed:16 desalignent le decodeur et le
#:    font tomber sur des octets arbitraires (0x00 x13). fixed:8 est la SEULE
#:    largeur qui n'en produit aucun.
TAG_DOUBLE = 0x07
TAG_STRING = 0x08
TAG_BYTES = 0x0A
TAG_ARRAY = 0x11
TAG_MAP = 0x12

KNOWN_TAGS = frozenset(
    {
        TAG_BOOL,
        TAG_INT8,
        TAG_INT16,
        TAG_INT32,
        TAG_INT64,
        TAG_DOUBLE,
        TAG_STRING,
        TAG_BYTES,
        TAG_ARRAY,
        TAG_MAP,
    }
)

#: Octets de type rencontres dans du vrai trafic mais dont le layout n'est pas
#: etabli. Presents ici pour que les diagnostics puissent les nommer, PAS pour
#: qu'on les decode.
UNKNOWN_TAGS = frozenset({0x06, 0x0C, 0x0D})

TAG_NAMES = {
    TAG_BOOL: "bool",
    TAG_INT8: "int8",
    TAG_INT16: "int16",
    TAG_INT32: "int32",
    TAG_INT64: "int64",
    TAG_DOUBLE: "double",
    TAG_STRING: "string",
    TAG_BYTES: "bytes",
    TAG_ARRAY: "array",
    TAG_MAP: "map",
}

_STRING_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def hex_context(buf: bytes, off: int, radius: int = 12) -> str:
    """Hexa autour d'un offset, avec un marqueur. Jamais de contenu decode."""
    lo = max(0, off - radius)
    hi = min(len(buf), off + radius + 1)
    before = buf[lo:off].hex(" ")
    at = buf[off : off + 1].hex()
    after = buf[off + 1 : hi].hex(" ")
    parts = [p for p in (before, f"[{at}]" if at else "[eof]", after) if p]
    return " ".join(parts)


class DecodeError(Exception):
    """Echec de decodage de la serialisation, localise."""

    def __init__(self, message: str, buf: bytes, offset: int) -> None:
        self.offset = offset
        self.hex = hex_context(buf, offset)
        super().__init__(f"{message} @ offset {offset}: {self.hex}")


class TruncatedValue(DecodeError):
    """La valeur deborde du payload."""


class UnknownTypeByte(DecodeError):
    """Octet de type dont le layout n'est pas etabli. On s'arrete net."""

    def __init__(self, tag: int, buf: bytes, offset: int) -> None:
        self.tag = tag
        known = " (inconnu documente)" if tag in UNKNOWN_TAGS else ""
        super().__init__(f"octet de type 0x{tag:02x} non supporte{known}", buf, offset)


# -- layouts candidats, utilises UNIQUEMENT par le sondeur -------------------


# Un "layout" est une hypothese sur la forme d'un octet de type inconnu. Jamais
# utilise par le decodage de production : `DEFAULT_TABLE` est vide. Le sondeur
# en injecte via une `TypeTable` pour tester des hypotheses, et rien d'autre.


class Fixed(NamedTuple):
    width: int

    @property
    def name(self) -> str:
        return f"fixed:{self.width}"


class LenPrefixed(NamedTuple):
    width: int  # 1, 2 ou 4

    @property
    def name(self) -> str:
        return f"len:u{self.width * 8}"


class Counted(NamedTuple):
    width: int          # largeur du compteur : 1 ou 2
    per_item: int       # 1 = valeurs typees ; 2 = paires (cle non typee, valeur)

    @property
    def name(self) -> str:
        kind = "arr" if self.per_item == 1 else "map"
        return f"count:u{self.width * 8}:{kind}"


@dataclass(frozen=True)
class TypeTable:
    """Table de types du decodeur.

    `extra` mappe un octet de type inconnu vers un layout hypothetique. La
    table de production est vide : rien ne s'ecrit tout seul dans la table de
    types, une conclusion du sondeur est une proposition a relire sur le hex.
    """

    extra: Mapping[int, Any] = field(default_factory=dict)

    def describe(self) -> str:
        if not self.extra:
            return "(table de production)"
        return ", ".join(
            f"0x{tag:02x}={lay.name}" for tag, lay in sorted(self.extra.items())
        )


DEFAULT_TABLE = TypeTable()


class _Decoder:
    __slots__ = ("buf", "table", "n")

    def __init__(self, buf: bytes, table: TypeTable) -> None:
        self.buf = buf
        self.table = table
        self.n = len(buf)

    def _need(self, off: int, count: int) -> None:
        if off + count > self.n:
            raise TruncatedValue(f"besoin de {count} octets", self.buf, min(off, self.n))

    def _uint(self, off: int, width: int) -> tuple[int, int]:
        self._need(off, width)
        return int.from_bytes(self.buf[off : off + width], "big", signed=False), off + width

    def _int(self, off: int, width: int) -> tuple[int, int]:
        self._need(off, width)
        return int.from_bytes(self.buf[off : off + width], "big", signed=True), off + width

    def _key(self, off: int) -> tuple[str, int]:
        length, off = self._uint(off, 2)
        self._need(off, length)
        raw = self.buf[off : off + length]
        return _decode_str(raw), off + length

    def value(self, off: int) -> tuple[Any, int]:
        self._need(off, 1)
        tag = self.buf[off]
        start = off
        off += 1
        if tag == TAG_BOOL:
            self._need(off, 1)
            return self.buf[off] != 0, off + 1
        if tag == TAG_INT8:
            return self._int(off, 1)
        if tag == TAG_INT16:
            return self._int(off, 2)
        if tag == TAG_INT32:
            return self._int(off, 4)
        if tag == TAG_INT64:
            return self._int(off, 8)
        if tag == TAG_DOUBLE:
            self._need(off, 8)
            return struct.unpack_from(">d", self.buf, off)[0], off + 8
        if tag == TAG_STRING:
            length, off = self._uint(off, 2)
            self._need(off, length)
            return _decode_str(self.buf[off : off + length]), off + length
        if tag == TAG_BYTES:
            # u32, pas u16 : seule difference avec les strings.
            length, off = self._uint(off, 4)
            self._need(off, length)
            return bytes(self.buf[off : off + length]), off + length
        if tag == TAG_ARRAY:
            count, off = self._uint(off, 2)
            items: list[Any] = []
            for _ in range(count):
                item, off = self.value(off)
                items.append(item)
            return items, off
        if tag == TAG_MAP:
            count, off = self._uint(off, 2)
            out: dict[str, Any] = {}
            for _ in range(count):
                key, off = self._key(off)
                val, off = self.value(off)
                out[key] = val
            return out, off

        layout = self.table.extra.get(tag)
        if layout is None:
            raise UnknownTypeByte(tag, self.buf, start)
        return self._hypothetical(tag, layout, off)

    def _hypothetical(self, tag: int, layout: Any, off: int) -> tuple[Any, int]:
        if isinstance(layout, Fixed):
            self._need(off, layout.width)
            return _Opaque(tag, bytes(self.buf[off : off + layout.width])), off + layout.width
        if isinstance(layout, LenPrefixed):
            length, off = self._uint(off, layout.width)
            self._need(off, length)
            return _Opaque(tag, bytes(self.buf[off : off + length])), off + length
        if isinstance(layout, Counted):
            count, off = self._uint(off, layout.width)
            items: list[Any] = []
            for _ in range(count):
                if layout.per_item == 2:
                    key, off = self._key(off)
                    val, off = self.value(off)
                    items.append((key, val))
                else:
                    val, off = self.value(off)
                    items.append(val)
            return _Opaque(tag, items), off
        raise UnknownTypeByte(tag, self.buf, off - 1)


class _Opaque(NamedTuple):
    """Valeur produite par un layout hypothetique. Jamais du domaine."""

    tag: int
    body: Any


def _decode_str(raw: bytes) -> str:
    for enc in _STRING_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def decode_value_at(payload: bytes, offset: int, table: TypeTable = DEFAULT_TABLE):
    """Decode une valeur typee a `offset`. Retourne (valeur, offset de fin)."""
    return _Decoder(payload, table).value(offset)


# -- selection de l'offset racine -- CONFIRME, ET CONTRE-INTUITIF -----------
#
# Le payload peut commencer par un petit en-tete applicatif, et cet en-tete
# decode souvent comme un objet valide lui aussi (un tag map suivi d'un petit
# compte est un motif d'octets courant). "Le premier offset qui parse" est donc
# le MAUVAIS critere : il attrape l'en-tete et rend un objet plausible avec deux
# champs bidons.
#
# Bon critere : le NOMBRE D'OCTETS CONSOMMES. On essaie chaque tag 0x12 dans les
# 64 premiers octets et on garde le decodage le plus glouton.
#
# Revers a connaitre : cette regle masque les echecs. Tant qu'un octet de type
# de l'enveloppe est refuse, l'enveloppe ne parse pas et la regle retourne
# silencieusement la map du dessous -- tout a l'air de marcher pendant que
# l'enveloppe est jetee. D'ou `RootDecode.exact` : la part des payloads qui
# decodent jusqu'a leur DERNIER OCTET EXACTEMENT est le garde-fou. Si ce n'est
# pas ~100 %, on ne decode pas ce qu'on croit.

ROOT_WINDOW = 64


@dataclass(frozen=True)
class RootDecode:
    value: Any
    root_offset: int
    end: int
    total: int

    @property
    def consumed(self) -> int:
        return self.end - self.root_offset

    @property
    def exact(self) -> bool:
        """Le decodage finit-il sur le dernier octet du payload ?"""
        return self.end == self.total

    @property
    def trailing(self) -> int:
        return self.total - self.end


class RootDecodeError(Exception):
    """Aucun offset racine ne decode. Porte le detail par offset essaye."""

    def __init__(self, errors: dict[int, DecodeError], total: int) -> None:
        self.errors = errors
        self.total = total
        if errors:
            first = min(errors)
            self.primary = errors[first]
            detail = f"{len(errors)} offset(s) racine essaye(s); premier: {self.primary}"
        else:
            self.primary = None
            detail = f"aucun tag 0x{TAG_MAP:02x} dans les {ROOT_WINDOW} premiers octets"
        super().__init__(f"payload de {total} octets non decodable -- {detail}")

    @property
    def unknown_tags(self) -> set[int]:
        return {e.tag for e in self.errors.values() if isinstance(e, UnknownTypeByte)}

    @property
    def blocking_tag(self) -> int | None:
        """Octet de type fautif, s'il est le meme pour tous les offsets."""
        tags = self.unknown_tags
        if len(tags) == 1:
            return next(iter(tags))
        return None


def decode_payload(
    payload: bytes,
    table: TypeTable = DEFAULT_TABLE,
    offsets: Iterable[int] | None = None,
    window: int = ROOT_WINDOW,
) -> RootDecode:
    """Decode un payload de trame en cherchant la racine la plus gloutonne.

    `offsets` restreint les offsets racine candidats. A utiliser pour le
    sondage : scanner 64 offsets sur un payload qui ne parse nulle part trouve
    des racines bidons au milieu de chaines et fabrique des octets de type qui
    n'existent pas.
    """
    total = len(payload)
    candidates = range(min(window, total)) if offsets is None else offsets
    best: RootDecode | None = None
    errors: dict[int, DecodeError] = {}
    for off in candidates:
        if off < 0 or off >= total or payload[off] != TAG_MAP:
            continue
        try:
            value, end = decode_value_at(payload, off, table)
        except DecodeError as exc:
            errors[off] = exc
            continue
        if best is None or end > best.end:
            best = RootDecode(value=value, root_offset=off, end=end, total=total)
    if best is None:
        raise RootDecodeError(errors, total)
    return best


# ---------------------------------------------------------------------------
# Encodeur -- colle au decodeur
# ---------------------------------------------------------------------------
#
# Il vit ici et pas dans un module de donnees synthetiques : le generateur doit
# emettre exactement les octets que le decodeur attend, et les separer est la
# facon dont les deux divergent.


class Tagged(NamedTuple):
    """Force un octet de type donne pour une valeur (tests, fixtures)."""

    tag: int
    value: Any


def i8(v: int) -> Tagged:
    return Tagged(TAG_INT8, v)


def i16(v: int) -> Tagged:
    return Tagged(TAG_INT16, v)


def i32(v: int) -> Tagged:
    return Tagged(TAG_INT32, v)


def i64(v: int) -> Tagged:
    return Tagged(TAG_INT64, v)


def raw_bytes(v: bytes) -> Tagged:
    return Tagged(TAG_BYTES, v)


def _int_tag(v: int) -> int:
    if -0x80 <= v <= 0x7F:
        return TAG_INT8
    if -0x8000 <= v <= 0x7FFF:
        return TAG_INT16
    if -0x8000_0000 <= v <= 0x7FFF_FFFF:
        return TAG_INT32
    if -0x8000_0000_0000_0000 <= v <= 0x7FFF_FFFF_FFFF_FFFF:
        return TAG_INT64
    raise ValueError("entier hors de portee int64")


_INT_WIDTHS = {TAG_INT8: 1, TAG_INT16: 2, TAG_INT32: 4, TAG_INT64: 8}


def _encode_key(key: str, out: bytearray) -> None:
    raw = key.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise ValueError("cle de map trop longue")
    out += struct.pack(">H", len(raw))
    out += raw


def _encode_into(value: Any, out: bytearray) -> None:
    if isinstance(value, Tagged):
        tag, value = value.tag, value.value
    elif isinstance(value, bool):
        tag = TAG_BOOL
    elif isinstance(value, int):
        tag = _int_tag(value)
    elif isinstance(value, float):
        tag = TAG_DOUBLE
    elif isinstance(value, str):
        tag = TAG_STRING
    elif isinstance(value, (bytes, bytearray)):
        tag = TAG_BYTES
    elif isinstance(value, (list, tuple)):
        tag = TAG_ARRAY
    elif isinstance(value, dict):
        tag = TAG_MAP
    else:
        raise TypeError(f"type non encodable: {type(value).__name__}")

    out.append(tag)
    if tag == TAG_BOOL:
        out.append(1 if value else 0)
    elif tag in _INT_WIDTHS:
        width = _INT_WIDTHS[tag]
        out += int(value).to_bytes(width, "big", signed=True)
    elif tag == TAG_DOUBLE:
        out += struct.pack(">d", float(value))
    elif tag == TAG_STRING:
        raw = str(value).encode("utf-8")
        if len(raw) > 0xFFFF:
            raise ValueError("string trop longue")
        out += struct.pack(">H", len(raw))
        out += raw
    elif tag == TAG_BYTES:
        raw = bytes(value)
        out += struct.pack(">I", len(raw))
        out += raw
    elif tag == TAG_ARRAY:
        items = list(value)
        if len(items) > 0xFFFF:
            raise ValueError("array trop long")
        out += struct.pack(">H", len(items))
        for item in items:
            _encode_into(item, out)
    elif tag == TAG_MAP:
        items = list(dict(value).items())
        if len(items) > 0xFFFF:
            raise ValueError("map trop longue")
        out += struct.pack(">H", len(items))
        for key, val in items:
            _encode_key(str(key), out)
            _encode_into(val, out)
    else:
        raise ValueError(f"octet de type non encodable: 0x{tag:02x}")


def encode_value(value: Any) -> bytes:
    out = bytearray()
    _encode_into(value, out)
    return bytes(out)


def encode_frame(
    payload: bytes,
    *,
    compress: bool = False,
    flag: int | None = None,
    prefix: bytes = b"",
) -> bytes:
    """Emballe un payload dans une trame telle que `FrameReader` l'attend.

    `prefix` simule l'en-tete applicatif qui precede parfois la racine.
    """
    body = prefix + payload
    if compress:
        comp = zstandard.ZstdCompressor().compress(body)
        head = struct.pack(">BHI", FLAG_ZSTD if flag is None else flag, len(comp), len(body))
        return head + comp
    if len(body) > 0xFFFF:
        raise ValueError("payload brut > 65535 octets : compresser")
    return struct.pack(">BH", FLAG_RAW if flag is None else flag, len(body)) + body


def encode_stream(frames: Sequence[bytes]) -> bytes:
    return b"".join(frames)


def iter_frames(data: bytes, name: str = "") -> Iterator[Frame]:
    """Helper de test : decoupe un tampon complet en trames."""
    reader = FrameReader(name)
    yield from reader.feed(data)
    yield from reader.flush()
