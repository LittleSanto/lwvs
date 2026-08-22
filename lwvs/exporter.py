"""Export -- le livrable principal.

Trois jeux de lignes, plats, sans imbrication. Contraintes qui comptent pour
l'outil en aval :

* `uid` est conserve : seule cle de jointure stable entre captures (les pseudos
  changent, `uid` non). Ses 4 derniers chiffres sur 16 sont le serveur d'origine.
* `scope` et `day` sont des COLONNES, jamais des noms de colonnes. Format long :
  une ligne par (joueur, scope, jour). L'outil en aval pivotera s'il veut.
* `captured_at` sur chaque ligne : un duel dure plusieurs jours et se capture en
  plusieurs sessions.
* AUCUNE AGREGATION. Pas de totaux par alliance, pas de moyennes, pas de parts.
  L'agregation est le travail de l'autre outil ; la faire ici livrerait deux
  fois la meme verite avec deux arrondis differents.
* `raw_type` reste a cote de `scope` pour que la deduction jour/cumul reste
  verifiable en aval.
* UTF-8 explicite, y compris pour le CSV : les pseudos contiennent des emoji, de
  l'arabe et du chinois. Sous Windows, un CSV ecrit sans encodage explicite
  sortirait en cp1252 et casserait.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import events as events_mod
from . import identity as identity_mod
from .store import Store

__all__ = ["DATASETS", "COLUMNS", "Feed", "FEEDS", "FEEDS_BY_KEY", "OwnAlliance",
           "resolve_own_alliance", "collect",
           "write_csv", "write_json", "build_records", "run_export"]

DATASETS = ("players", "group", "standing", "members", "camp", "server")

#: Valeur speciale de --alliance : resout TON alliance par la regle du
#: protocole (position du groupe == position de duelInfo) plutot que de figer
#: une abreviation, qui peut changer.
MINE = "mine"

COLUMNS: dict[str, tuple[str, ...]] = {
    "players": (
        "captured_at", "snapshot_id", "scope", "day", "raw_type", "rank", "uid",
        "name", "score", "server_id", "alliance_id", "alliance_name", "alliance_abbr",
    ),
    "group": (
        "captured_at", "snapshot_id", "group_code", "position", "alliance_id",
        "alliance_name", "alliance_abbr", "server_id", "round_result", "rank_type",
    ),
    "standing": (
        "captured_at", "snapshot_id", "scope", "group_code", "position",
        "rank_type", "round_result",
    ),
    "camp": (
        "captured_at", "snapshot_id", "rank", "uid", "name", "score",
        "server_id", "alliance_abbr", "country",
    ),
    "server": (
        "captured_at", "snapshot_id", "raw_type", "rank", "uid", "name",
        "hero_power", "level", "country", "alliance_id", "alliance_abbr",
        "alliance_name", "src_server", "server_id",
    ),
    "members": (
        "captured_at", "snapshot_id", "alliance_id", "uid", "name", "army_kill",
        "power", "alliance_rank", "today_progress", "weekly_progress",
        "main_city_lv", "server_id", "cur_server_id", "online", "join_time",
        "donate_time", "weekly_donate_time",
    ),
}


@dataclass(frozen=True)
class OwnAlliance:
    ids: list[str]
    #: "duel" | "roster" | "memoire" -- d'ou vient ce qu'on affirme.
    source: str
    label: str = ""


#: Ordre de resolution, du plus informatif au plus faible. Chacun est une
#: CERTITUDE sur l'identite, pas une heuristique :
#:
#: 1. duel   -- position du groupe == position de `duelInfo` (regle du
#:              protocole). Donne en plus la position et le code de groupe,
#:              mais exige l'ecran Duel dans la capture.
#: 2. roster -- `al.rank` porte `allianceId` : c'est TON roster par definition.
#:              Verifie identique a (1) sur donnees reelles.
#: 3. memoire -- apprise lors d'une capture precedente. N'est acceptee que si
#:              l'alliance apparait REELLEMENT dans la capture courante : une
#:              memoire perimee rendrait un export vide sans rien dire.


def resolve_own_alliance(store: Store, snapshot: int | None = None,
                         remember: bool = True) -> OwnAlliance:
    """Trouve ton alliance, et s'en souvient pour les captures suivantes."""
    for source, ids in (("duel", store.own_alliance_ids(snapshot)),
                        ("roster", store.roster_alliance_ids(snapshot))):
        if len(ids) == 1:
            info = store.describe_alliance(ids[0], snapshot)
            found = identity_mod.Identity(
                alliance_id=ids[0],
                alliance_abbr=info.get("alliance_abbr"),
                alliance_name=info.get("alliance_name"),
                server_id=info.get("server_id"),
                source=source,
            )
            if remember:
                identity_mod.save(found)
            return OwnAlliance(ids=ids, source=source, label=found.label)

    # La memoire n'est acceptee que si l'alliance apparait REELLEMENT dans la
    # capture -- par identifiant OU par abreviation, car certains classements
    # n'ont que l'abreviation.
    known = identity_mod.load()
    if known:
        par_id = known.alliance_id in store.alliance_ids_present(snapshot)
        par_abbr = bool(known.alliance_abbr) and (
            known.alliance_abbr.lower() in store.alliance_abbrs_present(snapshot))
        if par_id or par_abbr:
            return OwnAlliance(ids=[known.alliance_id], source="memoire",
                               label=known.label)

    detail = ""
    if known:
        detail = (f" L'alliance memorisee ({known.label}) n'apparait pas dans"
                  " cette capture : soit ce n'est pas la bonne, soit le"
                  " classement n'a pas ete recu.")
    raise ValueError(
        "impossible d'identifier ton alliance."
        " Ouvre une fois l'ecran Alliance -> Duel (qui envoie"
        " `get.alliance.duel.season.info` et `get.alliance.duel.group.info`)"
        " OU la liste des membres (`al.rank`). La reponse sera memorisee pour"
        " les captures suivantes." + detail
    )


def collect(
    store: Store,
    dataset: str,
    scope: str | None = None,
    day: int | None = None,
    alliance: str | None = None,
    snapshot: int | None = None,
) -> list[dict[str, Any]]:
    own_ids: list[str] | None = None
    if alliance and alliance.lower() == MINE:
        own_ids = resolve_own_alliance(store, snapshot).ids
        alliance = None

    if dataset == "players":
        rows = store.fetch_players(scope=scope, day=day, alliance=alliance,
                                   snapshot=snapshot, alliance_ids=own_ids)
    elif dataset == "members":
        rows = store.fetch_members(alliance_ids=own_ids, snapshot=snapshot)
    elif dataset == "camp":
        # Ce message ne porte PAS d'identifiant d'alliance, seulement `abbr`.
        # Le filtre se rabat donc sur l'abreviation -- moins solide qu'un id,
        # mais c'est tout ce que le jeu transmet ici, et l'inventer serait pire.
        abbrs = None
        if own_ids:
            abbrs = [a for a in
                     (store.describe_alliance(i, snapshot).get("alliance_abbr")
                      for i in own_ids) if a]
            if not abbrs:
                known = identity_mod.load()
                if known and known.alliance_abbr:
                    abbrs = [known.alliance_abbr]
            if not abbrs:
                raise ValueError(
                    "ce classement n'identifie les joueurs que par l'abreviation"
                    " d'alliance, et la tienne n'est pas connue. Capture une fois"
                    " l'ecran Duel ou la liste des membres."
                )
        rows = store.fetch_camp_rank(abbrs=abbrs, snapshot=snapshot)
    elif dataset == "server":
        # `rank.get` porte `allianceId` : le filtre par alliance est un vrai
        # identifiant, pas un rabattement sur l'abreviation comme pour `camp`.
        rows = store.fetch_server_rank(alliance_ids=own_ids, snapshot=snapshot)
    elif dataset == "group":
        rows = store.fetch_group(alliance=alliance, snapshot=snapshot,
                                 alliance_ids=own_ids)
    elif dataset == "standing":
        rows = store.fetch_standing(scope=scope if scope in ("current", "previous") else None,
                                    snapshot=snapshot)
    else:
        raise ValueError(f"jeu de donnees inconnu: {dataset}")
    cols = COLUMNS[dataset]
    return [{c: row.get(c) for c in cols} for row in rows]


def write_csv(rows: Sequence[dict[str, Any]], columns: Sequence[str],
              stream: io.TextIOBase) -> None:
    writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: "" if row.get(c) is None else row.get(c) for c in columns})


def write_json(payload: Any, stream: io.TextIOBase) -> None:
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


# ---------------------------------------------------------------------------
# Format "records" -- la forme attendue par l'outil OCR en aval
# ---------------------------------------------------------------------------
#
#   {"exported_at": "<ISO>", "mode": "<mode>",
#    "records": [{"rank": 1, "player_name": "...", "score": 123}]}
#
# Les champs propres a l'OCR (confidence, known_player_*, screen_rank, issues)
# n'ont pas d'equivalent ici : ils decrivent la fiabilite d'une lecture d'ecran,
# alors que ces valeurs viennent du fil. En inventer un serait mentir.
#
# `rank` est la POSITION DANS LA LISTE EXPORTEE (1..N, contigu), comme dans les
# fichiers de l'outil OCR. UNE SEULE regle pour tous les modes : deux
# semantiques sous un meme nom de champ seraient un piege pour l'aval. Les
# classements qui portent un rang de jeu (`lw.camp.battle.user.score.rank`) le
# voient donc renumerote -- et sans perte, puisque `--all-alliances` rend la
# liste complete, ou les deux coincident. Attention : filtre sur une seule alliance, ce n'est
# PAS le rang VS global, qui comporte des trous puisque les deux alliances sont
# classees ensemble. Le rang global reste disponible dans l'export normal.
#
# `uid` s'ajoute a la forme d'origine : c'est la SEULE cle de jointure stable
# entre exports. Les pseudos changent (`Nomade` -> `NomadeX`,
# `Kairo` -> `K a i r o` observes en 8 jours), et l'abreviation d'alliance
# aussi. Joindre sur le nom perd le joueur des qu'il se renomme. Meme nom que
# la colonne `uid` des autres exports : un seul nom pour une seule chose.

# Correspondance avec les modes de l'outil OCR (deux exports distincts) :
#
# * `kill_rank`   <- `army_kill` du roster (`al.rank`).
#   CONFIRME : sur 78 joueurs communs entre un export OCR du 02/08/2026 et une
#   capture du 10/08/2026, `army_kill` a augmente ou stagne pour les 78 -- la
#   signature d'un compteur cumulatif.
#
# * `weekly_rank` <- `score` du classement VS cumule (`al.battle.rank.info`
#   sans `day`), c'est-a-dire les points VS de la semaine de duel.
#
#   Une comparaison de rangs entre les deux fichiers ne prouverait RIEN : ils
#   portent sur des semaines de duel differentes, et a des stades differents
#   (jour 1 contre semaine complete). Deux competitions distinctes n'ont
#   aucune raison de classer les joueurs pareil. La correspondance vient de ce
#   que l'ecran mesure, pas d'une correlation.

# * `donation_weekly_rank` <- `weeklyProgress` du roster (`al.rank`), et
#   `donation_daily_rank` <- `todayProgress`. Ce sont les points de don, et ils
#   sont REMIS A ZERO periodiquement -- l'inverse exact de `kill_rank`. Aucune
#   difference a calculer en aval : la valeur envoyee est deja celle de la
#   periode. Voir `messages.py` pour ce qui etablit la lecture.

# * `thp_rank` <- `heroPower` du classement du serveur (`rank.get`). Le THP,
#   c'est-a-dire le chiffre de la fiche de profil -- CONFIRME a l'ecran : le
#   message porte `selfRanking`, qui designe la ligne du joueur, et sa valeur
#   est celle qu'il lisait en jeu.
#
#   Ni cumulatif ni remis a zero : c'est un INSTANTANE, qui monte et descend.
#
#   NE PAS le confondre avec `power` d'`al.rank`, qui est un AUTRE chiffre :
#   le rapport entre les deux va de 0.512 a 0.749 selon le joueur, donc ce
#   n'est pas un changement d'unite mais deux mesures distinctes. `power`
#   n'alimente aucun flux -- on n'exporte pas une metrique qu'on ne sait pas
#   nommer. Il reste une colonne du dataset `members` pour qui veut creuser.
#
#   Seul flux qui deborde de ton alliance : le classement couvre TOUT le
#   serveur (200 lignes, 15 alliances observees). Le filtre `mine` s'appuie sur
#   un vrai `allianceId`, pas sur l'abreviation.

#: dataset -> (colonne de score par defaut, mode par defaut)
#:
#: PAR DEFAUT seulement : la colonne de score n'est PAS une propriete du jeu de
#: lignes. `al.rank` transporte deux classements que le jeu affiche sur deux
#: ecrans differents -- les kills a vie et les points de don de la semaine. Un
#: flux choisit sa colonne via `Feed.metric` ; le defaut ne sert qu'a l'appel
#: nu (`export --dataset members`), dont le sens historique est les kills.
_RECORD_METRICS: dict[str, tuple[str, str]] = {
    "members": ("army_kill", "kill_rank"),
    "camp": ("score", "camp_battle_rank"),
    "players": ("score", "vs_rank"),
    "server": ("hero_power", "thp_rank"),
}

#: (dataset, colonne de score) -> mode par defaut.
#:
#: Le mode dit CE QUE `score` mesure. Deux metriques d'un meme jeu de lignes
#: doivent donc porter deux modes distincts : les fondre sous un seul empilerait
#: des points de don et des kills dans la meme serie cote site, et personne ne
#: le verrait avant que la courbe ne s'effondre.
_METRIC_MODES: dict[tuple[str, str], str] = {
    ("members", "army_kill"): "kill_rank",
    ("members", "weekly_progress"): "donation_weekly_rank",
    ("members", "today_progress"): "donation_daily_rank",
    ("camp", "score"): "camp_battle_rank",
    ("players", "score"): "vs_rank",
    ("server", "hero_power"): "thp_rank",
}


# ---------------------------------------------------------------------------
# Registre des flux exportables
# ---------------------------------------------------------------------------
#
# UN SEUL endroit decrit ce qui est exportable. La GUI le lit au lieu de coder
# en dur une liste de boutons : ajouter une statistique se fait ici, et elle
# apparait partout. Un jour ou l'autre il y aura d'autres classements a suivre.


@dataclass(frozen=True)
class Feed:
    key: str
    label: str
    dataset: str
    #: Commande du protocole qui alimente ce flux -- sert au voyant "vu / pas vu".
    command: str
    scope: str | None = None
    mode: str | None = None
    #: Colonne classee par ce flux. None = la colonne par defaut du dataset.
    #: C'est ce qui permet a deux flux de partager `al.rank` sans se marcher
    #: dessus : memes lignes, classement different.
    metric: str | None = None
    #: Vrai si le flux se decline par jour de duel : un classement par jour,
    #: jamais melanges.
    per_day: bool = False
    #: Vrai si le flux ne contient DEJA que ton alliance. `al.rank` est ton
    #: roster : lui appliquer le filtre `mine` serait redondant, et le ferait
    #: echouer sur une capture sans info de duel alors que la donnee est bonne.
    inherently_mine: bool = False
    hint: str = ""


FEEDS: tuple[Feed, ...] = (
    Feed(
        key="kills",
        label="Kills — roster de l'alliance",
        dataset="members",
        command="al.rank",
        mode="kill_rank",
        inherently_mine=True,
        hint="armyKill, cumulatif : la difference entre deux captures donne "
             "les kills de l'intervalle.",
    ),
    Feed(
        key="dons_semaine",
        label="Points de don — semaine",
        dataset="members",
        command="al.rank",
        metric="weekly_progress",
        mode="donation_weekly_rank",
        inherently_mine=True,
        hint="`weeklyProgress` du roster : les points de don depuis la remise a "
             "zero hebdomadaire. Deja la valeur de la periode -- contrairement "
             "aux kills, il n'y a AUCUNE difference a calculer en aval.",
    ),
    Feed(
        key="dons_jour",
        label="Points de don — aujourd'hui",
        dataset="members",
        command="al.rank",
        metric="today_progress",
        mode="donation_daily_rank",
        inherently_mine=True,
        hint="`todayProgress` : la part du jour dans le total de la semaine. "
             "Se vide a la remise a zero quotidienne -- a capturer le jour meme.",
    ),
    Feed(
        key="thp",
        label="THP — classement du serveur",
        dataset="server",
        command="rank.get",
        metric="hero_power",
        mode="thp_rank",
        hint="`heroPower` du classement du serveur : le chiffre de la fiche de "
             "profil (verifie a l'ecran). Ni cumulatif ni remis a zero -- c'est "
             "un INSTANTANE, qui monte et descend ; ne jamais soustraire deux "
             "captures. ATTENTION au faux ami : le champ `power` d'`al.rank` "
             "n'est PAS le THP (rapport 0.51 a 0.75 selon le joueur).",
    ),
    Feed(
        key="camp_battle",
        label="Classement d'événement (camp battle)",
        dataset="camp",
        command="lw.camp.battle.user.score.rank",
        mode="camp_battle_rank",
        hint="Les deux camps dans la meme liste. `rank` vient du jeu (il n'est "
             "pas positionnel ici) et `score` arrive en chaine. Aucun numero "
             "de journee n'est transmis.",
    ),
    Feed(
        key="vs_total",
        label="Points VS — cumul du duel",
        dataset="players",
        command="al.battle.rank.info",
        scope="total",
        mode="weekly_rank",
        hint="Classement sans `day` : le cumul de la semaine de duel.",
    ),
    Feed(
        key="vs_day",
        label="Points VS — par jour",
        dataset="players",
        command="al.battle.rank.info",
        scope="day",
        mode="daily_rank",
        per_day=True,
        hint="Un classement par jour. Deux jours ne se melangent jamais.",
    ),
)

FEEDS_BY_KEY: dict[str, Feed] = {f.key: f for f in FEEDS}


def build_records(
    rows: Sequence[dict[str, Any]],
    dataset: str,
    mode: str | None = None,
    scope: str | None = None,
    context: dict[str, Any] | None = None,
    declared_day: int | None = None,
    event: str | None = None,
    metric: str | None = None,
) -> dict[str, Any]:
    try:
        score_key, default_mode = _RECORD_METRICS[dataset]
    except KeyError:
        raise ValueError(
            f"le format 'records' ne s'applique qu'a {', '.join(_RECORD_METRICS)} :"
            f" '{dataset}' ne porte pas un score par joueur."
        ) from None

    if metric is not None:
        # Une colonne inconnue sortirait un document entier de scores a null
        # sans lever : le refus est immediat et nomme les colonnes possibles.
        #
        # Le controle porte sur les metriques DECLAREES, pas sur les colonnes du
        # dataset. Une colonne bien reelle mais sans mode -- `power` du roster,
        # qui n'est pas le THP et dont on ne sait pas ce qu'il agrege -- passait
        # le controle et retombait sur le mode par defaut du dataset : l'export
        # partait alors etiquete `kill_rank` avec des puissances dedans.
        connues = sorted(m for d, m in _METRIC_MODES if d == dataset)
        if (dataset, metric) not in _METRIC_MODES:
            connue_mais_muette = metric in COLUMNS[dataset]
            raise ValueError(
                f"colonne de score inconnue pour '{dataset}' : {metric!r}."
                + (" Cette colonne existe mais n'est pas une metrique publiable :"
                   " aucun `mode` ne dit ce qu'elle mesure."
                   if connue_mais_muette else "")
                + f" Possibles : {', '.join(connues)}."
            )
        score_key = metric
        default_mode = _METRIC_MODES[(dataset, metric)]

    snapshots = {r.get("snapshot_id") for r in rows}
    if len(snapshots) > 1:
        raise ValueError(
            "le format 'records' est un classement unique : il ne peut pas melanger"
            f" plusieurs captures (snapshots {sorted(snapshots)}). Choisis-en une"
            " avec --snapshot N."
        )

    days = {r.get("day") for r in rows} if dataset == "players" else set()
    if len(days) > 1:
        raise ValueError(
            "le format 'records' est un classement unique : il ne peut pas melanger"
            f" plusieurs jours de duel ({sorted(d for d in days if d is not None)})."
            " Choisis-en un avec --day N."
        )

    if mode is None:
        mode = default_mode
        if dataset == "players" and scope:
            # Noms descriptifs de CE qu'on exporte. On ne revendique pas
            # `weekly_rank` : la correspondance n'a pas pu etre etablie.
            # `weekly_rank` est le mode de l'outil en aval pour les points VS
            # cumules sur la semaine de duel. Le jour n'a pas de mode atteste
            # cote OCR : `daily_rank` est descriptif, surchargeable par --mode.
            mode = "weekly_rank" if scope == "total" else "daily_rank"

    ranked = sorted(rows, key=lambda r: (r.get(score_key) is None,
                                         -(r.get(score_key) or 0)))
    # `exported_at` porte l'instant de CAPTURE, pas l'instant d'ecriture : c'est
    # la seule date qui a un sens pour une serie temporelle, et cette forme n'a
    # pas de champ par ligne pour la porter.
    captured = next((r.get("captured_at") for r in ranked if r.get("captured_at")), None)
    doc: dict[str, Any] = {
        "exported_at": captured or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
    }
    # `context` est OMIS quand il est vide plutot que rendu a null : une cle
    # absente dit "pas su", une cle a null invite a la traiter comme une valeur.
    ctx = dict(context or {})
    # Journee MESUREE (le classement VS la declare) contre journee DECLAREE par
    # l'utilisateur. Les fondre sous un meme champ sans dire laquelle c'est
    # reviendrait a faire passer une affirmation humaine pour une mesure.
    mesuree = next(iter(days)) if dataset == "players" and days else None
    if mesuree is not None:
        ctx["day"] = mesuree
        ctx["day_source"] = "message"
    elif declared_day is not None:
        ctx["day"] = declared_day
        ctx["day_source"] = "declared"
    if event:
        # `event` n'est JAMAIS transmis par le jeu : toujours declare.
        # On sort l'IDENTIFIANT (cle de regroupement) et le LIBELLE
        # (affichage), comme `uid` / `player_name` : la cle ne bouge pas.
        resolu = events_mod.resolve(event)
        if resolu is None:
            connus = ", ".join(f"{e.id} ({e.label})" for e in events_mod.load())
            raise ValueError(
                f"evenement inconnu : {event!r}. Un libelle libre fragmenterait"
                " le suivi en aval, donc il faut le declarer d'abord :"
                f" `lwvs events --add \"{event}\"`. Connus : {connus}"
            )
        ctx["event"] = resolu.id
        ctx["event_label"] = resolu.label
    if ctx:
        doc["context"] = ctx
    doc["records"] = [
            {
                "rank": i + 1,
                "uid": r.get("uid"),
                "player_name": r.get("name"),
                "score": r.get(score_key),
            }
            for i, r in enumerate(ranked)
    ]
    return doc


def _open(path: Path, bom: bool) -> io.TextIOBase:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="")


@contextmanager
def _stdout_utf8():
    """stdout force en UTF-8, meme sur une console Windows en cp1252.

    Sans ca, l'export vers stdout casse sur le premier pseudo non latin -- et
    l'UTF-8 explicite du chemin fichier ne sert a rien puisqu'un utilisateur
    redirige naturellement vers un fichier avec `>`.
    """
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:          # stdout deja detourne (capsys, StringIO...)
        yield sys.stdout
        return
    sys.stdout.flush()
    stream = io.TextIOWrapper(buf, encoding="utf-8", newline="", write_through=True)
    try:
        yield stream
    finally:
        stream.flush()
        # detach() plutot que close() : fermer emporterait le vrai stdout.
        stream.detach()


def run_export(
    store: Store,
    dataset: str = "all",
    fmt: str = "json",
    out: str | None = None,
    scope: str | None = None,
    day: int | None = None,
    alliance: str | None = None,
    snapshot: int | None = None,
    bom: bool = False,
    mode: str | None = None,
    declared_day: int | None = None,
    event: str | None = None,
    metric: str | None = None,
) -> list[str]:
    """Ecrit l'export. Retourne la liste des cibles ecrites (ou ['-'])."""
    wanted = list(DATASETS) if dataset == "all" else [dataset]
    data = {
        name: collect(store, name, scope=scope, day=day, alliance=alliance,
                      snapshot=snapshot)
        for name in wanted
    }

    if fmt == "records":
        if len(wanted) > 1:
            raise ValueError(
                "le format 'records' porte un seul classement : choisis"
                " --dataset members ou --dataset players."
            )
        # Le contexte doit decrire la MEME alliance que celle qui a filtre les
        # lignes, y compris quand elle vient de la memoire.
        try:
            mine = resolve_own_alliance(store, snapshot, remember=False).ids
        except ValueError:
            mine = None
        payload = build_records(data[wanted[0]], wanted[0], mode=mode, scope=scope,
                                context=store.duel_context(snapshot, own_ids=mine),
                                declared_day=declared_day, event=event,
                                metric=metric)
        if out in (None, "-"):
            with _stdout_utf8() as stream:
                write_json(payload, stream)
            return ["-"]
        path = Path(out)
        with _open(path, bom=False) as fh:
            write_json(payload, fh)
        return [str(path)]

    if fmt == "json":
        payload: Any = data if len(wanted) > 1 else data[wanted[0]]
        if out in (None, "-"):
            with _stdout_utf8() as stream:
                write_json(payload, stream)
            return ["-"]
        path = Path(out)
        with _open(path, bom=False) as fh:   # JSON : jamais de BOM
            write_json(payload, fh)
        return [str(path)]

    if fmt != "csv":
        raise ValueError(f"format inconnu: {fmt}")

    if out in (None, "-"):
        if len(wanted) > 1:
            raise ValueError(
                "CSV sur stdout ne peut porter qu'un seul jeu de lignes : "
                "choisis --dataset players|group|standing, ou donne -o FICHIER."
            )
        with _stdout_utf8() as stream:
            write_csv(data[wanted[0]], COLUMNS[wanted[0]], stream)
        return ["-"]

    base = Path(out)
    written: list[str] = []
    for name in wanted:
        if len(wanted) > 1:
            target = base.with_name(f"{base.stem}.{name}{base.suffix or '.csv'}")
        else:
            target = base
        with _open(target, bom) as fh:
            write_csv(data[name], COLUMNS[name], fh)
        written.append(str(target))
    return written
