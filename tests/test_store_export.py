"""Ingestion idempotente, accumulation, et export."""

from __future__ import annotations

import csv
import io
import json
import sys

import pytest

from lwvs import exporter, wire
from lwvs.ingest import ingest
from lwvs.messages import (CMD_GROUP, CMD_MEMBERS, CMD_RANK, CMD_SEASON,
                           CMD_SERVER_RANK)
from lwvs.store import Store
from lwvs.wire import i8

from .helpers import (
    WireSource, envelope, group_body, members_body, rank_body, season_body,
    server_rank_body,
)


def stream_of(*payloads: bytes) -> bytes:
    return b"".join(wire.encode_frame(p, compress=True) for p in payloads)


def full_capture(repeats: int = 4, n: int = 195) -> bytes:
    """Le meme classement retransmis plusieurs fois -- 4x observe."""
    payloads = []
    for _ in range(repeats):
        payloads.append(envelope(CMD_RANK, rank_body(n=n, day=3)))
        payloads.append(envelope(CMD_RANK, rank_body(n=n, day=None)))
    payloads.append(envelope(CMD_GROUP, group_body(16)))
    payloads.append(envelope(CMD_SEASON, season_body(position=3)))
    payloads.append(envelope(CMD_MEMBERS, members_body(n=8)))
    payloads.append(envelope(CMD_SERVER_RANK, server_rank_body(n=8)))
    return stream_of(*payloads)


@pytest.fixture
def db(tmp_path):
    with Store(tmp_path / "t.sqlite3") as store:
        yield store


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


def test_ingestion_idempotente_ne_cumule_pas(db):
    result = ingest(WireSource(full_capture(repeats=4, n=195)), db)
    assert result.stats.exact == result.stats.decoded == result.stats.payloads

    rows = db.fetch_players(scope="day")
    assert len(rows) == 195, "4 retransmissions doivent donner 195 lignes, pas 780"

    # Et surtout : les scores ne sont pas additionnes.
    top = [r for r in rows if r["rank"] == 1][0]
    assert top["score"] == 100000

    total = db.fetch_players(scope="total")
    assert len(total) == 195
    assert {r["day"] for r in rows} == {3}
    assert {r["day"] for r in total} == {None}


def test_les_captures_s_accumulent_sans_ecraser(db):
    a = ingest(WireSource(full_capture(repeats=1, n=10)), db, note="jour 3")
    b = ingest(WireSource(full_capture(repeats=1, n=10)), db, note="jour 4")
    assert a.snapshot_id != b.snapshot_id
    assert len(db.snapshots()) == 2
    assert len(db.fetch_players(scope="day")) == 20
    assert len(db.fetch_players(scope="day", snapshot=a.snapshot_id)) == 10


def test_index_du_protocole(db):
    ingest(WireSource(full_capture(repeats=2, n=5)), db)
    seen = dict(db.commands_seen())
    assert seen[CMD_RANK] == 4
    assert seen[CMD_GROUP] == 1
    assert seen[CMD_SEASON] == 1


def test_groupe_et_standing_stockes(db):
    ingest(WireSource(full_capture(repeats=1, n=5)), db)
    assert len(db.fetch_group()) == 16
    standing = db.fetch_standing()
    assert {r["scope"] for r in standing} == {"current", "previous"}


def test_snapshot_vide_supprime(db):
    payload = envelope("get.king.info", {"x": i8(1)})
    result = ingest(WireSource(stream_of(payload)), db)
    assert result.dropped is True
    assert db.snapshots() == []


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_json_colonnes_exactes(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=4)), db)
    exporter.run_export(db, dataset="players", fmt="json", out=None)
    rows = json.loads(capsys.readouterr().out)
    assert list(rows[0]) == list(exporter.COLUMNS["players"])
    assert len(rows) == 8   # 4 joueurs x 2 scopes -> format long


def test_export_format_long_scope_et_day_en_colonnes(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=3)), db)
    exporter.run_export(db, dataset="players", fmt="json", out=None)
    rows = json.loads(capsys.readouterr().out)
    assert {r["scope"] for r in rows} == {"day", "total"}
    # Une ligne par (joueur, scope, jour) : jamais de colonne "jour_3".
    assert not any(k.startswith("day_") or k.startswith("scope_") for k in rows[0])


def test_export_conserve_uid_captured_at_et_raw_type(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=3)), db)
    exporter.run_export(db, dataset="players", fmt="json", out=None)
    rows = json.loads(capsys.readouterr().out)
    for row in rows:
        assert row["uid"] and len(row["uid"]) == 16   # cle de jointure stable
        assert row["captured_at"]                     # horodatage par ligne
        assert row["raw_type"] is not None            # deduction verifiable en aval


def test_export_n_agrege_rien(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=6)), db)
    exporter.run_export(db, dataset="all", fmt="json", out=None)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"players", "group", "standing", "members", "camp",
                            "server"}
    # Aucun total, aucune moyenne, aucune part : que des lignes brutes.
    for row in payload["players"]:
        assert set(row) == set(exporter.COLUMNS["players"])


def test_export_csv_utf8_explicite(db, tmp_path):
    ingest(WireSource(full_capture(repeats=1, n=5)), db)
    out = tmp_path / "vs.csv"
    written = exporter.run_export(db, dataset="players", fmt="csv", out=str(out))
    assert written == [str(out)]

    raw = out.read_bytes()
    assert "أحمد".encode("utf-8") in raw
    assert "小龍".encode("utf-8") in raw
    assert "🔥".encode("utf-8") in raw

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(exporter.COLUMNS["players"])
    assert len(rows) == 10


def test_export_csv_all_ecrit_un_fichier_par_jeu(db, tmp_path):
    ingest(WireSource(full_capture(repeats=1, n=3)), db)
    out = tmp_path / "vs.csv"
    written = exporter.run_export(db, dataset="all", fmt="csv", out=str(out))
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in written] == [
        "vs.players.csv", "vs.group.csv", "vs.standing.csv", "vs.members.csv",
        "vs.camp.csv", "vs.server.csv",
    ]


def test_export_csv_all_sur_stdout_est_refuse(db):
    ingest(WireSource(full_capture(repeats=1, n=2)), db)
    with pytest.raises(ValueError, match="un seul jeu"):
        exporter.run_export(db, dataset="all", fmt="csv", out=None)


def test_export_filtres(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=6)), db)

    exporter.run_export(db, dataset="players", fmt="json", out=None,
                        scope="day", day=3, alliance="TST")
    rows = json.loads(capsys.readouterr().out)
    assert rows
    assert {r["alliance_abbr"] for r in rows} == {"TST"}
    assert {r["scope"] for r in rows} == {"day"}
    assert {r["day"] for r in rows} == {3}


def test_export_csv_valeurs_nulles_deviennent_vides(db, tmp_path):
    ingest(WireSource(full_capture(repeats=1, n=2)), db)
    out = tmp_path / "t.csv"
    exporter.run_export(db, dataset="players", fmt="csv", out=str(out), scope="total")
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert all(r["day"] == "" for r in rows)


def test_export_stdout_survit_a_une_console_cp1252(db, monkeypatch):
    """Regression : l'export vers stdout cassait sur le premier pseudo non
    latin quand la console est en cp1252 (Windows par defaut). L'UTF-8
    explicite du chemin fichier ne suffit pas : on redirige avec `>`."""
    ingest(WireSource(full_capture(repeats=1, n=5)), db)

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", console)
    exporter.run_export(db, dataset="players", fmt="csv", out=None)

    out = raw.getvalue()
    assert "أحمد".encode("utf-8") in out
    assert "小龍".encode("utf-8") in out
    assert "🔥".encode("utf-8") in out
    assert len(out.decode("utf-8").strip().splitlines()) == 11   # entete + 10


# ---------------------------------------------------------------------------
# membres (al.rank) et resolution de TON alliance
# ---------------------------------------------------------------------------


def test_members_armykill_stocke_sans_cumul(db):
    """armyKill est deja cumulatif cote jeu : le stockage remplace, il
    n'additionne pas. Sinon une retransmission doublerait le compteur."""
    stream = stream_of(*([envelope(CMD_MEMBERS, members_body(n=8))] * 3))
    ingest(WireSource(stream), db)
    rows = db.fetch_members()
    assert len(rows) == 8, "3 retransmissions -> 8 membres, pas 24"
    assert rows[0]["army_kill"] == 100_000
    assert rows[0]["power"] == 50_000_000
    assert rows[0]["alliance_rank"] == 5


def test_members_export_colonnes(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=4)), db)
    exporter.run_export(db, dataset="members", fmt="json", out=None)
    rows = json.loads(capsys.readouterr().out)
    assert list(rows[0]) == list(exporter.COLUMNS["members"])
    assert rows[0]["uid"] and len(rows[0]["uid"]) == 16
    assert rows[0]["captured_at"]
    # Aucune difference calculee ici : que des valeurs brutes.
    assert "kills_delta" not in rows[0]


def test_la_difference_entre_snapshots_donne_les_kills(db):
    """Ce que l'accumulation rend possible, sans que l'outil l'agrege."""
    a = ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=5, base_kill=1000)))), db)
    b = ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=5, base_kill=1450)))), db)

    par_uid = {}
    for row in db.fetch_members():
        par_uid.setdefault(row["uid"], {})[row["snapshot_id"]] = row["army_kill"]
    deltas = {u: v[b.snapshot_id] - v[a.snapshot_id] for u, v in par_uid.items()}
    assert set(deltas.values()) == {450}


def test_alliance_mine_est_resolue_par_la_regle_du_protocole(db):
    """Ton alliance = celle du groupe dont la position egale celle de duelInfo.

    On ne fige jamais une abreviation : elle peut changer, la regle non.
    """
    ingest(WireSource(full_capture(repeats=1, n=6)), db)
    labels = db.own_alliance_labels()
    assert len(labels) == 1
    _, name, abbr = labels[0]
    assert (name, abbr) == ("Alliance 02", "A02")   # position 3 -> index 2


def test_alliance_mine_filtre_sans_nommer_l_abbr(db, capsys):
    stream = stream_of(
        envelope(CMD_RANK, rank_body(n=6, day=1)),
        envelope(CMD_GROUP, group_body(16)),
        envelope(CMD_SEASON, season_body(position=1)),   # position 1 -> A00
    )
    ingest(WireSource(stream), db)
    # rank_body met aid=...0001 pour TST et ...0002 pour OPP ;
    # group_body met allianceId=9000...+p, donc A00 == ...0000.
    # Aucun recouvrement : le filtre doit rendre zero ligne, pas tout.
    exporter.run_export(db, dataset="players", fmt="json", out=None, alliance="mine")
    rows = json.loads(capsys.readouterr().out)
    assert rows == []


def test_alliance_mine_echoue_clairement_si_non_resolvable(db):
    """Sans ecran Duel, sans roster et sans memoire : on refuse, en disant quoi
    ouvrir dans le jeu."""
    ingest(WireSource(stream_of(envelope(CMD_RANK, rank_body(n=3, day=1)))), db)
    with pytest.raises(ValueError, match="impossible d'identifier ton alliance"):
        exporter.collect(db, "players", alliance="mine")


def test_le_roster_suffit_a_identifier_ton_alliance(db):
    """`al.rank` porte `allianceId` : c'est ton roster par definition.

    C'est le cas frequent qui echouait -- une capture des onglets de classement
    n'envoie pas `get.alliance.duel.season.info`.
    """
    stream = stream_of(
        envelope(CMD_RANK, rank_body(n=6, day=1)),
        envelope(CMD_MEMBERS, members_body(n=4, alliance_id="9000000000000002")),
    )
    ingest(WireSource(stream), db)
    own = exporter.resolve_own_alliance(db)
    assert own.source == "roster"
    assert own.ids == ["9000000000000002"]


def test_l_alliance_est_memorisee_d_une_capture_a_l_autre(db, tmp_path):
    """La question posee : « tu peux pas retenir des precedentes sessions ? »"""
    from lwvs import identity

    complete = stream_of(
        envelope(CMD_RANK, rank_body(n=6, day=1)),
        envelope(CMD_GROUP, group_body(16)),
        envelope(CMD_SEASON, season_body(position=3)),
    )
    ingest(WireSource(complete), db)
    premiere = exporter.resolve_own_alliance(db)
    assert premiere.source == "duel"
    assert identity.load().alliance_id == premiere.ids[0]

    # Capture suivante : QUE le classement, comme quand on ouvre les onglets.
    with Store(tmp_path / "suivante.sqlite3") as autre:
        ingest(WireSource(stream_of(envelope(CMD_RANK, rank_body(n=6, day=2)))), autre)
        reprise = exporter.resolve_own_alliance(autre)
        assert reprise.source == "memoire"
        assert reprise.ids == premiere.ids
        assert len(exporter.collect(autre, "players", scope="day", alliance="mine")) == 3


def test_une_memoire_perimee_est_refusee_pas_appliquee(db, tmp_path):
    """Une alliance memorisee absente de la capture rendrait un export vide
    sans rien dire. On refuse et on l'explique."""
    from lwvs import identity

    identity.save(identity.Identity(alliance_id="alliance-d-avant",
                                    alliance_abbr="OLD", source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_RANK, rank_body(n=3, day=1)))), db)
    with pytest.raises(ValueError, match="OLD"):
        exporter.collect(db, "players", alliance="mine")


# ---------------------------------------------------------------------------
# format "records" (forme de l'outil OCR en aval)
# ---------------------------------------------------------------------------


def test_records_forme_exacte(db, capsys):
    corps = members_body(n=5)
    del corps["allianceId"]        # sans contexte, la forme d'origine exacte
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, corps))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None)
    payload = json.loads(capsys.readouterr().out)

    assert list(payload) == ["exported_at", "mode", "records"]
    assert payload["mode"] == "kill_rank"
    assert list(payload["records"][0]) == ["rank", "uid", "player_name", "score"]
    # Aucun champ OCR fabrique : ils decrivent la fiabilite d'une lecture
    # d'ecran, ces valeurs viennent du fil.
    for absent in ("confidence", "known_player_name", "known_player_status",
                   "screen_rank", "issues"):
        assert absent not in payload["records"][0]


def test_records_rang_contigu_et_trie_par_score(db, capsys):
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=6)))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None)
    recs = json.loads(capsys.readouterr().out)["records"]
    assert [r["rank"] for r in recs] == [1, 2, 3, 4, 5, 6]
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_records_exported_at_porte_l_instant_de_capture(db, capsys):
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=3)))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["exported_at"] == db.snapshots()[0].captured_at


def test_records_refuse_de_melanger_deux_captures(db):
    for _ in range(2):
        ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=3)))), db)
    with pytest.raises(ValueError, match="plusieurs captures"):
        exporter.run_export(db, dataset="members", fmt="records", out=None)


def test_records_mode_deduit_du_scope(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=4)), db)

    exporter.run_export(db, dataset="players", fmt="records", out=None, scope="total")
    assert json.loads(capsys.readouterr().out)["mode"] == "weekly_rank"

    exporter.run_export(db, dataset="players", fmt="records", out=None, scope="day")
    assert json.loads(capsys.readouterr().out)["mode"] == "daily_rank"

    exporter.run_export(db, dataset="players", fmt="records", out=None,
                        scope="total", mode="autre_chose")
    assert json.loads(capsys.readouterr().out)["mode"] == "autre_chose"


def test_records_refuse_les_datasets_sans_score(db):
    ingest(WireSource(full_capture(repeats=1, n=3)), db)
    with pytest.raises(ValueError, match="ne porte pas un score"):
        exporter.run_export(db, dataset="group", fmt="records", out=None)


def test_records_porte_uid_pour_survivre_aux_changements_de_pseudo(db, capsys):
    """Les pseudos changent (observe : `Nomade` -> `NomadeX` en 8 jours),
    l'abreviation d'alliance aussi. `uid` est la seule cle de jointure stable
    entre deux exports."""
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=4)))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None)
    recs = json.loads(capsys.readouterr().out)["records"]
    for r in recs:
        assert r["uid"] and len(r["uid"]) == 16
    assert len({r["uid"] for r in recs}) == len(recs)


def test_records_joint_deux_captures_malgre_un_pseudo_change(db, capsys):
    """Le scenario qui justifie uid : meme joueur, nom different."""
    avant = members_body(n=3, base_kill=1000)
    apres = members_body(n=3, base_kill=1500)
    apres["list"][0]["name"] = "PseudoTotalementDifferent"

    a = ingest(WireSource(stream_of(envelope(CMD_MEMBERS, avant))), db)
    b = ingest(WireSource(stream_of(envelope(CMD_MEMBERS, apres))), db)

    def records(snapshot):
        exporter.run_export(db, dataset="members", fmt="records", out=None,
                            snapshot=snapshot)
        return {r["uid"]: r for r in json.loads(capsys.readouterr().out)["records"]}

    ra, rb = records(a.snapshot_id), records(b.snapshot_id)
    joueur = next(iter(ra))
    assert ra[joueur]["player_name"] != rb[joueur]["player_name"]
    assert rb[joueur]["score"] - ra[joueur]["score"] == 500


# ---------------------------------------------------------------------------
# grab : capture -> JSON, sans base sur le disque
# ---------------------------------------------------------------------------


def test_grab_ecrit_les_json_sans_rien_laisser_sur_le_disque(tmp_path):
    """La base ne sert plus qu'a dedupliquer : elle n'a pas besoin d'un fichier.

    L'historisation est faite en aval, donc le livrable est le JSON, point.
    """
    from lwvs.grab import MEMORY_DB, grab

    out = tmp_path / "exports"
    grab(WireSource(full_capture(repeats=4, n=20)), out_dir=out, db_path=MEMORY_DB)

    noms = sorted(p.name for p in out.iterdir())
    assert noms == ["lwvs_dons_jour.json", "lwvs_dons_semaine.json",
                    "lwvs_kills.json", "lwvs_thp.json", "lwvs_vs_day_j3.json",
                    "lwvs_vs_total.json"]
    # Rien d'autre que les JSON demandes.
    assert not list(tmp_path.glob("*.sqlite3*"))

    kills = json.loads((out / "lwvs_kills.json").read_text(encoding="utf-8"))
    assert kills["mode"] == "kill_rank"
    assert len(kills["records"]) == 8
    assert all(r["uid"] for r in kills["records"])


def test_grab_deduplique_malgre_l_absence_de_fichier(tmp_path):
    """4 retransmissions du meme classement -> un seul classement exporte."""
    from lwvs.grab import MEMORY_DB, grab

    out = tmp_path / "exports"
    grab(WireSource(full_capture(repeats=4, n=30)), out_dir=out, db_path=MEMORY_DB,
         only_mine=False)
    vs = json.loads((out / "lwvs_vs_total.json").read_text(encoding="utf-8"))
    assert len(vs["records"]) == 30, "4 retransmissions ne doivent pas quadrupler"
    assert [r["rank"] for r in vs["records"][:3]] == [1, 2, 3]

    # Et avec le filtre : une seule des deux alliances du match.
    out2 = tmp_path / "exports2"
    grab(WireSource(full_capture(repeats=4, n=30)), out_dir=out2, db_path=MEMORY_DB)
    filtre = json.loads((out2 / "lwvs_vs_total.json").read_text(encoding="utf-8"))
    assert len(filtre["records"]) == 15


def test_grab_n_applique_pas_mine_au_roster(tmp_path):
    """`al.rank` EST ton roster : lui appliquer le filtre le ferait echouer sur
    une capture sans info de duel, alors que la donnee est bonne."""
    from lwvs.grab import MEMORY_DB, grab

    out = tmp_path / "exports"
    stream = stream_of(envelope(CMD_MEMBERS, members_body(n=6)))
    res = grab(WireSource(stream), out_dir=out, db_path=MEMORY_DB, only_mine=True)

    ecrits = {w.feed.key for w in res.written}
    assert "kills" in ecrits, "les kills doivent sortir sans info de duel"
    assert len(json.loads((out / "lwvs_kills.json").read_text(encoding="utf-8"))
               ["records"]) == 6


def test_grab_peut_quand_meme_accumuler(tmp_path):
    """`--db` reste disponible pour qui veut un historique local."""
    from lwvs.grab import grab

    db = tmp_path / "hist.sqlite3"
    for _ in range(2):
        grab(WireSource(full_capture(repeats=1, n=5)), out_dir=tmp_path / "o",
             db_path=str(db))
    with Store(db) as store:
        assert len(store.snapshots()) == 2


# ---------------------------------------------------------------------------
# contexte du duel : qui affronte qui
# ---------------------------------------------------------------------------


def test_l_adversaire_se_deduit_du_classement(db):
    """Le classement VS porte LES DEUX alliances : l'adversaire est celle dont
    l'identifiant n'est pas le tien."""
    ingest(WireSource(full_capture(repeats=1, n=8)), db)
    ctx = db.duel_context()

    assert ctx["alliance"]["alliance_abbr"] == "A02"     # position 3 du groupe
    assert ctx["opponent"]["id"] != ctx["alliance"]["id"]
    assert ctx["group_code"] == "G1"

    # Le libelle vient du GROUPE, pas du classement : lui seul porte `position`
    # et `server_id`. Sur des donnees reelles les deux concordent ; ici les
    # fixtures divergent volontairement, ce qui rend la priorite visible.
    assert ctx["opponent"]["alliance_abbr"] == "A01"
    assert ctx["opponent"]["position"] == 2
    assert ctx["opponent"]["server_id"] == 1201


def test_pas_d_adversaire_si_le_classement_n_en_designe_pas_deux(db):
    """Trois alliances voudraient dire deux duels melanges : on se tait."""
    stream = stream_of(
        envelope(CMD_GROUP, group_body(16)),
        envelope(CMD_SEASON, season_body(position=3)),
        envelope(CMD_MEMBERS, members_body(n=4)),
    )
    ingest(WireSource(stream), db)
    ctx = db.duel_context()
    assert "opponent" not in ctx, "sans classement VS, l'adversaire est inconnu"
    assert ctx["alliance"]["alliance_abbr"] == "A02"


def test_le_contexte_arrive_dans_le_document(db, capsys):
    ingest(WireSource(full_capture(repeats=1, n=8)), db)
    exporter.run_export(db, dataset="players", fmt="records", out=None,
                        scope="day", day=3, alliance="mine")
    doc = json.loads(capsys.readouterr().out)
    assert doc["context"]["opponent"]["alliance_abbr"] == "A01"
    assert doc["context"]["alliance"]["alliance_abbr"] == "A02"
    assert doc["context"]["day"] == 3
    # Les records gardent exactement leur forme : le contexte est a cote.
    assert list(doc["records"][0]) == ["rank", "uid", "player_name", "score"]


def test_un_contexte_vide_est_omis_pas_mis_a_null(db, capsys):
    """Une cle absente dit « pas su » ; une cle a null invite a la traiter
    comme une valeur."""
    corps = members_body(n=3)
    del corps["allianceId"]        # roster sans identifiant : rien n'est su
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, corps))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None)
    doc = json.loads(capsys.readouterr().out)
    assert "context" not in doc


# ---------------------------------------------------------------------------
# lw.camp.battle.user.score.rank -- classement d'evenement
# ---------------------------------------------------------------------------


def camp_body(n: int = 6, mine: str = "ORCA", autre: str = "VIPR") -> dict:
    from lwvs.wire import i32, i64
    liste = [
        {
            "rank": i32(i + 1),
            "uid": i64(1300000000000000 + i),
            "name": f"joueur{i}",
            "score": str(1_000_000 - i * 1000),   # CHAINE, comme sur le fil
            "serverId": i32(1234 if i % 2 == 0 else 1250),
            "abbr": mine if i % 2 == 0 else autre,
            "country": "fr",
        }
        for i in range(n)
    ]
    return {"list": liste, "self": {"rank": i32(1), "score": "1000000"}}


def test_camp_le_score_arrive_en_chaine_et_est_converti(db):
    from lwvs.messages import CMD_CAMP_RANK

    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=4)))), db)
    rows = db.fetch_camp_rank()
    assert len(rows) == 4
    assert rows[0]["score"] == 1_000_000
    assert isinstance(rows[0]["score"], int), "converti, pas laisse en texte"
    assert rows[0]["rank"] == 1


def test_camp_filtre_sur_l_abbr_faute_d_identifiant_d_alliance(db):
    """Ce message ne porte pas d'`aid` : le filtre se rabat sur `abbr`."""
    from lwvs import identity
    from lwvs.messages import CMD_CAMP_RANK

    identity.save(identity.Identity(alliance_id="peu-importe",
                                    alliance_abbr="ORCA", source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=6)))), db)

    tous = exporter.collect(db, "camp")
    mien = exporter.collect(db, "camp", alliance="mine")
    assert len(tous) == 6
    assert {r["alliance_abbr"] for r in mien} == {"ORCA"}
    assert len(mien) == 3


def test_camp_le_rang_est_renumerote_comme_les_autres_modes(db, capsys):
    """Une seule regle pour `rank` : deux semantiques sous un meme nom de champ
    seraient un piege pour l'aval."""
    from lwvs import identity
    from lwvs.messages import CMD_CAMP_RANK

    identity.save(identity.Identity(alliance_id="x", alliance_abbr="ORCA",
                                    source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=6)))), db)
    exporter.run_export(db, dataset="camp", fmt="records", out=None, alliance="mine")
    doc = json.loads(capsys.readouterr().out)

    assert doc["mode"] == "camp_battle_rank"
    # Le jeu donnait 1, 3, 5 pour ORCA : on renumerote sans trou.
    assert [r["rank"] for r in doc["records"]] == [1, 2, 3]
    assert [r["score"] for r in doc["records"]] == [1_000_000, 998_000, 996_000]


def test_la_memoire_est_acceptee_sur_l_abbr_quand_il_n_y_a_pas_d_identifiant(db):
    """Regression : une capture d'evenement ne contient aucun `alliance_id`.
    Le garde-fou d'anti-peremption doit accepter une correspondance par abbr,
    sinon il declare la memoire perimee alors que l'alliance est bien la."""
    from lwvs import identity
    from lwvs.messages import CMD_CAMP_RANK

    identity.save(identity.Identity(alliance_id="id-inconnu-ici",
                                    alliance_abbr="ORCA", source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=4)))), db)

    assert db.alliance_ids_present() == set(), "aucun identifiant dans ce message"
    own = exporter.resolve_own_alliance(db)
    assert own.source == "memoire"


# ---------------------------------------------------------------------------
# journee declaree vs journee mesuree
# ---------------------------------------------------------------------------


def test_une_journee_declaree_est_marquee_comme_telle(db, capsys):
    """Une affirmation humaine ne doit jamais passer pour une mesure."""
    from lwvs import identity
    from lwvs.messages import CMD_CAMP_RANK

    identity.save(identity.Identity(alliance_id="x", alliance_abbr="ORCA",
                                    source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=4)))), db)
    exporter.run_export(db, dataset="camp", fmt="records", out=None,
                        alliance="mine", declared_day=3,
                        event="S3 - Spice Wars")
    ctx = json.loads(capsys.readouterr().out)["context"]

    assert ctx["day"] == 3
    assert ctx["day_source"] == "declared"
    # L'identifiant est la cle ; le libelle n'est qu'un affichage.
    assert ctx["event"] == "s3_spice_wars"
    assert ctx["event_label"] == "S3 - Spice Wars"


def test_la_journee_du_message_prime_sur_la_declaration(db, capsys):
    """Le VS transmet la journee : elle fait foi contre ce qu'on annonce."""
    ingest(WireSource(full_capture(repeats=1, n=6)), db)   # jour 3 dans le message
    exporter.run_export(db, dataset="players", fmt="records", out=None,
                        scope="day", day=3, alliance="mine", declared_day=7)
    ctx = json.loads(capsys.readouterr().out)["context"]

    assert ctx["day"] == 3
    assert ctx["day_source"] == "message"


def test_grab_signale_un_desaccord_entre_message_et_declaration(db, tmp_path):
    from lwvs.grab import MEMORY_DB, grab

    result = grab(WireSource(full_capture(repeats=1, n=6)), out_dir=tmp_path / "o",
                  db_path=MEMORY_DB, declared_day=7)
    assert any("le message declare le jour 3" in c for c in result.conflicts)
    assert any("Le message fait foi" in c for c in result.conflicts)


def test_l_evenement_n_est_jamais_invente(db, capsys):
    """`event` n'existe pas dans le protocole : absent si non declare."""
    from lwvs import identity
    from lwvs.messages import CMD_CAMP_RANK

    identity.save(identity.Identity(alliance_id="x", alliance_abbr="ORCA",
                                    source="duel"))
    ingest(WireSource(stream_of(envelope(CMD_CAMP_RANK, camp_body(n=4)))), db)
    exporter.run_export(db, dataset="camp", fmt="records", out=None, alliance="mine")
    ctx = json.loads(capsys.readouterr().out).get("context", {})

    assert "event" not in ctx
    assert "day" not in ctx, "aucune journee n'est transmise par ce message"


# ---------------------------------------------------------------------------
# Points de don -- `al.rank` porte DEUX classements
# ---------------------------------------------------------------------------


def test_points_de_don_stockes_et_exportes(db, capsys):
    """`weeklyProgress` / `todayProgress` survivent jusqu'a l'export."""
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=5)))), db)
    exporter.run_export(db, dataset="members", fmt="json", out=None)
    rows = json.loads(capsys.readouterr().out)

    par_uid = {r["uid"]: r for r in rows}
    assert {r["weekly_progress"] for r in rows} == {0, 77, 154, 231, 308}
    assert {r["today_progress"] for r in rows} == {0, 11, 22, 33, 44}
    # 0 don cette semaine et 0 point vont ensemble -- c'est ce qui distingue
    # "n'a pas donne" de "pas mesure", qu'un score a 0 ne dit pas.
    zero = [r for r in par_uid.values() if r["weekly_progress"] == 0]
    assert [r["weekly_donate_time"] for r in zero] == [0]
    assert all(r["donate_time"] for r in rows)


def test_le_classement_des_dons_n_est_pas_celui_des_kills(db, capsys):
    """Meme message, meme lignes, ORDRE INVERSE.

    C'est le seul test qui distingue "le flux lit la bonne colonne" de "le flux
    lit une colonne" : dans la fixture, les dons croissent quand les kills
    decroissent.
    """
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=6)))), db)

    exporter.run_export(db, dataset="members", fmt="records", out=None)
    kills = json.loads(capsys.readouterr().out)
    exporter.run_export(db, dataset="members", fmt="records", out=None,
                        metric="weekly_progress")
    dons = json.loads(capsys.readouterr().out)

    assert kills["mode"] == "kill_rank"
    assert dons["mode"] == "donation_weekly_rank"
    assert [r["uid"] for r in dons["records"]] == \
        [r["uid"] for r in reversed(kills["records"])]
    scores = [r["score"] for r in dons["records"]]
    assert scores == sorted(scores, reverse=True)
    assert [r["rank"] for r in dons["records"]] == [1, 2, 3, 4, 5, 6]


def test_metrique_du_jour_a_son_propre_mode(db, capsys):
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=4)))), db)
    exporter.run_export(db, dataset="members", fmt="records", out=None,
                        metric="today_progress")
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "donation_daily_rank"
    assert payload["records"][0]["score"] == 33


def test_une_colonne_de_score_inconnue_est_refusee(db):
    """Sans ce refus, l'export sortirait N records a `score: null` sans lever."""
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=3)))), db)
    with pytest.raises(ValueError, match="colonne de score inconnue"):
        exporter.run_export(db, dataset="members", fmt="records", out=None,
                            metric="weeklyProgress")


def test_le_thp_vient_du_classement_serveur_pas_du_roster(db, capsys):
    """LE FAUX AMI. `al.rank` porte `power`, `rank.get` porte `heroPower`, et
    ce ne sont PAS le meme chiffre : sur donnees reelles leur rapport va de
    0.512 a 0.749 SELON LE JOUEUR. Le THP affiche a l'ecran est `heroPower`.

    Un flux `thp_rank` alimente par `power` publierait donc une autre metrique
    sous le nom du THP, sans que rien ne leve."""
    thp = exporter.FEEDS_BY_KEY["thp"]
    assert thp.command == "rank.get"
    assert thp.dataset == "server"
    assert thp.metric == "hero_power"
    assert thp.metric in exporter.COLUMNS[thp.dataset]
    # `power` ne doit alimenter AUCUN mode : on n'exporte pas une metrique
    # qu'on ne sait pas nommer.
    assert ("members", "power") not in exporter._METRIC_MODES
    ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=3)))), db)
    with pytest.raises(ValueError, match="colonne de score inconnue"):
        exporter.run_export(db, dataset="members", fmt="records", out=None,
                            metric="power")


def test_le_thp_se_classe_sur_hero_power(db, capsys):
    ingest(WireSource(stream_of(envelope(CMD_SERVER_RANK, server_rank_body(n=4)))), db)
    exporter.run_export(db, dataset="server", fmt="records", out=None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "thp_rank"
    assert [r["score"] for r in payload["records"]] == [
        160_000_000, 159_000_000, 158_000_000, 157_000_000]
    assert [r["rank"] for r in payload["records"]] == [1, 2, 3, 4]


def test_le_rang_du_classement_serveur_est_positionnel(db):
    """Aucune ligne ne porte de champ `rank` : l'ordre du tableau EST le rang.
    Le deduire d'un champ absent rendrait des rangs tous a None."""
    ingest(WireSource(stream_of(envelope(CMD_SERVER_RANK, server_rank_body(n=6)))), db)
    rows = db.fetch_server_rank()
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5, 6]
    scores = [r["hero_power"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_un_joueur_sans_alliance_ne_casse_pas_le_filtre(db):
    """5 joueurs sur 200 n'ont pas d'alliance en capture reelle. Le filtre doit
    les ecarter, pas trebucher sur la cle absente."""
    ingest(WireSource(stream_of(envelope(CMD_SERVER_RANK, server_rank_body(n=8)))), db)
    tous = db.fetch_server_rank()
    assert sum(1 for r in tous if r["alliance_id"] is None) == 2
    filtres = db.fetch_server_rank(alliance_ids=["9000000000000001"])
    assert filtres, "le filtre par identifiant doit rendre des lignes"
    assert all(r["alliance_id"] == "9000000000000001" for r in filtres)


def test_chaque_flux_a_un_mode_distinct():
    """Deux flux qui partageraient un `mode` empileraient deux metriques
    differentes dans la meme serie cote site."""
    modes = [f.mode for f in exporter.FEEDS]
    assert len(set(modes)) == len(modes)


def test_les_flux_dons_lisent_al_rank(db):
    dons = [f for f in exporter.FEEDS if f.key.startswith("dons_")]
    assert {f.metric for f in dons} == {"weekly_progress", "today_progress"}
    for feed in dons:
        assert feed.command == "al.rank"
        # Le roster EST ton alliance : lui appliquer le filtre `mine` le ferait
        # echouer sur une capture sans ecran Duel, pour rien.
        assert feed.inherently_mine
        assert feed.metric in exporter.COLUMNS[feed.dataset]


def test_une_base_anterieure_gagne_les_colonnes_de_don(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` ne touche pas une table existante : sans le
    rattrapage, toute ecriture dans une base d'avant echouerait."""
    import sqlite3

    path = tmp_path / "vieille.sqlite3"
    with Store(path) as store:
        for column in ("donate_time", "weekly_donate_time"):
            store.conn.execute(f"ALTER TABLE member_rows DROP COLUMN {column}")
        store.conn.commit()
    with sqlite3.connect(path) as raw:
        avant = {r[1] for r in raw.execute("PRAGMA table_info(member_rows)")}
    assert "donate_time" not in avant

    with Store(path) as store:          # reouverture = migration
        ingest(WireSource(stream_of(envelope(CMD_MEMBERS, members_body(n=3)))), store)
        rows = store.fetch_members()
    assert len(rows) == 3
    assert all(r["donate_time"] for r in rows)
