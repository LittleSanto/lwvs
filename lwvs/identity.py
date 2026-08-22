"""Memoire de TON alliance, d'une session a l'autre.

Pourquoi : la regle du protocole (position du groupe == position de `duelInfo`)
exige `get.alliance.duel.season.info` ET `get.alliance.duel.group.info` dans la
MEME capture. Ces deux messages n'arrivent que si l'ecran Duel se charge. Une
capture ou l'on ouvre seulement les onglets de classement ne les contient pas,
et le filtre `mine` echouait alors sur une capture pourtant parfaitement bonne.

Ce fichier ne contient que TON alliance -- un identifiant, une abreviation, un
nom. Il vit hors du depot, dans le repertoire de configuration de l'utilisateur.
`LWVS_HOME` permet de le deplacer (tests, poste partage).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Identity", "home", "path", "load", "save", "forget"]


@dataclass(frozen=True)
class Identity:
    alliance_id: str
    alliance_abbr: str | None = None
    alliance_name: str | None = None
    server_id: int | None = None
    #: Comment elle a ete apprise : "duel" (regle du protocole) ou "roster"
    #: (`al.rank`). Sert a dire a l'utilisateur d'ou vient ce qu'on affirme.
    source: str = ""
    learned_at: str = ""

    @property
    def label(self) -> str:
        return self.alliance_abbr or self.alliance_name or self.alliance_id[:8]


def home() -> Path:
    override = os.environ.get("LWVS_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "lwvs"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lwvs"


def path() -> Path:
    return home() / "identity.json"


def load() -> Identity | None:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("alliance_id"):
        return None
    known = {f for f in Identity.__dataclass_fields__}
    return Identity(**{k: v for k, v in data.items() if k in known})


def save(identity: Identity) -> Identity:
    stamped = Identity(**{**asdict(identity),
                          "learned_at": datetime.now(timezone.utc).isoformat(
                              timespec="seconds")})
    try:
        home().mkdir(parents=True, exist_ok=True)
        path().write_text(json.dumps(asdict(stamped), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except OSError:
        pass   # une memoire non ecrite ne doit jamais casser une capture
    return stamped


def forget() -> bool:
    try:
        path().unlink()
        return True
    except OSError:
        return False
