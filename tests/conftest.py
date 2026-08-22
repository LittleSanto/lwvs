"""Isolation des tests vis-a-vis de l'etat REEL de la machine.

`lwvs` memorise ton alliance dans le repertoire de configuration de
l'utilisateur. Sans cette isolation, la suite lit -- et ECRIT -- le fichier
d'un vrai poste : les tests deviennent dependants de qui les lance, et peuvent
ecraser une memoire legitime.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def isole_l_identite(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("lwvs-home")
    monkeypatch.setenv("LWVS_HOME", str(home))
    return home


# -- racine Tk partagee -----------------------------------------------------
# Vit ici et pas dans un fichier de test : deux suites de tests GUI la
# demandent, et deux racines Tk dans le meme processus se marchent dessus.
@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("pas d'affichage disponible")
    r.withdraw()
    apps: list = []
    yield _Root(r, apps)
    # Sans ca, le timer `after` et les threads survivent au test et polluent
    # les suivants : `filterwarnings = ["error"]` les transforme en echecs
    # attribues a d'autres tests.
    for app in apps:
        app.stop_polling()
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.name.startswith("Thread-"):
            t.join(timeout=5)
    r.update()
    r.destroy()


class _Root:
    """Racine Tk qui enregistre les App creees, pour les arreter proprement."""

    def __init__(self, tk_root, apps):
        self._tk = tk_root
        self._apps = apps

    def build(self, db=None, **kw):
        from lwvs.gui import App
        app = App(self._tk, db, **kw)
        self._apps.append(app)
        return app

    def __getattr__(self, name):
        return getattr(self._tk, name)
