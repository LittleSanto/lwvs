"""Enveloppe et messages VS, a travers la vraie couche trame.

Tous les tests partent d'octets de trame : un test qui passerait un corps nu au
parseur passerait pendant que ce qui arrive reellement sur le fil echoue.
"""

from __future__ import annotations

from lwvs import messages, wire
from lwvs.messages import CMD_GROUP, CMD_RANK, CMD_SEASON
from lwvs.pipeline import DecodeStats, decode_source
from lwvs.wire import i8, i16

from .helpers import WireSource, envelope, group_body, rank_body, season_body


def frames_for(*payloads: bytes, compress: bool = True) -> bytes:
    return b"".join(wire.encode_frame(p, compress=compress) for p in payloads)


def decode_all(stream_bytes: bytes):
    stats = DecodeStats()
    items = list(decode_source(WireSource(stream_bytes), stats))
    return items, stats


# ---------------------------------------------------------------------------
# enveloppe
# ---------------------------------------------------------------------------


def test_aiguillage_sur_le_nom_de_commande():
    payload = envelope("push.world.march.new", {"anything": i8(1)}, a=7, c=3)
    items, stats = decode_all(frames_for(payload))
    assert stats.exact == 1
    env = messages.unwrap(items[0].root.value)
    assert env.command == "push.world.march.new"
    assert env.body == {"anything": 1}
    # `a` et `c` externes : role inconnu, transportes tels quels sans
    # qu'on leur invente de sens.
    assert env.outer_a == 7
    assert env.outer_c == 3


def test_objet_hors_enveloppe():
    payload = wire.encode_value({"nope": i8(1)})
    items, _ = decode_all(frames_for(payload, compress=False))
    assert messages.unwrap(items[0].root.value) is None


# ---------------------------------------------------------------------------
# al.battle.rank.info
# ---------------------------------------------------------------------------


def test_rank_le_rang_est_positionnel():
    payload = envelope(CMD_RANK, rank_body(n=6, day=3))
    items, stats = decode_all(frames_for(payload))
    assert stats.exact == 1
    command, msg = messages.parse_message(items[0].root.value)
    assert command == CMD_RANK
    assert [r.rank for r in msg.rows] == [1, 2, 3, 4, 5, 6]
    # Aucun champ ne porte le rang : l'ordre du tableau *est* le classement.
    scores = [r.score for r in msg.rows]
    assert scores == sorted(scores, reverse=True)


def test_rank_les_deux_alliances_arrivent_dans_le_meme_message():
    payload = envelope(CMD_RANK, rank_body(n=8, day=2))
    items, _ = decode_all(frames_for(payload))
    _, msg = messages.parse_message(items[0].root.value)
    assert msg.abbrs == {"TST", "OPP"}   # `abbr` separe ton camp du leur


def test_rank_scope_deduit_de_la_presence_de_day_pas_de_type():
    du_jour = envelope(CMD_RANK, rank_body(day=4))
    cumul = envelope(CMD_RANK, rank_body(day=None))
    items, _ = decode_all(frames_for(du_jour, cumul))

    _, m_jour = messages.parse_message(items[0].root.value)
    _, m_cumul = messages.parse_message(items[1].root.value)
    assert (m_jour.scope, m_jour.day, m_jour.raw_type) == ("day", 4, 0)
    assert (m_cumul.scope, m_cumul.day, m_cumul.raw_type) == ("total", None, 1)


def test_rank_scope_suit_day_meme_si_type_dissent():
    """`type` est un entier opaque ; un classement qui declare son jour est
    evidemment celui de ce jour. Les deux sont stockes pour trancher plus tard."""
    payload = envelope(CMD_RANK, rank_body(day=5, raw_type=9))
    items, _ = decode_all(frames_for(payload))
    _, msg = messages.parse_message(items[0].root.value)
    assert msg.scope == "day"
    assert msg.day == 5
    assert msg.raw_type == 9


def test_rank_uid_reste_du_texte():
    payload = envelope(CMD_RANK, rank_body(n=2, day=1))
    items, _ = decode_all(frames_for(payload))
    _, msg = messages.parse_message(items[0].root.value)
    for row in msg.rows:
        assert isinstance(row.uid, str)
        assert len(row.uid) == 16   # 4 derniers chiffres = serveur d'origine


def test_rank_pseudos_non_latins_traversent_intacts():
    payload = envelope(CMD_RANK, rank_body(n=5, day=1))
    items, _ = decode_all(frames_for(payload))
    _, msg = messages.parse_message(items[0].root.value)
    names = "".join(r.name or "" for r in msg.rows)
    assert "أحمد" in names and "小龍" in names and "🔥" in names


# ---------------------------------------------------------------------------
# groupe et standing
# ---------------------------------------------------------------------------


def test_group_seize_alliances():
    payload = envelope(CMD_GROUP, group_body(16))
    items, _ = decode_all(frames_for(payload))
    command, msg = messages.parse_message(items[0].root.value)
    assert command == CMD_GROUP
    assert len(msg.rows) == 16
    assert msg.rows[0].position == 1
    assert msg.rows[0].group_code == "G1"
    assert msg.rows[3].alliance_abbr == "A03"


def test_season_courant_et_precedent():
    payload = envelope(CMD_SEASON, season_body(position=3))
    items, _ = decode_all(frames_for(payload))
    command, msg = messages.parse_message(items[0].root.value)
    assert command == CMD_SEASON
    assert [r.scope for r in msg.rows] == ["current", "previous"]
    assert msg.rows[0].position == 3
    assert msg.rows[1].group_code == "G0"


def test_ton_alliance_est_celle_dont_la_position_egale_celle_de_duelinfo():
    stream = frames_for(
        envelope(CMD_SEASON, season_body(position=3)),
        envelope(CMD_GROUP, group_body(16)),
    )
    items, _ = decode_all(stream)
    _, season = messages.parse_message(items[0].root.value)
    _, group = messages.parse_message(items[1].root.value)
    pos = messages.own_position(season)
    mine = [g for g in group.rows if g.position == pos]
    assert len(mine) == 1
    assert mine[0].alliance_abbr == "A02"   # position 3 -> index 2


# ---------------------------------------------------------------------------
# robustesse
# ---------------------------------------------------------------------------


def test_champs_manquants_ne_font_pas_planter():
    payload = envelope(CMD_RANK, {"rankInfo": [{"uid": "1"}, {}], "type": i8(1)})
    items, _ = decode_all(frames_for(payload, compress=False))
    _, msg = messages.parse_message(items[0].root.value)
    assert msg.scope == "total"
    assert msg.rows[0].uid == "1"
    assert msg.rows[0].name is None and msg.rows[0].score is None


def test_commande_inconnue_est_nommee_sans_etre_parsee():
    payload = envelope("get.king.info", {"whatever": i16(1)})
    items, _ = decode_all(frames_for(payload, compress=False))
    command, msg = messages.parse_message(items[0].root.value)
    assert command == "get.king.info"
    assert msg is None
