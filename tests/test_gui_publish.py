"""Cablage du bouton « Envoyer maintenant » de la GUI.

Teste le vrai chemin : App -> thread de travail -> HTTP -> file de messages,
contre un serveur local qui recoit reellement. Le transport lui-meme est
couvert par test_publish.py ; ici on verifie que la GUI l'appelle bien avec ce
qu'il faut et ne touche aucun widget hors du thread UI.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lwvs import wire
from lwvs.ingest import ingest
from lwvs.messages import (CMD_GROUP, CMD_MEMBERS, CMD_RANK, CMD_SEASON,
                           CMD_SERVER_RANK)
from lwvs.store import Store

from .helpers import (
    WireSource, envelope, group_body, members_body, rank_body, season_body,
    server_rank_body,
)

tk = pytest.importorskip("tkinter")


class _Sink(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):                       # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append({"headers": dict(self.headers), "raw": raw})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"stored","applied":3}')

    def log_message(self, *args):
        pass


@pytest.fixture
def sink():
    _Sink.received = []
    httpd = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/api/rankings"
    httpd.shutdown()
    httpd.server_close()


def _capture() -> bytes:
    payloads = [
        envelope(CMD_RANK, rank_body(n=6, day=1)),
        envelope(CMD_RANK, rank_body(n=6, day=None)),
        envelope(CMD_GROUP, group_body(16)),
        envelope(CMD_SEASON, season_body(position=3)),
        envelope(CMD_MEMBERS, members_body(n=5)),
        envelope(CMD_SERVER_RANK, server_rank_body(n=6)),
    ]
    return b"".join(wire.encode_frame(p, compress=True) for p in payloads)


def _pump(app, deadline=15.0, expected=3):
    """Draine la file comme le fait la boucle Tk, sans lancer mainloop."""
    end = time.time() + deadline
    while time.time() < end:
        app.root.update()
        app._drain()
        if any(m.startswith("  ") for m in _logged(app)):
            if sum(1 for m in _logged(app) if " HTTP " in m) >= expected:
                return
        time.sleep(0.05)


def _logged(app) -> list[str]:
    return app.log.get("1.0", "end").splitlines()


def test_le_bouton_envoyer_poste_chaque_classement(sink, root, tmp_path):
    db = tmp_path / "t.sqlite3"
    with Store(db) as store:
        ingest(WireSource(_capture()), store)

    app = root.build(str(db), keep=True)
    app.url_var.set(sink)
    app.token_var.set("jeton-de-test")
    app._refresh_feeds()
    app._publish_all()
    _pump(app, expected=6)

    assert len(_Sink.received) == 6, "un POST par classement pret"
    modes = {json.loads(r["raw"].decode("utf-8"))["mode"] for r in _Sink.received}
    # Un mode DISTINCT par metrique, y compris pour les QUATRE classements que
    # `al.rank` alimente a lui seul : cote site, un mode partage empilerait des
    # points de don, des kills et des puissances dans la meme serie.
    assert modes == {"kill_rank", "donation_weekly_rank", "donation_daily_rank",
                     "thp_rank", "weekly_rank", "daily_rank"}
    for r in _Sink.received:
        assert r["headers"]["Authorization"] == "Bearer jeton-de-test"
        assert r["headers"]["Idempotency-Key"]
    assert any("HTTP 200" in line for line in _logged(app))


def test_le_jeton_n_est_pas_ecrit_sauf_demande_explicite(sink, root, tmp_path):
    """Un jeton en clair dans un fichier est un vrai risque : opt-in."""
    from lwvs.gui import _load_prefs

    db = tmp_path / "t.sqlite3"
    with Store(db) as store:
        ingest(WireSource(_capture()), store)

    app = root.build(str(db), keep=True)
    app.url_var.set(sink)
    app.token_var.set("tres-secret")

    app.keep_token_var.set(False)
    app._remember_send()
    prefs = _load_prefs(str(db))
    assert prefs["post_url"] == sink
    assert "post_token" not in prefs

    app.keep_token_var.set(True)
    app._remember_send()
    assert _load_prefs(str(db))["post_token"] == "tres-secret"


def test_aucune_variable_tk_n_est_lue_hors_du_thread_ui(root, tmp_path, monkeypatch):
    """Regression : `_payload` et le worker de capture lisaient des variables Tk
    depuis un thread de travail, ce qui leve « main thread is not in main loop ».

    Le garde-fou : depuis un autre thread, toute lecture d'une variable Tk doit
    exploser. On rend ces lectures fatales et on verifie que le chemin de
    publication n'en fait aucune.
    """
    db = tmp_path / "t.sqlite3"
    with Store(db) as store:
        ingest(WireSource(_capture()), store)

    app = root.build(str(db), keep=True)
    app._refresh_feeds()

    ui_thread = threading.get_ident()
    boom: list[str] = []
    original = tk.BooleanVar.get

    def guarded(self):
        if threading.get_ident() != ui_thread:
            boom.append("lecture d'une variable Tk hors du thread UI")
        return original(self)

    monkeypatch.setattr(tk.BooleanVar, "get", guarded)

    jobs = [(v["feed"], v["day"], app._alliance_for(v["feed"]))
            for v in app._rows.values() if v["count"]]
    assert jobs

    def work():
        with Store(db) as store:
            for feed, day, alliance in jobs:
                app._payload(store, feed, day, 1, alliance)

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert not boom, boom[0]


def test_le_tableau_separe_jamais_recu_de_zero_ligne(root, tmp_path):
    """« aucune ligne » disait la meme chose pour un message que le jeu n'a
    jamais envoye et pour un message recu dont rien n'est sorti. Le premier se
    repare en rouvrant l'ecran, le second est un defaut de decodage."""
    db = tmp_path / "t.sqlite3"
    with Store(db) as store:
        ingest(WireSource(_capture()), store)      # sans classement d'evenement

    app = root.build(str(db), keep=True)
    app._seen = {"al.rank": 1}                     # une capture a tourne
    app._refresh_feeds()

    assert "jamais reçu" in app.tree.set("feed:camp_battle", "etat")
    assert app.tree.set("feed:kills", "etat").endswith("prêt")

    # Recu et pourtant vide : la ou il faut vraiment regarder.
    app._seen = {"lw.camp.battle.user.score.rank": 1}
    app._refresh_feeds()
    assert "à signaler" in app.tree.set("feed:camp_battle", "etat")


def test_un_seul_bouton_remplit_l_interface_ET_le_port(root, tmp_path):
    """Les deux champs se remplissent du meme signal, donc du meme clic : une
    trame du jeu decodee prouve a la fois l'interface et le port."""
    from lwvs.capture import Attempt, Detection, Interface, PortScorer
    from lwvs.gui import Msg

    app = root.build(str(tmp_path / "t.sqlite3"), keep=True)
    assert app.detect_btn is not None
    assert not hasattr(app, "_detect_port"), "un seul chemin de detection"

    device = "\\Device\\NPF_{ABC}"
    app._handle(Msg("ifaces", [Interface("3", device, "Wi-Fi")]))

    scorer = PortScorer()
    scorer.feed(41234, 55001, "0", _capture())
    att = Attempt(Interface("3", device, "Wi-Fi"), scorer.finish())
    app._detected(Detection(iface=att.iface, port=41234, report=att.report,
                            attempts=[att], seconds=2.4))

    assert app.port_var.get() == "41234"
    assert app.iface_var.get() == "Wi-Fi", "l'oeil lit le nom usuel"
    assert app._iface_device() == device, "tshark recoit le device"

    # Sans preuve, rien n'est ecrit : un port devine vaut moins que rien.
    muet = Detection(attempts=[Attempt(att.iface, PortScorer().finish())])
    app._detected(muet)
    assert app.port_var.get() == "41234"
    assert any("droits de capture" in line for line in _logged(app))


def test_la_liste_montre_les_noms_usuels_et_rend_les_devices(root, tmp_path):
    """Deroulee, la liste ne montrait que des GUID tronques, indiscernables a
    l'oeil. Le nom usuel s'affiche, le device reste la valeur reelle."""
    from lwvs.capture import Interface
    from lwvs.gui import Msg, _iface_labels

    wifi = "\\Device\\NPF_{9AC50FC4-0B0A-4370-92FE-712BDD494145}"
    eth = "\\Device\\NPF_{9485472E-585D-42B7-B15D-A3024EB71305}"
    homonyme = "\\Device\\NPF_{84F954C6-A63A-4C16-8120-FE04134CF189}"
    labels = _iface_labels([
        Interface("1", wifi, "Wi-Fi"),
        Interface("2", eth, "Ethernet"),
        Interface("3", homonyme, "Ethernet"),          # meme nom, autre lien
        Interface("4", "\\Device\\NPF_Loopback", ""),  # tshark ne nomme pas tout
    ])
    assert list(labels)[:2] == ["Wi-Fi", "Ethernet"]
    assert labels["Wi-Fi"] == wifi
    # Deux homonymes restent distincts : les confondre ferait capturer sur la
    # mauvaise interface sans que rien ne le dise.
    assert len(labels) == 4
    assert labels["Ethernet  ·  84F954C6"] == homonyme
    assert labels["Loopback"] == "\\Device\\NPF_Loopback"

    app = root.build(str(tmp_path / "t.sqlite3"), keep=True)
    app._handle(Msg("ifaces", [Interface("1", wifi, "Wi-Fi"),
                               Interface("2", eth, "Ethernet")]))
    assert app.iface_box.cget("values") == ("Wi-Fi", "Ethernet")
    app._set_iface(eth)
    assert app.iface_var.get() == "Ethernet"
    assert app._iface_device() == eth
