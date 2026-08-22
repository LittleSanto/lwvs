"""`grab` : capture -> JSON, en une commande, sans base sur le disque.

Pourquoi ce chemin existe : la base ne servait qu'a une chose que l'outil ne
fait plus. `armyKill` est cumulatif, donc les kills d'une periode sont une
DIFFERENCE entre deux captures -- il fallait un historique pour la calculer.
L'historisation est desormais faite en aval, donc l'historique local n'a plus
de contrepartie et devient un intermediaire a gerer pour rien.

Ce qui reste vrai : SQLite sert encore de moteur de deduplication et de
requete, mais EN MEMOIRE. Le meme classement est retransmis plusieurs fois dans
une capture (14 messages -> 379 lignes), et c'est la cle
`(snapshot, scope, day, uid)` qui le ramene a une verite unique. Ce travail-la
n'a jamais eu besoin d'un fichier.

`--db FICHIER` reste disponible pour qui veut quand meme accumuler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import exporter
from .capture import CaptureSource
from .exporter import Feed
from .ingest import IngestResult, ingest
from .pipeline import PayloadSaver
from .publish import PublishResult, publish
from .store import Store

__all__ = ["MEMORY_DB", "GrabResult", "grab"]

#: Base SQLite en RAM : rien n'est ecrit, rien n'est a nettoyer.
MEMORY_DB = ":memory:"


@dataclass
class Written:
    feed: Feed
    day: int | None
    path: Path
    records: int
    published: PublishResult | None = None


@dataclass
class GrabResult:
    ingest: IngestResult
    #: Comment ton alliance a ete identifiee : "duel", "roster" ou "memoire".
    own_source: str = ""
    own_label: str = ""
    written: list[Written] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (flux, raison)
    #: Ecarts entre ce que le message dit et ce que l'utilisateur a annonce.
    conflicts: list[str] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return sum(w.records for w in self.written)


def _feed_days(store: Store, feed: Feed, snapshot: int) -> list[int | None]:
    if not feed.per_day:
        return [None]
    days = sorted(
        {r["day"] for r in store.fetch_players(scope="day", snapshot=snapshot)
         if r["day"] is not None}
    )
    return list(days) or [None]


def grab(
    source: CaptureSource,
    out_dir: str | Path,
    db_path: str = MEMORY_DB,
    only_mine: bool = True,
    prefix: str = "lwvs",
    note: str | None = None,
    on_progress: Callable[[object, object], None] | None = None,
    post_url: str | None = None,
    token: str | None = None,
    save_dir: str | Path | None = None,
    declared_day: int | None = None,
    event: str | None = None,
) -> GrabResult:
    """Capture, decode, ecrit un JSON par flux disponible.

    `post_url` envoie en plus chaque document au service. Les fichiers sont
    ecrits AVANT l'envoi : un service injoignable ne doit jamais faire perdre
    une capture, elle est rejouable depuis le disque.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Une SEULE instance de Store : avec `:memory:`, rouvrir la base rendrait
    # une base vide. C'est la difference qui compte entre un fichier et la RAM.
    # Les payloads bruts servent a EXPLORER : `find` et `inspect` rejouent un
    # repertoire sans qu'il faille recapturer. Ils incluent les payloads qui
    # n'ont pas decode, seuls temoins d'un ecran encore inconnu.
    saver = PayloadSaver(save_dir) if save_dir else None
    store = Store(db_path)
    try:
        result = ingest(source, store, note=note, on_progress=on_progress,
                        saver=saver)
        grabbed = GrabResult(ingest=result)
        snapshot = result.snapshot_id
        # Resolu UNE fois : identique pour tous les flux d'une meme capture.
        own_ids = None
        try:
            own = exporter.resolve_own_alliance(store, snapshot)
            own_ids = own.ids
            grabbed.own_source, grabbed.own_label = own.source, own.label
        except ValueError:
            pass   # l'erreur sera rapportee par flux, avec le geste a faire
        context = store.duel_context(snapshot, own_ids=own_ids)

        for feed in exporter.FEEDS:
            for day in _feed_days(store, feed, snapshot):
                # `al.rank` est deja ton roster : lui appliquer `mine` le ferait
                # echouer sur une capture sans info de duel, pour rien.
                alliance = (exporter.MINE
                            if only_mine and not feed.inherently_mine else None)
                try:
                    rows = exporter.collect(
                        store, feed.dataset, scope=feed.scope, day=day,
                        alliance=alliance, snapshot=snapshot)
                except ValueError as exc:
                    grabbed.skipped.append((feed.key, str(exc)))
                    continue
                if not rows:
                    grabbed.skipped.append((feed.key, "aucune ligne dans cette capture"))
                    continue
                payload = exporter.build_records(
                    rows, feed.dataset, mode=feed.mode, scope=feed.scope,
                    context=context, declared_day=declared_day, event=event,
                    metric=feed.metric)
                ctx = payload.get("context", {})
                if (declared_day is not None and ctx.get("day_source") == "message"
                        and ctx.get("day") != declared_day):
                    grabbed.conflicts.append(
                        f"{feed.key} : le message declare le jour {ctx['day']},"
                        f" tu as annonce {declared_day}. Le message fait foi.")
                suffix = f"_j{day}" if day is not None else ""
                path = out / f"{prefix}_{feed.key}{suffix}.json"
                with path.open("w", encoding="utf-8", newline="") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                sent = publish(payload, post_url, token=token) if post_url else None
                grabbed.written.append(
                    Written(feed=feed, day=day, path=path,
                            records=len(payload["records"]), published=sent))
        return grabbed
    finally:
        store.close()
        if saver is not None:
            saver.close()
