"""`inspect` (cartographie) et `probe` (sondage d'octets de type)."""

from __future__ import annotations

from lwvs import inspection, probe, wire
from lwvs.messages import CMD_RANK
from lwvs.pipeline import PayloadSaver
from lwvs.wire import i8, i16

from .helpers import ListSource, WireSource, envelope, envelope_raw, rank_body, raw_map


def stream_of(*payloads: bytes) -> bytes:
    return b"".join(wire.encode_frame(p, compress=True) for p in payloads)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_inspect_liste_les_commandes_et_les_chemins():
    stream = stream_of(
        envelope(CMD_RANK, rank_body(n=3, day=2)),
        envelope("get.king.info", {"kingId": i16(4)}),
    )
    report = inspection.inspect_source(WireSource(stream))
    assert set(report.commands) == {CMD_RANK, "get.king.info"}

    paths = report.shapes[CMD_RANK].paths
    assert "p.rankInfo[].score" in paths
    assert "p.rankInfo[].abbr" in paths
    assert "p.day" in paths
    assert paths["p.rankInfo[].score"].most_common(1)[0][0] == "int"

    text = inspection.render_report(report)
    assert "index du protocole" in text
    assert CMD_RANK in text


def test_inspect_values_est_opt_in():
    stream = stream_of(envelope(CMD_RANK, rank_body(n=2, day=1)))
    report = inspection.inspect_source(WireSource(stream))
    sans = inspection.render_report(report, show_values=False)
    avec = inspection.render_report(report, show_values=True)
    assert "ex:" not in sans          # pas de donnees joueur par defaut
    assert "ex:" in avec              # ferme la boucle avec l'ecran


def test_inspect_filtres_reduisent_l_affichage_sans_rien_nommer():
    stream = stream_of(
        envelope(CMD_RANK, rank_body(n=2, day=1)),
        envelope("get.king.info", {"kingId": i16(4)}),
    )
    report = inspection.inspect_source(WireSource(stream), command_filter="rank")
    assert set(report.shapes) == {CMD_RANK}
    # Le filtre de commande ne touche pas l'index du protocole.
    assert "get.king.info" in report.commands

    text = inspection.render_report(report, key_filter="SCORE")
    assert "p.rankInfo[].score" in text
    assert "p.rankInfo[].abbr" not in text


def test_inspect_compte_les_echecs_par_octet_de_type_fautif():
    # 0x0D, encore inconnu. Ce test citait 0x07 avant qu'il soit etabli comme un
    # double : il faut un octet REELLEMENT non supporte, sinon il mesure une
    # troncature au lieu de l'octet de type fautif.
    good = envelope(CMD_RANK, rank_body(n=2, day=1))
    bad = envelope_raw("mystery.cmd", raw_map([("x", b"\x0d\x01\x02\x03")]))
    report = inspection.inspect_source(ListSource([good, bad, bad]))
    assert report.stats.decoded == 1
    assert report.stats.failed == 2
    assert report.stats.failures_by_tag[0x0D] == 2

    text = inspection.render_report(report)
    assert "0x0d" in text
    assert "INVISIBLES" in text


def test_saver_garde_les_echecs_quel_que_soit_le_filtre(tmp_path):
    good = envelope(CMD_RANK, rank_body(n=2, day=1))
    bad = envelope_raw("mystery.cmd", raw_map([("x", b"\x0d\x01")]))
    saver = PayloadSaver(tmp_path)
    try:
        # Filtre de commande actif : il ne doit rien retirer du disque.
        inspection.inspect_source(ListSource([good, bad]), command_filter="zzz",
                                  saver=saver)
    finally:
        saver.close()

    ok = list((tmp_path / "ok").glob("*.bin"))
    failed = list((tmp_path / "failed").glob("*.bin"))
    assert len(ok) == 1 and len(failed) == 1
    assert failed[0].read_bytes() == bad
    manifest = (tmp_path / "manifest.tsv").read_text(encoding="utf-8")
    assert "failed/" in manifest and "0x0d" in manifest


def test_reingestion_d_un_repertoire_sauvegarde(tmp_path):
    from lwvs.capture import DirSource

    stream = stream_of(envelope(CMD_RANK, rank_body(n=4, day=2)))
    saver = PayloadSaver(tmp_path)
    try:
        inspection.inspect_source(WireSource(stream), saver=saver)
    finally:
        saver.close()

    again = inspection.inspect_source(DirSource(tmp_path))
    assert again.commands[CMD_RANK] == 1
    assert again.stats.exact == 1


def test_garde_fou_exact_alerte_quand_il_reste_des_octets():
    payload = envelope(CMD_RANK, rank_body(n=2, day=1)) + b"\x99\x99"
    report = inspection.inspect_source(ListSource([payload]))
    assert report.stats.exact == 0
    assert "ALERTE" in report.stats.guardrail()
    assert "Tu ne decodes pas ce que tu crois" in report.stats.guardrail()


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def unknown_payload(tag: int, body: bytes) -> bytes:
    return envelope_raw("mystery.cmd", raw_map([("x", bytes([tag]) + body)]))


def test_probe_trouve_le_layout_qui_finit_exactement():
    good = [envelope(CMD_RANK, rank_body(n=3, day=d)) for d in (1, 2, 3)]
    bad = [unknown_payload(0x06, b"\xde\xad"), unknown_payload(0x06, b"\x12\x34")]
    result = probe.probe_source(ListSource(good + bad), tags=[0x06])

    assert result.aborted is None
    assert result.control_ok is True
    assert result.control_exact == 3 and result.control_total == 3
    assert result.blocking_tags == [0x06]

    verdicts = result.determinacy()
    assert verdicts[0x06][0] == "determine"
    assert verdicts[0x06][1] == ["fixed:2"]

    text = probe.render_probe(result)
    assert "groupe de controle" in text
    assert "PROPOSITION" in text
    assert "on ne classe pas par distance" in text.lower()


def test_probe_demasque_les_payloads_caches_par_la_racine_gloutonne():
    """Le mode d'echec silencieux de §1.6, teste de bout en bout.

    Le payload porte un octet de type refuse, mais une map interne decode
    quand meme dans les 64 premiers octets : la regle gloutonne rend cette
    racine bidon et le payload passe pour un succes. Restreindre la racine aux
    seuls offsets d'ancrage (releves sur les payloads EXACTS) le demasque.
    """
    decoy = wire.encode_value([{"uid": wire.i64(1000000000000000 + i)} for i in range(3)])
    hidden = envelope_raw(
        "vs.kill.rank.info",
        raw_map([("rankInfo", decoy), ("x", b"\x06\xaa\xbb")]),
    )

    # Sans ancrage, le payload "decode" -- mais pas jusqu'au dernier octet.
    sournois = wire.decode_payload(hidden)
    assert sournois.root_offset > 0
    assert sournois.exact is False

    good = [envelope(CMD_RANK, rank_body(n=3, day=d)) for d in (1, 2)]
    result = probe.probe_source(ListSource(good + [hidden] * 3), tags=[0x06])

    assert result.aborted is None
    assert result.unmasked == 3          # trois payloads sortis de l'ombre
    assert result.anchors == [0]         # l'offset 62 bidon n'est PAS un ancrage
    assert result.control_exact == 2 and result.control_total == 2
    assert result.determinacy()[0x06] == ("determine", ["fixed:2"])
    assert "racine bidon" in probe.render_probe(result)


def test_probe_rapporte_l_ambiguite_au_lieu_de_trancher():
    """Un octet de type dont le corps est vide est compatible avec plusieurs
    layouts : fixed:0, len:u8 (longueur 0)... Ce n'est pas a trancher a pile
    ou face."""
    good = [envelope(CMD_RANK, rank_body(n=2, day=1))]
    bad = [envelope_raw("mystery.cmd", raw_map([("x", b"\x06\x00"), ("y", wire.encode_value(i8(1)))]))]
    result = probe.probe_source(ListSource(good + bad), tags=[0x06])
    assert result.aborted is None
    verdict, names = result.determinacy()[0x06]
    assert verdict == "indetermine"
    assert len(names) > 1
    assert "INDETERMINE" in probe.render_probe(result)


def test_probe_s_arrete_sans_ancrage():
    bad = [unknown_payload(0x0C, b"\x01")]
    result = probe.probe_source(ListSource(bad), tags=[0x0C])
    assert result.aborted is not None
    assert "ancrage" in result.aborted or "offset d'ancrage" in result.aborted
    assert "racines bidons" in result.aborted


def test_probe_refuse_de_classer_si_le_groupe_de_controle_echoue():
    """Si les payloads qui decodent deja ne finissent pas exactement, le
    critere ne prouve rien sur les echecs non plus."""
    good_but_trailing = [envelope(CMD_RANK, rank_body(n=2, day=1)) + b"\x99"] * 3
    bad = [unknown_payload(0x06, b"\x01\x02")]
    result = probe.probe_source(ListSource(good_but_trailing + bad), tags=[0x06])
    assert result.control_ok is False
    assert result.aborted is not None
    assert "ne prouve" in result.aborted
    assert "INVALIDE" in probe.render_probe(result)


def test_probe_refuse_un_espace_de_recherche_trop_grand():
    good = [envelope(CMD_RANK, rank_body(n=2, day=1))]
    bad = [unknown_payload(t, b"\x01\x02") for t in (0x06, 0x07, 0x0C, 0x0D)]
    result = probe.probe_source(ListSource(good + bad), max_combos=100)
    assert result.aborted is not None
    assert "trop grand" in result.aborted
    assert "--tag" in result.aborted


def test_probe_ne_touche_jamais_la_table_de_production():
    good = [envelope(CMD_RANK, rank_body(n=2, day=1))]
    bad = [unknown_payload(0x06, b"\xaa\xbb")]
    probe.probe_source(ListSource(good + bad), tags=[0x06])
    assert wire.DEFAULT_TABLE.extra == {}
    assert 0x06 not in wire.KNOWN_TAGS


def test_probe_restreint_la_racine_aux_offsets_d_ancrage():
    header = wire.encode_value({"hd": i8(1)})
    good = [header + envelope(CMD_RANK, rank_body(n=2, day=1))]
    result = probe.probe_source(ListSource(good), tags=[0x06])
    assert result.anchors == [len(header)]


# ---------------------------------------------------------------------------
# decouverte de port
# ---------------------------------------------------------------------------


def test_scoring_de_port_applique_le_vrai_decodeur():
    """On ne matche ni une plage de ports ni un nom de processus : on fait
    tourner le decodeur. Si les octets se decoupent en trames valides, c'est
    le bon canal."""
    from lwvs.capture import PortScorer

    game = stream_of(
        envelope(CMD_RANK, rank_body(n=5, day=2)),
        envelope("get.alliance.duel.group.info", {"groupInfos": []}),
    )
    scorer = PortScorer()
    # Le canal du jeu, en segments desalignes.
    for i in range(0, len(game), 300):
        scorer.feed(41234, 55001, "0", game[i:i + 300])
    # Un port bavard qui ne porte pas de trames.
    for _ in range(40):
        scorer.feed(9100, 55002, "1", b"\x17\x03\x03\x00\x20" + b"\xa5" * 32)

    rep = scorer.finish()
    assert rep.total_packets > 0
    assert rep.scores[0].port == 41234
    assert rep.scores[0].exact == 2
    assert CMD_RANK in rep.scores[0].commands
    noise = [s for s in rep.scores if s.port == 9100][0]
    assert noise.exact == 0
    assert rep.diagnosis(keep_web=False) is None


def test_le_trafic_web_est_compte_pas_jete():
    """Sinon "tout est sur 443" est indiscernable de "aucun trafic"."""
    from lwvs.capture import PortScorer

    scorer = PortScorer()
    for _ in range(12):
        scorer.feed(443, 55003, "0", b"\x16\x03\x01" + b"\x00" * 40)
    rep = scorer.finish()
    assert rep.total_packets == 12
    assert rep.web_packets == 12
    assert rep.scores == []
    problem = rep.diagnosis(keep_web=False)
    assert "tous sur des ports web" in problem
    assert "--keep-web" in problem


def test_interface_muette_est_nommee_comme_telle():
    from lwvs.capture import PortScorer

    rep = PortScorer().finish()
    problem = rep.diagnosis(keep_web=False)
    assert "l'interface elle-meme ne voit rien" in problem
    assert "ifaces --probe" in problem


def test_erreur_tshark_ne_ressemble_pas_a_un_reseau_calme():
    from lwvs.capture import PortScorer

    scorer = PortScorer()
    rep = scorer.finish()
    rep.tshark_error = "The capture session could not be initiated"
    problem = rep.diagnosis(keep_web=False)
    assert problem.startswith("tshark a echoue")


def test_un_incident_de_relecture_n_infirme_pas_un_scoring_reussi():
    """Regression : un message stderr de phase 2 rendait un verdict d'echec
    alors que des ports avaient ete scores. Les octets deja decodes l'ont ete
    pour de bon ; un pcap tronque se lit jusqu'au bloc fautif."""
    from lwvs.capture import PortScorer

    scorer = PortScorer()
    game = stream_of(envelope(CMD_RANK, rank_body(n=4, day=1)))
    scorer.feed(18731, 53881, "0", game)
    rep = scorer.finish()
    assert rep.scores and rep.scores[0].exact == 1

    rep.warnings.append("relecture partielle du pcap (636 paquet(s) lus) : damaged")
    rep.tshark_error = "The file appears to be damaged or corrupt"
    assert rep.diagnosis(keep_web=False) is None, (
        "un incident de relecture ne doit pas infirmer des ports deja scores"
    )


def test_echec_de_capture_reste_un_echec_quand_rien_n_est_score():
    from lwvs.capture import PortScorer

    rep = PortScorer().finish()
    rep.tshark_error = "The capture session could not be initiated"
    assert rep.diagnosis(keep_web=False).startswith("tshark a echoue")


# ---------------------------------------------------------------------------
# detection (interface + port en une passe)
# ---------------------------------------------------------------------------


def _tshark_lines(port: int, data: bytes, stream: str = "0", chunk: int = 300):
    """Le format que tshark rend en `-T fields` : stream/src/dst/payload hexa."""
    return ["\t".join((stream, str(port), "55001", data[i:i + chunk].hex()))
            for i in range(0, len(data), chunk)]


def test_la_detection_s_arrete_des_que_c_est_prouve():
    """Le point de tout l'exercice : ne pas aller au bout du chronometre.

    Une trame qui decode jusqu'a son dernier octet prouve l'interface ET le
    port. Continuer d'ecouter apres la preuve, c'est faire attendre pour rien.
    """
    import threading

    from lwvs.capture import PortScorer, _score_stream

    game = stream_of(
        envelope(CMD_RANK, rank_body(n=5, day=2)),
        envelope(CMD_RANK, rank_body(n=5, day=3)),
        envelope("get.alliance.duel.group.info", {"groupInfos": []}),
    )
    lines = _tshark_lines(41234, game)
    # De quoi ecouter bien plus longtemps si personne n'arrete rien.
    lines += ["\t".join(("1", "9100", "55002", (b"\xa5" * 200).hex()))] * 500

    read: list[str] = []

    def served():
        for line in lines:
            read.append(line)
            yield line

    stop = threading.Event()
    scorer = PortScorer()
    _score_stream(served(), scorer, stop, min_exact=2)

    assert stop.is_set(), "la preuve etait la, l'ecoute aurait du s'arreter"
    assert len(read) < len(lines), "le flux a ete lu jusqu'au bout malgre la preuve"
    top = scorer.leader()
    assert top is not None and top.port == 41234 and top.exact >= 2


def test_une_seule_trame_exacte_ne_suffit_pas_a_conclure():
    """Un protocole voisin peut se decouper juste une fois par hasard. Deux
    trames exactes sur le meme port, non : c'est le seuil."""
    import threading

    from lwvs.capture import PortScorer, _score_stream

    lines = _tshark_lines(41234, stream_of(envelope(CMD_RANK, rank_body(n=3, day=1))))
    stop = threading.Event()
    scorer = PortScorer()
    _score_stream(iter(lines), scorer, stop, min_exact=2)

    assert not stop.is_set()
    assert scorer.leader().exact == 1


def _attempt(device: str, packets: list[tuple[int, bytes]]):
    from lwvs.capture import Attempt, Interface, PortScorer

    scorer = PortScorer()
    for i, (port, data) in enumerate(packets):
        scorer.feed(port, 55000 + i, str(i), data)
    return Attempt(Interface("1", device, device.upper()), scorer.finish())


def test_la_detection_nomme_les_trois_pannes():
    """« rien trouve » recouvre trois pannes qui ne se reparent pas au meme
    endroit : pas de droits de capture, jeu ferme, tout en TLS."""
    from lwvs.capture import Detection

    muet = Detection(attempts=[_attempt("eth0", [])])
    assert "droits" in muet.diagnosis() and "Npcap" in muet.diagnosis()

    web = Detection(attempts=[_attempt("eth0", [(443, b"\x16\x03\x01" + b"\x00" * 40)])])
    assert "ports web" in web.diagnosis()

    bruit = _attempt("eth0", [(9100, b"\xa5" * 60)])
    ferme = Detection(iface=bruit.iface, report=bruit.report, attempts=[bruit])
    assert "Le jeu tournait-il" in ferme.diagnosis()

    trouve = Detection(iface=bruit.iface, port=41234, report=bruit.report,
                       attempts=[bruit])
    assert trouve.diagnosis() is None and trouve.found
