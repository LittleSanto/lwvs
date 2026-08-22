"""Stockage SQLite.

SEUL MODULE QUI PARLE A LA BASE. Aucun SQL ailleurs.

Elle sert a ACCUMULER : un duel dure plusieurs jours et se capture en
plusieurs sessions, donc chaque capture ajoute un snapshot et n'ecrase jamais
les precedents. C'est ce qui rend l'export historisable plutot que limite a la
derniere session.

Ingestion IDEMPOTENTE : le meme classement est retransmis plusieurs fois dans
une capture (4x observe). La cle est (snapshot, scope, day, uid) et l'ecriture
est un REPLACE -- surtout pas un cumul, qui rapporterait 4x le vrai score.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .messages import (CampRankMessage, GroupMessage, MembersMessage,
                       RankMessage, SeasonMessage, ServerRankMessage)

__all__ = ["Store", "IngestCounters", "SnapshotInfo", "utc_now"]

DEFAULT_DB = "lwvs.sqlite3"

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    source       TEXT NOT NULL,
    note         TEXT,
    frames       INTEGER NOT NULL DEFAULT 0,
    payloads     INTEGER NOT NULL DEFAULT 0,
    decoded      INTEGER NOT NULL DEFAULT 0,
    exact        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rank_rows (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    scope          TEXT    NOT NULL,
    day            INTEGER,
    raw_type       INTEGER,
    rank           INTEGER NOT NULL,
    uid            TEXT    NOT NULL,
    name           TEXT,
    score          INTEGER,
    server_id      INTEGER,
    alliance_id    TEXT,
    alliance_name  TEXT,
    alliance_abbr  TEXT
);

-- Idempotence : (snapshot, scope, day, uid). `day` est NULL pour le cumul et
-- NULL n'entre pas en conflit dans un index unique SQLite, d'ou le COALESCE.
CREATE UNIQUE INDEX IF NOT EXISTS ux_rank_rows
    ON rank_rows (snapshot_id, scope, COALESCE(day, -1), uid);
CREATE INDEX IF NOT EXISTS ix_rank_uid ON rank_rows (uid);
CREATE INDEX IF NOT EXISTS ix_rank_abbr ON rank_rows (alliance_abbr);

CREATE TABLE IF NOT EXISTS group_rows (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    group_code     TEXT,
    position       INTEGER,
    alliance_id    TEXT,
    alliance_name  TEXT,
    alliance_abbr  TEXT,
    server_id      INTEGER,
    round_result   INTEGER,
    rank_type      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_group_rows
    ON group_rows (snapshot_id, COALESCE(group_code, ''), COALESCE(position, -1),
                   COALESCE(alliance_id, ''));

CREATE TABLE IF NOT EXISTS standings (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    scope          TEXT NOT NULL,
    group_code     TEXT,
    position       INTEGER,
    rank_type      INTEGER,
    round_result   INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_standings
    ON standings (snapshot_id, scope);

CREATE TABLE IF NOT EXISTS member_rows (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    alliance_id     TEXT,
    uid             TEXT    NOT NULL,
    name            TEXT,
    army_kill       INTEGER,
    power           INTEGER,
    alliance_rank   INTEGER,
    today_progress  INTEGER,   -- points de don du jour
    weekly_progress INTEGER,   -- points de don de la semaine
    main_city_lv    INTEGER,
    server_id       INTEGER,
    cur_server_id   INTEGER,
    online          INTEGER,
    join_time       INTEGER,
    donate_time        INTEGER,
    weekly_donate_time INTEGER
);
-- Idempotence : un membre par snapshot. `al.rank` peut etre retransmis.
CREATE UNIQUE INDEX IF NOT EXISTS ux_member_rows
    ON member_rows (snapshot_id, uid);
CREATE INDEX IF NOT EXISTS ix_member_uid ON member_rows (uid);

CREATE TABLE IF NOT EXISTS camp_rank_rows (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    rank           INTEGER,
    uid            TEXT    NOT NULL,
    name           TEXT,
    score          INTEGER,
    server_id      INTEGER,
    alliance_abbr  TEXT,
    country        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_camp_rank_rows
    ON camp_rank_rows (snapshot_id, uid);

-- Classement du serveur (`rank.get`) : le SEUL porteur du THP (`hero_power`),
-- et le seul classement qui couvre des joueurs hors de ton alliance.
--
-- `raw_type` est l'ONGLET du classement, transporte sans interpretation. Il
-- entre dans la cle d'unicite : deux onglets peuvent citer le meme joueur avec
-- deux metriques differentes, et les confondre en ecraserait une.
CREATE TABLE IF NOT EXISTS server_rank_rows (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    raw_type       INTEGER,
    rank           INTEGER,
    uid            TEXT    NOT NULL,
    name           TEXT,
    hero_power     INTEGER,   -- le THP, tel qu'affiche sur la fiche de profil
    level          INTEGER,
    country        TEXT,
    alliance_id    TEXT,      -- ABSENT pour un joueur sans alliance
    alliance_abbr  TEXT,
    alliance_name  TEXT,
    src_server     INTEGER,
    server_id      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_server_rank_rows
    ON server_rank_rows (snapshot_id, COALESCE(raw_type, -1), uid);

-- Index du protocole : quelles commandes ont ete vues, combien de fois.
-- Aucun contenu de payload n'est stocke.
CREATE TABLE IF NOT EXISTS commands_seen (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    command     TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, command)
);
"""

SCHEMA_VERSION = "2"

#: Colonnes ajoutees apres coup. `CREATE TABLE IF NOT EXISTS` ne touche pas une
#: table qui existe deja : sans ce rattrapage, une base creee par une version
#: anterieure resterait sans les colonnes et toutes les ecritures echoueraient.
#: Ajout SEULEMENT -- on ne renomme ni ne supprime, la base accumule un
#: historique qu'on ne sait pas recapturer.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("member_rows", "donate_time", "INTEGER"),
    ("member_rows", "weekly_donate_time", "INTEGER"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class IngestCounters:
    rank_messages: int = 0
    rank_rows: int = 0
    group_messages: int = 0
    group_rows: int = 0
    season_messages: int = 0
    standing_rows: int = 0
    member_messages: int = 0
    member_rows: int = 0
    camp_messages: int = 0
    camp_rows: int = 0
    server_rank_messages: int = 0
    server_rank_rows: int = 0
    commands: dict[str, int] = field(default_factory=dict)

    def note_command(self, command: str) -> None:
        self.commands[command] = self.commands.get(command, 0) + 1


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    captured_at: str
    source: str
    note: str | None
    frames: int
    payloads: int
    decoded: int
    exact: int


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._add_missing_columns()
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        for table, column, decl in _ADDED_COLUMNS:
            present = {r["name"] for r in
                       self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in present:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- cycle de vie -----------------------------------------------------
    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- snapshots --------------------------------------------------------
    def create_snapshot(self, source: str, note: str | None = None,
                        captured_at: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots(captured_at, source, note) VALUES (?,?,?)",
            (captured_at or utc_now(), source, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_snapshot(self, snapshot_id: int, *, frames: int, payloads: int,
                        decoded: int, exact: int) -> None:
        self.conn.execute(
            "UPDATE snapshots SET frames=?, payloads=?, decoded=?, exact=? WHERE id=?",
            (frames, payloads, decoded, exact, snapshot_id),
        )
        self.conn.commit()

    def drop_snapshot_if_empty(self, snapshot_id: int) -> bool:
        rows = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM rank_rows WHERE snapshot_id=:s)"
            "     + (SELECT COUNT(*) FROM group_rows WHERE snapshot_id=:s)"
            "     + (SELECT COUNT(*) FROM standings WHERE snapshot_id=:s)"
            "     + (SELECT COUNT(*) FROM member_rows WHERE snapshot_id=:s)"
            "     + (SELECT COUNT(*) FROM camp_rank_rows WHERE snapshot_id=:s)"
            "     + (SELECT COUNT(*) FROM server_rank_rows WHERE snapshot_id=:s) AS n",
            {"s": snapshot_id},
        ).fetchone()
        if rows and rows["n"] == 0:
            self.conn.execute("DELETE FROM commands_seen WHERE snapshot_id=?", (snapshot_id,))
            self.conn.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
            self.conn.commit()
            return True
        return False

    def count_rows(self, snapshot_id: int) -> dict[str, int]:
        """Lignes reellement STOCKEES pour ce snapshot, apres idempotence."""
        out: dict[str, int] = {}
        for name, table in (("players", "rank_rows"), ("group", "group_rows"),
                            ("standing", "standings"), ("members", "member_rows"),
                            ("camp", "camp_rank_rows"),
                            ("server", "server_rank_rows")):
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            out[name] = int(row["n"])
        return out

    def snapshots(self) -> list[SnapshotInfo]:
        rows = self.conn.execute(
            "SELECT id, captured_at, source, note, frames, payloads, decoded, exact "
            "FROM snapshots ORDER BY id"
        ).fetchall()
        return [SnapshotInfo(**dict(r)) for r in rows]

    def commands_seen(self, snapshot_id: int | None = None) -> list[tuple[str, int]]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT command, SUM(count) AS n FROM commands_seen "
                "GROUP BY command ORDER BY n DESC, command"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT command, count AS n FROM commands_seen WHERE snapshot_id=? "
                "ORDER BY n DESC, command",
                (snapshot_id,),
            ).fetchall()
        return [(r["command"], r["n"]) for r in rows]

    # -- ecriture ---------------------------------------------------------
    def record_command(self, snapshot_id: int, command: str, count: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO commands_seen(snapshot_id, command, count) VALUES (?,?,?) "
            "ON CONFLICT(snapshot_id, command) DO UPDATE SET count = count + excluded.count",
            (snapshot_id, command, count),
        )

    def add_rank_message(self, snapshot_id: int, msg: RankMessage) -> int:
        # REPLACE, jamais un cumul : le meme classement est retransmis plusieurs
        # fois: additionner rapporterait 4x le vrai score.
        rows = [
            (snapshot_id, msg.scope, msg.day, msg.raw_type, r.rank, r.uid, r.name,
             r.score, r.server_id, r.alliance_id, r.alliance_name, r.alliance_abbr)
            for r in msg.rows
            if r.uid
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO rank_rows"
            "(snapshot_id, scope, day, raw_type, rank, uid, name, score, server_id,"
            " alliance_id, alliance_name, alliance_abbr)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def add_group_message(self, snapshot_id: int, msg: GroupMessage) -> int:
        rows = [
            (snapshot_id, r.group_code, r.position, r.alliance_id, r.alliance_name,
             r.alliance_abbr, r.server_id, r.round_result, r.rank_type)
            for r in msg.rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO group_rows"
            "(snapshot_id, group_code, position, alliance_id, alliance_name,"
            " alliance_abbr, server_id, round_result, rank_type)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def add_season_message(self, snapshot_id: int, msg: SeasonMessage) -> int:
        rows = [
            (snapshot_id, r.scope, r.group_code, r.position, r.rank_type, r.round_result)
            for r in msg.rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO standings"
            "(snapshot_id, scope, group_code, position, rank_type, round_result)"
            " VALUES (?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def add_members_message(self, snapshot_id: int, msg: MembersMessage) -> int:
        rows = [
            (snapshot_id, msg.alliance_id, r.uid, r.name, r.army_kill, r.power,
             r.alliance_rank, r.today_progress, r.weekly_progress, r.main_city_lv,
             r.server_id, r.cur_server_id,
             None if r.online is None else int(r.online), r.join_time,
             r.donate_time, r.weekly_donate_time)
            for r in msg.rows
            if r.uid
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO member_rows"
            "(snapshot_id, alliance_id, uid, name, army_kill, power, alliance_rank,"
            " today_progress, weekly_progress, main_city_lv, server_id, cur_server_id,"
            " online, join_time, donate_time, weekly_donate_time)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def add_camp_rank_message(self, snapshot_id: int, msg: CampRankMessage) -> int:
        rows = [
            (snapshot_id, r.rank, r.uid, r.name, r.score, r.server_id,
             r.alliance_abbr, r.country)
            for r in msg.rows
            if r.uid
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO camp_rank_rows"
            "(snapshot_id, rank, uid, name, score, server_id, alliance_abbr, country)"
            " VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def fetch_camp_rank(self, abbrs: Sequence[str] | None = None,
                        snapshot: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, c.snapshot_id AS snapshot_id,"
            " c.rank, c.uid, c.name, c.score, c.server_id, c.alliance_abbr, c.country"
            " FROM camp_rank_rows c JOIN snapshots s ON s.id = c.snapshot_id"
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if snapshot is not None:
            where.append("c.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if abbrs:
            keys = [f":ab{i}" for i in range(len(abbrs))]
            where.append(f"c.alliance_abbr IN ({', '.join(keys)}) COLLATE NOCASE")
            params.update({f"ab{i}": v for i, v in enumerate(abbrs)})
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.snapshot_id, c.rank"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def add_server_rank_message(self, snapshot_id: int, msg: ServerRankMessage) -> int:
        rows = [
            (snapshot_id, msg.raw_type, r.rank, r.uid, r.name, r.hero_power,
             r.level, r.country, r.alliance_id, r.alliance_abbr, r.alliance_name,
             r.src_server, msg.server_id)
            for r in msg.rows
            if r.uid
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO server_rank_rows"
            "(snapshot_id, raw_type, rank, uid, name, hero_power, level, country,"
            " alliance_id, alliance_abbr, alliance_name, src_server, server_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def fetch_server_rank(self, alliance_ids: Sequence[str] | None = None,
                          snapshot: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, v.snapshot_id AS snapshot_id,"
            " v.raw_type, v.rank, v.uid, v.name, v.hero_power, v.level, v.country,"
            " v.alliance_id, v.alliance_abbr, v.alliance_name, v.src_server,"
            " v.server_id"
            " FROM server_rank_rows v JOIN snapshots s ON s.id = v.snapshot_id"
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if snapshot is not None:
            where.append("v.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if alliance_ids:
            keys = [f":aid{i}" for i in range(len(alliance_ids))]
            where.append(f"v.alliance_id IN ({', '.join(keys)})")
            params.update({f"aid{i}": v for i, v in enumerate(alliance_ids)})
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.snapshot_id, v.raw_type, v.rank"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- resolution de TON alliance --------------------------------------
    def own_alliance_ids(self, snapshot: int | None = None) -> list[str]:
        """Ton alliance = celle du groupe dont la `position` egale celle de
        `duelInfo` (regle CONFIRMEE du protocole).

        On resout a chaque export plutot que de figer une abreviation : `abbr`
        et le nom peuvent changer, la regle non.
        """
        sql = (
            "SELECT DISTINCT g.alliance_id FROM standings t"
            " JOIN group_rows g ON g.snapshot_id = t.snapshot_id"
            "                  AND g.position = t.position"
            " WHERE t.scope = 'current' AND g.alliance_id IS NOT NULL"
        )
        params: dict[str, Any] = {}
        if snapshot is not None:
            sql += " AND t.snapshot_id = :snapshot"
            params["snapshot"] = snapshot
        return [r["alliance_id"] for r in self.conn.execute(sql, params).fetchall()]

    def roster_alliance_ids(self, snapshot: int | None = None) -> list[str]:
        """Ton alliance d'apres `al.rank`, qui porte `allianceId`.

        `al.rank` EST le roster de ton alliance : son `allianceId` t'identifie
        directement, sans avoir besoin de l'ecran Duel. Verifie egal a la regle
        season+group sur donnees reelles.
        """
        sql = ("SELECT DISTINCT alliance_id FROM member_rows"
               " WHERE alliance_id IS NOT NULL")
        params: dict[str, Any] = {}
        if snapshot is not None:
            sql += " AND snapshot_id = :s"
            params["s"] = snapshot
        return [r["alliance_id"] for r in self.conn.execute(sql, params).fetchall()]

    def alliance_ids_present(self, snapshot: int | None = None) -> set[str]:
        """Tous les identifiants d'alliance vus dans cette capture.

        Sert de garde-fou a la memoire : une alliance memorisee qui n'apparait
        nulle part dans la capture est perimee, et l'utiliser rendrait un export
        vide sans rien dire.
        """
        out: set[str] = set()
        for table in ("rank_rows", "member_rows", "group_rows", "server_rank_rows"):
            sql = f"SELECT DISTINCT alliance_id FROM {table} WHERE alliance_id IS NOT NULL"
            params: dict[str, Any] = {}
            if snapshot is not None:
                sql += " AND snapshot_id = :s"
                params["s"] = snapshot
            out.update(r["alliance_id"] for r in self.conn.execute(sql, params).fetchall())
        return out

    def alliance_abbrs_present(self, snapshot: int | None = None) -> set[str]:
        """Abreviations vues dans cette capture, en minuscules.

        Certains messages -- `lw.camp.battle.user.score.rank` par exemple --
        n'identifient l'alliance QUE par son abreviation. Un garde-fou qui ne
        regarderait que les identifiants declarerait une memoire perimee alors
        que l'alliance est bel et bien dans les donnees.
        """
        out: set[str] = set()
        for table in ("rank_rows", "group_rows", "camp_rank_rows",
                      "server_rank_rows"):
            sql = (f"SELECT DISTINCT alliance_abbr FROM {table}"
                   " WHERE alliance_abbr IS NOT NULL")
            params: dict[str, Any] = {}
            if snapshot is not None:
                sql += " AND snapshot_id = :s"
                params["s"] = snapshot
            out.update(r["alliance_abbr"].lower()
                       for r in self.conn.execute(sql, params).fetchall())
        return out

    def describe_alliance(self, alliance_id: str,
                          snapshot: int | None = None) -> dict[str, Any]:
        """Libelles connus pour cet identifiant, dans la capture courante."""
        for table, cols in (("group_rows", ("alliance_name", "alliance_abbr",
                                            "server_id", "position")),
                            ("rank_rows", ("alliance_name", "alliance_abbr")),
                            ("server_rank_rows", ("alliance_name", "alliance_abbr",
                                                  "server_id"))):
            sql = (f"SELECT {', '.join(cols)} FROM {table} WHERE alliance_id = :aid")
            params: dict[str, Any] = {"aid": alliance_id}
            if snapshot is not None:
                sql += " AND snapshot_id = :s"
                params["s"] = snapshot
            row = self.conn.execute(sql + " LIMIT 1", params).fetchone()
            if row:
                return {c: row[c] for c in cols if row[c] is not None}
        return {}

    def own_alliance_labels(self, snapshot: int | None = None) -> list[tuple[str, str, str]]:
        """(id, nom, abbr) de ton alliance, pour l'afficher a l'utilisateur."""
        sql = (
            "SELECT DISTINCT g.alliance_id, g.alliance_name, g.alliance_abbr"
            " FROM standings t JOIN group_rows g ON g.snapshot_id = t.snapshot_id"
            "                              AND g.position = t.position"
            " WHERE t.scope = 'current' AND g.alliance_id IS NOT NULL"
        )
        params: dict[str, Any] = {}
        if snapshot is not None:
            sql += " AND t.snapshot_id = :snapshot"
            params["snapshot"] = snapshot
        return [(r["alliance_id"], r["alliance_name"] or "", r["alliance_abbr"] or "")
                for r in self.conn.execute(sql, params).fetchall()]

    def duel_context(self, snapshot: int | None = None,
                     own_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Qui affronte qui, ce duel-ci.

        L'adversaire se DEDUIT : le classement VS porte les deux alliances du
        match, donc c'est celle dont l'identifiant n'est pas le tien. On ne
        conclut que si le classement en contient exactement deux -- trois
        voudrait dire qu'on a melange deux duels, et deviner serait pire que
        se taire.

        Les cles absentes sont omises, jamais devinees.
        """
        ctx: dict[str, Any] = {}

        def described(alliance_id: str) -> dict[str, Any]:
            row = self.conn.execute(
                "SELECT alliance_name, alliance_abbr, server_id, position, group_code"
                " FROM group_rows WHERE alliance_id = :aid"
                + (" AND snapshot_id = :s" if snapshot is not None else "")
                + " LIMIT 1",
                {"aid": alliance_id, "s": snapshot},
            ).fetchone()
            out: dict[str, Any] = {"id": alliance_id}
            if row:
                out.update({k: row[k] for k in
                            ("alliance_name", "alliance_abbr", "server_id", "position")
                            if row[k] is not None})
            else:
                # Pas dans le groupe capture : on prend ce que le classement sait.
                rank = self.conn.execute(
                    "SELECT alliance_name, alliance_abbr FROM rank_rows"
                    " WHERE alliance_id = :aid"
                    + (" AND snapshot_id = :s" if snapshot is not None else "")
                    + " LIMIT 1",
                    {"aid": alliance_id, "s": snapshot},
                ).fetchone()
                if rank:
                    out.update({k: rank[k] for k in ("alliance_name", "alliance_abbr")
                                if rank[k] is not None})
                else:
                    # Ni groupe ni classement VS : une capture peut ne contenir
                    # que le classement du serveur, qui nomme lui aussi les
                    # alliances. Sans ce recours, le contexte d'un document THP
                    # se reduirait a un identifiant nu.
                    srv = self.conn.execute(
                        "SELECT alliance_name, alliance_abbr, server_id"
                        " FROM server_rank_rows WHERE alliance_id = :aid"
                        + (" AND snapshot_id = :s" if snapshot is not None else "")
                        + " LIMIT 1",
                        {"aid": alliance_id, "s": snapshot},
                    ).fetchone()
                    if srv:
                        out.update({k: srv[k] for k in
                                    ("alliance_name", "alliance_abbr", "server_id")
                                    if srv[k] is not None})
            return out

        mine = list(own_ids) if own_ids else self.own_alliance_ids(snapshot)
        if len(mine) == 1:
            ctx["alliance"] = described(mine[0])

        sql = ("SELECT DISTINCT alliance_id FROM rank_rows"
               " WHERE alliance_id IS NOT NULL")
        params: dict[str, Any] = {}
        if snapshot is not None:
            sql += " AND snapshot_id = :s"
            params["s"] = snapshot
        seen = [r["alliance_id"] for r in self.conn.execute(sql, params).fetchall()]
        if len(seen) == 2 and len(mine) == 1 and mine[0] in seen:
            other = next(a for a in seen if a != mine[0])
            ctx["opponent"] = described(other)

        row = self.conn.execute(
            "SELECT group_code FROM standings WHERE scope='current'"
            + (" AND snapshot_id = :s" if snapshot is not None else "")
            + " AND group_code IS NOT NULL LIMIT 1",
            {"s": snapshot},
        ).fetchone()
        if row:
            ctx["group_code"] = row["group_code"]
        return ctx

    def fetch_members(
        self, alliance_ids: Sequence[str] | None = None, snapshot: int | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, m.snapshot_id AS snapshot_id,"
            " m.alliance_id, m.uid, m.name, m.army_kill, m.power, m.alliance_rank,"
            " m.today_progress, m.weekly_progress, m.main_city_lv, m.server_id,"
            " m.cur_server_id, m.online, m.join_time, m.donate_time,"
            " m.weekly_donate_time"
            " FROM member_rows m JOIN snapshots s ON s.id = m.snapshot_id"
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if snapshot is not None:
            where.append("m.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if alliance_ids:
            keys = [f":aid{i}" for i in range(len(alliance_ids))]
            where.append(f"m.alliance_id IN ({', '.join(keys)})")
            params.update({f"aid{i}": v for i, v in enumerate(alliance_ids)})
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.snapshot_id, m.army_kill DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def commit(self) -> None:
        self.conn.commit()

    # -- lecture (export) -------------------------------------------------
    def fetch_players(
        self,
        scope: str | None = None,
        day: int | None = None,
        alliance: str | None = None,
        snapshot: int | None = None,
        alliance_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, r.snapshot_id AS snapshot_id,"
            " r.scope, r.day, r.raw_type, r.rank, r.uid, r.name, r.score,"
            " r.server_id, r.alliance_id, r.alliance_name, r.alliance_abbr"
            " FROM rank_rows r JOIN snapshots s ON s.id = r.snapshot_id"
        )
        where, params = self._filters(scope, day, alliance, snapshot, table="r")
        if alliance_ids:
            keys = [f":aid{i}" for i in range(len(alliance_ids))]
            where.append(f"r.alliance_id IN ({', '.join(keys)})")
            params.update({f"aid{i}": v for i, v in enumerate(alliance_ids)})
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.snapshot_id, r.scope, COALESCE(r.day, -1), r.rank"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def fetch_group(
        self, alliance: str | None = None, snapshot: int | None = None,
        alliance_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, g.snapshot_id AS snapshot_id,"
            " g.group_code, g.position, g.alliance_id, g.alliance_name,"
            " g.alliance_abbr, g.server_id, g.round_result, g.rank_type"
            " FROM group_rows g JOIN snapshots s ON s.id = g.snapshot_id"
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if snapshot is not None:
            where.append("g.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if alliance:
            where.append(
                "(g.alliance_abbr LIKE :alliance COLLATE NOCASE"
                " OR g.alliance_name LIKE :alliance COLLATE NOCASE"
                " OR g.alliance_id = :alliance_exact)"
            )
            params["alliance"] = f"%{alliance}%"
            params["alliance_exact"] = alliance
        if alliance_ids:
            keys = [f":aid{i}" for i in range(len(alliance_ids))]
            where.append(f"g.alliance_id IN ({', '.join(keys)})")
            params.update({f"aid{i}": v for i, v in enumerate(alliance_ids)})
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY g.snapshot_id, g.position"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def fetch_standing(
        self, scope: str | None = None, snapshot: int | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.captured_at AS captured_at, t.snapshot_id AS snapshot_id,"
            " t.scope, t.group_code, t.position, t.rank_type, t.round_result"
            " FROM standings t JOIN snapshots s ON s.id = t.snapshot_id"
        )
        where: list[str] = []
        params: dict[str, Any] = {}
        if snapshot is not None:
            where.append("t.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if scope:
            where.append("t.scope = :scope")
            params["scope"] = scope
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.snapshot_id, t.scope"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    @staticmethod
    def _filters(
        scope: str | None, day: int | None, alliance: str | None,
        snapshot: int | None, table: str,
    ) -> tuple[list[str], dict[str, Any]]:
        where: list[str] = []
        params: dict[str, Any] = {}
        if scope:
            where.append(f"{table}.scope = :scope")
            params["scope"] = scope
        if day is not None:
            where.append(f"{table}.day = :day")
            params["day"] = day
        if snapshot is not None:
            where.append(f"{table}.snapshot_id = :snapshot")
            params["snapshot"] = snapshot
        if alliance:
            where.append(
                f"({table}.alliance_abbr LIKE :alliance COLLATE NOCASE"
                f" OR {table}.alliance_name LIKE :alliance COLLATE NOCASE"
                f" OR {table}.alliance_id = :alliance_exact)"
            )
            params["alliance"] = f"%{alliance}%"
            params["alliance_exact"] = alliance
        return where, params

    # -- resume -----------------------------------------------------------
    def summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.snapshot_id, s.captured_at, r.scope, r.day, r.raw_type,"
            " COUNT(*) AS players, COUNT(DISTINCT r.alliance_abbr) AS alliances"
            " FROM rank_rows r JOIN snapshots s ON s.id = r.snapshot_id"
            " GROUP BY r.snapshot_id, r.scope, r.day"
            " ORDER BY r.snapshot_id, r.scope, COALESCE(r.day, -1)"
        ).fetchall()
        return [dict(r) for r in rows]
