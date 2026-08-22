"""`inspect` : cartographie du protocole.

C'est la partie la plus importante de l'outil, et elle est construite AVANT de
chercher quoi que ce soit. Elle rapporte :

* la liste des noms de commande vus -- l'index du protocole ;
* les formes de messages distinctes, en chemins de cles types
  (`p.rankInfo[].score  int32`) ;
* le compte des payloads qui se cadrent mais ne decodent pas, groupe par octet
  de type fautif.

Les filtres `--command` / `--key` sont des devinettes et c'est SANS RISQUE :
ils reduisent un affichage, ils ne nomment pas un champ. Deviner dans un
parseur stockerait un chiffre faux sans jamais lever d'erreur -- c'est la
ligne a ne pas franchir.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import wire
from .capture import CaptureSource
from .messages import unwrap
from .pipeline import DecodeStats, PayloadSaver, decode_source

__all__ = ["Shape", "InspectReport", "inspect_source", "render_report"]

_MAX_SAMPLE = 60


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray)):
        return "bytes"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "map"
    return type(value).__name__


def _sample(value: Any) -> str:
    """Un exemple de valeur, tronque. Uniquement en mode --values (opt-in)."""
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:16]).hex(" ")
        return f"<{len(value)} octets> {head}{' ...' if len(value) > 16 else ''}"
    if isinstance(value, str):
        s = value.replace("\n", "\\n")
        return s if len(s) <= _MAX_SAMPLE else s[:_MAX_SAMPLE] + "..."
    return str(value)


def walk(value: Any, prefix: str = "") -> Iterable[tuple[str, str, Any]]:
    """Rend (chemin, type, valeur) pour chaque noeud feuille ou conteneur."""
    kind = _type_name(value)
    if isinstance(value, dict):
        if prefix:
            yield prefix, kind, value
        for key, sub in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from walk(sub, child)
    elif isinstance(value, list):
        child = f"{prefix}[]"
        if prefix:
            yield prefix, kind, value
        seen_scalar = False
        for item in value:
            if isinstance(item, (dict, list)):
                yield from walk(item, child)
            elif not seen_scalar:
                seen_scalar = True
                yield child, _type_name(item), item
    else:
        yield prefix, kind, value


@dataclass
class Shape:
    command: str
    messages: int = 0
    paths: dict[str, Counter] = field(default_factory=dict)   # chemin -> types
    samples: dict[str, Any] = field(default_factory=dict)
    signatures: Counter = field(default_factory=Counter)

    def add(self, root: Any) -> None:
        self.messages += 1
        sig: list[str] = []
        for path, kind, value in walk(root):
            if not path:
                continue
            types = self.paths.setdefault(path, Counter())
            types[kind] += 1
            sig.append(f"{path}:{kind}")
            if path not in self.samples and not isinstance(value, (dict, list)):
                self.samples[path] = value
        self.signatures["|".join(sorted(set(sig)))] += 1


@dataclass
class InspectReport:
    stats: DecodeStats = field(default_factory=DecodeStats)
    shapes: dict[str, Shape] = field(default_factory=dict)
    commands: Counter = field(default_factory=Counter)
    unenveloped: int = 0

    def add(self, root_value: Any) -> None:
        env = unwrap(root_value)
        if env is None:
            self.unenveloped += 1
            command = "(hors enveloppe)"
            body: Any = root_value
        else:
            command = env.command
            body = {"p": env.body}
        self.commands[command] += 1
        shape = self.shapes.get(command)
        if shape is None:
            shape = self.shapes[command] = Shape(command)
        shape.add(body)


def inspect_source(
    source: CaptureSource,
    command_filter: str | None = None,
    saver: PayloadSaver | None = None,
) -> InspectReport:
    report = InspectReport()
    # NB : le saver est branche sur decode_source, donc il sauve TOUT, filtre
    # ou pas. Le filtre ne s'applique qu'a l'affichage.
    for item in decode_source(source, report.stats, saver):
        if item.root is None:
            continue
        report.add(item.root.value)
    if command_filter:
        needle = command_filter.lower()
        report.shapes = {
            k: v for k, v in report.shapes.items() if needle in k.lower()
        }
    return report


def render_report(
    report: InspectReport,
    key_filter: str | None = None,
    show_values: bool = False,
    max_paths: int = 400,
) -> str:
    out: list[str] = []
    st = report.stats

    out.append("=== index du protocole : commandes vues ===")
    if not report.commands:
        out.append("  (aucune)")
    for command, n in report.commands.most_common():
        out.append(f"  {n:6d}  {command}")

    out.append("")
    out.append("=== volumes ===")
    out.append(f"  payloads cadres      : {st.payloads}")
    out.append(f"  decodes              : {st.decoded}")
    out.append(f"  dont exacts          : {st.exact}")
    out.append(f"  non decodes          : {st.failed}")
    if report.unenveloped:
        out.append(f"  hors enveloppe       : {report.unenveloped}")
    if st.root_offsets:
        anchors = ", ".join(
            f"{off} (x{n})" for off, n in sorted(st.root_offsets.items())
        )
        out.append(f"  offsets racine       : {anchors}")
    if st.trailing_bytes:
        tails = ", ".join(
            f"+{n}o (x{c})" for n, c in sorted(st.trailing_bytes.items())
        )
        out.append(f"  octets non consommes : {tails}")

    out.append("")
    out.append("=== echecs de decodage, groupes par octet de type fautif ===")
    if not st.failures_by_tag and not st.failures_other:
        out.append("  (aucun)")
    for tag, n in st.failures_by_tag.most_common():
        known = " [inconnu documente]" if tag in wire.UNKNOWN_TAGS else ""
        out.append(f"  {n:6d}  0x{tag:02x}{known}")
    if st.failures_other:
        out.append(f"  {st.failures_other:6d}  (autre : troncature / pas de racine)")
    if st.failed:
        share = 100.0 * st.failed / max(1, st.payloads)
        out.append(
            f"  -> {share:.1f} % des payloads sont illisibles, donc INVISIBLES pour"
            " tous les filtres."
        )

    out.append("")
    out.append(st.guardrail())

    needle = key_filter.lower() if key_filter else None
    out.append("")
    out.append("=== formes de messages ===")
    if show_values:
        out.append("  (--values actif : de vraies donnees joueur sont affichees)")
    for command in sorted(report.shapes):
        shape = report.shapes[command]
        paths = sorted(shape.paths)
        if needle:
            paths = [p for p in paths if needle in p.lower()]
            if not paths:
                continue
        out.append("")
        out.append(f"--- {command}   ({shape.messages} message(s), "
                   f"{len(shape.signatures)} forme(s) distincte(s))")
        width = min(60, max((len(p) for p in paths), default=10))
        for path in paths[:max_paths]:
            types = shape.paths[path]
            kinds = ",".join(k for k, _ in types.most_common())
            line = f"    {path.ljust(width)}  {kinds}"
            if show_values and path in shape.samples:
                line += f"   ex: {_sample(shape.samples[path])}"
            out.append(line)
        if len(paths) > max_paths:
            out.append(f"    ... {len(paths) - max_paths} chemins de plus")
    return "\n".join(out)
