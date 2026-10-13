"""Domaine : enveloppe et messages VS.

Ce module ne voit que des objets Python deja decodes par `wire`. Il ne connait
aucun octet.

Regle de vie privee : les champs non mappes sont JETES ici. Seuls leur nom et
leur type peuvent remonter, via `inspection`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "Envelope",
    "unwrap",
    "command_of",
    "CMD_RANK",
    "CMD_GROUP",
    "CMD_SEASON",
    "VS_COMMANDS",
    "RankRow",
    "RankMessage",
    "GroupRow",
    "GroupMessage",
    "StandingRow",
    "SeasonMessage",
    "CMD_MEMBERS",
    "MemberRow",
    "MembersMessage",
    "CMD_CAMP_RANK",
    "CampRankRow",
    "CampRankMessage",
    "CMD_SERVER_RANK",
    "ServerRankRow",
    "ServerRankMessage",
    "CMD_CANYON_SIGNUP",
    "CanyonSignupRow",
    "CanyonSignupMessage",
    "parse_message",
    "MAIL_DESERT_STORM",
    "MAIL_CANYON_STORM",
    "STORM_KINDS",
    "StormAlliance",
    "StormRow",
    "StormReport",
    "parse_storm_mail",
    "MAIL_TRAIN_PURCHASED",
    "MAIL_TRAIN_DEPARTED",
    "MAIL_TRAIN_ARRIVED",
    "TRAIN_MAILS",
    "TrainPerson",
    "TrainRun",
    "game_date",
    "parse_train_mail",
]


# ---------------------------------------------------------------------------
# Enveloppe -- CONFIRME
# ---------------------------------------------------------------------------
#
#   {p: {p: <corps>, c: "<nom de commande>"}, a: int16, c: int8}
#
# `c` interne porte le NOM DE COMMANDE. C'est le discriminant de type de
# message : on aiguille la-dessus, pas sur la presence d'une cle marqueur.
#
# `a` et `c` sur la map externe : role inconnu. On les transporte tels quels
# sans leur inventer de sens.


@dataclass(frozen=True)
class Envelope:
    command: str
    body: Any
    outer_a: Any = None
    outer_c: Any = None


def unwrap(obj: Any) -> Envelope | None:
    """Deballe l'enveloppe. None si l'objet n'en est pas une.

    Tout le trafic ne porte pas de commande. Un battement de coeur observe en
    capture reelle a la forme {a, c, p: {clientTime, serverTime}} : meme `a` et
    `c` externes, mais pas de `p.c`. `inspect` les compte comme "hors
    enveloppe" -- ce n'est pas un echec de decodage, c'est un autre message.
    """
    if not isinstance(obj, dict):
        return None
    inner = obj.get("p")
    if not isinstance(inner, dict):
        return None
    command = inner.get("c")
    if not isinstance(command, str) or not command:
        return None
    return Envelope(
        command=command,
        body=inner.get("p"),
        outer_a=obj.get("a"),
        outer_c=obj.get("c"),
    )


def command_of(obj: Any) -> str | None:
    env = unwrap(obj)
    return env.command if env else None


CMD_RANK = "al.battle.rank.info"
CMD_GROUP = "get.alliance.duel.group.info"
CMD_SEASON = "get.alliance.duel.season.info"
CMD_MEMBERS = "al.rank"
#: Classement d'evenement "camp battle" (saison 2). Confirme sur capture reelle :
#: les valeurs correspondent exactement a l'ecran, joueur par joueur.
CMD_CAMP_RANK = "lw.camp.battle.user.score.rank"
#: Classement du serveur. C'est le SEUL message qui porte le THP (`heroPower`),
#: et il couvre tout le serveur, pas seulement ton alliance.
CMD_SERVER_RANK = "rank.get"
#: Inscrits du Canyon Storm, par equipe. Le nom `quarantine` est celui du jeu,
#: pas le notre. Voir `parse_canyon_signup`.
CMD_CANYON_SIGNUP = "quarantine.act.player.list"
VS_COMMANDS = (CMD_RANK, CMD_GROUP, CMD_SEASON, CMD_MEMBERS, CMD_CAMP_RANK,
               CMD_SERVER_RANK, CMD_CANYON_SIGNUP)


# ---------------------------------------------------------------------------
# al.battle.rank.info -- le classement des joueurs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankRow:
    rank: int           # POSITIONNEL : l'ordre du tableau *est* le classement
    uid: str | None
    name: str | None
    score: int | None
    server_id: int | None
    alliance_id: str | None
    alliance_name: str | None
    alliance_abbr: str | None


@dataclass(frozen=True)
class RankMessage:
    scope: str               # "day" | "total"
    day: int | None
    raw_type: int | None
    rows: tuple[RankRow, ...] = ()


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def _as_id(v: Any) -> str | None:
    """Identifiants gardes en TEXTE : 16 chiffres, aucune arithmetique dessus.

    `uid` est la seule cle de jointure stable entre captures : les pseudos
    changent, `uid` non. C'est une donnee personnelle, mais l'amputer casserait
    le suivi d'un joueur d'un jour a l'autre.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v or None
    return None


def _as_str(v: Any) -> str | None:
    if isinstance(v, str):
        return v or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def parse_rank(body: Any) -> RankMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("rankInfo")
    if not isinstance(entries, list):
        return None
    # Jour vs cumul : on deduit le scope de la PRESENCE de `day`, pas de
    # `type`. Un classement qui declare le jour qu'il couvre est evidemment
    # celui de ce jour ; `type` est un entier opaque. Les deux concordent dans
    # toutes les captures, mais on stocke les deux pour qu'une capture
    # ulterieure puisse trancher (cf. colonne `raw_type` de l'export).
    day = _as_int(body.get("day")) if "day" in body else None
    scope = "day" if "day" in body else "total"
    raw_type = _as_int(body.get("type"))

    rows: list[RankRow] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        rows.append(
            RankRow(
                rank=i + 1,   # aucun champ ne porte le rang : il est positionnel
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                score=_as_int(entry.get("score")),
                server_id=_as_int(entry.get("serverId")),
                alliance_id=_as_id(entry.get("aid")),
                alliance_name=_as_str(entry.get("alName")),
                alliance_abbr=_as_str(entry.get("abbr")),
            )
        )
    return RankMessage(scope=scope, day=day, raw_type=raw_type, rows=tuple(rows))


# ---------------------------------------------------------------------------
# get.alliance.duel.group.info -- les 16 alliances du groupe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupRow:
    group_code: str | None
    position: int | None
    alliance_id: str | None
    alliance_name: str | None
    alliance_abbr: str | None
    server_id: int | None
    round_result: int | None
    rank_type: int | None


@dataclass(frozen=True)
class GroupMessage:
    rows: tuple[GroupRow, ...] = ()


def parse_group(body: Any) -> GroupMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("groupInfos")
    if not isinstance(entries, list):
        return None
    rows: list[GroupRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rows.append(
            GroupRow(
                group_code=_as_str(entry.get("group")),
                position=_as_int(entry.get("position")),
                alliance_id=_as_id(entry.get("allianceId")),
                alliance_name=_as_str(entry.get("name")),
                alliance_abbr=_as_str(entry.get("abbr")),
                server_id=_as_int(entry.get("serverId")),
                round_result=_as_int(entry.get("roundResult")),
                rank_type=_as_int(entry.get("rankType")),
            )
        )
    return GroupMessage(rows=tuple(rows))


# ---------------------------------------------------------------------------
# get.alliance.duel.season.info -- ta propre alliance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StandingRow:
    scope: str          # "current" | "previous"
    group_code: str | None
    position: int | None
    rank_type: int | None
    round_result: int | None


@dataclass(frozen=True)
class SeasonMessage:
    rows: tuple[StandingRow, ...] = ()


_SEASON_KEYS = (("duelInfo", "current"), ("lastDuelInfo", "previous"))


def parse_season(body: Any) -> SeasonMessage | None:
    if not isinstance(body, dict):
        return None
    rows: list[StandingRow] = []
    for key, scope in _SEASON_KEYS:
        info = body.get(key)
        if not isinstance(info, dict):
            continue
        rows.append(
            StandingRow(
                scope=scope,
                group_code=_as_str(info.get("group")),
                position=_as_int(info.get("position")),
                rank_type=_as_int(info.get("rankType")),
                round_result=_as_int(info.get("roundResult")),
            )
        )
    if not rows:
        return None
    return SeasonMessage(rows=tuple(rows))


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# al.rank -- le roster de TON alliance, seul porteur de armyKill
# ---------------------------------------------------------------------------
#
# armyKill est un compteur CUMULATIF porte par le joueur, pas par le duel
# (confirme a l'ecran). Il n'existe que pour ta propre alliance : `al.rank` ne
# transmet pas le roster adverse. Les kills d'un intervalle s'obtiennent donc
# par DIFFERENCE entre deux snapshots, joints sur `uid` -- c'est precisement ce
# que l'accumulation permet. La difference n'est pas calculee ici : l'export ne
# sort que des lignes brutes.
#
# CE MESSAGE PORTE DEUX CLASSEMENTS, pas un. L'ecran "Points de don" est
# `weeklyProgress`, et il n'a rien a voir avec `armyKill` :
#
#   * `weeklyProgress` -- points de don accumules depuis la remise a zero
#     hebdomadaire. REMIS A ZERO chaque semaine : c'est une valeur directe, pas
#     une difference a calculer. Contraire exact de `armyKill`.
#   * `todayProgress`  -- la part du jour dans ce total.
#
# Ce que la capture du lundi 10/08/2026 etablit, sur 99 membres :
#   - `todayProgress == weeklyProgress` pour les 99 -> la semaine venait de
#     redemarrer, donc les deux compteurs partagent bien la meme remise a zero
#     et `weeklyProgress` court du lundi au dimanche ;
#   - `weeklyDonateTime == 0` exactement quand `weeklyProgress == 0` (99/99) ->
#     "progress" est bien alimente par les DONS, et par rien d'autre ;
#   - les 99 valeurs sont multiples de 50, comme celles lues a l'ecran.
#
# TROISIEME classement dans le meme message : `power`, la puissance totale du
# joueur (THP), celle de la fiche de profil. Elle ne se comporte comme aucun
# des deux autres :
#
#   * `armyKill` ne descend jamais (cumulatif a vie) ;
#   * `weeklyProgress` retombe a zero, a date fixe ;
#   * `power` monte ET descend a tout moment -- c'est un INSTANTANE, pas un
#     compteur. Entre les captures du 10/08/2026 et du 11/08/2026, sur les 99
#     membres communs : 60 en hausse, 38 EN BAISSE, 1 stable, la plus forte
#     baisse a -16 671 288. Une baisse est le fonctionnement normal (troupes
#     perdues, soignees, equipement change), pas une anomalie de mesure.
#
# Consequence : une difference entre deux captures n'a de sens ni comme
# "puissance gagnee sur la periode" ni comme signal d'erreur. On envoie la
# valeur brute et on le dit au contrat.
#
# `donateTime` / `weeklyDonateTime` sont des HORODATAGES en millisecondes
# (~1.78e12), pas des compteurs de dons : 0 = aucun don sur la periode. On les
# garde parce que ce sont eux qui distinguent "n'a pas donne" de "pas mesure",
# ce qu'un score a 0 ne dit pas.


@dataclass(frozen=True)
class MemberRow:
    uid: str | None
    name: str | None
    army_kill: int | None
    power: int | None            # puissance totale (THP) : instantane, pas compteur
    alliance_rank: int | None   # rang DANS l'alliance (R1-R5), pas le rang VS
    today_progress: int | None      # points de don du jour
    weekly_progress: int | None     # points de don de la semaine
    main_city_lv: int | None
    server_id: int | None
    cur_server_id: int | None
    online: bool | None
    join_time: int | None
    donate_time: int | None = None          # dernier don, epoch ms (0 = jamais)
    weekly_donate_time: int | None = None   # dernier don de la semaine, epoch ms


@dataclass(frozen=True)
class MembersMessage:
    alliance_id: str | None
    rows: tuple[MemberRow, ...] = ()


def _as_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def parse_members(body: Any) -> MembersMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("list")
    if not isinstance(entries, list):
        return None
    rows: list[MemberRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rows.append(
            MemberRow(
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                army_kill=_as_int(entry.get("armyKill")),
                power=_as_int(entry.get("power")),
                alliance_rank=_as_int(entry.get("rank")),
                today_progress=_as_int(entry.get("todayProgress")),
                weekly_progress=_as_int(entry.get("weeklyProgress")),
                main_city_lv=_as_int(entry.get("mainCityLv")),
                server_id=_as_int(entry.get("serverId")),
                cur_server_id=_as_int(entry.get("curServerId")),
                online=_as_bool(entry.get("online")),
                join_time=_as_int(entry.get("joinTime")),
                donate_time=_as_int(entry.get("donateTime")),
                weekly_donate_time=_as_int(entry.get("weeklyDonateTime")),
            )
        )
    return MembersMessage(alliance_id=_as_id(body.get("allianceId")), rows=tuple(rows))


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# lw.camp.battle.user.score.rank -- classement d'evenement
# ---------------------------------------------------------------------------
#
# Trois differences avec `al.battle.rank.info`, chacune verifiee sur capture :
#
# 1. `score` est une CHAINE, pas un entier. On la convertit, mais elle arrive
#    en texte -- une recherche numerique dans les valeurs decodees la manque.
# 2. `rank` EST un champ du message. C'est l'inverse du classement VS, ou le
#    rang est positionnel. On lit celui du jeu et on ne le recalcule pas.
# 3. Pas d'`aid` : l'appartenance n'est portee que par `abbr`. Il n'y a donc
#    pas d'identifiant d'alliance stable dans ce message.
#
# Le message ne porte AUCUN numero de journee. On n'en invente pas.


@dataclass(frozen=True)
class CampRankRow:
    rank: int | None
    uid: str | None
    name: str | None
    score: int | None
    server_id: int | None
    alliance_abbr: str | None
    country: str | None


@dataclass(frozen=True)
class CampRankMessage:
    rows: tuple[CampRankRow, ...] = ()
    self_rank: int | None = None
    self_score: int | None = None


def parse_camp_rank(body: Any) -> CampRankMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("list")
    if not isinstance(entries, list):
        return None
    rows: list[CampRankRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rows.append(
            CampRankRow(
                rank=_as_int(entry.get("rank")),
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                score=_as_int(entry.get("score")),   # arrive en chaine
                server_id=_as_int(entry.get("serverId")),
                alliance_abbr=_as_str(entry.get("abbr")),
                country=_as_str(entry.get("country")),
            )
        )
    own = body.get("self") if isinstance(body.get("self"), dict) else {}
    return CampRankMessage(rows=tuple(rows),
                           self_rank=_as_int(own.get("rank")),
                           self_score=_as_int(own.get("score")))


# ---------------------------------------------------------------------------
# rank.get -- le classement du serveur, seul porteur du THP
# ---------------------------------------------------------------------------
#
# `heroPower` EST le chiffre affiche sur la fiche de profil (verifie a l'ecran :
# selfRanking pointait la ligne du joueur, et sa valeur est celle qu'il lisait).
# THP = Total Hero Power.
#
# LE FAUX AMI QUI A COUTE UN FLUX ENTIER : `al.rank` porte un champ `power`, et
# ce n'est PAS le meme chiffre. Sur les 42 joueurs communs entre les deux
# captures, le rapport `heroPower / power` va de 0.512 a 0.749 -- 23.7 points
# d'ecart SELON LE JOUEUR. Un changement d'unite serait constant ; celui-la ne
# l'est pas. `power` agrege quelque chose de plus large, dont la composition
# n'est pas etablie : il reste une colonne du roster et n'alimente aucun flux,
# parce qu'on n'exporte pas une metrique qu'on ne sait pas nommer.
#
# Trois particularites :
#
# 1. Le rang est POSITIONNEL. Aucune ligne ne porte de champ `rank`, et la
#    liste arrive triee par `heroPower` decroissant (verifie sur 200 lignes).
# 2. `type` discrimine l'ONGLET du classement. Un seul (13) a ete observe, et
#    on ne sait pas ce que valent les autres : il est transporte tel quel, sans
#    interpretation, pour que l'aval puisse verifier qu'il ne melange pas deux
#    onglets sous une meme metrique.
# 3. `allianceId` est ABSENT pour les joueurs sans alliance (5 sur 200). Ce
#    n'est pas un defaut de decodage : le filtre par alliance les ecarte
#    naturellement, et c'est le bon comportement.


@dataclass(frozen=True)
class ServerRankRow:
    rank: int           # POSITIONNEL, comme le classement VS
    uid: str | None
    name: str | None
    hero_power: int | None      # LE THP
    level: int | None
    country: str | None
    alliance_id: str | None
    alliance_abbr: str | None
    alliance_name: str | None
    src_server: int | None


@dataclass(frozen=True)
class ServerRankMessage:
    server_id: int | None = None
    raw_type: int | None = None
    self_ranking: int | None = None
    rows: tuple[ServerRankRow, ...] = ()


def parse_server_rank(body: Any) -> ServerRankMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("serverRanking")
    if not isinstance(entries, list):
        return None
    rows: list[ServerRankRow] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        rows.append(
            ServerRankRow(
                rank=i + 1,   # aucun champ ne porte le rang : il est positionnel
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                hero_power=_as_int(entry.get("heroPower")),
                level=_as_int(entry.get("level")),
                country=_as_str(entry.get("country")),
                alliance_id=_as_id(entry.get("allianceId")),
                alliance_abbr=_as_str(entry.get("abbr")),
                alliance_name=_as_str(entry.get("alliancename")),
                src_server=_as_int(entry.get("srcServer")),
            )
        )
    return ServerRankMessage(
        server_id=_as_int(body.get("serverId")),
        raw_type=_as_int(body.get("type")),
        self_ranking=_as_int(body.get("selfRanking")),
        rows=tuple(rows),
    )


# ---------------------------------------------------------------------------
# quarantine.act.player.list -- inscrits du Canyon Storm, par equipe
# ---------------------------------------------------------------------------
#
# Etabli le 13/10/2026 sur la capture `captures/canyon` (l'ecran d'inscription
# ouvert, onglets A puis B). Le message liste les 98 membres de TON alliance ;
# `group` dit lequel est inscrit et dans quelle equipe : 0 = personne (52),
# 1 = 22 joueurs, 2 = 24 joueurs. L'ecran affichait A = 22 et B = 24 : deux
# effectifs differents, donc la correspondance 1 = A, 2 = B n'est pas un hasard.
#
# Ce qu'on NE lit PAS :
#
# * `state` : 1 = titulaire, 2 = remplacant. NON confirme a l'ecran, mais
#   chaque equipe compte EXACTEMENT 20 joueurs a `state` 1 (le plafond de
#   titulaires) plus un reste a `state` 2 : 2 en A, 4 en B (<= 10, le plafond de
#   remplacants). Un plafond atteint pile dans les deux equipes ne se rencontre
#   pas par hasard. Une autre valeur de `state` : pas de role, jamais devine.
# * `heroPower` et `power` : on ne sait pas si `heroPower` est le THP de
#   `rank.get` (rapport 1,08 a 1,17 selon le joueur sur les 44 communs, mais
#   deux dates). On n'exporte pas une metrique qu'on ne sait pas nommer.
#
# Le meme message arrive plusieurs fois par capture (6 fois, identique) : la cle
# (snapshot, uid) le ramene a une verite.

_CANYON_TEAMS_BY_GROUP = {1: "A", 2: "B"}
_CANYON_ROLES_BY_STATE = {1: "starter", 2: "substitute"}


@dataclass(frozen=True)
class CanyonSignupRow:
    uid: str | None
    name: str | None
    team: str               # "A" | "B"
    server_id: int | None
    role: str | None = None     # "starter" | "substitute" | None (inconnu)


@dataclass(frozen=True)
class CanyonSignupMessage:
    rows: tuple[CanyonSignupRow, ...] = ()


def parse_canyon_signup(body: Any) -> CanyonSignupMessage | None:
    if not isinstance(body, dict):
        return None
    entries = body.get("users")
    if not isinstance(entries, list):
        return None
    rows: list[CanyonSignupRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # group 0 = membre non inscrit ; une valeur inconnue est ecartee, pas
        # rangee dans une equipe au hasard.
        team = _CANYON_TEAMS_BY_GROUP.get(_as_int(entry.get("group")))
        if team is None:
            continue
        rows.append(
            CanyonSignupRow(
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                team=team,
                server_id=_as_int(entry.get("serverId")),
                role=_CANYON_ROLES_BY_STATE.get(_as_int(entry.get("state"))),
            )
        )
    return CanyonSignupMessage(rows=tuple(rows))


_PARSERS = {
    CMD_RANK: parse_rank,
    CMD_GROUP: parse_group,
    CMD_SEASON: parse_season,
    CMD_MEMBERS: parse_members,
    CMD_CAMP_RANK: parse_camp_rank,
    CMD_SERVER_RANK: parse_server_rank,
    CMD_CANYON_SIGNUP: parse_canyon_signup,
}


def parse_message(obj: Any):
    """(commande, message typé | None) pour un objet racine decode."""
    env = unwrap(obj)
    if env is None:
        return None, None
    parser = _PARSERS.get(env.command)
    if parser is None:
        return env.command, None
    return env.command, parser(env.body)


# ---------------------------------------------------------------------------
# Rapports de Desert Storm / Canyon Storm -- par MAIL, pas par commande
# ---------------------------------------------------------------------------
#
# Source : la table `MailData` du cache client (voir `gamecache.py`), pas le
# fil. Le contenu est le JSON `contents`, deja decode ; ce module n'en voit que
# des dict. Le `type` du mail est le discriminant, comme le nom de commande
# ailleurs.
#
# Etabli le 28/09/2026 contre les captures d'ecran des mails des 24 et
# 25/09/2026, valeur par valeur (scores des joueurs, scores d'alliance, MVP) :
#
# Desert Storm (type 109)
#   * `ranks[]` : `uid`, `name`, `score`, `rank` -- rang REEL, 1..n contigu,
#     liste deja triee. Aucun `allianceId` par ligne : seule `abbr`.
#   * `alliances[]` : les deux alliances, avec leur `score` et `alId`.
#   * `result` : 1 = victoire, 0 = defaite (verifie : 215 835 contre 373 018
#     -> 0 ; 298 773 contre 279 837 -> 1).
#   * AUCUN marqueur d'equipe. Voir `_desert_team`.
#
# Canyon Storm (type 181)
#   * `ranks[]` : `uid`, `name`, `allianceId`, `score` = total, et sa
#     ventilation `battleScore` + `cooperationScore` (assistance) +
#     `tacticsScore` (strategie). La somme est exacte pour 206 lignes sur 206.
#   * PAS de champ de rang, et la liste n'est PAS triee. Le rang du jeu se
#     recalcule par score decroissant sur TOUT le camp -- alliees comprises :
#     c'est ce qui donne a l'ecran les trous 5, 6, 8, 9, 10, 13... quand la
#     case "Mon Alliance" masque l'alliee. Verifie rang par rang.
#   * `ranks[]` ne contient que TON camp, qui peut reunir deux alliances.
#     `alliances[].role` dit le camp de chacune, `obj.role` le tien.
#   * Le score et l'effectif de chaque CAMP sont dans `landlord` (role 1) et
#     `farmer` (role 2) : 258 747 et 38 pour le "38/40" de l'ecran du 24/09.
#   * L'equipe EST dans le mail : le parametre du titre. Voir `_CANYON_TEAMS`.
#
# Participation : figurer dans `ranks[]`, c'est avoir participe. `ranks` peut
# depasser l'effectif affiche (44 lignes pour "38/40") : il compte aussi ceux
# qui sont entres puis sortis. Une absence ne dit pas "inscrit mais absent" --
# le mail ne porte pas la liste des inscrits.

MAIL_DESERT_STORM = 109
MAIL_CANYON_STORM = 181
#: type de mail -> identifiant stable de l'evenement.
STORM_KINDS: dict[int, str] = {
    MAIL_DESERT_STORM: "desert_storm",
    MAIL_CANYON_STORM: "canyon_storm",
}

#: Parametre du titre -> equipe. CONFIRME sur les mails du 24/09/2026 : le titre
#: affiche "Force Operationnelle A" pour `_5` (camp GolD + SynD contre ForD) et
#: "B" pour `_6` (GolD contre VOXM + TIWA). Un parametre absent de la table
#: laisse l'equipe inconnue : deviner serait pire.
_CANYON_TEAMS = {
    "YiBianJinQu_trivial_tips_5": "A",
    "YiBianJinQu_trivial_tips_6": "B",
}

#: Desert Storm : le mail ne porte AUCUN marqueur d'equipe. La correspondance
#: est DECLAREE par l'alliance (28/09/2026) : le creneau du soir est l'equipe A,
#: celui du debut d'apres-midi l'equipe B. Elle se lit sur l'heure UTC du mail,
#: constante d'une saison a l'autre (11:30 et 20:30 UTC, soit 13:30 et 22:30
#: l'ete a Paris, 12:30 et 21:30 l'hiver). C'est une affirmation humaine, pas
#: une mesure : elle sort marquee `team_source: "declared"`.
_DESERT_EVENING_FROM_UTC_HOUR = 16


def _desert_team(created_ms: int) -> str:
    hour = (created_ms // 3_600_000) % 24
    return "A" if hour >= _DESERT_EVENING_FROM_UTC_HOUR else "B"


@dataclass(frozen=True)
class StormAlliance:
    alliance_id: str | None
    abbr: str | None
    name: str | None
    server_id: int | None
    #: Desert seulement : le score de l'alliance. Canyon ne le porte pas ici.
    score: int | None = None


@dataclass(frozen=True)
class StormRow:
    uid: str | None
    name: str | None
    score: int | None
    #: Rang du jeu : lu (Desert) ou recalcule sur tout le camp (Canyon).
    game_rank: int | None
    alliance_id: str | None
    alliance_abbr: str | None
    # Canyon seulement : la ventilation de `score`.
    battle_score: int | None = None
    assist_score: int | None = None
    strategy_score: int | None = None


@dataclass(frozen=True)
class StormReport:
    kind: str                       # "desert_storm" | "canyon_storm"
    mail_uid: str
    created_ms: int
    team: str | None                # "A" | "B" | None
    team_source: str | None         # "message" | "declared"
    won: bool | None
    #: Le camp du destinataire (une ou deux alliances), puis l'adversaire.
    own_side: tuple[StormAlliance, ...] = ()
    other_side: tuple[StormAlliance, ...] = ()
    rows: tuple[StormRow, ...] = ()
    #: Score et effectif de chaque camp. Desert : le score de l'alliance, pas
    #: d'effectif transmis.
    own_score: int | None = None
    other_score: int | None = None
    own_members: int | None = None
    other_members: int | None = None


def _storm_alliance(entry: dict[str, Any]) -> StormAlliance:
    return StormAlliance(
        alliance_id=_as_id(entry.get("alId")),
        abbr=_as_str(entry.get("abbr")),
        name=_as_str(entry.get("name")),
        server_id=_as_int(entry.get("serverId")),
        score=_as_int(entry.get("score")),
    )


def _dicts(v: Any) -> list[dict[str, Any]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _title_param(title: Any) -> str | None:
    try:
        params = title["h"]["title"]["dialog"]["params"]
    except (KeyError, TypeError):
        return None
    for p in _dicts(params):
        text = _as_str(p.get("text"))
        if text:
            return text
    return None


def parse_storm_mail(mail_type: int, mail_uid: str, created_ms: int,
                     title: Any, contents: Any) -> StormReport | None:
    """Rapport de Storm, ou None si ce mail n'en est pas un lisible."""
    kind = STORM_KINDS.get(mail_type)
    if kind is None or not isinstance(contents, dict):
        return None
    obj = contents.get("obj")
    if not isinstance(obj, dict) or not isinstance(obj.get("ranks"), list):
        return None
    alliances = _dicts(obj.get("alliances"))
    ranks = _dicts(obj.get("ranks"))

    if mail_type == MAIL_CANYON_STORM:
        mine = _as_int(obj.get("role"))
        own = tuple(_storm_alliance(a) for a in alliances
                    if mine is not None and _as_int(a.get("role")) == mine)
        other = tuple(_storm_alliance(a) for a in alliances
                      if mine is None or _as_int(a.get("role")) != mine)
        by_id = {a.alliance_id: a for a in own + other if a.alliance_id}
        # Tri stable : a score egal, l'ordre du mail departage, faute de mieux.
        ordered = sorted(ranks, key=lambda e: -(_as_int(e.get("score")) or 0))
        rows = []
        for i, entry in enumerate(ordered):
            aid = _as_id(entry.get("allianceId"))
            rows.append(StormRow(
                uid=_as_id(entry.get("uid")),
                name=_as_str(entry.get("name")),
                score=_as_int(entry.get("score")),
                game_rank=i + 1,
                alliance_id=aid,
                alliance_abbr=_as_str(entry.get("abbr"))
                or (by_id[aid].abbr if aid in by_id else None),
                battle_score=_as_int(entry.get("battleScore")),
                assist_score=_as_int(entry.get("cooperationScore")),
                strategy_score=_as_int(entry.get("tacticsScore")),
            ))
        team = _CANYON_TEAMS.get(_title_param(title) or "")
        camps = {1: obj.get("landlord"), 2: obj.get("farmer")}
        mine_camp = camps.get(mine) if isinstance(camps.get(mine), dict) else {}
        theirs = camps.get(3 - mine) if mine in (1, 2) else None
        theirs = theirs if isinstance(theirs, dict) else {}
        return StormReport(
            kind=kind, mail_uid=mail_uid, created_ms=created_ms,
            team=team, team_source="message" if team else None,
            won=obj.get("isWin") if isinstance(obj.get("isWin"), bool) else None,
            own_side=own, other_side=other, rows=tuple(rows),
            own_score=_as_int(mine_camp.get("score")),
            other_score=_as_int(theirs.get("score")),
            own_members=_as_int(mine_camp.get("member")),
            other_members=_as_int(theirs.get("member")))

    # Desert Storm : une alliance par camp. Les lignes n'ont que `abbr` ; on la
    # rattache a `alId` via la liste des alliances du MEME mail, seulement si
    # l'abreviation y est sans ambiguite.
    every = [_storm_alliance(a) for a in alliances]
    abbr_count: dict[str, int] = {}
    for a in every:
        if a.abbr:
            abbr_count[a.abbr] = abbr_count.get(a.abbr, 0) + 1
    by_abbr = {a.abbr: a for a in every if a.abbr and abbr_count[a.abbr] == 1}
    rows = []
    for entry in ranks:
        abbr = _as_str(entry.get("abbr"))
        rows.append(StormRow(
            uid=_as_id(entry.get("uid")),
            name=_as_str(entry.get("name")),
            score=_as_int(entry.get("score")),
            game_rank=_as_int(entry.get("rank")),
            alliance_id=by_abbr[abbr].alliance_id if abbr in by_abbr else None,
            alliance_abbr=abbr,
        ))
    present = {r.alliance_abbr for r in rows if r.alliance_abbr}
    result = _as_int(obj.get("result"))
    own = tuple(a for a in every if a.abbr in present)
    other = tuple(a for a in every if a.abbr not in present)
    return StormReport(
        kind=kind, mail_uid=mail_uid, created_ms=created_ms,
        team=_desert_team(created_ms), team_source="declared",
        won=None if result is None else result == 1,
        own_side=own, other_side=other, rows=tuple(rows),
        own_score=own[0].score if len(own) == 1 else None,
        other_score=other[0].score if len(other) == 1 else None)


# ---------------------------------------------------------------------------
# Trains d'alliance -- par MAIL, comme Storm
# ---------------------------------------------------------------------------
#
# Etabli le 11/10/2026 sur la boite du poste de reference : 71 trains, du
# 14/03 au 10/10/2026. Chaque membre recoit les mails de CHAQUE train de son
# alliance, quel que soit son role -- d'ou un train par jour dans la boite.
#
#   130  depart (dialogue 458512, l'heure d'arrivee en parametre). `obj` est
#        l'objet train lui-meme, `marchInfo.isEnd` = false.
#   128  arrivee (458513). `obj.trainObj` = le meme objet, final, et
#        `obj.history[]` = les attaques subies.
#   127  annonce d'un train ACHETE (458511) : conducteur et heure de depart,
#        4 h plus tard, sans objet. Le train annonce porte `buyFlag` = 1. Les
#        trois cas observes tombent un jour qui a deja eu son train quotidien.
#   129  MES recompenses (passager 1028, VIP/Guardian 1030) : propres au
#        destinataire, inutiles ici.
#
# Le meme `uuid` relie 130 et 128 (69 trains sur 69 arrives). Les deux 130
# sans 128 : un train encore en route, un dont l'arrivee a expire du cache.
#
# Verifie sur les 71 trains :
#   * Le conducteur = `ownerId` = `uid` = l'unique passager du wagon 0
#     (`seatNum` 1) = l'utilisateur du titre. 71/71.
#   * `vipInfo` = le VIP **ou** le Guardian : jamais un passager, jamais le
#     conducteur. `vipType` tranche, prouve par l'escouade de defense
#     (`formation`), dont les `heroUuid` sont propres a un joueur. Sous
#     l'hypothese "vipType 1 = la formation est celle de `vipInfo`" -- un
#     Guardian defend le train avec SES heros --, 43 paires de trains
#     partageant des heros sur 43 designent le meme joueur ; sous l'hypothese
#     inverse, 3 sur 43. Avec `vipType` 0, la formation est celle du
#     conducteur : `vipInfo` est un VIP, simple beneficiaire. `vipInfo`
#     absent : ni l'un ni l'autre (2 trains).
#   * `history[].isWin` est du point de vue du TRAIN : `robTimes` = nombre
#     d'attaques a isWin = false (69/69), et `completeness` = 1 - 0,1 x
#     `robTimes` (69/69 ; deux pillages au plus).
#   * `uuid` depasse 2^53 : garde en TEXTE, comme `uid`.
#
# NON etabli : `selfSelect` (false 3 fois sur 71). Hypothese : conducteur
# designe par un R4/R5 (true) contre tire au sort par le jeu (false). Transmis
# brut, sous son nom, sans sens qui le ferait passer pour une mesure.

MAIL_TRAIN_PURCHASED = 127
MAIL_TRAIN_DEPARTED = 130
MAIL_TRAIN_ARRIVED = 128
#: Les seuls types qui portent l'objet train.
TRAIN_MAILS = (MAIL_TRAIN_DEPARTED, MAIL_TRAIN_ARRIVED)

#: `vipInfo.vipType` -> role de l'escorte. Une valeur absente de la table
#: laisse le role inconnu : deviner serait pire.
_ESCORT_ROLES = {0: "vip", 1: "guardian"}

#: L'heure du serveur de Last War est UTC-2 : la journee de jeu -- celle du
#: train quotidien -- change a 02:00 UTC. Coherent avec les trois achats
#: observes : un train parti a 01:14 UTC suit l'annonce d'achat faite apres le
#: train quotidien de 21:04 UTC la veille ; en jour UTC, les deux tomberaient
#: deux jours differents et le lendemain aurait l'air d'en avoir deux.
_GAME_DAY_SHIFT_MS = 2 * 3_600_000


def game_date(ms: int) -> str:
    """Instant UTC (ms) -> jour du jeu, AAAA-MM-JJ (heure du serveur, UTC-2)."""
    when = datetime.fromtimestamp((ms - _GAME_DAY_SHIFT_MS) / 1000, tz=timezone.utc)
    return f"{when:%Y-%m-%d}"


@dataclass(frozen=True)
class TrainPerson:
    uid: str | None
    name: str | None
    alliance_abbr: str | None


@dataclass(frozen=True)
class TrainRun:
    train_id: str
    mail_uid: str
    #: Emission du mail : le depart (130) ou l'arrivee (128).
    created_ms: int
    arrived: bool
    departed_ms: int | None
    #: Arrivee reelle si `arrived`, prevue sinon.
    arrival_ms: int | None
    conductor: TrainPerson
    #: Le VIP ou le Guardian, selon `escort_role`. None : aucun designe.
    escort: TrainPerson | None
    escort_role: str | None          # "vip" | "guardian" | None (inconnu)
    purchased: bool | None
    self_select: bool | None
    alliance_id: str | None
    alliance_abbr: str | None
    alliance_name: str | None
    server_id: int | None
    #: Hors conducteur, qui occupe seul le wagon 0.
    passengers: int = 0
    # Arrivee seulement : pillages, cargaison restante, attaques subies.
    rob_times: int | None = None
    completeness: float | None = None
    attacks: int | None = None
    attacks_repelled: int | None = None


def _person(entry: Any) -> TrainPerson | None:
    if not isinstance(entry, dict) or not _as_id(entry.get("uid")):
        return None
    return TrainPerson(uid=_as_id(entry.get("uid")), name=_as_str(entry.get("name")),
                       alliance_abbr=_as_str(entry.get("abbr")))


def _as_float(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def parse_train_mail(mail_type: int, mail_uid: str, created_ms: int,
                     contents: Any) -> TrainRun | None:
    """Train d'alliance, ou None si ce mail n'en porte pas un lisible."""
    if mail_type not in TRAIN_MAILS or not isinstance(contents, dict):
        return None
    obj = contents.get("obj")
    if not isinstance(obj, dict):
        return None
    arrived = mail_type == MAIL_TRAIN_ARRIVED
    train = obj.get("trainObj") if arrived else obj
    if not isinstance(train, dict):
        return None
    train_id = _as_id(train.get("uuid"))
    owner = _as_id(train.get("ownerId")) or _as_id(train.get("uid"))
    if not train_id or not owner:
        return None
    march = train.get("marchInfo") if isinstance(train.get("marchInfo"), dict) else {}
    seated = [p for c in _dicts(march.get("carriageList"))
              for p in _dicts(c.get("passengerList"))]
    # Le nom du conducteur : sur l'objet train, sinon sur son siege.
    seat = next((p for p in seated if _as_id(p.get("uid")) == owner), {})
    conductor = TrainPerson(
        uid=owner, name=_as_str(train.get("name")) or _as_str(seat.get("name")),
        alliance_abbr=_as_str(train.get("abbr")) or _as_str(seat.get("abbr")))
    vip = train.get("vipInfo")
    escort = _person(vip)
    buy = _as_int(train.get("buyFlag"))
    select = train.get("selfSelect")
    run = TrainRun(
        train_id=train_id, mail_uid=mail_uid, created_ms=created_ms, arrived=arrived,
        departed_ms=_as_int(train.get("sendTime")),
        arrival_ms=_as_int(train.get("arriveTime")),
        conductor=conductor, escort=escort,
        escort_role=_ESCORT_ROLES.get(_as_int(vip.get("vipType"))) if escort else None,
        purchased=None if buy is None else buy == 1,
        self_select=select if isinstance(select, bool) else None,
        alliance_id=_as_id(train.get("allianceId")),
        alliance_abbr=_as_str(train.get("abbr")),
        alliance_name=_as_str(train.get("alliancename")),
        server_id=_as_int(train.get("serverId")),
        passengers=sum(1 for p in seated if _as_id(p.get("uid")) != owner),
    )
    if not arrived:
        return run
    history = _dicts(obj.get("history"))
    return replace(run,
                   rob_times=_as_int(march.get("robTimes")),
                   completeness=_as_float(train.get("completeness")),
                   attacks=len(history),
                   attacks_repelled=sum(1 for h in history if h.get("isWin") is True))
