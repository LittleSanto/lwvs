"""Domaine : enveloppe et messages VS.

Ce module ne voit que des objets Python deja decodes par `wire`. Il ne connait
aucun octet.

Regle de vie privee : les champs non mappes sont JETES ici. Seuls leur nom et
leur type peuvent remonter, via `inspection`.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    "parse_message",
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
VS_COMMANDS = (CMD_RANK, CMD_GROUP, CMD_SEASON, CMD_MEMBERS, CMD_CAMP_RANK,
               CMD_SERVER_RANK)


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

    @property
    def abbrs(self) -> set[str]:
        """Les deux alliances du match arrivent dans le meme message."""
        return {r.alliance_abbr for r in self.rows if r.alliance_abbr}


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


_PARSERS = {
    CMD_RANK: parse_rank,
    CMD_GROUP: parse_group,
    CMD_SEASON: parse_season,
    CMD_MEMBERS: parse_members,
    CMD_CAMP_RANK: parse_camp_rank,
    CMD_SERVER_RANK: parse_server_rank,
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


def own_position(season: SeasonMessage | None) -> int | None:
    """Ton alliance = celle du groupe dont la `position` egale celle de duelInfo."""
    if season is None:
        return None
    for row in season.rows:
        if row.scope == "current":
            return row.position
    return None
