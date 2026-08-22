"""Habillage de la GUI : palette, styles ttk, petits widgets d'agrement.

CE MODULE EST PUREMENT VISUEL. Il ne connait ni le protocole, ni la base, ni
les flux : `gui.py` decide, ce module peint. Tkinter/ttk uniquement -- l'outil
doit tourner sur un poste de joueur, aucune dependance n'est ajoutee.

Le theme de base est **`clam`**, et ce n'est pas un gout : c'est le seul theme
livre avec Python dont tous les elements acceptent des couleurs. Sous `vista`
(defaut Windows) boutons, champs et cases sont dessines par l'OS et ignorent
silencieusement `style.configure` -- on croit avoir change la couleur, rien ne
bouge.

Les widgets a l'interieur d'une carte sont des `tk.Label` / `tk.Frame` plutot
que leurs equivalents ttk : le fond d'une carte differe du fond de la fenetre,
et un `tk.Label` porte sa couleur directement au lieu de dependre d'un style
nomme qu'on oublie d'appliquer une fois sur deux.
"""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont, ttk
from typing import Callable


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    bg: str            # fond de la fenetre
    surface: str       # fond d'une carte
    surface_alt: str   # survol, en-tete de tableau, bouton secondaire
    field: str         # fond d'un champ de saisie
    border: str
    text: str
    muted: str         # texte secondaire, aides
    accent: str
    accent_hover: str
    accent_press: str
    accent_soft: str   # fond d'un badge ou d'une ligne selectionnee
    on_accent: str
    ok: str
    warn: str
    err: str
    disabled_bg: str
    disabled_fg: str


DARK = Palette(
    name="sombre", dark=True,
    bg="#13161b", surface="#1a1e25", surface_alt="#222831", field="#0f1217",
    border="#2b323d", text="#e6e9ef", muted="#8b95a5",
    accent="#4c8dff", accent_hover="#649cff", accent_press="#3a7ae4",
    accent_soft="#22304a", on_accent="#ffffff",
    ok="#3ecf8e", warn="#f0a640", err="#ff6b6b",
    disabled_bg="#1d222a", disabled_fg="#59616e",
)

LIGHT = Palette(
    name="clair", dark=False,
    bg="#eef1f5", surface="#ffffff", surface_alt="#f3f5f9", field="#ffffff",
    border="#dde3ec", text="#161a20", muted="#6b7482",
    accent="#2f6fed", accent_hover="#4480f5", accent_press="#255ccc",
    accent_soft="#e4ecfd", on_accent="#ffffff",
    ok="#128a5a", warn="#a86a0b", err="#cf3b3b",
    disabled_bg="#e9edf3", disabled_fg="#a5adbb",
)

PALETTES = {"sombre": DARK, "clair": LIGHT}


# -- polices ----------------------------------------------------------------
# Une famille absente n'est pas une erreur Tk : elle est remplacee par une
# police par defaut souvent laide. On choisit donc explicitement la premiere
# famille reellement installee.
def _pick(root: tk.Misc, candidates: tuple[str, ...], fallback: str) -> str:
    available = {f.lower() for f in tkfont.families(root)}
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


class Fonts:
    def __init__(self, root: tk.Misc) -> None:
        ui = _pick(root, ("Segoe UI Variable Text", "Segoe UI", "Inter",
                          "Cantarell", "DejaVu Sans"), "TkDefaultFont")
        mono = _pick(root, ("Cascadia Mono", "Consolas", "JetBrains Mono",
                            "DejaVu Sans Mono"), "TkFixedFont")
        self.body = (ui, 10)
        self.bold = (ui, 10, "bold")
        self.small = (ui, 9)
        self.small_bold = (ui, 9, "bold")
        self.title = (ui, 16, "bold")
        self.badge = (ui, 10, "bold")
        self.mono = (mono, 9)


def apply(root: tk.Tk, mode: str = "sombre") -> tuple[Palette, Fonts]:
    """Installe la palette sur `root` et renvoie de quoi peindre le reste."""
    pal = PALETTES.get(mode, DARK)
    fonts = Fonts(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:      # pragma: no cover - clam est livre avec Tk
        pass

    root.configure(background=pal.bg)
    # La liste deroulante d'un Combobox est un Listbox Tk classique : elle
    # n'est pas atteignable par les styles ttk, seulement par les options.
    root.option_add("*TCombobox*Listbox.background", pal.surface_alt)
    root.option_add("*TCombobox*Listbox.foreground", pal.text)
    root.option_add("*TCombobox*Listbox.selectBackground", pal.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", pal.on_accent)
    root.option_add("*TCombobox*Listbox.font", fonts.body)
    root.option_add("*Dialog.msg.font", fonts.body)

    flat = {"relief": "flat", "borderwidth": 0}
    style.configure(".", background=pal.bg, foreground=pal.text,
                    font=fonts.body, focuscolor=pal.accent)
    style.configure("TFrame", background=pal.bg)
    style.configure("Card.TFrame", background=pal.surface)
    style.configure("TLabel", background=pal.bg, foreground=pal.text)
    style.configure("Card.TLabel", background=pal.surface, foreground=pal.text)

    # -- boutons ------------------------------------------------------------
    style.configure("TButton", background=pal.surface_alt, foreground=pal.text,
                    bordercolor=pal.border, lightcolor=pal.surface_alt,
                    darkcolor=pal.surface_alt, padding=(14, 7), **flat)
    style.map(
        "TButton",
        background=[("disabled", pal.disabled_bg), ("pressed", pal.border),
                    ("active", pal.border)],
        foreground=[("disabled", pal.disabled_fg)],
        lightcolor=[("pressed", pal.border), ("active", pal.border)],
        darkcolor=[("pressed", pal.border), ("active", pal.border)],
    )
    style.configure("Accent.TButton", background=pal.accent,
                    foreground=pal.on_accent, bordercolor=pal.accent,
                    lightcolor=pal.accent, darkcolor=pal.accent)
    style.map(
        "Accent.TButton",
        background=[("disabled", pal.disabled_bg), ("pressed", pal.accent_press),
                    ("active", pal.accent_hover)],
        foreground=[("disabled", pal.disabled_fg)],
        lightcolor=[("pressed", pal.accent_press), ("active", pal.accent_hover)],
        darkcolor=[("pressed", pal.accent_press), ("active", pal.accent_hover)],
    )
    # Bouton discret : meme fond que la carte, il n'existe qu'au survol.
    style.configure("Ghost.TButton", background=pal.surface,
                    foreground=pal.muted, bordercolor=pal.border,
                    lightcolor=pal.surface, darkcolor=pal.surface,
                    padding=(10, 5), font=fonts.small)
    style.map(
        "Ghost.TButton",
        background=[("disabled", pal.surface), ("pressed", pal.surface_alt),
                    ("active", pal.surface_alt)],
        foreground=[("disabled", pal.disabled_fg), ("active", pal.text)],
        lightcolor=[("pressed", pal.surface_alt), ("active", pal.surface_alt)],
        darkcolor=[("pressed", pal.surface_alt), ("active", pal.surface_alt)],
    )
    style.configure("Icon.TButton", padding=(8, 4), font=fonts.body)

    # -- champs -------------------------------------------------------------
    for name in ("TEntry", "TCombobox"):
        style.configure(name, fieldbackground=pal.field, background=pal.field,
                        foreground=pal.text, bordercolor=pal.border,
                        lightcolor=pal.border, darkcolor=pal.border,
                        insertcolor=pal.text, arrowcolor=pal.muted,
                        padding=(8, 6), relief="flat", borderwidth=1)
        style.map(name,
                  bordercolor=[("focus", pal.accent), ("hover", pal.muted)],
                  lightcolor=[("focus", pal.accent)],
                  darkcolor=[("focus", pal.accent)],
                  foreground=[("disabled", pal.disabled_fg)],
                  fieldbackground=[("disabled", pal.disabled_bg),
                                   ("readonly", pal.field)],
                  arrowcolor=[("hover", pal.text)])
    style.map("TCombobox",
              selectbackground=[("readonly", pal.field)],
              selectforeground=[("readonly", pal.text)])

    # -- tableau ------------------------------------------------------------
    # Le layout par defaut ajoute un cadre grave typique de clam : on ne garde
    # que la zone de donnees.
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview", background=pal.surface,
                    fieldbackground=pal.surface, foreground=pal.text,
                    rowheight=30, borderwidth=0, font=fonts.body)
    style.map("Treeview",
              background=[("selected", pal.accent_soft)],
              foreground=[("selected", pal.text)])
    style.configure("Treeview.Heading", background=pal.surface_alt,
                    foreground=pal.muted, font=fonts.small_bold,
                    padding=(10, 8), relief="flat", borderwidth=0)
    style.map("Treeview.Heading",
              background=[("active", pal.surface_alt), ("pressed", pal.surface_alt)],
              relief=[("active", "flat"), ("pressed", "flat")])

    # -- barres -------------------------------------------------------------
    for orient in ("Vertical", "Horizontal"):
        name = f"Slim.{orient}.TScrollbar"
        thumb = f"{orient}.Scrollbar.thumb"
        trough = f"{orient}.Scrollbar.trough"
        style.layout(name, [(trough, {"sticky": "ns" if orient == "Vertical" else "we",
                                      "children": [(thumb, {"expand": "1",
                                                            "sticky": "nswe"})]})])
        style.configure(name, background=pal.border, troughcolor=pal.surface,
                        bordercolor=pal.surface, lightcolor=pal.border,
                        darkcolor=pal.border, arrowsize=0, width=8, **flat)
        style.map(name, background=[("active", pal.muted)])

    for key, colour in (("Ok", pal.ok), ("Warn", pal.warn), ("Accent", pal.accent)):
        style.configure(f"{key}.Horizontal.TProgressbar", troughcolor=pal.field,
                        background=colour, bordercolor=pal.field,
                        lightcolor=colour, darkcolor=colour, thickness=6, **flat)

    _titlebar(root, pal.dark)
    return pal, fonts


def _titlebar(root: tk.Tk, dark: bool) -> None:
    """Barre de titre Windows accordee au theme. Cosmetique : jamais fatal.

    `wm_frame()` donne la fenetre DECOREE ; `winfo_id()` donne la zone client,
    dont le parent n'est pas encore le frame tant que Tk n'a pas mappe la
    fenetre -- l'attribut partait alors dans le vide, barre restee blanche.

    Windows ne repeint pas la barre a chaud : il faut un cycle cache/montre.
    On ne le fait que si la fenetre est REELLEMENT affichee, sinon une racine
    volontairement masquee (les tests le font) surgirait a l'ecran.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        try:
            hwnd = int(root.wm_frame(), 16)
        except (ValueError, tk.TclError):
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE, recent puis ancien
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        if root.winfo_viewable():
            root.withdraw()
            root.deiconify()
    except (OSError, AttributeError, tk.TclError):
        pass


# -- briques d'interface ----------------------------------------------------


def card(parent: tk.Misc, pal: Palette, fonts: Fonts, title: str = "",
         step: str = "", hint: str = "") -> tuple[tk.Frame, tk.Frame]:
    """Une carte : cadre 1px sur fond `surface`. Renvoie (exterieur, contenu)."""
    outer = tk.Frame(parent, background=pal.surface, highlightthickness=1,
                     highlightbackground=pal.border, highlightcolor=pal.border,
                     borderwidth=0)
    if title:
        head = tk.Frame(outer, background=pal.surface)
        head.pack(fill="x", padx=16, pady=(11, 0))
        if step:
            badge = tk.Canvas(head, width=22, height=22, background=pal.surface,
                              highlightthickness=0, borderwidth=0)
            badge.create_oval(0, 0, 21, 21, fill=pal.accent_soft, outline="")
            badge.create_text(11, 11, text=step, fill=pal.accent, font=fonts.small_bold)
            badge.pack(side="left", padx=(0, 9))
        tk.Label(head, text=title, background=pal.surface, foreground=pal.text,
                 font=fonts.bold).pack(side="left")
        if hint:
            tk.Label(head, text=hint, background=pal.surface, foreground=pal.muted,
                     font=fonts.small).pack(side="right")
    body = tk.Frame(outer, background=pal.surface)
    body.pack(fill="both", expand=True, padx=16, pady=(9, 12))
    return outer, body


def label(parent: tk.Misc, pal: Palette, fonts: Fonts, text: str = "",
          kind: str = "body", **kw) -> tk.Label:
    colour = {"body": pal.text, "muted": pal.muted, "ok": pal.ok,
              "warn": pal.warn, "err": pal.err, "accent": pal.accent}[kind]
    font = fonts.small if kind == "muted" else fonts.body
    return tk.Label(parent, text=text, background=pal.surface, foreground=colour,
                    font=kw.pop("font", font), justify=kw.pop("justify", "left"),
                    anchor=kw.pop("anchor", "w"), **kw)


class Dot(tk.Canvas):
    """Pastille de couleur : l'etat se lit d'un coup d'oeil, sans lire le texte."""

    def __init__(self, parent: tk.Misc, background: str, size: int = 9) -> None:
        super().__init__(parent, width=size, height=size, background=background,
                         highlightthickness=0, borderwidth=0)
        self._id = self.create_oval(0, 0, size - 1, size - 1, fill="", outline="")

    def colour(self, value: str) -> None:
        self.itemconfigure(self._id, fill=value)


class Check(tk.Frame):
    """Case a cocher dessinee, liee a une `tk.BooleanVar`.

    Pourquoi ne pas utiliser `ttk.Checkbutton` : cochee, la case de `clam`
    affiche une CROIX. Une croix se lit comme un refus -- exactement le
    contraire de l'etat qu'elle represente. Ici c'est une vraie coche.

    La trace posee sur la variable est RETIREE a la destruction : les
    variables Tk survivent au changement de theme, qui detruit et reconstruit
    l'arbre des widgets. Une trace oubliee redessinerait un canvas mort.
    """

    def __init__(self, parent: tk.Misc, pal: Palette, fonts: Fonts, text: str,
                 variable: tk.BooleanVar, command=None) -> None:
        super().__init__(parent, background=pal.surface, takefocus=True)
        self.pal, self.var, self.command = pal, variable, command
        self.box = tk.Canvas(self, width=17, height=17, background=pal.surface,
                             highlightthickness=0, borderwidth=0, cursor="hand2")
        self.box.pack(side="left", pady=1)
        self.text = tk.Label(self, text=text, background=pal.surface,
                             foreground=pal.text, font=fonts.body, cursor="hand2")
        self.text.pack(side="left", padx=(8, 0))
        for widget in (self, self.box, self.text):
            widget.bind("<Button-1>", self._toggle)
            widget.bind("<Enter>", lambda _e: self._draw(True))
            widget.bind("<Leave>", lambda _e: self._draw(False))
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<Destroy>", self._forget)
        self._trace = variable.trace_add("write", lambda *_: self._draw(False))
        self._draw(False)

    def _forget(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _toggle(self, _event=None) -> str:
        self.var.set(not self.var.get())
        self.focus_set()
        if self.command is not None:
            self.command()
        return "break"

    def _draw(self, hover: bool) -> None:
        pal, on = self.pal, bool(self.var.get())
        self.box.delete("all")
        self.box.create_rectangle(
            1, 1, 15, 15, width=1, fill=pal.accent if on else pal.field,
            outline=pal.accent if on else (pal.muted if hover else pal.border))
        if on:
            self.box.create_line(4, 8, 7, 11, 12, 5, fill=pal.on_accent, width=2,
                                 capstyle="round", joinstyle="round")


class Tooltip:
    """Bulle d'aide. Les raccourcis clavier ne se devinent pas ; ils s'affichent.

    Le `after` en attente est annule au depart du pointeur ET a la destruction
    du widget : un timer survivant a sa fenetre reveille du code sur des widgets
    morts, exactement la faute que la boucle de drainage evite deja.

    `text` accepte un appelable : il est evalue a l'affichage, ce qui permet a
    une bulle de dire l'etat du moment (la valeur reelle derriere un libelle,
    par exemple) plutot qu'un texte fige a la construction.
    """

    def __init__(self, widget: tk.Widget, text: str | Callable[[], str],
                 pal: Palette, fonts: Fonts, delay: int = 450) -> None:
        self.widget, self.text, self.pal, self.fonts = widget, text, pal, fonts
        self.delay = delay
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None or not self.widget.winfo_viewable():
            return
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(background=self.pal.border)
        tk.Label(tip, text=text, background=self.pal.surface_alt,
                 foreground=self.pal.text, font=self.fonts.small,
                 justify="left", padx=9, pady=5).pack(padx=1, pady=1)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
