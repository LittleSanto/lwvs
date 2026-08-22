"""Catalogue des evenements, editable par l'utilisateur.

Pourquoi un catalogue plutot qu'un champ libre : `context.event` sert de cle de
regroupement en aval. En texte libre, « S3 - Spice Wars », « S3 – Spice Wars »
(tiret long) et « s3 spice wars » deviennent TROIS evenements distincts dans le
site, et personne ne s'en apercoit avant que les courbes se coupent en morceaux.

Chaque evenement a donc un IDENTIFIANT stable et un LIBELLE d'affichage -- la
meme discipline que `uid` / `player_name` : la cle ne bouge pas, l'etiquette
peut changer.

Le fichier vit a cote de la memoire d'alliance, hors du depot, et
`LWVS_HOME` le deplace (tests, poste partage).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from .identity import home

__all__ = ["Event", "slugify", "path", "load", "save", "add", "remove", "resolve",
           "DEFAULTS"]


@dataclass(frozen=True)
class Event:
    id: str
    label: str


#: Amorce du catalogue. L'utilisateur ajoute les suivants.
DEFAULTS: tuple[Event, ...] = (
    Event(id="s3_spice_wars", label="S3 - Spice Wars"),
)


def slugify(label: str) -> str:
    """« S3 - Spice Wars » -> « s3_spice_wars »."""
    ascii_only = "".join(
        c for c in unicodedata.normalize("NFKD", label)
        if not unicodedata.combining(c)
    )
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_only.lower()).strip("_")
    return slug or "evenement"


def path() -> Path:
    return home() / "events.json"


def load() -> list[Event]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(DEFAULTS)
    if not isinstance(data, list):
        return list(DEFAULTS)
    out: list[Event] = []
    for item in data:
        if isinstance(item, dict) and item.get("id") and item.get("label"):
            out.append(Event(id=str(item["id"]), label=str(item["label"])))
    return out or list(DEFAULTS)


def save(events: list[Event]) -> None:
    try:
        home().mkdir(parents=True, exist_ok=True)
        path().write_text(
            json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass   # un catalogue non ecrit ne doit jamais casser une capture


def add(label: str, event_id: str | None = None) -> Event:
    """Ajoute un evenement. Un libelle deja connu ne cree pas de doublon."""
    events = load()
    new = Event(id=event_id or slugify(label), label=label.strip())
    for existing in events:
        if existing.id == new.id:
            # Meme identifiant : on met a jour le libelle plutot que de
            # dupliquer. La cle prime, l'etiquette suit.
            events = [new if e.id == new.id else e for e in events]
            save(events)
            return new
    events.append(new)
    save(events)
    return new


def remove(event_id: str) -> bool:
    events = load()
    reste = [e for e in events if e.id != event_id]
    if len(reste) == len(events):
        return False
    save(reste)
    return True


def resolve(value: str) -> Event | None:
    """Retrouve un evenement par identifiant ou par libelle, souplement.

    Rend None si inconnu : l'appelant doit REFUSER plutot que fabriquer un
    evenement au vol, sinon la faute de frappe qu'on voulait eviter revient
    par la fenetre.
    """
    if not value:
        return None
    needle = value.strip()
    lowered = needle.lower()
    events = load()
    for event in events:
        if event.id == needle or event.label == needle:
            return event
    for event in events:
        if event.id.lower() == lowered or event.label.lower() == lowered:
            return event
    # Tolerance a la ponctuation : tiret court/long, espaces multiples.
    slug = slugify(needle)
    for event in events:
        if event.id == slug or slugify(event.label) == slug:
            return event
    return None
