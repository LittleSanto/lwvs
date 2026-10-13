"""`storm` : rapports de Desert Storm et Canyon Storm -> JSON.

Le pendant de `grab` pour les donnees qui n'arrivent pas sur le fil : lit le
cache du client (`gamecache`), interprete les mails (`messages`), ecrit un
document par bataille (`exporter`), et poste s'il le faut (`publish`). Ce
module ne decide rien d'autre que l'enchainement.

Une bataille = un mail = une equipe. Chaque semaine en produit quatre : Desert
A et B le vendredi, Canyon A et B le jeudi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import exporter, gamecache
from . import identity as identity_mod
from .messages import STORM_KINDS, StormReport, parse_storm_mail
from .publish import PublishResult, publish

__all__ = ["StormWritten", "StormResult", "DEFAULT_DAYS", "since_ms", "load_reports",
           "label_of", "summary_of", "game_db", "remembered_alliance",
           "export_storm"]


@dataclass
class StormWritten:
    report: StormReport
    path: Path
    records: int
    published: PublishResult | None = None


@dataclass
class StormResult:
    db_path: Path
    mails: int = 0
    #: Copies exactes d'un mail deja lu (le client en garde parfois deux).
    duplicates: int = 0
    unreadable: int = 0
    own_label: str = ""
    written: list[StormWritten] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (bataille, raison)


def _signature(r: StormReport) -> tuple:
    # Le client stocke parfois le meme rapport sous deux `uid` de mail (vu :
    # 06/03, 13/03, 20/03 et 27/03/2026). Meme bataille, memes lignes : un seul
    # document, sinon l'aval compterait deux fois la meme participation.
    # Pas d'heure dans la signature : deux copies a 10:00:59 et 10:01:00
    # tombaient dans deux minutes differentes. Les scores suffisent, deux
    # batailles distinctes n'ont jamais les memes.
    return (r.kind, r.team,
            tuple(sorted((x.uid or "", x.score or 0) for x in r.rows)))


def load_reports(db_path: str | Path, since_ms: int | None = None,
                 kinds: Sequence[str] | None = None) -> tuple[list[StormReport], int, int, int]:
    """(rapports dedoublonnes, mails lus, doublons, illisibles)."""
    types = [t for t, k in STORM_KINDS.items() if not kinds or k in kinds]
    mails = gamecache.read_mails(db_path, types, since_ms=since_ms)
    reports: list[StormReport] = []
    seen: set[tuple] = set()
    duplicates = unreadable = 0
    for m in mails:
        rep = parse_storm_mail(m.mail_type, m.uid, m.created_ms, m.title, m.contents)
        if rep is None:
            unreadable += 1
            continue
        sig = _signature(rep)
        if sig in seen:
            duplicates += 1
            continue
        seen.add(sig)
        reports.append(rep)
    return reports, len(mails), duplicates, unreadable


#: Periode par defaut : la semaine passee. Reexporter un mail deja envoye est
#: sans danger (document identique, meme cle d'idempotence).
DEFAULT_DAYS = 7


def since_ms(text: str | None, now: datetime | None = None) -> int | None:
    """Periode saisie -> borne en ms UTC. CLI et GUI passent par ici.

    Vide : les `DEFAULT_DAYS` derniers jours. `all` : tout l'historique (None).
    `AAAA-MM-JJ` : a partir de ce jour, 00:00 UTC.
    """
    text = (text or "").strip()
    if text.lower() == "all":
        return None
    if not text:
        now = now or datetime.now(timezone.utc)
        return int((now - timedelta(days=DEFAULT_DAYS)).timestamp() * 1000)
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"period: expected YYYY-MM-DD, empty or 'all' -- not {text!r}") from None
    return int(day.timestamp() * 1000)


def label_of(r: StormReport) -> str:
    when = datetime.fromtimestamp(r.created_ms / 1000, tz=timezone.utc)
    return f"{r.kind} {when:%Y-%m-%d} {r.team or when.strftime('%H%M')}"


def summary_of(r: StormReport) -> dict[str, object]:
    """Ce qu'on peut AFFICHER d'une bataille : aucun pseudo, aucun uid.

    `--list` et la GUI montrent la meme chose ; le contenu joueur ne sort que
    par un export explicite.
    """
    when = datetime.fromtimestamp(r.created_ms / 1000, tz=timezone.utc)
    return {
        "kind": r.kind,
        "date": f"{when:%Y-%m-%d}",
        "team": r.team,
        "declared": r.team_source == "declared",
        "players": len(r.rows),
        "side": "+".join(a.abbr or "?" for a in r.own_side),
        "opponents": "+".join(a.abbr or "?" for a in r.other_side),
        "result": {True: "win", False: "loss", None: "?"}[r.won],
    }


def game_db(db_path: str | Path | None) -> Path:
    """La base du jeu donnee, sinon celle de ce poste. Storm et trains."""
    path = Path(db_path) if db_path else gamecache.default_db_path()
    if path is None:
        raise gamecache.CacheUnavailable(
            "game database not found under AppData\\LocalLow. Give its path"
            " with --game-db.")
    return path


def remembered_alliance(only_mine: bool) -> identity_mod.Identity | None:
    """Ton alliance memorisee, exigee si `only_mine`. Storm et trains.

    La meme memoire que `grab` : un identifiant appris sur `al.rank` ou l'ecran
    Duel. Les `alId` des mails sont du meme espace (verifie : identiques pour
    GolD). Elle n'est acceptee, mail par mail, que si l'alliance y figure
    reellement -- voir `build_storm_records` et `build_train_records`.
    """
    known = identity_mod.load()
    if only_mine and known is None:
        raise ValueError(
            "your alliance is not known yet. Capture once with the member list"
            " open (it is remembered), or untick « My alliance only »"
            " (--all-alliances).")
    return known


def export_storm(
    out_dir: str | Path,
    db_path: str | Path | None = None,
    since_ms: int | None = None,
    kinds: Sequence[str] | None = None,
    only_mine: bool = True,
    prefix: str = "lwvs",
    post_url: str | None = None,
    token: str | None = None,
) -> StormResult:
    """Un JSON par bataille. Fichiers ecrits AVANT l'envoi, comme `grab`."""
    path = game_db(db_path)
    reports, n, dups, bad = load_reports(path, since_ms=since_ms, kinds=kinds)
    result = StormResult(db_path=path, mails=n, duplicates=dups, unreadable=bad)

    known = remembered_alliance(only_mine)
    own_id = known.alliance_id if known else None
    if known:
        result.own_label = known.label

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for rep in reports:
        label = label_of(rep)
        try:
            payload = exporter.build_storm_records(rep, own_id, only_mine=only_mine)
        except ValueError as exc:
            result.skipped.append((label, str(exc)))
            continue
        if not payload["records"]:
            result.skipped.append((label, "no player of your alliance in this mail"))
            continue
        target = exporter.write_doc(out, f"{prefix}_{label.replace(' ', '_')}",
                                    used, payload)
        sent = publish(payload, post_url, token=token) if post_url else None
        result.written.append(StormWritten(report=rep, path=target,
                                           records=len(payload["records"]),
                                           published=sent))
    return result
