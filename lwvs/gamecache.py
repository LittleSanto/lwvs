"""Le cache local du client : seul module a connaitre sa base.

Pourquoi une deuxieme source que le reseau : les rapports de Desert Storm et de
Canyon Storm arrivent par mail, et un mail ne passe sur le fil qu'UNE fois, a
sa reception -- une synchronisation incrementale (`firstSeqId`). Rouvrir le
mail ensuite ne transmet rien : trois captures du 28/09/2026 (ouverture des
mails, connexion complete, tous ports TCP confondus) n'ont pas contenu un seul
des chiffres affiches. Le client relit sa propre base.

Cette base est un SQLite, `config.db`, avec une table `MailData`. Les colonnes
`title`, `contents` et `custom` y sont du JSON en texte. Elle garde des mois
d'historique (Desert Storm depuis septembre 2025 sur le poste de reference).

Regles :

* **Lecture seule, toujours.** `mode=ro` dans l'URI : ce fichier appartient au
  jeu, et une ecriture accidentelle pourrait corrompre son etat.
* Ce module est le SEUL a connaitre la forme de cette base -- nom de table,
  colonnes, JSON en texte. Une mise a jour du client doit casser ce fichier et
  rien d'autre, comme `wire.py` pour le fil. `store.py` reste le seul a parler
  a NOTRE base : les deux ne se melangent pas.
* Aucun contenu n'est logue. Les erreurs citent un chemin et un nom de table.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

__all__ = ["CachedMail", "CacheUnavailable", "default_db_path", "read_mails"]

#: Le client a change d'editeur de repertoire en cours de route : `FirstFun`
#: est un reliquat (derniere ecriture en 2025 sur le poste de reference),
#: `FunFly` le courant. Les deux coexistent, d'ou le choix au plus recent.
_PUBLISHERS = ("FunFly", "FirstFun")
_GAME_DIR = "Last War-Survival Game"
_DB_NAME = "config.db"
_TABLE = "MailData"


class CacheUnavailable(RuntimeError):
    """La base du client est absente, verrouillee, ou n'a plus la forme connue."""


@dataclass(frozen=True)
class CachedMail:
    uid: str
    mail_type: int
    #: Millisecondes epoch UTC : l'instant ou le serveur a emis le mail, soit
    #: la fin de la bataille pour un rapport.
    created_ms: int
    to_user: str | None
    title: Any        # JSON decode, ou None s'il ne l'est pas
    contents: Any
    custom: Any


def default_db_path() -> Path | None:
    """La base du client sur ce poste, ou None s'il n'y en a pas."""
    locallow = Path.home() / "AppData" / "LocalLow"
    found = [p for p in (locallow / pub / _GAME_DIR / _DB_NAME for pub in _PUBLISHERS)
             if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _json(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def read_mails(path: str | Path, types: Sequence[int],
               since_ms: int | None = None) -> list[CachedMail]:
    """Les mails des types demandes, du plus ancien au plus recent."""
    db = Path(path)
    if not db.is_file():
        raise CacheUnavailable(
            f"game database not found: {db}. Has the game been run on this PC,"
            " with this Windows account?")
    # URI + mode=ro : sqlite refuse alors toute ecriture, y compris la creation
    # d'un journal. `as_uri()` echappe les espaces du chemin.
    uri = db.resolve().as_uri() + "?mode=ro"
    marks = ",".join("?" for _ in types)
    sql = (f"SELECT uid, type, createTime, toUser, title, contents, custom"
           f" FROM {_TABLE} WHERE type IN ({marks})")
    params: list[Any] = list(types)
    if since_ms is not None:
        sql += " AND createTime >= ?"
        params.append(since_ms)
    sql += " ORDER BY createTime, uid"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
            known = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if _TABLE not in known:
                raise CacheUnavailable(
                    f"{db} has no {_TABLE} table: a game update changed the"
                    " format of its mailbox.")
            rows = con.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CacheUnavailable(
            f"cannot read {db} ({type(exc).__name__}: {exc}). If the game is"
            " writing to it, retry in a few seconds.") from None

    return [
        CachedMail(uid=str(uid), mail_type=int(mtype), created_ms=int(created or 0),
                   to_user=str(to_user) if to_user else None,
                   title=_json(title), contents=_json(contents), custom=_json(custom))
        for uid, mtype, created, to_user, title, contents, custom in rows
    ]
