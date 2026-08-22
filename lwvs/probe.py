"""Sondeur d'octets de type inconnus.

Cherche exhaustivement quelle combinaison de layouts fait decoder le plus de
payloads JUSQU'A LEUR DERNIER OCTET EXACTEMENT.

On NE classe PAS les candidats par "distance parcourue avant l'echec" : une
largeur trop longue avale l'octet de type suivant et parse donc PLUS LOIN que
la bonne. Ce critere classe a l'envers. Seul "finit exactement a la fin" est
incorruptible.

Trois garde-fous, sinon ce n'est qu'une devinette habillee :

1. GROUPE DE CONTROLE -- on verifie d'abord que les payloads qui decodent deja
   passent le meme test. S'ils ne le passent pas, le test ne prouve rien sur
   les echecs non plus, et il faut le dire au lieu de classer.
2. L'AMBIGUITE SE RAPPORTE -- les combinaisons ex aequo sont toutes egalement
   compatibles avec les octets ; un octet sur lequel elles divergent est
   INDETERMINE, pas tranche a pile ou face.
3. RIEN NE S'ECRIT TOUT SEUL dans la table de types. Ce module ne modifie
   jamais `wire`. Une conclusion est une proposition a relire sur le hex.

La recherche de racine est restreinte aux OFFSETS OU LA CAPTURE ANCRE
REELLEMENT SES MESSAGES, releves sur les seuls payloads EXACTS. Un payload qui
decode en laissant une queue n'a pas revele un ancrage : il a revele une racine
bidon. Scanner les 64 offsets sur un payload qui ne parse nulle part trouve des
racines bidons au milieu de chaines et fabrique des octets de type qui
n'existent pas.
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from . import wire
from .capture import CaptureSource
from .pipeline import DecodeStats, decode_source

__all__ = ["CANDIDATES", "ProbeResult", "probe_source", "render_probe"]

Layout = wire.Fixed | wire.LenPrefixed | wire.Counted

#: Espace de recherche : largeur fixe, prefixe de longueur, conteneur compte.
CANDIDATES: tuple[Layout, ...] = (
    wire.Fixed(0),
    wire.Fixed(1),
    wire.Fixed(2),
    wire.Fixed(4),
    wire.Fixed(8),
    wire.Fixed(16),
    wire.LenPrefixed(1),
    wire.LenPrefixed(2),
    wire.LenPrefixed(4),
    wire.Counted(1, 1),
    wire.Counted(2, 1),
    wire.Counted(1, 2),
    wire.Counted(2, 2),
)

CONTROL_THRESHOLD = 0.95

_BY_NAME = {c.name: c for c in CANDIDATES}


def parse_layout(text: str) -> Layout:
    """"fixed:4" -> Fixed(4). Refuse tout ce qui n'est pas dans l'espace."""
    try:
        return _BY_NAME[text.strip()]
    except KeyError:
        raise ValueError(
            f"layout inconnu: {text!r}. Valeurs possibles : "
            + ", ".join(sorted(_BY_NAME))
        ) from None


def parse_assumption(text: str) -> tuple[int, Layout]:
    """"0x06=fixed:4" -> (6, Fixed(4))."""
    tag_s, _, layout_s = text.partition("=")
    if not layout_s:
        raise ValueError(f"attendu TAG=LAYOUT, recu {text!r}")
    return int(tag_s.strip(), 0), parse_layout(layout_s)


@dataclass
class ComboScore:
    assignment: dict[int, Layout]
    exact: int = 0
    decoded: int = 0

    @property
    def label(self) -> str:
        return ", ".join(
            f"0x{tag:02x}={lay.name}" for tag, lay in sorted(self.assignment.items())
        )


@dataclass
class ProbeResult:
    stats: DecodeStats
    anchors: list[int]
    control_total: int = 0
    control_exact: int = 0
    #: Payloads qui ne decodaient que via une racine bidon : une fois la racine
    #: restreinte aux ancrages reels, ils revelent un octet de type refuse.
    #: C'est exactement le mode d'echec silencieux de la regle gloutonne.
    unmasked: int = 0
    other_failures: int = 0
    blocking_tags: list[int] = field(default_factory=list)
    sample_sizes: list[int] = field(default_factory=list)
    combos_tested: int = 0
    scores: list[ComboScore] = field(default_factory=list)
    #: Octets de type inconnus rencontres APRES l'octet sonde, pendant le
    #: scoring. Un payload peut en enchainer plusieurs : le detecteur ne voit
    #: que le premier, d'ou l'interet de dire ce qui bloque ensuite.
    downstream_tags: Counter = field(default_factory=Counter)
    #: Nature des echecs pendant le scoring, pour ne pas conclure a l'aveugle.
    failure_kinds: Counter = field(default_factory=Counter)
    #: Layouts SUPPOSES resolus pour ce sondage. Ce ne sont pas des faits : ils
    #: conditionnent tout ce qui suit, et sont reaffiches comme tels.
    assumed: dict[int, Layout] = field(default_factory=dict)
    aborted: str | None = None

    @property
    def control_ratio(self) -> float:
        return self.control_exact / self.control_total if self.control_total else 0.0

    @property
    def control_ok(self) -> bool:
        return self.control_total > 0 and self.control_ratio >= CONTROL_THRESHOLD

    @property
    def best(self) -> list[ComboScore]:
        if not self.scores:
            return []
        top = max(s.exact for s in self.scores)
        if top == 0:
            return []
        return [s for s in self.scores if s.exact == top]

    def determinacy(self) -> dict[int, tuple[str, list[str]]]:
        """Par octet de type : ('determine'|'indetermine', layouts ex aequo)."""
        out: dict[int, tuple[str, list[str]]] = {}
        winners = self.best
        if not winners:
            return out
        for tag in self.blocking_tags:
            names = sorted({w.assignment[tag].name for w in winners if tag in w.assignment})
            verdict = "determine" if len(names) == 1 else "indetermine"
            out[tag] = (verdict, names)
        return out


def probe_source(
    source: CaptureSource,
    tags: Sequence[int] | None = None,
    max_payloads: int = 20,
    max_combos: int = 500,
    progress: bool = False,
    assume: dict[int, Layout] | None = None,
) -> ProbeResult:
    # --- passe 1 : fenetre complete, uniquement pour RELEVER LES ANCRAGES ----
    #
    # Seuls les payloads qui finissent EXACTEMENT au dernier octet ancrent
    # vraiment un message. Un payload qui decode en laissant une queue n'a pas
    # revele un ancrage : il a revele une racine bidon (typiquement une map
    # trouvee au milieu d'une chaine). L'inclure ici polluerait tout le reste.
    assumed = dict(assume or {})
    base = wire.TypeTable(extra=assumed)

    stats = DecodeStats()
    exact_count = 0
    decodable_count = 0
    suspect: list[bytes] = []   # decodent mais pas exactement, ou pas du tout

    for item in decode_source(source, stats, table=base):
        if item.root is not None:
            decodable_count += 1
            if item.root.exact:
                exact_count += 1
                continue
        suspect.append(item.payload.data)

    result = ProbeResult(stats=stats, anchors=stats.anchor_offsets(), assumed=assumed)

    if not exact_count:
        if decodable_count:
            result.control_total = decodable_count
            result.control_exact = 0
            result.aborted = (
                "groupe de controle invalide : aucun des payloads qui decodent ne"
                " finit exactement a son dernier octet. Le critere 'finit"
                " exactement' ne prouve donc rien sur les echecs non plus."
            )
        else:
            result.aborted = (
                "aucun payload ne decode : pas d'offset d'ancrage releve. Sans"
                " ancrage, scanner les 64 offsets fabriquerait des racines bidons"
                " au milieu de chaines. Capture d'abord du trafic qui decode."
            )
        return result

    # --- passe 2 : racine RESTREINTE aux ancrages -------------------------
    #
    # Un payload exact en passe 1 l'est forcement ici : son propre offset est un
    # ancrage et il consomme deja tout le payload, donc il reste maximal. On ne
    # rejuge que les suspects.
    failing: list[tuple[bytes, set[int]]] = []
    control_fail = 0
    for data in suspect:
        try:
            root = wire.decode_payload(data, base, offsets=result.anchors)
        except wire.RootDecodeError as exc:
            unknown = exc.unknown_tags
            if unknown:
                failing.append((data, unknown))
                result.unmasked += 1
            else:
                result.other_failures += 1
            continue
        except wire.DecodeError:
            result.other_failures += 1
            continue
        # Decode a un vrai ancrage sans consommer tout le payload : c'est le
        # mode d'echec silencieux de §1.6, et il casse le groupe de controle.
        control_fail += 1

    result.control_exact = exact_count
    result.control_total = exact_count + control_fail

    # Garde-fou 1 : sans groupe de controle valide, le test ne prouve rien.
    if not result.control_ok:
        result.aborted = (
            "groupe de controle invalide : des payloads decodent a un vrai offset"
            " d'ancrage sans consommer tout le payload. Le critere 'finit"
            " exactement' ne prouve donc rien sur les echecs non plus."
        )
        return result

    if not failing:
        return result

    observed_tags = sorted({t for _, tags_ in failing for t in tags_})
    observed_tags = [t for t in observed_tags if t not in assumed]
    wanted = sorted(set(tags)) if tags else observed_tags
    result.blocking_tags = [t for t in wanted if t in observed_tags] or wanted
    if not result.blocking_tags:
        result.aborted = "aucun octet de type inconnu bloquant dans cet echantillon."
        return result

    space = len(CANDIDATES) ** len(result.blocking_tags)
    if space > max_combos:
        result.aborted = (
            f"espace de recherche trop grand : {space} combinaisons pour les"
            f" octets {', '.join(f'0x{t:02x}' for t in result.blocking_tags)}"
            f" (plafond {max_combos}). Restreins avec --tag 0x06 (un seul octet a"
            " la fois : seuls les payloads dont c'est le seul blocage pourront"
            " atteindre 'exact', et c'est dit tel quel), ou augmente"
            " --max-combos en acceptant le temps de calcul."
        )
        return result

    # Echantillon : les plus PETITS payloads bloques d'abord. Ils sont aussi
    # informatifs et bien moins couteux a decoder des milliers de fois.
    sample = [data for data, _ in sorted(failing, key=lambda x: len(x[0]))][:max_payloads]
    result.sample_sizes = [len(d) for d in sample]

    for i, combo in enumerate(itertools.product(CANDIDATES, repeat=len(result.blocking_tags))):
        assignment = dict(zip(result.blocking_tags, combo))
        table = wire.TypeTable(extra={**assumed, **assignment})
        score = ComboScore(assignment=assignment)
        probed = set(assignment) | set(assumed)
        for data in sample:
            try:
                root = wire.decode_payload(data, table, offsets=result.anchors)
            except wire.RootDecodeError as exc:
                result.downstream_tags.update(exc.unknown_tags - probed)
                for err in exc.errors.values():
                    result.failure_kinds[type(err).__name__] += 1
                continue
            except wire.DecodeError as exc:
                result.failure_kinds[type(exc).__name__] += 1
                continue
            score.decoded += 1
            if root.exact:
                score.exact += 1
        result.scores.append(score)
        result.combos_tested += 1
        if progress and (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{space} combinaisons", flush=True)

    result.scores.sort(key=lambda s: (s.exact, s.decoded), reverse=True)
    return result


def render_probe(result: ProbeResult, top: int = 8) -> str:
    out: list[str] = []
    st = result.stats

    out.append("=== sondage d'octets de type inconnus ===")
    if result.assumed:
        assumed = ", ".join(f"0x{t:02x}={l.name}" for t, l in sorted(result.assumed.items()))
        out.append(f"  HYPOTHESES ADMISES : {assumed}")
        out.append("  Ce ne sont pas des faits. Tout ce qui suit en depend : si l'une")
        out.append("  d'elles est fausse, les conclusions le sont aussi.")
    out.append(f"  payloads cadres : {st.payloads}   decodes : {st.decoded}   "
               f"echecs : {st.failed}")
    out.append("")
    out.append("--- garde-fou 1 : groupe de controle ---")
    out.append(
        f"  payloads qui decodent a un vrai ancrage et finissent exactement a leur"
        f" dernier octet : {result.control_exact}/{result.control_total}"
        f" ({100.0 * result.control_ratio:.1f} %)"
    )
    if result.control_total and not result.control_ok:
        out.append("  -> INVALIDE. Le critere 'finit exactement' ne prouve rien ici.")
    elif result.control_ok:
        out.append("  -> valide : le critere est un oracle utilisable.")

    out.append("")
    out.append("--- ancrage ---")
    out.append(f"  offsets racine releves sur les payloads EXACTS : "
               f"{result.anchors or '(aucun)'}")
    out.append("  la recherche de racine est restreinte a ces offsets. Un payload qui")
    out.append("  decode en laissant une queue n'a pas revele un ancrage mais une")
    out.append("  racine bidon ; il ne compte pas ici.")
    if result.unmasked:
        out.append(
            f"  {result.unmasked} payload(s) ne decodaient que par une racine bidon :"
            " une fois la racine ancree, ils revelent un octet de type refuse."
        )
    if result.other_failures:
        out.append(f"  {result.other_failures} echec(s) sans octet de type identifiable"
                   " (troncature / pas de racine a l'ancrage).")

    if result.aborted:
        out.append("")
        out.append("--- arret ---")
        out.append(f"  {result.aborted}")
        return "\n".join(out)

    if not result.blocking_tags:
        out.append("")
        out.append("Aucun octet de type inconnu bloquant : rien a sonder.")
        return "\n".join(out)

    out.append("")
    out.append("--- echantillon ---")
    out.append(
        f"  {len(result.sample_sizes)} payload(s) bloque(s), les plus petits d'abord "
        f"(tailles : {result.sample_sizes})"
    )
    out.append(f"  octets sondes : "
               f"{', '.join(f'0x{t:02x}' for t in result.blocking_tags)}")
    out.append(f"  combinaisons testees : {result.combos_tested}")

    out.append("")
    out.append("--- classement (critere : finit EXACTEMENT au dernier octet) ---")
    out.append("  Note : on ne classe pas par distance parcourue. Une largeur trop")
    out.append("  longue avale l'octet de type suivant et parse plus loin que la")
    out.append("  bonne ; ce critere classerait a l'envers.")
    if not result.scores or result.scores[0].exact == 0:
        out.append("  Aucune combinaison ne fait finir un seul payload exactement.")
        if result.failure_kinds:
            kinds = ", ".join(f"{k} (x{n})" for k, n in result.failure_kinds.most_common())
            out.append(f"  Nature des echecs : {kinds}")
        if result.downstream_tags:
            downstream = ", ".join(f"0x{t:02x} (x{n})"
                                   for t, n in result.downstream_tags.most_common())
            out.append(f"  En cours de route, ces payloads butent sur : {downstream}")
            out.append("  Deux lectures, que rien ne departage ici :")
            out.append("    - le layout sonde n'est pas dans l'espace de recherche ;")
            out.append("    - ou il y est, mais un AUTRE octet inconnu suit, et le")
            out.append("      payload ne peut pas finir exactement tant qu'il reste.")
            together = " ".join(f"--tag 0x{t:02x}" for t in
                                sorted(set(result.blocking_tags) | set(result.downstream_tags)))
            out.append(f"  Sonde-les ensemble pour trancher : {together}")
        elif result.failure_kinds:
            out.append("  Aucun octet de type inconnu ne subsiste en aval : ce n'est donc")
            out.append("  PAS 'un autre inconnu bloque plus loin'. Chaque largeur essayee")
            out.append("  tombe a cote, ce qui pointe vers un layout absent de l'espace")
            out.append("  de recherche -- largeur variable non modelisee, par exemple.")
        out.append("  Ne conclus rien : elargis la capture ou l'espace.")
        return "\n".join(out)
    for score in result.scores[:top]:
        out.append(
            f"  exact={score.exact:3d}/{len(result.sample_sizes):<3d}"
            f"  decode={score.decoded:3d}  {score.label}"
        )

    out.append("")
    out.append("--- garde-fou 2 : ambiguite ---")
    winners = result.best
    out.append(f"  {len(winners)} combinaison(s) ex aequo au sommet.")
    for tag, (verdict, names) in sorted(result.determinacy().items()):
        if verdict == "determine":
            out.append(f"  0x{tag:02x} : proposition = {names[0]}")
        else:
            out.append(
                f"  0x{tag:02x} : INDETERMINE -- les ex aequo divergent "
                f"({', '.join(names)}). Toutes sont egalement compatibles avec les"
                " octets ; ce n'est pas a trancher a pile ou face."
            )

    out.append("")
    out.append("--- garde-fou 3 ---")
    out.append("  Rien n'a ete ecrit dans la table de types. Ce qui precede est une")
    out.append("  PROPOSITION a relire sur le hex avant de toucher lwvs/wire.py.")
    return "\n".join(out)
