"""Ingestion : source -> messages VS -> base.

Ce module ne fait aucun SQL (c'est `store`) et ne touche aucun octet (c'est
`wire`). Il aiguille sur le NOM DE COMMANDE de l'enveloppe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .capture import CaptureSource
from .messages import (
    CMD_GROUP,
    CMD_CAMP_RANK,
    CMD_MEMBERS,
    CMD_RANK,
    CMD_SEASON,
    CMD_SERVER_RANK,
    CampRankMessage,
    GroupMessage,
    MembersMessage,
    RankMessage,
    SeasonMessage,
    ServerRankMessage,
    parse_message,
)
from .pipeline import DecodeStats, PayloadSaver, decode_source
from .store import IngestCounters, Store

__all__ = ["IngestResult", "ingest"]


@dataclass
class IngestResult:
    snapshot_id: int
    stats: DecodeStats
    counters: IngestCounters
    stored: dict[str, int] = field(default_factory=dict)
    dropped: bool = False


def ingest(
    source: CaptureSource,
    store: Store,
    note: str | None = None,
    saver: PayloadSaver | None = None,
    progress: bool = False,
    on_progress: Callable[[DecodeStats, IngestCounters], None] | None = None,
) -> IngestResult:
    snapshot_id = store.create_snapshot(source.description, note)
    stats = DecodeStats()
    counters = IngestCounters()

    for item in decode_source(source, stats, saver):
        if item.root is None:
            continue
        command, msg = parse_message(item.root.value)
        if command is None:
            continue
        counters.note_command(command)
        store.record_command(snapshot_id, command)
        if msg is None:
            continue
        if command == CMD_RANK and isinstance(msg, RankMessage):
            counters.rank_messages += 1
            counters.rank_rows += store.add_rank_message(snapshot_id, msg)
        elif command == CMD_GROUP and isinstance(msg, GroupMessage):
            counters.group_messages += 1
            counters.group_rows += store.add_group_message(snapshot_id, msg)
        elif command == CMD_SEASON and isinstance(msg, SeasonMessage):
            counters.season_messages += 1
            counters.standing_rows += store.add_season_message(snapshot_id, msg)
        elif command == CMD_MEMBERS and isinstance(msg, MembersMessage):
            counters.member_messages += 1
            counters.member_rows += store.add_members_message(snapshot_id, msg)
        elif command == CMD_CAMP_RANK and isinstance(msg, CampRankMessage):
            counters.camp_messages += 1
            counters.camp_rows += store.add_camp_rank_message(snapshot_id, msg)
        elif command == CMD_SERVER_RANK and isinstance(msg, ServerRankMessage):
            counters.server_rank_messages += 1
            counters.server_rank_rows += store.add_server_rank_message(snapshot_id, msg)
        if progress and stats.payloads % 200 == 0:
            print(f"  ... {stats.payloads} payloads, {stats.decoded} decodes", flush=True)
        if on_progress is not None:
            # Appele depuis le thread de capture : a la charge de l'appelant de
            # marshaler vers son thread UI.
            on_progress(stats, counters)

    store.commit()
    store.finish_snapshot(
        snapshot_id,
        frames=source.stats.frames or stats.payloads,
        payloads=stats.payloads,
        decoded=stats.decoded,
        exact=stats.exact,
    )
    stored = store.count_rows(snapshot_id)
    dropped = store.drop_snapshot_if_empty(snapshot_id)
    return IngestResult(snapshot_id, stats, counters, stored, dropped)
