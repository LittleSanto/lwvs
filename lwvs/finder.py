"""`find` : retrouver un chiffre vu a l'ecran dans le trafic decode.

C'est l'outil qui FERME LA BOUCLE. Tu lis « 32 170 354 » sur un classement, tu
captures l'ecran, et cette commande te dit quelle commande du protocole et quel
chemin de cle portent cette valeur. Le champ s'identifie tout seul : aucune
devinette de nom, aucune correlation approximative.

Deux passes complementaires :

* dans les objets DECODES -- rend un chemin de cle exploitable directement ;
* dans les octets BRUTS -- un payload qui ne decode pas est invisible a la
  premiere passe, et c'est justement celui qu'il faut reperer. Trouver la
  valeur dans ses octets prouve qu'elle est la, meme si on ne sait pas encore
  la lire.

UN NOMBRE N'ARRIVE PAS TOUJOURS EN NOMBRE. `lw.camp.battle.user.score.rank`
transmet `score` en CHAINE (« 27539623 »). Chercher 27539623 en n'inspectant
que les entiers rendait « NON trouve » sur un classement pourtant present et
parfaitement decode -- le pire des resultats, puisqu'il envoie relire le hex
d'un message qui n'a rien a se reprocher. Les deux passes cherchent donc aussi
la forme TEXTE du nombre : chaine decodee cote objets, chiffres ASCII cote
octets.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from .capture import CaptureSource
from .inspection import walk
from .messages import unwrap
from .pipeline import DecodeStats, decode_source

__all__ = ["Hit", "RawHit", "FindReport", "as_number", "find_values",
           "render_find"]


@dataclass(frozen=True)
class Hit:
    command: str
    path: str
    value: Any
    needle: Any


@dataclass(frozen=True)
class RawHit:
    label: str
    needle: int
    width: int
    offset: int
    #: "int" (grand-boutiste, `width` octets) ou "texte" (chiffres ASCII).
    encoding: str = "int"


@dataclass
class FindReport:
    stats: DecodeStats = field(default_factory=DecodeStats)
    hits: list[Hit] = field(default_factory=list)
    raw_hits: list[RawHit] = field(default_factory=list)
    #: Chemins distincts, comptes : c'est le resume qui sert.
    by_path: Counter = field(default_factory=Counter)
    found: set[Any] = field(default_factory=set)
    missing: list[Any] = field(default_factory=list)


def _patterns(value: int) -> list[tuple[bytes, int, str]]:
    out: list[tuple[bytes, int, str]] = []
    for width, fmt in ((4, ">i"), (8, ">q")):
        try:
            out.append((struct.pack(fmt, value), width, "int"))
        except struct.error:
            pass
    # La forme texte : c'est celle des scores d'evenement sur le fil.
    ascii_digits = str(value).encode("ascii")
    out.append((ascii_digits, len(ascii_digits), "texte"))
    return out


def as_number(text: str) -> int | None:
    """Le nombre porte par une chaine, ou None si ce n'en est pas une.

    Memes separateurs toleres que sur la ligne de commande, pour que
    `--value 27 539 623` retrouve la chaine « 27539623 ».
    """
    stripped = text.strip().replace(" ", "").replace(" ", "").replace(",", "")
    if not stripped or not stripped.lstrip("-").isdigit():
        return None
    return int(stripped)


def find_values(
    source: CaptureSource,
    numbers: Sequence[int] = (),
    texts: Sequence[str] = (),
    scan_raw: bool = True,
) -> FindReport:
    report = FindReport()
    wanted_numbers = set(numbers)
    lowered = [t.lower() for t in texts]
    raw_patterns = [(pat, width, encoding, value)
                    for value in wanted_numbers
                    for pat, width, encoding in _patterns(value)]

    for item in decode_source(source, report.stats):
        if scan_raw:
            for pat, width, encoding, value in raw_patterns:
                off = item.payload.data.find(pat)
                if off >= 0:
                    report.raw_hits.append(
                        RawHit(label=item.payload.label, needle=value,
                               width=width, offset=off, encoding=encoding))
        if item.root is None:
            continue
        env = unwrap(item.root.value)
        command = env.command if env else "(hors enveloppe)"
        body: Any = {"p": env.body} if env else item.root.value
        for path, _kind, value in walk(body):
            if not path:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value in wanted_numbers:
                report.hits.append(Hit(command, path, value, value))
                report.by_path[f"{command}  {path}"] += 1
                report.found.add(value)
            elif isinstance(value, str):
                # Un score peut arriver en chaine : on le compare aussi comme
                # nombre, sinon le classement d'evenement reste introuvable.
                numeric = as_number(value)
                if numeric is not None and numeric in wanted_numbers:
                    report.hits.append(Hit(command, path, value, numeric))
                    report.by_path[f"{command}  {path}"] += 1
                    report.found.add(numeric)
                low = value.lower()
                for needle, original in zip(lowered, texts):
                    if needle and needle in low:
                        report.hits.append(Hit(command, path, value, original))
                        report.by_path[f"{command}  {path}"] += 1
                        report.found.add(original)

    report.missing = [n for n in list(numbers) + list(texts) if n not in report.found]
    return report


def render_find(report: FindReport, show_values: bool = False,
                top: int = 30) -> str:
    out: list[str] = []
    st = report.stats
    out.append(f"payloads {st.payloads}  decodes {st.decoded}  echecs {st.failed}")
    out.append(st.guardrail())
    out.append("")

    if report.by_path:
        out.append("=== trouve, par commande et chemin de cle ===")
        for label, n in report.by_path.most_common(top):
            out.append(f"  {n:5d}  {label}")
        if show_values:
            out.append("")
            out.append("--- correspondances ---")
            for hit in report.hits[:top]:
                out.append(f"  {hit.needle!r} -> {hit.command}  {hit.path} = {hit.value!r}")
    else:
        out.append("=== aucune valeur trouvee dans les objets decodes ===")

    if report.raw_hits:
        out.append("")
        out.append("=== trouve dans les OCTETS de payloads (dont ceux qui ne")
        out.append("    decodent pas : invisibles a la recherche ci-dessus) ===")
        for hit in report.raw_hits[:top]:
            forme = (f"en int{hit.width * 8}" if hit.encoding == "int"
                     else f"en texte ({hit.width} chiffres ASCII)")
            out.append(f"  {hit.needle:,} {forme} @ offset {hit.offset}"
                       f"  dans {hit.label}")

    if report.missing:
        out.append("")
        out.append("=== NON trouve ===")
        for needle in report.missing:
            out.append(f"  {needle!r}")
        out.append("  Trois lectures possibles, et rien ne les departage ici :")
        out.append("    - l'ecran n'a pas ete recharge pendant la capture ;")
        out.append("    - la valeur affichee est calculee et n'est jamais")
        out.append("      transmise telle quelle (somme, arrondi, conversion) ;")
        out.append("    - elle est dans un payload qui ne decode pas ET dans un")
        out.append("      encodage non teste (autre largeur, autre endianness).")
    return "\n".join(out)
