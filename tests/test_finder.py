"""`find` : retrouver a quel champ correspond un chiffre vu a l'ecran."""

from __future__ import annotations

from lwvs import wire
from lwvs.finder import find_values, render_find
from lwvs.messages import CMD_MEMBERS, CMD_RANK

from .helpers import (
    ListSource, WireSource, envelope, envelope_raw, members_body, rank_body, raw_map,
)


def stream_of(*payloads: bytes) -> bytes:
    return b"".join(wire.encode_frame(p, compress=True) for p in payloads)


def test_find_nomme_la_commande_et_le_chemin():
    """Le geste qui ferme la boucle : je lis 100000 a l'ecran, l'outil me dit
    quel champ le porte."""
    stream = stream_of(
        envelope(CMD_RANK, rank_body(n=5, day=1)),      # top score = 100000
        envelope(CMD_MEMBERS, members_body(n=3)),       # top armyKill = 100000
    )
    report = find_values(WireSource(stream), numbers=[100000])

    chemins = set(report.by_path)
    assert f"{CMD_RANK}  p.rankInfo[].score" in chemins
    assert f"{CMD_MEMBERS}  p.list[].armyKill" in chemins
    assert not report.missing


def test_find_cherche_aussi_un_pseudo():
    # `plain` est le 5e pseudo du jeu de fixtures : il faut au moins 5 joueurs.
    stream = stream_of(envelope(CMD_RANK, rank_body(n=6, day=1)))
    report = find_values(WireSource(stream), texts=["plain"])
    assert any("p.rankInfo[].name" in p for p in report.by_path)


def test_find_repere_la_valeur_dans_un_payload_qui_ne_decode_pas():
    """Un payload illisible est invisible a la recherche sur objets decodes --
    et c'est justement celui qu'il faut reperer."""
    import struct

    valeur = 32170354
    illisible = envelope_raw(
        "al.season2.rank.info",
        raw_map([("x", b"\x06" + struct.pack(">i", valeur))]),   # 0x06 = non decodable
    )
    report = find_values(ListSource([illisible]), numbers=[valeur])

    assert report.stats.failed == 1
    assert not report.by_path, "aucun objet decode ne peut le porter"
    assert report.raw_hits and report.raw_hits[0].needle == valeur
    assert report.raw_hits[0].width == 4

    texte = render_find(report)
    assert "OCTETS" in texte


def test_find_dit_clairement_quand_il_ne_trouve_pas():
    stream = stream_of(envelope(CMD_RANK, rank_body(n=3, day=1)))
    report = find_values(WireSource(stream), numbers=[999_999_999])
    assert report.missing == [999_999_999]
    texte = render_find(report)
    assert "NON trouve" in texte
    # Ne tranche pas entre les causes possibles.
    assert "rien ne les departage" in texte


def test_find_trouve_un_score_transmis_en_chaine():
    """Le classement d'evenement transmet `score` en CHAINE. Chercher le
    nombre lu a l'ecran ne doit pas rendre « NON trouve » sur un message
    parfaitement decode -- c'est ce qui envoyait relire le hex pour rien."""
    from lwvs.messages import CMD_CAMP_RANK
    from lwvs.wire import i32, i64

    corps = {
        "list": [{"rank": i32(1), "uid": i64(1234567890001234),
                  "name": "NomadeX", "score": "27539623", "abbr": "ORCA"}],
        "self": {"rank": i32(1), "score": "27539623"},
    }
    report = find_values(WireSource(stream_of(envelope(CMD_CAMP_RANK, corps))),
                         numbers=[27539623])

    assert f"{CMD_CAMP_RANK}  p.list[].score" in set(report.by_path)
    assert not report.missing
    # Et le nombre est retrouve meme ecrit comme a l'ecran.
    assert any(h.needle == 27539623 and h.value == "27539623" for h in report.hits)


def test_find_repere_les_chiffres_ascii_dans_un_payload_illisible():
    """Meme illisible, un payload qui porte le score en texte doit se
    signaler : les motifs int32/int64 seuls ne l'attrapent pas."""
    valeur = 27539623
    illisible = envelope_raw(
        "lw.camp.battle.vs.info",
        raw_map([("x", b"\x0c" + str(valeur).encode("ascii"))]),   # 0x0C = non decodable
    )
    report = find_values(ListSource([illisible]), numbers=[valeur])

    assert report.stats.failed == 1
    textuels = [h for h in report.raw_hits if h.encoding == "texte"]
    assert textuels and textuels[0].needle == valeur
    assert "texte" in render_find(report)
