"""Point d'entree du .exe : lance la GUI, et raconte ce qui casse.

Pourquoi ce fichier plutot que `lwvs/gui.py` directement : PyInstaller lance un
SCRIPT, pas un module de paquet -- les imports relatifs de `gui.py` echoueraient.

Pourquoi le try/except : un .exe empaquete en mode fenetre n'a NI console NI
stderr. Une exception au demarrage (tkinter absent, DLL manquante, repertoire
de config illisible) ferait disparaitre la fenetre sans un mot, et le seul
retour utilisateur serait « ca ne s'ouvre pas ». On ecrit donc la trace dans un
fichier ET on la montre.
"""

from __future__ import annotations

import sys
import traceback


def _report(trace: str) -> None:
    chemin = "(non ecrit)"
    try:
        from lwvs.identity import home
        cible = home() / "lwvs-crash.log"
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(trace, encoding="utf-8")   # UTF-8 explicite : la console
        chemin = str(cible)                          # Windows est en cp1252
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        racine = tk.Tk()
        racine.withdraw()
        messagebox.showerror(
            "lwvs — demarrage impossible",
            "lwvs n'a pas pu demarrer.\n\n"
            f"{trace.strip().splitlines()[-1]}\n\n"
            f"Detail ecrit dans :\n{chemin}")
        racine.destroy()
    except Exception:
        print(trace, file=sys.stderr)


def main() -> int:
    try:
        from lwvs.gui import main as gui_main
        return gui_main()
    except Exception:
        _report(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
