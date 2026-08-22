"""Envoi HTTP : teste contre un vrai serveur local, pas contre un mock.

Un mock de `urlopen` validerait mon appel a mon propre mock. Un serveur qui
recoit vraiment la requete valide l'en-tete, le corps et l'encodage.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lwvs import exporter, wire
from lwvs.grab import MEMORY_DB, grab
from lwvs.messages import CMD_MEMBERS
from lwvs.publish import body_of, idempotency_key, publish

from .helpers import WireSource, envelope, members_body


class _Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200

    def do_POST(self):                       # noqa: N802 - impose par la stdlib
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).received.append({
            "path": self.path,
            "headers": dict(self.headers),
            "raw": raw,
        })
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):            # silence
        pass


@pytest.fixture
def server():
    _Recorder.received = []
    _Recorder.status = 200
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}/ingest"
    httpd.shutdown()
    httpd.server_close()


def test_publish_envoie_le_document_tel_quel(server):
    _, url = server
    payload = {"exported_at": "2026-08-10T21:58:45+00:00", "mode": "kill_rank",
               "records": [{"rank": 1, "uid": "1234567890001234",
                            "player_name": "Ω Zoé🔥", "score": 42}]}
    res = publish(payload, url, token="secret")

    assert res.ok and res.status == 200
    got = _Recorder.received[0]
    assert got["headers"]["Authorization"] == "Bearer secret"
    assert got["headers"]["Idempotency-Key"] == idempotency_key(body_of(payload))
    # UTF-8 explicite jusque sur le fil : les pseudos ne sont pas echappes.
    assert "Ω Zoé🔥".encode("utf-8") in got["raw"]
    assert json.loads(got["raw"].decode("utf-8")) == payload


def test_meme_document_meme_cle_idempotence(server):
    _, url = server
    payload = {"exported_at": "x", "mode": "kill_rank", "records": []}
    a = publish(payload, url)
    b = publish(dict(reversed(list(payload.items()))), url)   # ordre des cles differe
    assert a.key == b.key, "la cle ne doit pas dependre de l'ordre des cles JSON"


def test_deux_captures_ont_des_cles_differentes(server):
    _, url = server
    base = {"mode": "kill_rank", "records": [{"rank": 1, "uid": "1", "score": 5}]}
    a = publish({**base, "exported_at": "2026-08-10T21:00:00+00:00"}, url)
    b = publish({**base, "exported_at": "2026-08-11T21:00:00+00:00"}, url)
    assert a.key != b.key, "deux mesures distinctes doivent toutes deux compter"


def test_un_echec_http_ne_leve_pas(server):
    _, url = server
    _Recorder.status = 503
    res = publish({"mode": "x", "records": []}, url)
    assert not res.ok
    assert res.status == 503
    assert "503" in res.error


def test_service_injoignable_ne_leve_pas():
    res = publish({"mode": "x", "records": []}, "http://127.0.0.1:9/nowhere")
    assert not res.ok and res.status == 0 and res.error


def test_grab_ecrit_avant_d_envoyer(server, tmp_path):
    """Un service injoignable ne doit pas faire perdre la capture."""
    stream = b"".join(
        wire.encode_frame(p, compress=True)
        for p in [envelope(CMD_MEMBERS, members_body(n=5))]
    )
    out = tmp_path / "exports"
    result = grab(WireSource(stream), out_dir=out, db_path=MEMORY_DB,
                  post_url="http://127.0.0.1:9/nowhere")

    assert (out / "lwvs_kills.json").exists(), "le JSON doit exister malgre l'echec"
    assert result.written[0].published is not None
    assert not result.written[0].published.ok


def test_grab_poste_ce_qu_il_ecrit(server, tmp_path):
    _, url = server
    stream = b"".join(
        wire.encode_frame(p, compress=True)
        for p in [envelope(CMD_MEMBERS, members_body(n=4))]
    )
    out = tmp_path / "exports"
    result = grab(WireSource(stream), out_dir=out, db_path=MEMORY_DB, post_url=url)

    assert result.written[0].published.ok
    envoye = json.loads(_Recorder.received[0]["raw"].decode("utf-8"))
    sur_disque = json.loads((out / "lwvs_kills.json").read_text(encoding="utf-8"))
    assert envoye == sur_disque, "un seul format, deux transports"


def test_le_contrat_est_le_meme_que_le_fichier():
    """`--format records` et `--post` produisent le meme document."""
    rows = [{"snapshot_id": 1, "captured_at": "t", "uid": "1", "name": "a",
             "army_kill": 9}]
    payload = exporter.build_records(rows, "members")
    assert json.loads(body_of(payload).decode("utf-8")) == payload
