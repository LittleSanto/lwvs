"""Interface graphique : capturer, voir ce qui arrive, exporter.

Tkinter, donc aucune dependance : il est dans la stdlib et natif sous Windows.
L'habillage (palette, styles ttk, cartes, bulles d'aide) vit dans `theme.py` ;
ici on ne fait que poser des widgets et cabler des appels.

CETTE COUCHE NE DECIDE RIEN. Elle appelle `capture`, `ingest` et `exporter`
comme la CLI, et lit le registre `exporter.FEEDS` au lieu de coder en dur une
liste de boutons. Ajouter une statistique se fait dans le registre, pas ici.

Threads : la capture tourne dans un thread de travail, Tkinter n'est pas
thread-safe. Tout ce qui vient du thread de capture transite par une Queue que
la boucle Tk draine avec `after()`. Aucun widget n'est touche hors du thread UI.

Les variables Tk sont creees dans `_vars()`, JAMAIS dans `_build()` : changer de
theme reconstruit l'arbre des widgets, et des variables recreees a cette
occasion perdraient la saisie en cours.
"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (__version__, capture, events as events_mod, exporter, gamecache,
               identity, storm, theme, trains)
from .capture import LiveSource, TsharkNotFound
from .publish import publish
from .ingest import ingest
from .store import DEFAULT_DB, Store

TITLE = f"lwvs {__version__} — Last War VS capture"
_POLL_MS = 120
# Periodes proposees a cote de « Since » ; storm.since_ms sait toutes les lire.
SINCE_CHOICES = ("last 7 days", "last 14 days", "last 30 days", "all")
#: Plafond de la detection, pas une duree d'attente : elle s'arrete a la preuve.
_DETECT_SECONDS = 20
#: En dessous, la carte « Stats site » des reglages passe sous le bord de la
#: fenetre, et le tableau de la capture n'a plus la place de ses lignes.
_MIN_SIZE = (960, 640)


#: Lien de secours affiche quand tshark manque.
_WIRESHARK_URL = "https://www.wireshark.org/download.html"

#: `OwnAlliance.source` est une valeur interne (tests et memoire la comparent) :
#: on ne la traduit qu'a l'affichage.
_SOURCE_LABELS = {"duel": "Duel screen", "roster": "member list",
                  "memoire": "memory"}


# Le defaut de `store` est RELATIF : lance depuis un raccourci (ou un .exe
# installe), il suit un repertoire courant imprevisible -- la base atterrit
# n'importe ou, et le fichier de preferences avec elle puisqu'il se deduit
# d'elle. La GUI ancre donc les deux dans le repertoire de configuration.
def _default_db() -> str:
    home = identity.home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DEFAULT_DB
    return str(home / DEFAULT_DB)


def _migrate_legacy_prefs(db_path: str) -> None:
    """Recupere une fois les preferences ecrites a cote du repertoire courant.

    Sans ca, deplacer la base ferait repartir de zero les postes existants :
    interface, port, URL du site -- tout serait a resaisir sans explication.
    """
    new = _prefs_path(db_path)
    legacy = _prefs_path(DEFAULT_DB)
    if new == legacy or new.exists() or not legacy.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


# L'interface reseau ne change jamais d'une session a l'autre, le port si.
# Retaper le GUID a chaque lancement est une friction inutile ; on s'en souvient.
def _prefs_path(db_path: str) -> Path:
    return Path(db_path).resolve().with_suffix(".gui.json")


def _load_prefs(db_path: str) -> dict[str, Any]:
    try:
        return json.loads(_prefs_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_prefs(db_path: str, prefs: dict[str, Any]) -> None:
    try:
        path = _prefs_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass   # une preference non sauvee ne doit jamais casser la capture


# Le device tshark (`\Device\NPF_{9485472E-585D-...}`) est ce que tshark exige,
# pas ce qu'un humain reconnait : deroulee, la liste ne montrait que des GUID
# tronques, tous identiques a l'oeil. On affiche le nom usuel et on garde le
# device comme valeur reelle -- memorise, compare, jamais montre.
def _short_device(device: str) -> str:
    """De quoi distinguer deux interfaces homonymes, sans etaler le GUID."""
    tail = device.rsplit("\\", 1)[-1]
    if tail.startswith("NPF_"):
        tail = tail[4:]
    return tail.strip("{}").split("-", 1)[0] or device


def _iface_labels(ifaces: "list[capture.Interface]") -> dict[str, str]:
    """Libelle affiche -> device. L'ordre de la liste est conserve."""
    labels: dict[str, str] = {}
    for iface in ifaces:
        base = (iface.name or "").strip() or _short_device(iface.device)
        label = base
        # Un poste declare parfois deux interfaces de meme nom : les confondre
        # ferait capturer sur la mauvaise sans que rien ne le dise.
        if label in labels:
            label = f"{base}  ·  {_short_device(iface.device)}"
        labels[label] = iface.device
    return labels


# ---------------------------------------------------------------------------
# messages du thread de travail vers l'UI
# ---------------------------------------------------------------------------


@dataclass
class Msg:
    kind: str   # "log" | "progress" | "done" | "sent" | "error" | "ifaces"
                # | "detecting" | "detected" | "detect_end" | "busy"
                # | "no_tshark"
    payload: Any = None


class App:
    def __init__(self, root: tk.Tk, db_path: str | None = None,
                 keep: bool | None = None) -> None:
        self.root = root
        self.db_path = db_path or _default_db()
        _migrate_legacy_prefs(self.db_path)
        self.q: "queue.Queue[Msg]" = queue.Queue()
        self.source: LiveSource | None = None
        self.worker: threading.Thread | None = None
        self.snapshot_id: int | None = None
        self.started_at: datetime | None = None
        self.prefs = _load_prefs(self.db_path)
        # Par defaut on ne conserve rien : l'historisation est faite en aval.
        # SQLite reste le moteur de deduplication, mais dans un fichier
        # temporaire efface a la fermeture. (Pas `:memory:` : la GUI rouvre la
        # base a chaque lecture, et rouvrir une base RAM en rendrait une vide.)
        self._temp_db = Path(tempfile.gettempdir()) / f"lwvs-session-{id(self)}.sqlite3"
        self._iface_values: list[str] = []
        #: Libelle affiche -> device tshark. Le libelle est pour l'oeil.
        self._iface_by_label: dict[str, str] = {}
        self._seen: dict[str, int] = {}
        self._busy = 0
        #: tshark introuvable : la capture est impossible et il faut le DIRE,
        #: pas laisser une trace de pile dans le journal (cf. `_spawn`).
        self._tshark_missing = False
        #: Base du jeu pour Storm. None = trouvee sous AppData\LocalLow ; la
        #: preference ne sert qu'aux postes ou le jeu est installe ailleurs.
        self.storm_db: str | None = self.prefs.get("storm_game_db") or None
        self._storm_rows: list[dict[str, Any]] = []
        self._train_rows: list[dict[str, Any]] = []
        #: Ecran affiche et journal deplie : survivent au changement de theme.
        self._page = "capture"
        self._log_open = False
        #: Start sans interface ni port : detecter d'abord, puis demarrer seul.
        self._start_after_detect = False
        self._vars(keep)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        root.title(TITLE)
        root.minsize(*_MIN_SIZE)
        self._restore_geometry()
        self.pal, self.fonts = theme.apply(root, self.prefs.get("theme", "sombre"))
        self._build()
        self._bind_keys()
        self._refresh_feeds()
        # Le timer se replanifie tout seul : on garde son id pour pouvoir
        # l'annuler, sinon il survit a la fenetre et touche des widgets morts.
        self._poll_id: str | None = self.root.after(_POLL_MS, self._drain)
        self._load_interfaces()
        self._refresh_storm()
        self._refresh_trains()

    # -- etat persistant --------------------------------------------------
    def _vars(self, keep: bool | None) -> None:
        """Toutes les variables Tk, creees une seule fois pour la vie de l'App."""
        self._keep = tk.BooleanVar(
            value=bool(self.prefs.get("keep", False)) if keep is None else keep)
        self.iface_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(self.prefs.get("port", "")))
        self.status_var = tk.StringVar(value="ready")
        self.guard_var = tk.StringVar(value="no capture yet")
        self.alliance_var = tk.StringVar(value="")
        self.mine_var = tk.BooleanVar(value=bool(self.prefs.get("mine", True)))
        self.day_var = tk.StringVar(value=str(self.prefs.get("declare_day", "") or ""))
        self.event_var = tk.StringVar(value=self.prefs.get("event", ""))
        self.url_var = tk.StringVar(value=self.prefs.get("post_url", ""))
        self.token_var = tk.StringVar(value=self.prefs.get("post_token", ""))
        self.autosend_var = tk.BooleanVar(value=bool(self.prefs.get("autosend", False)))
        self.keep_token_var = tk.BooleanVar(
            value=bool(self.prefs.get("keep_token", False)))
        self.storm_since_var = tk.StringVar(
            value=self.prefs.get("storm_since") or SINCE_CHOICES[0])
        self.storm_state_var = tk.StringVar(value="reading the game's mailbox…")
        self.trains_since_var = tk.StringVar(
            value=self.prefs.get("trains_since") or SINCE_CHOICES[0])
        self.trains_state_var = tk.StringVar(value="reading the game's mailbox…")
        self.net_var = tk.StringVar()
        for var in (self.iface_var, self.port_var):
            var.trace_add("write", lambda *_: self._sync_net())
        self._sync_net()

    def _sync_net(self) -> None:
        iface, port = self.iface_var.get(), self.port_var.get().strip()
        if not port.isdigit():
            self.net_var.set("found by itself when you press Start")
        else:
            self.net_var.set(f"{iface or 'no interface'}  ·  port {port}")

    def _restore_geometry(self) -> None:
        remembered = self.prefs.get("geometry", "")
        if isinstance(remembered, str) and remembered.count("+") == 2:
            try:
                self.root.geometry(remembered)
                return
            except tk.TclError:
                pass
        # Bornee a l'ecran : une taille par defaut plus haute que le moniteur
        # placerait la barre des taches par-dessus le bas de la fenetre.
        w = min(1120, self.root.winfo_screenwidth() - 40)
        h = min(760, self.root.winfo_screenheight() - 90)
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        pal = self.pal
        self.frame = tk.Frame(self.root, background=pal.bg)
        self.frame.pack(fill="both", expand=True)

        # Un ecran par tache, choisi dans un menu lateral : capturer, lire les
        # mails, regler. Les reglages qu'on ne touche qu'une fois ne se melent
        # plus aux boutons qu'on presse a chaque capture.
        self._build_nav(self.frame)
        tk.Frame(self.frame, background=pal.border, width=1).pack(side="left", fill="y")
        main = tk.Frame(self.frame, background=pal.bg)
        main.pack(side="left", fill="both", expand=True)
        self._build_prereq(main)

        body = tk.Frame(main, background=pal.bg)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        self.body_frame = body
        self.pages: dict[str, tk.Frame] = {}
        for name, build in (("capture", self._build_capture_page),
                            ("mailbox", self._build_mailbox_page),
                            ("settings", self._build_settings_page)):
            page = tk.Frame(body, background=pal.bg)
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
            build(page)
            self.pages[name] = page
        self._show_page(self._page)

        self._say("the JSON exports are the deliverable; nothing is kept "
                  "locally unless you tick \"Keep history locally\" in Settings.")
        self._apply_prereq()

    def _show_page(self, name: str) -> None:
        self._page = name
        self.pages[name].tkraise()
        for key, btn in self.nav_btns.items():
            btn.configure(style="NavOn.TButton" if key == name else "Nav.TButton")

    def _build_nav(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        nav = tk.Frame(parent, background=pal.bg, width=190)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Label(nav, text="lwvs", background=pal.bg, foreground=pal.text,
                 font=fonts.title, anchor="w").pack(fill="x", padx=24, pady=(18, 0))
        tk.Label(nav, text=f"Last War stats · {__version__}", background=pal.bg,
                 foreground=pal.muted, font=fonts.small,
                 anchor="w").pack(fill="x", padx=24, pady=(0, 18))
        self.nav_btns: dict[str, ttk.Button] = {}
        for key, text in (("capture", "◉   Capture"), ("mailbox", "✉   Mailbox"),
                          ("settings", "⚙   Settings")):
            btn = ttk.Button(nav, text=text, style="Nav.TButton",
                             command=lambda k=key: self._show_page(k))
            btn.pack(fill="x", padx=12, pady=2)
            self.nav_btns[key] = btn

        pill = tk.Frame(nav, background=pal.surface, highlightthickness=1,
                        highlightbackground=pal.border, highlightcolor=pal.border)
        pill.pack(side="bottom", fill="x", padx=12, pady=(0, 16))
        self.state_dot = theme.Dot(pill, pal.surface)
        self.state_dot.pack(side="left", padx=(11, 7), pady=10, anchor="n")
        self.state_dot.colour(pal.muted)
        tk.Label(pill, textvariable=self.status_var, background=pal.surface,
                 foreground=pal.text, font=fonts.small, anchor="w", justify="left",
                 wraplength=130).pack(side="left", fill="x", padx=(0, 10), pady=6)
        # Barre indeterminee : elle ne dit pas « combien », elle dit « ca vit ».
        # Sans elle, une detection de 25 s est indiscernable d'un gel.
        self.busy_bar = ttk.Progressbar(nav, style="Accent.Horizontal.TProgressbar",
                                        mode="indeterminate")

    def _page_head(self, page: tk.Frame, title: str, hint: str = "") -> tk.Frame:
        """Titre de l'ecran, une phrase d'aide, et la place des boutons a droite."""
        pal, fonts = self.pal, self.fonts
        head = tk.Frame(page, background=pal.bg)
        head.pack(fill="x")
        text = tk.Frame(head, background=pal.bg)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=title, background=pal.bg, foreground=pal.text,
                 font=fonts.title, anchor="w").pack(fill="x")
        if hint:
            tk.Label(text, text=hint, background=pal.bg, foreground=pal.muted,
                     font=fonts.small, anchor="w", justify="left",
                     wraplength=560).pack(fill="x", pady=(2, 0))
        return head

    def _on_bg(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(parent, text=text, background=self.pal.bg,
                        foreground=self.pal.muted, font=self.fonts.small)

    def _build_prereq(self, parent: tk.Frame) -> None:
        """Ecran « Wireshark manquant ». Construit toujours, affiche si besoin.

        Le construire ici et pas a la volee le fait survivre au changement de
        theme, qui detruit et reconstruit tout l'arbre des widgets.
        """
        pal, fonts = self.pal, self.fonts
        self.prereq = tk.Frame(parent, background=pal.surface, highlightthickness=1,
                               highlightbackground=pal.warn, highlightcolor=pal.warn)
        inner = tk.Frame(self.prereq, background=pal.surface)
        inner.pack(fill="x", padx=16, pady=12)
        tk.Label(inner, text="⚠  Wireshark is required, and it could not be found",
                 background=pal.surface, foreground=pal.warn,
                 font=fonts.bold).pack(anchor="w")
        theme.label(
            inner, pal, fonts,
            "lwvs does not sniff the network itself: it delegates to tshark, "
            "which ships with Wireshark.\n"
            "Install Wireshark leaving Npcap ticked, then restart lwvs.\n"
            "When Npcap installs, DO NOT tick \"Restrict Npcap driver's access "
            "to Administrators only\":\n"
            "ticked, no interface will see a single packet unless lwvs runs as "
            "administrator.",
            kind="muted").pack(anchor="w", pady=(6, 10))
        ttk.Button(inner, text="Open the download page",
                   style="Accent.TButton",
                   command=lambda: webbrowser.open(_WIRESHARK_URL)).pack(anchor="w")

    def _apply_prereq(self) -> None:
        """Accorde l'interface au prerequis. Idempotent, rejoue apres _build."""
        if not self._tshark_missing:
            self.prereq.pack_forget()
            return
        self.prereq.pack(fill="x", padx=24, pady=(20, 0), before=self.body_frame)
        for name in ("detect_btn", "start_btn", "stop_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.configure(state="disabled")
        self._status("Wireshark missing", "err")

    # -- ecran 1 : capture ------------------------------------------------
    def _build_capture_page(self, page: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        head = self._page_head(
            page, "Capture", "Open each VS tab in the game and let it load: the game "
                             "only sends a ranking when its tab loads.")
        self.stop_btn = ttk.Button(head, text="■  Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        theme.Tooltip(self.stop_btn, "Stop capturing   ·   F5", pal, fonts)
        self.start_btn = ttk.Button(head, text="▶  Start", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.pack(side="right", padx=(0, 8))
        theme.Tooltip(self.start_btn,
                      "Start capturing   ·   F5\n"
                      "The first time, the interface and port are found by\n"
                      "themselves before the capture starts.", pal, fonts)

        tiles = tk.Frame(page, background=pal.bg)
        tiles.pack(fill="x", pady=(16, 12))
        for i in range(3):
            tiles.columnconfigure(i, weight=1, uniform="tile")
        guard = self._tile(tiles, 0, "Decoded fully")
        # Le garde-fou est LE chiffre qui dit si on decode ce qu'on croit : il
        # merite une tuile, pas une ligne de texte gris perdue dans le journal.
        self.guard_bar = ttk.Progressbar(guard, style="Ok.Horizontal.TProgressbar",
                                         mode="determinate", maximum=100, value=0)
        self.guard_bar.pack(fill="x", pady=(2, 5))
        theme.label(guard, pal, fonts, kind="muted", textvariable=self.guard_var,
                    wraplength=220).pack(fill="x")
        theme.Tooltip(self.guard_bar,
                      "Share of payloads decoded down to their last byte.\n"
                      "Below 100 %, you are not decoding what you think.",
                      pal, fonts)
        net = self._tile(tiles, 1, "Network")
        theme.label(net, pal, fonts, kind="muted", textvariable=self.net_var,
                    wraplength=220).pack(fill="x")
        ally = self._tile(tiles, 2, "Your alliance")
        theme.label(ally, pal, fonts, kind="muted", textvariable=self.alliance_var,
                    wraplength=220).pack(fill="x")

        # La barre d'actions est posee AVANT le tableau : `pack` sert dans
        # l'ordre, et un tableau extensible pose d'abord ne lui laisserait rien.
        bar = tk.Frame(page, background=pal.bg)
        bar.pack(side="bottom", fill="x", pady=(12, 16))
        self._on_bg(bar, "Day").pack(side="left")
        ttk.Entry(bar, textvariable=self.day_var, width=4).pack(side="left",
                                                                padx=(6, 14))
        self._on_bg(bar, "Event").pack(side="left")
        # Liste fermee : un libelle libre fragmenterait le suivi en aval.
        self.event_box = ttk.Combobox(bar, textvariable=self.event_var, width=22,
                                      state="readonly")
        self.event_box.pack(side="left", padx=6)
        theme.Tooltip(self.event_box, "Day and event are declared by you,\n"
                                      "not read from the game.", pal, fonts)
        add_btn = ttk.Button(bar, text="+", width=3, style="Ghost.TButton",
                             command=self._add_event)
        add_btn.pack(side="left")
        theme.Tooltip(add_btn, "Add an event to the catalogue", pal, fonts)
        self._reload_events()
        self.log_btn = ttk.Button(bar, style="Ghost.TButton", command=self._toggle_log)
        self.log_btn.pack(side="left", padx=(14, 0))

        self.send_btn = ttk.Button(bar, text="Send to the site",
                                   style="Accent.TButton",
                                   command=self._publish_all, state="disabled")
        self.send_btn.pack(side="right")
        theme.Tooltip(self.send_btn,
                      "Publishes every ready ranking   ·   Ctrl+↵\n"
                      "This sends the names and ids of every player in them.",
                      pal, fonts)
        self.exportall_btn = ttk.Button(bar, text="Save JSON…",
                                        command=self._export_all, state="disabled")
        self.exportall_btn.pack(side="right", padx=8)
        theme.Tooltip(self.exportall_btn,
                      "Writes one file per ready ranking   ·   Ctrl+Shift+E\n"
                      "Double-click a row to save only that one   ·   Ctrl+E",
                      pal, fonts)

        self.tree_holder = tk.Frame(page, background=pal.surface, highlightthickness=1,
                                    highlightbackground=pal.border,
                                    highlightcolor=pal.border)
        self.tree_holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(self.tree_holder, columns=("vu", "etat", "lignes"),
                                 show="tree headings", height=7, selectmode="browse")
        self.tree.heading("#0", text="Ranking", anchor="w")
        self.tree.heading("vu", text="Messages", anchor="center")
        self.tree.heading("etat", text="Status", anchor="w")
        self.tree.heading("lignes", text="Rows", anchor="e")
        self.tree.column("#0", width=280, minwidth=180)
        self.tree.column("vu", width=80, anchor="center", stretch=False)
        self.tree.column("etat", width=260, anchor="w", stretch=False)
        self.tree.column("lignes", width=60, anchor="e", stretch=False)
        # Une ligne prete garde la couleur du texte : peindre en vert les 4/5
        # des lignes ne signale plus rien. Seul l'anormal prend une couleur.
        for tag, colour in (("warn", pal.warn), ("muted", pal.muted)):
            self.tree.tag_configure(tag, foreground=colour)
        scroll = ttk.Scrollbar(self.tree_holder, style="Slim.Vertical.TScrollbar",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())
        self.tree.bind("<Double-1>", lambda _e: self._export())

        self._build_log(page)

    def _tile(self, parent: tk.Frame, column: int, title: str) -> tk.Frame:
        outer, body = theme.card(parent, self.pal, self.fonts, title)
        outer.grid(row=0, column=column, sticky="nsew",
                   padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        return body

    def _build_log(self, page: tk.Frame) -> None:
        """Journal replie sous le tableau : utile quand ca casse, pas a chaque clic.

        Toujours construit (tout le code y ecrit), seulement affiche a la demande.
        """
        pal, fonts = self.pal, self.fonts
        self.log_panel = tk.Frame(page, background=pal.bg)
        holder = tk.Frame(self.log_panel, background=pal.bg)
        holder.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(holder, height=8, wrap="word", state="disabled",
                           background=pal.field, foreground=pal.text,
                           insertbackground=pal.text, selectbackground=pal.accent_soft,
                           font=fonts.mono, relief="flat", borderwidth=0,
                           highlightthickness=1, highlightbackground=pal.border,
                           highlightcolor=pal.border, padx=10, pady=8, spacing1=1)
        self.log.tag_configure("time", foreground=pal.muted)
        self.log.tag_configure("detail", foreground=pal.muted)
        self.log.tag_configure("err", foreground=pal.err)
        self.log.tag_configure("ok", foreground=pal.ok)
        scroll = ttk.Scrollbar(holder, style="Slim.Vertical.TScrollbar",
                               command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bar = tk.Frame(self.log_panel, background=pal.bg)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="Copy", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right")
        ttk.Button(bar, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right", padx=8)
        self._toggle_log(self._log_open)

    def _toggle_log(self, show: bool | None = None) -> None:
        self._log_open = (not self._log_open) if show is None else show
        if self._log_open:
            self.log_panel.pack(side="bottom", fill="x", before=self.tree_holder)
        else:
            self.log_panel.pack_forget()
        self.log_btn.configure(text="Hide log" if self._log_open else "Show log")

    # -- ecran 2 : mails du jeu -------------------------------------------
    def _build_mailbox_page(self, page: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        self._page_head(page, "Mailbox",
                        "Read from the game's own mailbox on this PC — no capture "
                        "needed. Just open the game once after the battle.")
        outer, card_body = theme.card(page, pal, fonts)
        outer.pack(fill="both", expand=True, pady=(16, 16))
        # Storm et trains ont la meme source et les memes gestes : deux onglets
        # d'un meme ecran, pas deux ecrans.
        self.results_tabs = ttk.Notebook(card_body, style="Card.TNotebook")
        self.results_tabs.pack(fill="both", expand=True)
        storm_tab = tk.Frame(self.results_tabs, background=pal.surface)
        trains_tab = tk.Frame(self.results_tabs, background=pal.surface)
        self.results_tabs.add(storm_tab, text="Storm")
        self.results_tabs.add(trains_tab, text="Trains")
        self._build_storm(storm_tab)
        self._build_trains(trains_tab)

    def _build_storm(self, body: tk.Frame) -> None:
        """Desert / Canyon Storm : hors capture, lu dans la base du jeu.

        Ecran a part, et non des lignes du tableau de la capture : ce tableau
        dit ce qu'une CAPTURE a vu, et les mails ne passent jamais sur le fil
        quand on les ouvre. Rien ici ne depend de tshark.
        """
        pal, fonts = self.pal, self.fonts
        theme.label(body, pal, fonts, kind="muted",
                    text="One battle per team: Canyon on Thursday, Desert on "
                         "Friday.", wraplength=520).pack(fill="x", pady=(0, 10))

        bar = tk.Frame(body, background=pal.surface)
        bar.pack(fill="x")
        theme.label(bar, pal, fonts, "Since", kind="muted").pack(side="left")
        self._since_box(bar, self.storm_since_var, self._refresh_storm)
        ttk.Button(bar, text="↻", width=3, style="Ghost.TButton",
                   command=self._refresh_storm).pack(side="left")
        self.storm_send_btn = ttk.Button(bar, text="Send to the site", state="disabled",
                                         style="Accent.TButton",
                                         command=self._storm_send)
        self.storm_send_btn.pack(side="right")
        theme.Tooltip(self.storm_send_btn,
                      "Writes one JSON per battle, then sends them to the site\n"
                      "(URL and token from Settings)", pal, fonts)
        self.storm_export_btn = ttk.Button(bar, text="Save JSON…", state="disabled",
                                           command=self._storm_export)
        self.storm_export_btn.pack(side="right", padx=8)
        theme.Tooltip(self.storm_export_btn, "One JSON file per battle", pal, fonts)

        holder = tk.Frame(body, background=pal.surface)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self.storm_tree = ttk.Treeview(
            holder, columns=("team", "players", "camps", "result"),
            show="tree headings", height=6, selectmode="none")
        for col, text, width, anchor in (("#0", "Battle", 190, "w"),
                                         ("team", "Team", 90, "w"),
                                         ("players", "Players", 70, "e"),
                                         ("camps", "Camps", 200, "w"),
                                         ("result", "Result", 60, "w")):
            self.storm_tree.heading(col, text=text, anchor=anchor)
            self.storm_tree.column(col, width=width, anchor=anchor,
                                   stretch=col in ("#0", "camps"))
        self.storm_tree.tag_configure("loss", foreground=pal.muted)
        scroll = ttk.Scrollbar(holder, style="Slim.Vertical.TScrollbar",
                               command=self.storm_tree.yview)
        self.storm_tree.configure(yscrollcommand=scroll.set)
        self.storm_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        theme.label(body, pal, fonts, kind="muted", textvariable=self.storm_state_var,
                    wraplength=520).pack(fill="x", pady=(8, 0))

    def _since_box(self, bar: tk.Frame, var: tk.StringVar, refresh) -> None:
        """Periode : un choix dans la liste, ou une date tapee a la main."""
        box = ttk.Combobox(bar, textvariable=var, values=SINCE_CHOICES, width=13)
        box.pack(side="left", padx=(6, 6))
        box.bind("<<ComboboxSelected>>", lambda _e: refresh())
        box.bind("<Return>", lambda _e: refresh())
        theme.Tooltip(box, "Pick a period, or type a date:\n"
                           "YYYY-MM-DD = from that day (UTC)", self.pal, self.fonts)

    def _build_trains(self, body: tk.Frame) -> None:
        """Trains d'alliance : conducteur, VIP ou Guardian, lus dans les mails.

        Meme source et meme discipline que Storm. Le tableau ne montre aucun
        pseudo : qui conduisait sort par l'export, pas par l'ecran.
        """
        pal, fonts = self.pal, self.fonts
        theme.label(body, pal, fonts, kind="muted",
                    text="One line per train: conductor, VIP or Guardian, "
                         "attacks.", wraplength=520).pack(fill="x", pady=(0, 10))

        bar = tk.Frame(body, background=pal.surface)
        bar.pack(fill="x")
        theme.label(bar, pal, fonts, "Since", kind="muted").pack(side="left")
        self._since_box(bar, self.trains_since_var, self._refresh_trains)
        ttk.Button(bar, text="↻", width=3, style="Ghost.TButton",
                   command=self._refresh_trains).pack(side="left")
        self.trains_send_btn = ttk.Button(bar, text="Send to the site",
                                          state="disabled", style="Accent.TButton",
                                          command=self._trains_send)
        self.trains_send_btn.pack(side="right")
        theme.Tooltip(self.trains_send_btn,
                      "Writes one JSON per train, then sends them to the site\n"
                      "(URL and token from Settings)", pal, fonts)
        self.trains_export_btn = ttk.Button(bar, text="Save JSON…", state="disabled",
                                            command=self._trains_export)
        self.trains_export_btn.pack(side="right", padx=8)
        theme.Tooltip(self.trains_export_btn, "One JSON file per train", pal, fonts)

        holder = tk.Frame(body, background=pal.surface)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self.trains_tree = ttk.Treeview(
            holder, columns=("escort", "passengers", "trip"),
            show="tree headings", height=6, selectmode="none")
        for col, text, width, anchor in (("#0", "Train (game day)", 220, "w"),
                                         ("escort", "Escort", 90, "w"),
                                         ("passengers", "Passengers", 80, "e"),
                                         ("trip", "Trip", 200, "w")):
            self.trains_tree.heading(col, text=text, anchor=anchor)
            self.trains_tree.column(col, width=width, anchor=anchor,
                                    stretch=col in ("#0", "trip"))
        self.trains_tree.tag_configure("enroute", foreground=pal.muted)
        scroll = ttk.Scrollbar(holder, style="Slim.Vertical.TScrollbar",
                               command=self.trains_tree.yview)
        self.trains_tree.configure(yscrollcommand=scroll.set)
        self.trains_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        theme.label(body, pal, fonts, kind="muted", textvariable=self.trains_state_var,
                    wraplength=520).pack(fill="x", pady=(8, 0))

    # -- ecran 3 : reglages -----------------------------------------------
    def _build_settings_page(self, page: tk.Frame) -> None:
        self._page_head(page, "Settings")
        cols = tk.Frame(page, background=self.pal.bg)
        cols.pack(fill="both", expand=True, pady=(16, 16))
        cols.columnconfigure((0, 1), weight=1, uniform="col")
        left = tk.Frame(cols, background=self.pal.bg)
        left.grid(row=0, column=0, sticky="new", padx=(0, 8))
        right = tk.Frame(cols, background=self.pal.bg)
        right.grid(row=0, column=1, sticky="new", padx=(8, 0))
        self._build_send(left)
        self._build_source(right)
        self._build_data(right)

    def _build_send(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Stats site")
        outer.pack(fill="x")
        body.columnconfigure(0, weight=1)

        theme.label(body, pal, fonts, "URL", kind="muted").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.url_var, width=10).grid(
            row=1, column=0, sticky="we", pady=(3, 0))

        theme.label(body, pal, fonts, "Token", kind="muted").grid(
            row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(body, textvariable=self.token_var, width=10, show="•").grid(
            row=3, column=0, sticky="we", pady=(3, 0))

        theme.Check(body, pal, fonts, "Send automatically when a capture stops",
                    self.autosend_var).grid(row=4, column=0, sticky="w",
                                            pady=(10, 0))
        # Un jeton en clair dans un fichier est un vrai risque : on ne le retient
        # que sur demande explicite, et on le dit.
        theme.Check(body, pal, fonts, "Remember the token (plain text on this PC)",
                    self.keep_token_var).grid(row=5, column=0, sticky="w", pady=(4, 0))

        warn = tk.Frame(body, background=pal.surface)
        warn.grid(row=6, column=0, sticky="we", pady=(12, 0))
        dot = theme.Dot(warn, pal.surface)
        dot.pack(side="left", padx=(0, 7), pady=(4, 0), anchor="n")
        dot.colour(pal.warn)
        theme.label(warn, pal, fonts, kind="muted", wraplength=320,
                    text="Sending publishes the names and ids of every player "
                         "in the ranking.").pack(side="left", fill="x")

    def _build_source(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Network")
        outer.pack(fill="x", pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=90)

        theme.label(body, pal, fonts, kind="muted", wraplength=360,
                    text="Found by itself when you press Start. Change it only "
                         "if detection fails.").grid(row=0, column=0, columnspan=2,
                                                     sticky="w", pady=(0, 10))
        theme.label(body, pal, fonts, "Network interface", kind="muted").grid(
            row=1, column=0, sticky="w")
        theme.label(body, pal, fonts, "Game port", kind="muted").grid(
            row=1, column=1, sticky="w", padx=(10, 0))
        self.iface_box = ttk.Combobox(body, textvariable=self.iface_var,
                                      state="readonly", width=10)
        self.iface_box.grid(row=2, column=0, sticky="we", pady=(3, 0))
        self.iface_box.configure(values=self._iface_values)
        # Le device reste consultable : c'est lui qu'on copie dans un --iface.
        theme.Tooltip(self.iface_box,
                      lambda: self._iface_device() or "no interface", pal, fonts)
        port_entry = ttk.Entry(body, textvariable=self.port_var, width=8)
        port_entry.grid(row=2, column=1, sticky="we", padx=(10, 0), pady=(3, 0))

        # Un seul bouton : interface et port se prouvent par le MEME signal,
        # une trame du jeu qui decode jusqu'a son dernier octet. Les demander
        # separement etait deux attentes a la file pour une seule reponse.
        self.detect_btn = ttk.Button(body, text="⌖  Detect again",
                                     style="Ghost.TButton", command=self._detect)
        self.detect_btn.grid(row=3, column=0, columnspan=2, sticky="w",
                             pady=(12, 0))
        theme.Tooltip(self.detect_btn,
                      "Listens on every interface at once and stops as soon\n"
                      "as a game frame is recognised — a few seconds if the\n"
                      "game is running.   ·   F4",
                      pal, fonts)

    def _build_data(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Data and display")
        outer.pack(fill="x")
        theme.Check(body, pal, fonts, "My alliance only", self.mine_var,
                    command=self._refresh_feeds).pack(anchor="w")
        keep_box = theme.Check(body, pal, fonts, "Keep history locally",
                               self._keep, command=self._refresh_feeds)
        keep_box.pack(anchor="w", pady=(4, 0))
        theme.Tooltip(keep_box, "Unticked, the session database is deleted on\n"
                                "close: only the JSON exports remain.",
                      pal, fonts)
        ttk.Button(body, text="☀  Light theme" if pal.dark else "☾  Dark theme",
                   style="Ghost.TButton",
                   command=self._toggle_theme).pack(anchor="w", pady=(12, 0))

    def _bind_keys(self) -> None:
        # Les raccourcis sont annonces dans les bulles d'aide : un raccourci
        # qui ne se decouvre pas n'existe pas.
        self.root.bind("<F5>", lambda _e: self._toggle_capture())
        self.root.bind("<F4>", lambda _e: self._detect())
        self.root.bind("<Control-e>", lambda _e: self._export())
        self.root.bind("<Control-E>", lambda _e: self._export_all())
        self.root.bind("<Control-Return>", lambda _e: self._publish_all())

    def _toggle_theme(self) -> None:
        self.prefs["theme"] = "clair" if self.pal.dark else "sombre"
        _save_prefs(self.db_path, self.prefs)
        history = self.log.get("1.0", "end-1c")
        self.frame.destroy()
        self.pal, self.fonts = theme.apply(self.root, self.prefs["theme"])
        self._build()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", history + "\n" if history else "")
        self.log.see("end")
        self.log.configure(state="disabled")
        self._refresh_feeds()
        self._refresh_storm()
        self._refresh_trains()

    # -- utilitaires UI ---------------------------------------------------
    def _say(self, text: str, kind: str = "") -> None:
        if not kind and text.startswith("  "):
            kind = "err" if "FAILED" in text else "detail"
        if not kind and text.startswith("[error]"):
            kind = "err"
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  ", ("time",))
        self.log.insert("end", f"{text}\n", (kind,) if kind else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))
        self._say("log copied to the clipboard.")

    def _status(self, text: str, kind: str = "idle") -> None:
        self.status_var.set(text)
        self.state_dot.colour({"idle": self.pal.muted, "busy": self.pal.accent,
                               "ok": self.pal.ok, "err": self.pal.err}[kind])

    def _set_busy(self, delta: int) -> None:
        """Compteur, pas booleen : deux taches de fond se chevauchent souvent."""
        was, self._busy = self._busy, max(0, self._busy + delta)
        if not was and self._busy:
            self.busy_bar.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
            self.busy_bar.start(14)
        elif was and not self._busy:
            self.busy_bar.stop()
            self.busy_bar.pack_forget()

    def _spawn(self, fn, *args) -> None:
        self._set_busy(+1)

        def run():
            try:
                fn(*args)
            except Exception as exc:  # remonte dans le journal, pas dans le vide
                self.q.put(Msg("error", f"{type(exc).__name__}: {exc}"))
                self.q.put(Msg("log", traceback.format_exc(limit=3)))
            finally:
                self.q.put(Msg("busy", -1))
        t = threading.Thread(target=run, daemon=True)
        t.start()

    # -- interfaces et port ----------------------------------------------
    def _load_interfaces(self) -> None:
        def work():
            try:
                ifaces = capture.list_interfaces()
            except TsharkNotFound:
                # Ce n'est pas une panne du programme mais un prerequis absent :
                # `_spawn` en ferait une trace de pile illisible pour un joueur.
                self.q.put(Msg("no_tshark", None))
                return
            self.q.put(Msg("ifaces", ifaces))
        self._spawn(work)

    def _detect(self) -> None:
        """Interface ET port en une seule écoute, arrêtée dès la preuve.

        La variable Tk est lue ICI, sur le thread UI : la relire depuis le
        worker lèverait « main thread is not in main loop ».
        """
        prefer = self._iface_device()
        self._say("detecting: listening on every interface at once, stopping "
                  "as soon as a game frame is recognised. Keep the game open.")
        self._status("detecting…", "busy")
        self.detect_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")

        def work():
            try:
                def tick(remaining: int, packets: int, exact: int) -> None:
                    self.q.put(Msg("detecting", (remaining, packets, exact)))

                found = capture.autodetect(seconds=_DETECT_SECONDS, prefer=prefer,
                                           on_progress=tick)
                self.q.put(Msg("detected", found))
            finally:
                self.q.put(Msg("detect_end", None))
        self._spawn(work)

    def _detected(self, found: "capture.Detection") -> None:
        then_start, self._start_after_detect = self._start_after_detect, False
        for att in found.attempts[:4]:
            best = att.best
            note = (f"port {best.port} · {best.exact} exact frames"
                    if best is not None and best.exact
                    else (att.error or f"{att.report.total_packets} packets, "
                                       "no game frame"))
            self._say(f"  {att.iface.name or att.iface.device}: {note}")
        if found.iface is not None:
            self._set_iface(found.iface.device)
        problem = found.diagnosis()
        if problem:
            self._say(f"[error] {problem}")
            self._status("detection found nothing", "err")
            return
        self.port_var.set(str(found.port))
        self._say(f"-> {found.iface.name or found.iface.device}, port "
                  f"{found.port} — found in {found.seconds:.0f} s.", "ok")
        self._status("interface and port found", "ok")
        if then_start:
            self._start()

    def _iface_device(self) -> str:
        """Le device tshark derriere le libelle affiche -- la vraie valeur."""
        value = self.iface_var.get()
        if not value:
            return ""
        # Un libelle inconnu de la table ne peut venir que d'une saisie directe
        # du device (tests, liste jamais chargee) : on le rend tel quel.
        return self._iface_by_label.get(value, value)

    def _set_iface(self, device: str) -> None:
        """Selectionne une interface par son device, quel que soit le libelle."""
        for label, dev in self._iface_by_label.items():
            if dev == device:
                self.iface_var.set(label)
                return
        self.iface_var.set(device)

    # -- capture ----------------------------------------------------------
    def _toggle_capture(self) -> None:
        if str(self.stop_btn.cget("state")) == "normal":
            self._stop()
        elif str(self.start_btn.cget("state")) == "normal":
            self._start()

    def _start(self) -> None:
        iface, port = self._iface_device(), self.port_var.get().strip()
        if not iface or not port.isdigit():
            # Rien a demander au joueur : la detection trouve les deux, et la
            # capture demarre d'elle-meme a la preuve.
            self._start_after_detect = True
            self._detect()
            return
        self.prefs.update({"iface": iface, "port": port, "mine": self.mine_var.get(),
                           "keep": self._keep.get()})
        _save_prefs(self.db_path, self.prefs)
        self.source = LiveSource(iface, int(port))
        self.started_at = datetime.now(timezone.utc)
        self._seen = {}
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._status("capturing…", "busy")
        self._say(f"capture started on port {port}")

        source = self.source
        # Resolu ICI : `_active_db()` lit la variable Tk `_keep`, et la lire
        # depuis le thread de capture leve "main thread is not in main loop".
        db = self._active_db()

        def work():
            def on_progress(stats, counters):
                self.q.put(Msg("progress", (stats.payloads, stats.decoded,
                                            stats.exact, stats.failed,
                                            dict(counters.commands))))
            result = None
            try:
                with Store(db) as store:
                    result = ingest(source, store, note="gui", on_progress=on_progress)
            finally:
                # Meme en cas d'echec : sans "done", Start resterait grise.
                self.q.put(Msg("done", result))
        self._spawn(work)

    def _stop(self) -> None:
        if self.source is not None:
            self.source.stop()
            self._status("stopping…", "busy")
            self._say("stop requested — frames already received are being processed.")
        self.stop_btn.configure(state="disabled")

    # -- boucle de drainage ----------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(_POLL_MS, self._drain)

    def _handle(self, msg: Msg) -> None:
        if msg.kind == "log":
            self._say(msg.payload)
        elif msg.kind == "error":
            self._say(f"[error] {msg.payload}")
            self._status("error", "err")
        elif msg.kind == "busy":
            self._set_busy(msg.payload)
        elif msg.kind == "no_tshark":
            self._tshark_missing = True
            self._apply_prereq()
            self._say("tshark not found: install Wireshark (see the top of the "
                      "window), then restart lwvs.", "err")
        elif msg.kind == "ifaces":
            self._iface_by_label = _iface_labels(msg.payload)
            values = list(self._iface_by_label)
            self._iface_values = values
            self.iface_box.configure(values=values)
            if not self.iface_var.get():
                # On restaure le dernier choix plutot que de prendre la premiere
                # interface de la liste : elle est souvent morte. La preference
                # retient le DEVICE : un libelle usuel peut changer de sens
                # d'une machine a l'autre, le device non.
                remembered = self.prefs.get("iface", "")
                match = next((lbl for lbl, dev in self._iface_by_label.items()
                              if dev == remembered), None) if remembered else None
                self.iface_var.set(match or (values[0] if values else ""))
        elif msg.kind == "detecting":
            remaining, packets, exact = msg.payload
            self._status(f"detecting… {remaining} s max · {packets} packets"
                         + (f" · {exact} game frames" if exact else ""), "busy")
        elif msg.kind == "detected":
            self._detected(msg.payload)
        elif msg.kind == "detect_end":
            self._start_after_detect = False
            self.detect_btn.configure(state="normal")
            if str(self.stop_btn.cget("state")) != "normal":
                self.start_btn.configure(state="normal")
        elif msg.kind == "progress":
            if msg.payload is None:
                self._status("ready")
                return
            payloads, decoded, exact, failed, commands = msg.payload
            elapsed = ""
            if self.started_at:
                secs = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
                elapsed = f"{secs // 60}:{secs % 60:02d}  ·  "
            self._status(
                f"{elapsed}{payloads} payloads · {decoded} decoded · {failed} failed",
                "busy")
            self._guard(exact, decoded)
            self._seen = dict(commands)
            self._update_seen(commands)
        elif msg.kind == "sent":
            self._sync_buttons()
            if msg.payload:
                self._say("send complete.", "ok")
                self._status("send complete", "ok")
        elif msg.kind == "done":
            self._finish(msg.payload)
        elif msg.kind == "storm_list":
            self._storm_listed(msg.payload)
        elif msg.kind == "storm_unavailable":
            self._storm_rows = []
            self.storm_tree.delete(*self.storm_tree.get_children())
            self.storm_state_var.set(f"⚠ {msg.payload}")
            self._sync_storm()
        elif msg.kind == "storm_done":
            self._sync_storm()
            self._status(*msg.payload)
        elif msg.kind == "train_list":
            self._trains_listed(msg.payload)
        elif msg.kind == "train_unavailable":
            self._train_rows = []
            self.trains_tree.delete(*self.trains_tree.get_children())
            self.trains_state_var.set(f"⚠ {msg.payload}")
            self._sync_trains()
        elif msg.kind == "train_done":
            self._sync_trains()
            self._status(*msg.payload)

    def _guard(self, exact: int, decoded: int) -> None:
        pct = 100.0 * exact / decoded if decoded else 0.0
        self.guard_bar.configure(
            value=pct,
            style=("Ok" if pct >= 99 else "Warn") + ".Horizontal.TProgressbar")
        if not decoded:
            self.guard_var.set("no capture yet")
            return
        self.guard_var.set(
            f"safeguard: {exact}/{decoded} decoded down to the last byte "
            f"({pct:.0f} %)"
            + ("" if pct >= 99
               else "  ⚠ below 100 %, you are not decoding what you think"))

    def _finish(self, result) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if result is None:      # le worker a echoue : l'erreur suit dans le journal
            return
        self.snapshot_id = None if result.dropped else result.snapshot_id
        st, c = result.stats, result.counters
        self._guard(st.exact, st.decoded)
        self._status(
            f"done — {st.payloads} payloads, {st.decoded} decoded, "
            f"{st.failed} failed", "err" if result.dropped else "ok")
        self._say(f"capture finished: snapshot #{result.snapshot_id}", "ok")
        if st.failures_by_tag:
            detail = "  ".join(f"0x{t:02x}×{n}" for t, n in st.failures_by_tag.most_common())
            self._say(f"failures by offending type byte: {detail}")
        for name, n in sorted(c.commands.items(), key=lambda kv: -kv[1]):
            self._say(f"  {n:4d}  {name}")
        self._seen = dict(c.commands)
        if result.dropped:
            self._say("no usable message: empty snapshot dropped. "
                      "Was the VS screen actually reloaded?", "err")
        self._refresh_feeds()
        if self.autosend_var.get() and self.url_var.get().strip():
            self._publish_all()

    # -- tableau des flux -------------------------------------------------
    def _update_seen(self, commands: dict[str, int]) -> None:
        for feed in exporter.FEEDS:
            n = commands.get(feed.command, 0)
            if not n:
                continue
            for item in self.tree.get_children():
                if item == f"feed:{feed.key}" or item.startswith(f"feed:{feed.key}:"):
                    self.tree.set(item, "vu", str(n))

    def _refresh_feeds(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows: dict[str, dict[str, Any]] = {}

        snapshot = self.snapshot_id or self._latest_snapshot()

        with Store(self._active_db()) as store:
            if self.mine_var.get() and snapshot is not None:
                try:
                    own = exporter.resolve_own_alliance(store, snapshot)
                    self.alliance_var.set(
                        f"{own.label}  ·  from the "
                        f"{_SOURCE_LABELS.get(own.source, own.source)}")
                except ValueError:
                    self.alliance_var.set(
                        "⚠ not identified — open the Duel screen or the "
                        "member list once, it will be remembered")
            elif not self.mine_var.get():
                self.alliance_var.set("every alliance (see Settings)")
            else:
                self.alliance_var.set("known after the first capture")
            for feed in exporter.FEEDS:
                alliance = (exporter.MINE
                            if self.mine_var.get() and not feed.inherently_mine
                            else None)
                days = [None]
                if feed.per_day and snapshot is not None:
                    days = self._days(store, snapshot) or [None]
                for day in days:
                    key = f"feed:{feed.key}" + (f":{day}" if day is not None else "")
                    label = feed.label + (f" — day {day}" if day is not None else "")
                    seen = self._seen.get(feed.command, 0)
                    count, state, tag = 0, "not seen yet", "muted"
                    if snapshot is not None:
                        try:
                            rows = exporter.collect(
                                store, feed.dataset, scope=feed.scope, day=day,
                                alliance=alliance, snapshot=snapshot)
                            count = len(rows)
                            state, tag = self._etat(count, seen)
                        except ValueError as exc:
                            state, tag = str(exc)[:60], "warn"
                    self.tree.insert("", "end", iid=key, text=label,
                                     tags=(tag,) if tag else (),
                                     values=(seen or "", state, count or ""))
                    self._rows[key] = {"feed": feed, "day": day, "count": count}
        self._sync_buttons()

    def _etat(self, count: int, seen: int) -> tuple[str, str]:
        """Libelle d'une ligne du tableau.

        « aucune ligne » melangeait deux situations que rien ne separait a
        l'ecran : le jeu n'a jamais envoye le message, ou il l'a envoye et on
        n'en a rien tire. La premiere se repare en jeu (rouvrir l'ecran), la
        seconde est un vrai defaut de decodage. Les confondre a fait chercher
        un bug la ou il n'y en avait pas.

        `_seen` est vide tant qu'aucune capture n'a tourne dans cette session :
        on ne peut alors rien affirmer sur ce que le jeu a envoye, et on s'en
        tient au constat brut.
        """
        if count:
            return "✓  ready", ""
        if not self._seen:
            return "no rows", "muted"
        if not seen:
            return "message never received — reopen the screen", "muted"
        return "received, but no rows: please report", "warn"

    def _active_db(self) -> str:
        return self.db_path if self._keep.get() else str(self._temp_db)

    def stop_polling(self) -> None:
        """Arrete la boucle de drainage. Idempotent."""
        if getattr(self, "_poll_id", None) is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _on_close(self) -> None:
        self.stop_polling()
        try:
            self.prefs["geometry"] = self.root.winfo_geometry()
            _save_prefs(self.db_path, self.prefs)
        except tk.TclError:
            pass
        if not self._keep.get():
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(self._temp_db) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
        self.root.destroy()

    @staticmethod
    def _days(store: Store, snapshot: int) -> list[int]:
        return sorted(
            {r["day"] for r in store.fetch_players(scope="day", snapshot=snapshot)
             if r["day"] is not None}
        )

    def _latest_snapshot(self) -> int | None:
        with Store(self._active_db()) as store:
            snaps = store.snapshots()
        return snaps[-1].id if snaps else None

    def _sync_buttons(self) -> None:
        n = sum(1 for v in getattr(self, "_rows", {}).values() if v["count"])
        self.exportall_btn.configure(state="normal" if n else "disabled")
        self.send_btn.configure(
            state="normal" if n else "disabled",
            text=f"Send {n} ranking{'s' * (n > 1)} to the site" if n else "Send to the site")

    # -- export -----------------------------------------------------------
    def _export(self) -> None:
        sel = self.tree.selection()
        if not sel or not self._rows.get(sel[0], {}).get("count"):
            return
        entry = self._rows[sel[0]]
        feed, day = entry["feed"], entry["day"]
        suffix = f"_j{day}" if day is not None else ""
        path = filedialog.asksaveasfilename(
            title=f"Export — {feed.label}",
            defaultextension=".json",
            initialfile=f"lwvs_{feed.key}{suffix}.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        self._write(feed, day, path)

    def _export_all(self) -> None:
        if not any(v["count"] for v in getattr(self, "_rows", {}).values()):
            return
        folder = filedialog.askdirectory(title="Destination folder")
        if not folder:
            return
        for entry in self._rows.values():
            if not entry["count"]:
                continue
            feed, day = entry["feed"], entry["day"]
            suffix = f"_j{day}" if day is not None else ""
            self._write(feed, day, str(Path(folder) / f"lwvs_{feed.key}{suffix}.json"))

    def _reload_events(self) -> None:
        catalogue = events_mod.load()
        libelles = [""] + [e.label for e in catalogue]
        self.event_box.configure(values=libelles)
        if self.event_var.get() not in libelles:
            self.event_var.set("")

    def _add_event(self) -> None:
        from tkinter import simpledialog

        libelle = simpledialog.askstring(
            TITLE, "Name of the new event (e.g. S4 - Another event):",
            parent=self.root)
        if not libelle or not libelle.strip():
            return
        cree = events_mod.add(libelle.strip())
        self._reload_events()
        self.event_var.set(cree.label)
        self._say(f"event added: {cree.id}  ({cree.label})")

    def _declared(self) -> tuple[int | None, str | None]:
        """Lu sur le thread UI : ce sont des variables Tk."""
        brut = self.day_var.get().strip()
        jour = int(brut) if brut.isdigit() else None
        return jour, (self.event_var.get().strip() or None)

    def _alliance_for(self, feed: exporter.Feed) -> str | None:
        return (exporter.MINE
                if self.mine_var.get() and not feed.inherently_mine else None)

    def _publish_all(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self._show_page("settings")
            messagebox.showinfo(TITLE, "Enter the site URL in Settings.")
            return
        token = self.token_var.get().strip() or None
        # On lit l'etat de l'UI ICI, sur le thread UI, et on ne passe au worker
        # que des donnees simples.
        # Tout ce que le worker utilisera est lu ICI, sur le thread UI, y
        # compris le filtre d'alliance : `mine_var` est une variable Tk, et la
        # lire depuis un autre thread leve "main thread is not in main loop".
        jobs = [(v["feed"], v["day"], self._alliance_for(v["feed"]))
                for v in self._rows.values() if v["count"]]
        if not jobs:
            messagebox.showinfo(TITLE, "Nothing to send: no usable capture.")
            return
        snapshot = self.snapshot_id or self._latest_snapshot()
        db = self._active_db()
        declared = self._declared()
        self._remember_send()
        self.send_btn.configure(state="disabled")
        self._status("sending…", "busy")
        self._say(f"sending {len(jobs)} ranking(s) to {url}…")

        def work():
            ok = False
            try:
                with Store(db) as store:
                    for feed, day, alliance in jobs:
                        try:
                            payload = self._payload(store, feed, day, snapshot,
                                                    alliance, declared)
                        except ValueError as exc:
                            self.q.put(Msg("log", f"  {feed.key}: refused — {exc}"))
                            continue
                        res = publish(payload, url, token=token)
                        tag = "OK" if res.ok else "FAILED"
                        self.q.put(Msg("log", f"  {feed.key}: {tag} HTTP {res.status}"
                                              f"  {res.response.strip()[:180] or res.error}"))
                ok = True
            finally:
                # Meme en cas d'echec : sans "sent", Send resterait grise.
                self.q.put(Msg("sent", ok))
        self._spawn(work)

    def _payload(self, store: Store, feed: exporter.Feed, day: int | None,
                 snapshot: int | None, alliance: str | None,
                 declared: tuple[int | None, str | None] = (None, None),
                 ) -> dict[str, Any]:
        """Ne lit AUCUNE variable Tk : appelable depuis le thread de travail."""
        rows = exporter.collect(store, feed.dataset, scope=feed.scope, day=day,
                                alliance=alliance, snapshot=snapshot)
        jour, event = declared
        return exporter.build_records(rows, feed.dataset, mode=feed.mode,
                                      scope=feed.scope, metric=feed.metric,
                                      context=exporter.context_for(store, snapshot),
                                      declared_day=jour, event=event)

    def _remember_send(self) -> None:
        self.prefs["post_url"] = self.url_var.get().strip()
        self.prefs["autosend"] = self.autosend_var.get()
        self.prefs["keep_token"] = self.keep_token_var.get()
        jour, event = self._declared()
        self.prefs["declare_day"] = jour
        self.prefs["event"] = event or ""
        # Le jeton n'est ecrit que si l'utilisateur l'a explicitement demande.
        if self.keep_token_var.get():
            self.prefs["post_token"] = self.token_var.get().strip()
        else:
            self.prefs.pop("post_token", None)
        _save_prefs(self.db_path, self.prefs)

    def _write(self, feed: exporter.Feed, day: int | None, path: str) -> None:
        snapshot = self.snapshot_id or self._latest_snapshot()
        try:
            with Store(self._active_db()) as store:
                written = exporter.run_export(
                    store, dataset=feed.dataset, fmt="records", out=path,
                    scope=feed.scope, day=day,
                    alliance=(exporter.MINE if self.mine_var.get()
                              and not feed.inherently_mine else None),
                    snapshot=snapshot, mode=feed.mode, metric=feed.metric,
                    declared_day=self._declared()[0], event=self._declared()[1],
                )
            self._say(f"written: {written[0]}", "ok")
        except ValueError as exc:
            self._say(f"[error] export refused: {exc}")
            messagebox.showerror(TITLE, str(exc))


    # -- Storm (mails du jeu) ----------------------------------------------
    # Meme discipline que la capture : les variables Tk sont lues ICI, sur le
    # thread UI, et le worker ne recoit que des valeurs simples.

    def _refresh_storm(self) -> None:
        text = self.storm_since_var.get()
        try:
            since = storm.since_ms(text)
        except ValueError as exc:
            self.storm_state_var.set(f"⚠ {exc}")
            return
        self.prefs["storm_since"] = text.strip()
        _save_prefs(self.db_path, self.prefs)
        db = self.storm_db
        self.storm_state_var.set("reading the game's mailbox…")

        def work():
            try:
                path = db or gamecache.default_db_path()
                if path is None:
                    raise gamecache.CacheUnavailable(
                        "game database not found under AppData\\LocalLow — has "
                        "the game been run on this PC, with this Windows account?")
                reports, n, dups, bad = storm.load_reports(path, since_ms=since)
            except gamecache.CacheUnavailable as exc:
                # Un prerequis, pas une panne : une phrase, pas une trace de pile.
                self.q.put(Msg("storm_unavailable", str(exc)))
                return
            # Seulement le resume affichable : aucun pseudo ne quitte le worker.
            self.q.put(Msg("storm_list",
                           ([storm.summary_of(r) for r in reports], n, dups, bad)))
        self._spawn(work)

    def _storm_listed(self, payload) -> None:
        rows, n, dups, bad = payload
        self._storm_rows = rows
        tree = self.storm_tree
        tree.delete(*tree.get_children())
        for i, r in enumerate(reversed(rows)):          # le plus recent en haut
            kind = "Canyon" if r["kind"] == "canyon_storm" else "Desert"
            team = (r["team"] or "?") + ("  (declared)" if r["declared"] else "")
            tree.insert("", "end", iid=f"storm:{i}", text=f"{kind}  ·  {r['date']}",
                        tags=("loss",) if r["result"] == "loss" else (),
                        values=(team, r["players"],
                                f"{r['side']}  vs  {r['opponents']}", r["result"]))
        state = f"{len(rows)} battle(s) in the period"
        if dups:
            state += f"  ·  {dups} duplicate mail(s) merged"
        if bad:
            state += f"  ·  {bad} unreadable"
        if any(r["declared"] for r in rows):
            state += ("  ·  Desert team is declared, not in the mail: "
                      "evening = A, early afternoon = B")
        if not rows:
            state = ("no Storm battle in the period — widen « Since » "
                     "(a date, or all)")
        self.storm_state_var.set(state)
        self._sync_storm()

    def _sync_storm(self) -> None:
        state = "normal" if self._storm_rows else "disabled"
        self.storm_export_btn.configure(state=state)
        self.storm_send_btn.configure(state=state)

    def _storm_export(self) -> None:
        if not self._storm_rows:
            return
        folder = filedialog.askdirectory(
            title="Destination folder", initialdir=self.prefs.get("storm_out") or None)
        if not folder:
            return
        self.prefs["storm_out"] = folder
        _save_prefs(self.db_path, self.prefs)
        self._storm_run(folder, None, None)

    def _storm_send(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self._show_page("settings")
            messagebox.showinfo(TITLE, "Enter the site URL in Settings.")
            return
        self._remember_send()
        # Les fichiers sont ecrits AVANT l'envoi, comme partout : un site
        # injoignable ne fait rien perdre.
        folder = self.prefs.get("storm_out") or str(identity.home() / "exports")
        self._storm_run(folder, url, self.token_var.get().strip() or None)

    def _storm_run(self, folder: str, url: str | None, token: str | None) -> None:
        try:
            since = storm.since_ms(self.storm_since_var.get())
        except ValueError as exc:
            self.storm_state_var.set(f"⚠ {exc}")
            return
        only_mine = self.mine_var.get()
        db = self.storm_db
        self.storm_export_btn.configure(state="disabled")
        self.storm_send_btn.configure(state="disabled")
        self._status("storm: sending…" if url else "storm: writing…", "busy")

        def work():
            try:
                res = storm.export_storm(folder, db_path=db, since_ms=since,
                                         only_mine=only_mine, post_url=url, token=token)
            except (gamecache.CacheUnavailable, ValueError, OSError) as exc:
                self.q.put(Msg("log", f"[error] storm: {exc}"))
                self.q.put(Msg("storm_done", ("storm: nothing written", "err")))
                return
            failed = 0
            for w in res.written:
                self.q.put(Msg("log", f"written: {w.path}  ({w.records} records)"))
                if w.published is not None:
                    pub = w.published
                    failed += not pub.ok
                    tag = "OK" if pub.ok else "FAILED"
                    self.q.put(Msg("log", f"  {w.path.stem}: {tag} HTTP {pub.status}"
                                          f"  {pub.response.strip()[:180] or pub.error}"))
            for label, why in res.skipped:
                self.q.put(Msg("log", f"  {label}: skipped — {why}"))
            done = f"storm: {len(res.written)} battle(s) written"
            if url:
                done += f", {len(res.written) - failed} sent"
            self.q.put(Msg("storm_done", (done, "err" if failed else "ok")))
        self._spawn(work)


    # -- Trains d'alliance (mails du jeu) ----------------------------------
    # Calque de Storm : variables Tk lues sur le thread UI, et seul le resume
    # sans pseudo (`trains.summary_of`) quitte le worker.

    def _refresh_trains(self) -> None:
        text = self.trains_since_var.get()
        try:
            since = trains.since_ms(text)
        except ValueError as exc:
            self.trains_state_var.set(f"⚠ {exc}")
            return
        self.prefs["trains_since"] = text.strip()
        _save_prefs(self.db_path, self.prefs)
        db = self.storm_db
        self.trains_state_var.set("reading the game's mailbox…")

        def work():
            try:
                path = db or gamecache.default_db_path()
                if path is None:
                    raise gamecache.CacheUnavailable(
                        "game database not found under AppData\\LocalLow — has "
                        "the game been run on this PC, with this Windows account?")
                runs, n, bad = trains.load_trains(path, since_ms=since)
            except gamecache.CacheUnavailable as exc:
                self.q.put(Msg("train_unavailable", str(exc)))
                return
            self.q.put(Msg("train_list",
                           ([trains.summary_of(r) for r in runs], n, bad)))
        self._spawn(work)

    def _trains_listed(self, payload) -> None:
        rows, n, bad = payload
        self._train_rows = rows
        tree = self.trains_tree
        tree.delete(*tree.get_children())
        for i, r in enumerate(reversed(rows)):          # le plus recent en haut
            label = f"{r['date']}  ·  {r['departed']} UTC"
            if r["purchased"]:
                label += "  ·  purchased"
            trip = (f"{r['attacks']} attack(s), {r['robbed']} robbed"
                    if r["arrived"] else "en route")
            tree.insert("", "end", iid=f"train:{i}", text=label,
                        tags=() if r["arrived"] else ("enroute",),
                        values=(r["escort"], r["passengers"], trip))
        state = f"{len(rows)} train(s) in the period"
        bought = sum(1 for r in rows if r["purchased"])
        if bought:
            state += f"  ·  {bought} purchased"
        if bad:
            state += f"  ·  {bad} unreadable"
        if not rows:
            state = "no train in the period — widen « Since » (a date, or all)"
        self.trains_state_var.set(state)
        self._sync_trains()

    def _sync_trains(self) -> None:
        state = "normal" if self._train_rows else "disabled"
        self.trains_export_btn.configure(state=state)
        self.trains_send_btn.configure(state=state)

    def _trains_export(self) -> None:
        if not self._train_rows:
            return
        folder = filedialog.askdirectory(
            title="Destination folder",
            initialdir=self.prefs.get("trains_out") or self.prefs.get("storm_out") or None)
        if not folder:
            return
        self.prefs["trains_out"] = folder
        _save_prefs(self.db_path, self.prefs)
        self._trains_run(folder, None, None)

    def _trains_send(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self._show_page("settings")
            messagebox.showinfo(TITLE, "Enter the site URL in Settings.")
            return
        self._remember_send()
        folder = self.prefs.get("trains_out") or str(identity.home() / "exports")
        self._trains_run(folder, url, self.token_var.get().strip() or None)

    def _trains_run(self, folder: str, url: str | None, token: str | None) -> None:
        try:
            since = trains.since_ms(self.trains_since_var.get())
        except ValueError as exc:
            self.trains_state_var.set(f"⚠ {exc}")
            return
        only_mine = self.mine_var.get()
        db = self.storm_db
        self.trains_export_btn.configure(state="disabled")
        self.trains_send_btn.configure(state="disabled")
        self._status("trains: sending…" if url else "trains: writing…", "busy")

        def work():
            try:
                res = trains.export_trains(folder, db_path=db, since_ms=since,
                                           only_mine=only_mine, post_url=url,
                                           token=token)
            except (gamecache.CacheUnavailable, ValueError, OSError) as exc:
                self.q.put(Msg("log", f"[error] trains: {exc}"))
                self.q.put(Msg("train_done", ("trains: nothing written", "err")))
                return
            failed = 0
            for w in res.written:
                self.q.put(Msg("log", f"written: {w.path}  ({w.records} records)"))
                if w.published is not None:
                    pub = w.published
                    failed += not pub.ok
                    tag = "OK" if pub.ok else "FAILED"
                    self.q.put(Msg("log", f"  {w.path.stem}: {tag} HTTP {pub.status}"
                                          f"  {pub.response.strip()[:180] or pub.error}"))
            for label, why in res.skipped:
                self.q.put(Msg("log", f"  {label}: skipped — {why}"))
            done = f"trains: {len(res.written)} train(s) written"
            if url:
                done += f", {len(res.written) - failed} sent"
            self.q.put(Msg("train_done", (done, "err" if failed else "ok")))
        self._spawn(work)


def main(db_path: str | None = None, keep: bool | None = None) -> int:
    root = tk.Tk()
    App(root, db_path, keep=keep)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
