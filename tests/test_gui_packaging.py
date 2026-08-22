"""Les deux pieges qui ne se voient QUE hors du repertoire du projet.

Ils ne cassent rien tant qu'on lance `python -m lwvs gui` depuis les sources :
le repertoire courant est alors le bon par accident. Empaquetee et lancee
depuis un raccourci, la GUI perd ce hasard -- et ces deux tests le disent.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lwvs import capture, gui
from lwvs.capture import TsharkNotFound

pytest.importorskip("tkinter")


def _pump(root, app, pred, deadline=10.0):
    """Draine la file comme le ferait la boucle Tk, sans lancer mainloop."""
    end = time.time() + deadline
    while time.time() < end and not pred():
        root.update()
        app.stop_polling()
        app._drain()
    return pred()


# -- 1. la base et les preferences ne suivent PAS le repertoire courant -----
def test_base_et_prefs_ancrees_dans_le_repertoire_de_config(
        root, isole_l_identite, tmp_path, monkeypatch):
    """`store.DEFAULT_DB` est relatif : sans ancrage, tout suit le CWD.

    Lancee depuis un raccourci, la GUI ecrivait sa base et ses preferences dans
    un repertoire imprevisible -- ou pas du tout, si c'etait `Program Files`.
    """
    monkeypatch.setattr(capture, "list_interfaces", lambda *a, **k: [])
    monkeypatch.chdir(tmp_path)          # un CWD qui n'est pas celui du projet

    app = root.build()                   # aucun --db : le cas de l'utilisateur

    assert Path(app.db_path).parent == isole_l_identite
    prefs = gui._prefs_path(app.db_path)
    assert prefs.parent == isole_l_identite

    # Le repertoire de config peut ne jamais avoir servi : ecrire dedans doit
    # le creer, sinon les preferences sont perdues en silence.
    gui._save_prefs(app.db_path, {"port": "11731"})
    assert prefs.exists()
    assert not (tmp_path / "lwvs.gui.json").exists()


def test_prefs_existantes_migrees_une_fois(root, isole_l_identite, tmp_path,
                                           monkeypatch):
    """Deplacer les preferences ne doit pas faire repartir de zero."""
    monkeypatch.setattr(capture, "list_interfaces", lambda *a, **k: [])
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lwvs.gui.json").write_text('{"port": "12345"}', encoding="utf-8")

    app = root.build()

    assert app.prefs["port"] == "12345"
    assert gui._prefs_path(app.db_path).parent == isole_l_identite


# -- 2. tshark absent : un prerequis, pas une trace de pile ----------------
def test_wireshark_manquant_affiche_un_ecran_et_pas_un_traceback(
        root, tmp_path, monkeypatch):
    """Le cas du premier lancement chez quelqu'un d'autre.

    `_spawn` attrape tout et deverse `traceback.format_exc()` dans le journal :
    lisible pour qui a ecrit le code, mur pour un joueur. Le prerequis absent
    doit se dire, et desarmer les boutons qui ne peuvent plus rien faire.
    """
    def absent(*a, **k):
        raise TsharkNotFound("tshark introuvable")

    monkeypatch.setattr(capture, "list_interfaces", absent)
    monkeypatch.chdir(tmp_path)

    app = root.build()
    assert _pump(root, app, lambda: app._tshark_missing), "prerequis jamais signale"

    assert app.prereq.winfo_manager() == "pack", "la banniere n'est pas affichee"
    assert str(app.start_btn["state"]) == "disabled"
    assert str(app.detect_btn["state"]) == "disabled"

    journal = app.log.get("1.0", "end-1c")
    assert "Wireshark" in journal
    assert "Traceback" not in journal


def test_la_banniere_survit_au_changement_de_theme(root, tmp_path, monkeypatch):
    """Changer de theme detruit et reconstruit tout l'arbre des widgets."""
    def absent(*a, **k):
        raise TsharkNotFound("tshark introuvable")

    monkeypatch.setattr(capture, "list_interfaces", absent)
    monkeypatch.chdir(tmp_path)

    app = root.build()
    assert _pump(root, app, lambda: app._tshark_missing)

    app._toggle_theme()

    assert app.prereq.winfo_manager() == "pack"
    assert str(app.start_btn["state"]) == "disabled"


# -- 3. la version doit etre lisible a l'ecran ----------------------------
def test_la_version_est_dans_le_titre():
    """Sans elle, tout support a distance commence par « tu as quelle version ? »."""
    from lwvs import __version__
    assert __version__ in gui.TITLE
