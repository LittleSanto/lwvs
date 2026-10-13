"""`trains` : trains d'alliance -> JSON, un document par train.

Le pendant de `storm` pour les trains : lit le cache du client (`gamecache`),
interprete les mails (`messages`), ecrit un document par train (`exporter`), et
poste s'il le faut (`publish`). Ce module ne decide rien d'autre que
l'enchainement -- et la fusion d'un depart avec son arrivee.

Le but en aval : confronter les trains REELLEMENT partis au planning des
conducteurs, et compter VIP et Guardians. D'ou `train_date` en jour du jeu, et
`purchased` qui separe le train quotidien d'un train achete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import exporter, gamecache
from .messages import TRAIN_MAILS, TrainRun, game_date, parse_train_mail
from .publish import PublishResult, publish
from .storm import game_db, remembered_alliance, since_ms

__all__ = ["TrainWritten", "TrainResult", "since_ms", "load_trains", "label_of",
           "summary_of", "export_trains"]


@dataclass
class TrainWritten:
    run: TrainRun
    path: Path
    records: int
    published: PublishResult | None = None


@dataclass
class TrainResult:
    db_path: Path
    mails: int = 0
    unreadable: int = 0
    own_label: str = ""
    written: list[TrainWritten] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (train, raison)


def load_trains(db_path: str | Path,
                since_ms: int | None = None) -> tuple[list[TrainRun], int, int]:
    """(trains, mails lus, illisibles), du plus ancien depart au plus recent.

    Depart (130) et arrivee (128) du meme train ne font qu'un : l'arrivee, plus
    complete, l'emporte. Un train dont seule l'arrivee tombe dans la periode
    est garde -- il a roule dedans.
    """
    mails = gamecache.read_mails(db_path, list(TRAIN_MAILS), since_ms=since_ms)
    best: dict[str, TrainRun] = {}
    unreadable = 0
    for m in mails:
        run = parse_train_mail(m.mail_type, m.uid, m.created_ms, m.contents)
        if run is None:
            unreadable += 1
            continue
        held = best.get(run.train_id)
        if held is None or (run.arrived, run.created_ms) > (held.arrived, held.created_ms):
            best[run.train_id] = run
    runs = sorted(best.values(),
                  key=lambda r: (r.departed_ms or r.created_ms, r.train_id))
    return runs, len(mails), unreadable


def _departure(r: TrainRun) -> datetime:
    ms = r.departed_ms if r.departed_ms is not None else r.created_ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def label_of(r: TrainRun) -> str:
    """`train AAAA-MM-JJ HHMM` : jour du jeu, heure de depart UTC."""
    when = _departure(r)
    ms = r.departed_ms if r.departed_ms is not None else r.created_ms
    return f"train {game_date(ms)} {when:%H%M}"


def summary_of(r: TrainRun) -> dict[str, object]:
    """Ce qu'on peut AFFICHER d'un train : aucun pseudo, aucun uid.

    Comme `storm.summary_of` : `--list` et la GUI montrent la meme chose, et le
    contenu joueur ne sort que par un export explicite.
    """
    ms = r.departed_ms if r.departed_ms is not None else r.created_ms
    return {
        "date": game_date(ms),
        "departed": f"{_departure(r):%H:%M}",
        "purchased": bool(r.purchased),
        "escort": r.escort_role or ("?" if r.escort else "none"),
        "passengers": r.passengers,
        "arrived": r.arrived,
        "attacks": r.attacks,
        "robbed": r.rob_times,
        "alliance": r.alliance_abbr or "?",
    }


def export_trains(
    out_dir: str | Path,
    db_path: str | Path | None = None,
    since_ms: int | None = None,
    only_mine: bool = True,
    prefix: str = "lwvs",
    post_url: str | None = None,
    token: str | None = None,
) -> TrainResult:
    """Un JSON par train. Fichiers ecrits AVANT l'envoi, comme `grab`."""
    path = game_db(db_path)
    runs, n, bad = load_trains(path, since_ms=since_ms)
    result = TrainResult(db_path=path, mails=n, unreadable=bad)

    known = remembered_alliance(only_mine)
    own_id = known.alliance_id if known else None
    if known:
        result.own_label = known.label

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for run in runs:
        label = label_of(run)
        try:
            payload = exporter.build_train_records(run, own_id, only_mine=only_mine)
        except ValueError as exc:
            result.skipped.append((label, str(exc)))
            continue
        target = exporter.write_doc(out, f"{prefix}_{label.replace(' ', '_')}",
                                    used, payload)
        sent = publish(payload, post_url, token=token) if post_url else None
        result.written.append(TrainWritten(run=run, path=target,
                                           records=len(payload["records"]),
                                           published=sent))
    return result
