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

from . import __version__, capture, events as events_mod, exporter, identity, theme
from .capture import LiveSource, TsharkNotFound
from .publish import publish
from .ingest import ingest
from .store import DEFAULT_DB, Store

TITLE = f"lwvs {__version__} — Last War VS capture"
_POLL_MS = 120
#: Plafond de la detection, pas une duree d'attente : elle s'arrete a la preuve.
_DETECT_SECONDS = 20
#: La colonne de gauche empile quatre cartes : en dessous de cette hauteur, le
#: bouton « Envoyer » passe sous le bord de la fenetre et devient introuvable.
_MIN_SIZE = (1000, 780)


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
        h = min(860, self.root.winfo_screenheight() - 90)
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        pal = self.pal
        self.frame = tk.Frame(self.root, background=pal.bg)
        self.frame.pack(fill="both", expand=True)

        self._build_header(self.frame)
        self._build_prereq(self.frame)

        body = tk.Frame(self.frame, background=pal.bg)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.body_frame = body
        body.columnconfigure(0, minsize=400)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, background=pal.bg)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = tk.Frame(body, background=pal.bg)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_source(left)
        self._build_capture(left)
        self._build_send(left)
        self._build_feeds(right)
        self._build_log(right)

        self._say("the JSON exports are the deliverable; nothing is kept "
                  "locally unless you tick \"Keep history locally\".")
        self._apply_prereq()

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
        self.prereq.pack(fill="x", padx=16, pady=(0, 12), before=self.body_frame)
        for name in ("detect_btn", "start_btn", "stop_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.configure(state="disabled")
        self._status("Wireshark missing", "err")

    def _build_header(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        head = tk.Frame(parent, background=pal.bg)
        head.pack(fill="x", padx=16, pady=(14, 12))

        left = tk.Frame(head, background=pal.bg)
        left.pack(side="left")
        tk.Label(left, text="lwvs", background=pal.bg, foreground=pal.text,
                 font=fonts.title).pack(side="left")
        tk.Label(left, text="Last War VS capture", background=pal.bg,
                 foreground=pal.muted, font=fonts.small).pack(side="left",
                                                              padx=(10, 0), pady=(6, 0))

        right = tk.Frame(head, background=pal.bg)
        right.pack(side="right")
        toggle = ttk.Button(right, text="☀" if pal.dark else "☾", width=3,
                            style="Icon.TButton", command=self._toggle_theme)
        toggle.pack(side="right", padx=(10, 0))
        theme.Tooltip(toggle, "Toggle light / dark", pal, fonts)

        # Barre indeterminee : elle ne dit pas « combien », elle dit « ca vit ».
        # Sans elle, une detection de 25 s est indiscernable d'un gel.
        self.busy_bar = ttk.Progressbar(right, style="Accent.Horizontal.TProgressbar",
                                        mode="indeterminate", length=90)

        pill = tk.Frame(right, background=pal.surface, highlightthickness=1,
                        highlightbackground=pal.border, highlightcolor=pal.border)
        pill.pack(side="right")
        self.state_dot = theme.Dot(pill, pal.surface)
        self.state_dot.pack(side="left", padx=(11, 7), pady=8)
        self.state_dot.colour(pal.muted)
        tk.Label(pill, textvariable=self.status_var, background=pal.surface,
                 foreground=pal.text, font=fonts.body).pack(side="left", padx=(0, 13))

    def _build_source(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Source", step="1")
        outer.pack(fill="x", pady=(0, 12))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=90)

        theme.label(body, pal, fonts, "Network interface", kind="muted").grid(
            row=0, column=0, sticky="w")
        theme.label(body, pal, fonts, "Game port", kind="muted").grid(
            row=0, column=1, sticky="w", padx=(10, 0))
        self.iface_box = ttk.Combobox(body, textvariable=self.iface_var,
                                      state="readonly", width=10)
        self.iface_box.grid(row=1, column=0, sticky="we", pady=(3, 0))
        self.iface_box.configure(values=self._iface_values)
        # Le device reste consultable : c'est lui qu'on copie dans un --iface.
        theme.Tooltip(self.iface_box,
                      lambda: self._iface_device() or "no interface", pal, fonts)
        port_entry = ttk.Entry(body, textvariable=self.port_var, width=8)
        port_entry.grid(row=1, column=1, sticky="we", padx=(10, 0), pady=(3, 0))

        # Un seul bouton : interface et port se prouvent par le MEME signal,
        # une trame du jeu qui decode jusqu'a son dernier octet. Les demander
        # separement etait deux attentes a la file pour une seule reponse.
        self.detect_btn = ttk.Button(body, text="⌖  Detect",
                                     style="Ghost.TButton", command=self._detect)
        self.detect_btn.grid(row=2, column=0, columnspan=2, sticky="we",
                             pady=(12, 0))
        theme.Tooltip(self.detect_btn,
                      "Listens on every interface at once and stops as soon\n"
                      "as a game frame is recognised — a few seconds if the\n"
                      "game is running.   ·   F4",
                      pal, fonts)

    def _build_capture(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Capture", step="2")
        outer.pack(fill="x", pady=(0, 12))

        row = tk.Frame(body, background=pal.surface)
        row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="▶  Start", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left")
        theme.Tooltip(self.start_btn, "Start capturing   ·   F5", pal, fonts)
        self.stop_btn = ttk.Button(row, text="■  Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        theme.Tooltip(self.stop_btn, "Stop capturing   ·   F5", pal, fonts)

        # Le garde-fou est LE chiffre qui dit si on decode ce qu'on croit : il
        # merite une barre, pas une ligne de texte gris perdue dans le journal.
        self.guard_bar = ttk.Progressbar(body, style="Ok.Horizontal.TProgressbar",
                                         mode="determinate", maximum=100, value=0)
        self.guard_bar.pack(fill="x", pady=(14, 5))
        theme.label(body, pal, fonts, kind="muted", textvariable=self.guard_var,
                    wraplength=340).pack(fill="x")
        theme.Tooltip(self.guard_bar,
                      "Share of payloads decoded down to their last byte.\n"
                      "Below 100 %, you are not decoding what you think.",
                      pal, fonts)

        tip = tk.Frame(body, background=pal.surface_alt, highlightthickness=1,
                       highlightbackground=pal.border)
        tip.pack(fill="x", pady=(12, 0))
        tk.Label(tip, text="Open the VS screen while capturing, tab by tab, and "
                           "let each one load: the game only sends a ranking "
                           "when its tab loads.",
                 background=pal.surface_alt, foreground=pal.muted, font=fonts.small,
                 justify="left", anchor="w", wraplength=340).pack(
            fill="x", padx=11, pady=8)

    def _build_feeds(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "What was seen", step="3")
        outer.pack(fill="both", expand=True, pady=(0, 12))

        holder = tk.Frame(body, background=pal.surface)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("vu", "etat", "lignes"),
                                 show="tree headings", height=7, selectmode="browse")
        self.tree.heading("#0", text="Statistic", anchor="w")
        self.tree.heading("vu", text="Messages", anchor="center")
        self.tree.heading("etat", text="Status", anchor="w")
        self.tree.heading("lignes", text="Rows", anchor="e")
        self.tree.column("#0", width=300, minwidth=180)
        self.tree.column("vu", width=90, anchor="center", stretch=False)
        self.tree.column("etat", width=150, anchor="w", stretch=False)
        self.tree.column("lignes", width=70, anchor="e", stretch=False)
        # Une ligne prete garde la couleur du texte : peindre en vert les 4/5
        # des lignes ne signale plus rien. Seul l'anormal prend une couleur.
        for tag, colour in (("warn", pal.warn), ("muted", pal.muted)):
            self.tree.tag_configure(tag, foreground=colour)
        scroll = ttk.Scrollbar(holder, style="Slim.Vertical.TScrollbar",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())
        self.tree.bind("<Double-1>", lambda _e: self._export())

        theme.label(body, pal, fonts, kind="muted", textvariable=self.alliance_var,
                    wraplength=520).pack(fill="x", pady=(10, 0))

        opts = tk.Frame(body, background=pal.surface)
        opts.pack(fill="x", pady=(8, 0))
        theme.Check(opts, pal, fonts, "My alliance only", self.mine_var,
                    command=self._refresh_feeds).pack(side="left")
        keep_box = theme.Check(opts, pal, fonts, "Keep history locally",
                               self._keep, command=self._refresh_feeds)
        keep_box.pack(side="left", padx=24)
        theme.Tooltip(keep_box, "Unticked, the session database is deleted on\n"
                                "close: only the JSON exports remain.",
                      pal, fonts)

        decl = tk.Frame(body, background=pal.surface)
        decl.pack(fill="x", pady=(12, 0))
        theme.label(decl, pal, fonts, "Day", kind="muted").pack(side="left")
        ttk.Entry(decl, textvariable=self.day_var, width=4).pack(side="left",
                                                                 padx=(6, 16))
        theme.label(decl, pal, fonts, "Event", kind="muted").pack(side="left")
        # Liste fermee : un libelle libre fragmenterait le suivi en aval.
        self.event_box = ttk.Combobox(decl, textvariable=self.event_var, width=26,
                                      state="readonly")
        self.event_box.pack(side="left", padx=6)
        add_btn = ttk.Button(decl, text="+", width=3, style="Ghost.TButton",
                             command=self._add_event)
        add_btn.pack(side="left")
        theme.Tooltip(add_btn, "Add an event to the catalogue", pal, fonts)
        self._reload_events()
        # Sous la ligne, pas a sa suite : a droite d'une combo large, la mise en
        # garde sortait du cadre et se faisait tronquer.
        theme.label(body, pal, fonts,
                    "these two fields are declared by you, not read from the game",
                    kind="muted").pack(fill="x", pady=(6, 0))

        actions = tk.Frame(body, background=pal.surface)
        actions.pack(fill="x", pady=(14, 0))
        self.export_btn = ttk.Button(actions, text="Export as JSON…",
                                     style="Accent.TButton",
                                     command=self._export, state="disabled")
        self.export_btn.pack(side="right")
        theme.Tooltip(self.export_btn,
                      "Exports the selected row   ·   Ctrl+E\n"
                      "(double-clicking the row works too)", pal, fonts)
        self.exportall_btn = ttk.Button(actions, text="Export all…",
                                        command=self._export_all, state="disabled")
        self.exportall_btn.pack(side="right", padx=8)
        theme.Tooltip(self.exportall_btn,
                      "Writes one file per ready ranking   ·   Ctrl+Shift+E",
                      pal, fonts)

    def _build_send(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Send to the site", step="4")
        outer.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        theme.label(body, pal, fonts, "URL", kind="muted").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.url_var, width=10).grid(
            row=1, column=0, sticky="we", pady=(3, 0))

        theme.label(body, pal, fonts, "Token", kind="muted").grid(
            row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(body, textvariable=self.token_var, width=10, show="•").grid(
            row=3, column=0, sticky="we", pady=(3, 0))

        theme.Check(body, pal, fonts, "Send after every capture",
                    self.autosend_var).grid(row=4, column=0, sticky="w",
                                            pady=(10, 0))
        # Un jeton en clair dans un fichier est un vrai risque : on ne le retient
        # que sur demande explicite, et on le dit.
        theme.Check(body, pal, fonts, "Remember the token (plain text on this PC)",
                    self.keep_token_var).grid(row=5, column=0, sticky="w", pady=(4, 0))

        self.send_btn = ttk.Button(body, text="Send now",
                                   style="Accent.TButton",
                                   command=self._publish_all, state="disabled")
        self.send_btn.grid(row=6, column=0, sticky="we", pady=(12, 0))
        theme.Tooltip(self.send_btn, "Publishes every ready ranking   ·   Ctrl+↵",
                      pal, fonts)

        warn = tk.Frame(body, background=pal.surface)
        warn.grid(row=7, column=0, sticky="we", pady=(10, 0))
        dot = theme.Dot(warn, pal.surface)
        dot.pack(side="left", padx=(0, 7), pady=(4, 0), anchor="n")
        dot.colour(pal.warn)
        theme.label(warn, pal, fonts, kind="muted", wraplength=320,
                    text="Sending publishes the names and ids of every player "
                         "in the ranking.").pack(side="left", fill="x")

    def _build_log(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, "Log",
                                 hint="this session's history")
        outer.pack(fill="both", expand=True)

        holder = tk.Frame(body, background=pal.surface)
        holder.pack(fill="both", expand=True)
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

        bar = tk.Frame(body, background=pal.surface)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Copy", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right")
        ttk.Button(bar, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right", padx=8)

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
            self.busy_bar.pack(side="right", padx=(0, 12))
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
            messagebox.showinfo(TITLE, "An interface and a numeric port are required.")
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
            with Store(db) as store:
                result = ingest(source, store, note="gui", on_progress=on_progress)
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
            self.detect_btn.configure(state="normal")
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
            self._say("send complete.", "ok")
            self._status("send complete", "ok")
        elif msg.kind == "done":
            self._finish(msg.payload)

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
        self.snapshot_id = None if result.dropped else result.snapshot_id
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
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
                        f"alliance: {own.label}  ·  source: "
                        f"{_SOURCE_LABELS.get(own.source, own.source)}")
                except ValueError:
                    self.alliance_var.set(
                        "⚠ alliance not identified — open the Duel screen or the "
                        "member list once, it will be remembered")
            else:
                self.alliance_var.set("")
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
        ready = [k for k, v in getattr(self, "_rows", {}).items() if v["count"]]
        sel = self.tree.selection()
        self.export_btn.configure(
            state="normal" if sel and self._rows.get(sel[0], {}).get("count") else "disabled")
        self.exportall_btn.configure(state="normal" if ready else "disabled")
        if hasattr(self, "send_btn"):
            self.send_btn.configure(state="normal" if ready else "disabled")

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
            messagebox.showinfo(TITLE, "Enter the site URL.")
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
            with Store(db) as store:
                for feed, day, alliance in jobs:
                    try:
                        payload = self._payload(store, feed, day, snapshot, alliance,
                                                declared)
                    except ValueError as exc:
                        self.q.put(Msg("log", f"  {feed.key}: refused — {exc}"))
                        continue
                    res = publish(payload, url, token=token)
                    tag = "OK" if res.ok else "FAILED"
                    self.q.put(Msg("log", f"  {feed.key}: {tag} HTTP {res.status}"
                                          f"  {res.response.strip()[:180] or res.error}"))
            self.q.put(Msg("sent", None))
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
                                      context=store.duel_context(snapshot),
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


def main(db_path: str | None = None, keep: bool | None = None) -> int:
    root = tk.Tk()
    App(root, db_path, keep=keep)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
