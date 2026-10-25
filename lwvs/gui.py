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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (__version__, capture, events as events_mod, exporter, gamecache,
               i18n, identity, storm, theme, trains)
from .capture import LiveSource, TsharkNotFound
from .i18n import t
from .publish import DEFAULT_URL, check_connection, normalize_url, publish
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

#: Les sections du tableau, dans l'ordre d'affichage. Une section vide est
#: masquee : l'ecran ne montre que ce qui a ete capture ou lu.
_SECTIONS = (("grp:global", "🛡", "Global"), ("grp:vs", "⚔", "VS"),
             ("grp:ranking", "🏆", "Ranking"), ("grp:events", "⛈", "Events"),
             ("grp:trains", "🚂", "Trains"))

#: Section et rang de chaque flux. Un flux absent d'ici va dans Events, apres
#: ceux-ci (Meteor, et tout futur evenement).
_FEED_PLACE = {
    "roles": "grp:global",
    "vs_total": "grp:vs", "vs_day": "grp:vs",
    "thp": "grp:ranking", "kills": "grp:ranking", "dons_semaine": "grp:ranking",
    "camp_battle": "grp:events",
    "desert_a": "grp:events", "desert_b": "grp:events",
    "canyon_a": "grp:events", "canyon_b": "grp:events",
}
_FEED_ORDER = list(_FEED_PLACE)

#: Icone et libelle court de chaque flux a l'ecran. Le libelle long
#: d'`exporter.FEEDS` reste celui de la CLI ; un flux absent d'ici garde le sien.
#: Symboles du plan de base Unicode : Tk les rend avec Segoe UI Symbol.
_FEED_LOOK = {
    "kills": ("☠", "Kills"),
    "dons_semaine": ("⛃", "Donations · week"),
    "roles": ("♛", "Members"),
    "thp": ("⚡", "THP · server"),
    "camp_battle": ("⚑", "Camp battle"),
    "canyon_a": ("⛰", "Canyon · team A"),
    "canyon_b": ("⛰", "Canyon · team B"),
    "desert_a": ("☀", "Desert · team A"),
    "desert_b": ("☀", "Desert · team B"),
    "vs_total": ("⚔", "VS · total"),
    "vs_day": ("⚔", "VS · day"),
    "meteor_alliances_t1": ("☄", "Meteor · alliances · round 1"),
    "meteor_alliances_t2": ("☄", "Meteor · alliances · round 2"),
    "meteor_players_t1": ("☄", "Meteor · players · round 1"),
    "meteor_players_t2": ("☄", "Meteor · players · round 2"),
}


# Le defaut de `store` est RELATIF : lance depuis un raccourci (ou un .exe
# installe), il suit un repertoire courant imprevisible -- la base atterrit
# n'importe ou, et le fichier de preferences avec elle puisqu'il se deduit
# d'elle. La GUI ancre donc les deux dans le repertoire de configuration.
def _short_date(iso: str) -> str:
    """« 2026-10-23 » -> « 23/10 » : l'annee n'apporte rien sur une tuile."""
    return f"{iso[8:10]}/{iso[5:7]}" if len(iso) >= 10 else iso


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
                # | "no_tshark" | "check"
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
        i18n.set_lang(self.prefs.get("lang", "en"))
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
        self._tiles: dict[str, dict[str, Any]] = {}
        self._train_rows: list[dict[str, Any]] = []
        #: Ecran affiche et journal deplie : survivent au changement de theme.
        self._page = "home"
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
        self._refresh_mail()

    # -- etat persistant --------------------------------------------------
    def _vars(self, keep: bool | None) -> None:
        """Toutes les variables Tk, creees une seule fois pour la vie de l'App."""
        self._keep = tk.BooleanVar(
            value=bool(self.prefs.get("keep", False)) if keep is None else keep)
        self.iface_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(self.prefs.get("port", "")))
        self.status_var = tk.StringVar(value=t("ready"))
        self.guard_var = tk.StringVar(value=t("no capture yet"))
        self.alliance_var = tk.StringVar(value="")
        self.roster_var = tk.StringVar(value="")
        self.mine_var = tk.BooleanVar(value=bool(self.prefs.get("mine", True)))
        self.day_var = tk.StringVar(value=str(self.prefs.get("declare_day", "") or ""))
        self.event_var = tk.StringVar(value=self.prefs.get("event", ""))
        self.url_var = tk.StringVar(value=self.prefs.get("post_url") or DEFAULT_URL)
        self.check_var = tk.StringVar()
        self.token_var = tk.StringVar(value=self.prefs.get("post_token", ""))
        self.autosend_var = tk.BooleanVar(value=bool(self.prefs.get("autosend", False)))
        self.keep_token_var = tk.BooleanVar(
            value=bool(self.prefs.get("keep_token", False)))
        # Une seule periode pour Storm et trains : ils viennent de la meme boite.
        self.since_var = tk.StringVar(
            value=self.prefs.get("mail_since") or self.prefs.get("storm_since")
            or SINCE_CHOICES[0])
        self.lang_var = tk.StringVar(value=i18n.LANGS[i18n.current()])
        self.net_var = tk.StringVar()
        for var in (self.iface_var, self.port_var):
            var.trace_add("write", lambda *_: self._sync_net())
        self._sync_net()

    def _sync_net(self) -> None:
        iface, port = self.iface_var.get(), self.port_var.get().strip()
        if not port.isdigit():
            self.net_var.set(t("found by itself when you press Start"))
        else:
            self.net_var.set(f"{iface or t('no interface')}  ·  {t('port')} {port}")

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

        # Deux ecrans seulement : tout ce qu'on fait a chaque bataille (capture
        # ET mails du jeu) sur l'accueil, et a part les reglages qu'on ne touche
        # qu'une fois.
        self._build_nav(self.frame)
        tk.Frame(self.frame, background=pal.border, width=1).pack(side="left", fill="y")
        main = tk.Frame(self.frame, background=pal.bg)
        main.pack(side="left", fill="both", expand=True)
        self._build_prereq(main)

        body = tk.Frame(main, background=pal.bg)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        self.body_frame = body
        self.pages: dict[str, tk.Frame] = {}
        for name, build in (("home", self._build_home_page),
                            ("settings", self._build_settings_page)):
            page = tk.Frame(body, background=pal.bg)
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
            build(page)
            self.pages[name] = page
        self._show_page(self._page)

        self._say(t("the JSON exports are the deliverable; nothing is kept "
                    "locally unless you tick \"Keep history locally\" in Settings."))
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
        for key, text in (("home", "◉   " + t("Stats")),
                          ("settings", "⚙   " + t("Settings"))):
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
                     wraplength=440).pack(fill="x", pady=(2, 0))
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
        tk.Label(inner, text="⚠  " + t("Wireshark is required, and it could not be found"),
                 background=pal.surface, foreground=pal.warn,
                 font=fonts.bold).pack(anchor="w")
        theme.label(
            inner, pal, fonts,
            t("lwvs does not sniff the network itself: it delegates to tshark, "
              "which ships with Wireshark.\n"
              "Install Wireshark leaving Npcap ticked, then restart lwvs.\n"
              "When Npcap installs, DO NOT tick \"Restrict Npcap driver's access "
              "to Administrators only\":\n"
              "ticked, no interface will see a single packet unless lwvs runs as "
              "administrator."),
            kind="muted").pack(anchor="w", pady=(6, 10))
        ttk.Button(inner, text=t("Open the download page"),
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
        self._status(t("Wireshark missing"), "err")

    # -- ecran 1 : accueil (capture + mails du jeu) ----------------------
    def _build_home_page(self, page: tk.Frame) -> None:
        """Un seul tableau : ce que la capture a vu, puis ce que la boite mail
        du jeu contient (Storm, trains). Un seul « Envoyer » pour le tout.

        Les mails ne passent jamais sur le fil : ils sont lus dans la base du
        jeu, sans tshark, et rafraichis a l'ouverture.
        """
        pal, fonts = self.pal, self.fonts
        head = self._page_head(
            page, t("Stats"),
            t("Capture: open each VS tab in the game and let it load. "
              "Storm and trains are read from the game's mailbox on this PC, "
              "no capture needed."))
        self.stop_btn = ttk.Button(head, text="■  " + t("Stop"), command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        theme.Tooltip(self.stop_btn, t("Stop capturing") + "   ·   F5", pal, fonts)
        self.start_btn = ttk.Button(head, text="▶  " + t("Start capture"),
                                    style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="right", padx=(0, 8))
        theme.Tooltip(self.start_btn,
                      t("Start capturing") + "   ·   F5\n"
                      + t("The first time, the interface and port are found by\n"
                          "themselves before the capture starts."), pal, fonts)

        self._build_banner(page)

        # La barre d'actions est posee AVANT le tableau : `pack` sert dans
        # l'ordre, et un tableau extensible pose d'abord ne lui laisserait rien.
        outer_bar = tk.Frame(page, background=pal.bg)
        outer_bar.pack(side="bottom", fill="x", pady=(12, 16))
        # Deux lignes : ce que vous declarez, puis ce que vous faites. Sur une
        # seule, la barre debordait a la largeur minimale (et en FR / DE).
        bar = tk.Frame(outer_bar, background=pal.bg)
        bar.pack(fill="x")
        self._on_bg(bar, t("Day")).pack(side="left")
        ttk.Entry(bar, textvariable=self.day_var, width=4).pack(side="left",
                                                                padx=(6, 14))
        self._on_bg(bar, t("Event")).pack(side="left")
        # Liste fermee : un libelle libre fragmenterait le suivi en aval.
        self.event_box = ttk.Combobox(bar, textvariable=self.event_var, width=22,
                                      state="readonly")
        self.event_box.pack(side="left", padx=6)
        theme.Tooltip(self.event_box, t("Day and event are declared by you,\n"
                                        "not read from the game."), pal, fonts)
        add_btn = ttk.Button(bar, text="+", width=3, style="Ghost.TButton",
                             command=self._add_event)
        add_btn.pack(side="left")
        theme.Tooltip(add_btn, t("Add an event to the catalogue"), pal, fonts)
        self._reload_events()
        self._on_bg(bar, t("Mail since")).pack(side="left", padx=(14, 0))
        self._since_box(bar)
        bar = tk.Frame(outer_bar, background=pal.bg)
        bar.pack(fill="x", pady=(8, 0))
        self.log_btn = ttk.Button(bar, style="Ghost.TButton", command=self._toggle_log)
        self.log_btn.pack(side="left")

        self.send_btn = ttk.Button(bar, text=t("Send to the site"),
                                   style="Accent.TButton",
                                   command=self._send_all, state="disabled")
        self.send_btn.pack(side="right")
        theme.Tooltip(self.send_btn,
                      t("Publishes every ready line (rankings, Storm, trains)")
                      + "   ·   Ctrl+↵\n"
                      + t("This sends the names and ids of every player in them."),
                      pal, fonts)
        self.exportall_btn = ttk.Button(bar, text=t("Save JSON…"),
                                        command=self._export_all, state="disabled")
        self.exportall_btn.pack(side="right", padx=8)
        theme.Tooltip(self.exportall_btn,
                      t("Writes one file per ready line") + "   ·   Ctrl+Shift+E\n"
                      + t("Double-click a ranking to save only that one")
                      + "   ·   Ctrl+E",
                      pal, fonts)

        self._build_tiles(page)
        self._build_log(page)

    def _build_banner(self, page: tk.Frame) -> None:
        """Bandeau du haut : qui est ton alliance, et si la capture decode bien."""
        pal, fonts = self.pal, self.fonts
        banner = tk.Frame(page, background=pal.surface, highlightthickness=1,
                          highlightbackground=pal.border, highlightcolor=pal.border)
        banner.pack(fill="x", pady=(16, 12))
        tk.Label(banner, text="🛡", background=pal.surface, foreground=pal.accent,
                 font=(fonts.title[0], 24)).pack(side="left", padx=(16, 12), pady=10)
        # Le garde-fou reste visible : c'est LE chiffre qui dit si on decode ce
        # qu'on croit. Discret, mais toujours la.
        health = tk.Frame(banner, background=pal.surface, width=230)
        health.pack(side="right", fill="y", padx=16, pady=10)
        health.pack_propagate(False)
        self.guard_bar = ttk.Progressbar(health, style="Ok.Horizontal.TProgressbar",
                                         mode="determinate", maximum=100, value=0)
        self.guard_bar.pack(fill="x", pady=(4, 4))
        theme.Tooltip(self.guard_bar,
                      t("Share of payloads decoded down to their last byte.\n"
                        "Below 100 %, you are not decoding what you think."),
                      pal, fonts)
        theme.label(health, pal, fonts, kind="muted", textvariable=self.guard_var,
                    wraplength=230).pack(fill="x")
        theme.label(health, pal, fonts, kind="muted", textvariable=self.net_var,
                    wraplength=230).pack(fill="x")
        who = tk.Frame(banner, background=pal.surface)
        who.pack(side="left", fill="x", expand=True, pady=10)
        tk.Label(who, textvariable=self.alliance_var, background=pal.surface,
                 foreground=pal.text, font=fonts.title, anchor="w", justify="left",
                 wraplength=420).pack(fill="x")
        theme.label(who, pal, fonts, kind="muted", textvariable=self.roster_var
                    ).pack(fill="x")

    def _build_tiles(self, page: tk.Frame) -> None:
        """Zone des tuiles, defilante : une rangee par section, une tuile par donnee."""
        pal = self.pal
        self._selected: str | None = None
        self._cols = 0
        self.tiles_holder = tk.Frame(page, background=pal.bg)
        self.tiles_holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(self.tiles_holder, background=pal.bg, highlightthickness=0,
                           borderwidth=0)
        scroll = ttk.Scrollbar(self.tiles_holder, style="Slim.Vertical.TScrollbar",
                               command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.tiles_area = tk.Frame(canvas, background=pal.bg)
        window = canvas.create_window(0, 0, window=self.tiles_area, anchor="nw")
        self.tiles_area.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))

        def resized(event) -> None:
            canvas.itemconfigure(window, width=event.width)
            cols = max(1, event.width // 215)
            if cols != self._cols:
                self._cols = cols
                self._render_tiles()
        canvas.bind("<Configure>", resized)
        # La molette ne defile que si le pointeur est sur les tuiles.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def _put_tile(self, key: str, section: str, order: float, icon: str, name: str,
                  big: str, state: str, tag: str, tip: str = "",
                  detail: str = "") -> None:
        self._tiles[key] = dict(section=section, order=order, icon=icon, name=name,
                                big=big, state=state, tag=tag, tip=tip, detail=detail)

    def _drop_tiles(self, prefix: str) -> None:
        for key in [k for k in self._tiles if k.startswith(prefix)]:
            del self._tiles[key]
        if self._selected and self._selected not in self._tiles:
            self._selected = None

    def _render_tiles(self) -> None:
        """Redessine toutes les tuiles. Peu nombreuses : plus simple que les patcher."""
        pal, fonts = self.pal, self.fonts
        for child in self.tiles_area.winfo_children():
            child.destroy()
        colours = {"ok": pal.ok, "warn": pal.warn, "muted": pal.muted, "": pal.accent}
        cols = max(1, self._cols)
        shown = False
        for section, icon, title in _SECTIONS:
            tiles = sorted((v["order"], k) for k, v in self._tiles.items()
                           if v["section"] == section)
            if not tiles:
                continue
            shown = True
            tk.Label(self.tiles_area, text=f"{icon}  {t(title)}", background=pal.bg,
                     foreground=pal.text, font=fonts.bold, anchor="w"
                     ).pack(fill="x", pady=(10, 6))
            grid = tk.Frame(self.tiles_area, background=pal.bg)
            grid.pack(fill="x")
            for c in range(cols):
                grid.columnconfigure(c, weight=1, uniform="tile")
            for i, (_order, key) in enumerate(tiles):
                spec = self._tiles[key]
                colour = colours.get(spec["tag"], pal.accent)
                chosen = key == self._selected
                tile = tk.Frame(grid, background=pal.surface, cursor="hand2",
                                highlightthickness=2 if chosen else 1,
                                highlightbackground=pal.accent if chosen else pal.border,
                                highlightcolor=pal.accent if chosen else pal.border)
                tile.grid(row=i // cols, column=i % cols, sticky="nsew",
                          padx=(0, 8), pady=(0, 8))
                tk.Frame(tile, background=colour, height=3).pack(fill="x")
                icon_lbl = tk.Label(tile, text=spec["icon"], background=pal.surface,
                                    foreground=colour, font=(fonts.title[0], 20))
                icon_lbl.pack(side="left", padx=(10, 6), pady=8)
                text = tk.Frame(tile, background=pal.surface)
                text.pack(side="left", fill="x", expand=True, pady=6, padx=(0, 8))
                parts = [
                    tk.Label(text, text=spec["name"], background=pal.surface,
                             foreground=pal.muted, font=fonts.small, anchor="w",
                             justify="left", wraplength=135),
                    tk.Label(text, text=spec["big"], background=pal.surface,
                             foreground=pal.text, font=fonts.title, anchor="w"),
                    tk.Label(text, text=spec["state"], background=pal.surface,
                             foreground=colour, font=fonts.small, anchor="w"),
                ]
                if spec["detail"]:
                    parts.append(tk.Label(text, text=spec["detail"], background=pal.surface,
                                          foreground=pal.muted, font=fonts.small,
                                          anchor="w", justify="left", wraplength=135))
                for part in parts:
                    part.pack(fill="x")
                for widget in (tile, icon_lbl, text, *parts):
                    widget.bind("<Button-1>", lambda _e, k=key: self._select_tile(k))
                    widget.bind("<Double-1>", lambda _e, k=key: self._export())
                    if spec["tip"]:
                        theme.Tooltip(widget, spec["tip"], pal, fonts)
        if not shown:
            theme.label(self.tiles_area, pal, fonts, kind="muted",
                        text=t("Nothing captured yet: press Start capture, then open "
                               "the screens in the game."),
                        wraplength=500).pack(pady=40)

    def _select_tile(self, key: str) -> None:
        self._selected = key
        self._render_tiles()
        self._sync_buttons()
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
        ttk.Button(bar, text=t("Copy"), style="Ghost.TButton",
                   command=self._copy_log).pack(side="right")
        ttk.Button(bar, text=t("Clear"), style="Ghost.TButton",
                   command=self._clear_log).pack(side="right", padx=8)
        self._toggle_log(self._log_open)

    def _toggle_log(self, show: bool | None = None) -> None:
        self._log_open = (not self._log_open) if show is None else show
        if self._log_open:
            self.log_panel.pack(side="bottom", fill="x", before=self.tiles_holder)
        else:
            self.log_panel.pack_forget()
        self.log_btn.configure(text=t("Hide log") if self._log_open else t("Show log"))

    def _since_box(self, bar: tk.Frame) -> None:
        """Periode des mails : un choix dans la liste, ou une date tapee a la main."""
        box = ttk.Combobox(bar, textvariable=self.since_var, values=SINCE_CHOICES,
                           width=13)
        box.pack(side="left", padx=(6, 0))
        box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_mail())
        box.bind("<Return>", lambda _e: self._refresh_mail())
        theme.Tooltip(box, t("Period for Storm and trains. Pick one, or type a date:\n"
                             "YYYY-MM-DD = from that day (UTC)"), self.pal, self.fonts)
        refresh = ttk.Button(bar, text="↻", width=3, style="Ghost.TButton",
                             command=self._refresh_mail)
        refresh.pack(side="left", padx=(4, 0))
        theme.Tooltip(refresh, t("Read the game's mailbox again"), self.pal, self.fonts)

    # -- ecran 2 : reglages -----------------------------------------------
    def _build_settings_page(self, page: tk.Frame) -> None:
        self._page_head(page, t("Settings"))
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
        outer, body = theme.card(parent, pal, fonts, t("Stats site"))
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

        theme.Check(body, pal, fonts, t("Send automatically when a capture stops"),
                    self.autosend_var).grid(row=4, column=0, sticky="w",
                                            pady=(10, 0))
        # Un jeton en clair dans un fichier est un vrai risque : on ne le retient
        # que sur demande explicite, et on le dit.
        theme.Check(body, pal, fonts, t("Remember the token (plain text on this PC)"),
                    self.keep_token_var).grid(row=5, column=0, sticky="w", pady=(4, 0))

        warn = tk.Frame(body, background=pal.surface)
        warn.grid(row=6, column=0, sticky="we", pady=(12, 0))
        dot = theme.Dot(warn, pal.surface)
        dot.pack(side="left", padx=(0, 7), pady=(4, 0), anchor="n")
        dot.colour(pal.warn)
        theme.label(warn, pal, fonts, kind="muted", wraplength=320,
                    text=t("Sending publishes the names and ids of every player "
                           "in the ranking.")).pack(side="left", fill="x")

        theme.label(body, pal, fonts, kind="muted", wraplength=320,
                    text=t("URL and token: your alliance's R4 Tools → Push feed "
                           "page on the site.")).grid(row=7, column=0, sticky="w",
                                                    pady=(12, 0))
        test_btn = ttk.Button(body, text=t("Test the connection"),
                              command=self._test_connection)
        test_btn.grid(row=8, column=0, sticky="w", pady=(8, 0))
        theme.Tooltip(test_btn, t("Checks the URL and the token. "
                                  "Sends no ranking."), pal, fonts)
        theme.label(body, pal, fonts, kind="muted", wraplength=320,
                    textvariable=self.check_var).grid(row=9, column=0, sticky="w",
                                                      pady=(6, 0))

    def _build_source(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, t("Network"))
        outer.pack(fill="x", pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=90)

        theme.label(body, pal, fonts, kind="muted", wraplength=360,
                    text=t("Found by itself when you press Start. Change it only "
                           "if detection fails.")).grid(row=0, column=0, columnspan=2,
                                                        sticky="w", pady=(0, 10))
        theme.label(body, pal, fonts, t("Network interface"), kind="muted").grid(
            row=1, column=0, sticky="w")
        theme.label(body, pal, fonts, t("Game port"), kind="muted").grid(
            row=1, column=1, sticky="w", padx=(10, 0))
        self.iface_box = ttk.Combobox(body, textvariable=self.iface_var,
                                      state="readonly", width=10)
        self.iface_box.grid(row=2, column=0, sticky="we", pady=(3, 0))
        self.iface_box.configure(values=self._iface_values)
        # Le device reste consultable : c'est lui qu'on copie dans un --iface.
        theme.Tooltip(self.iface_box,
                      lambda: self._iface_device() or t("no interface"), pal, fonts)
        port_entry = ttk.Entry(body, textvariable=self.port_var, width=8)
        port_entry.grid(row=2, column=1, sticky="we", padx=(10, 0), pady=(3, 0))

        # Un seul bouton : interface et port se prouvent par le MEME signal,
        # une trame du jeu qui decode jusqu'a son dernier octet. Les demander
        # separement etait deux attentes a la file pour une seule reponse.
        self.detect_btn = ttk.Button(body, text="⌖  " + t("Detect again"),
                                     style="Ghost.TButton", command=self._detect)
        self.detect_btn.grid(row=3, column=0, columnspan=2, sticky="w",
                             pady=(12, 0))
        theme.Tooltip(self.detect_btn,
                      t("Listens on every interface at once and stops as soon\n"
                        "as a game frame is recognised — a few seconds if the\n"
                        "game is running.") + "   ·   F4",
                      pal, fonts)

    def _build_data(self, parent: tk.Frame) -> None:
        pal, fonts = self.pal, self.fonts
        outer, body = theme.card(parent, pal, fonts, t("Data and display"))
        outer.pack(fill="x")
        theme.Check(body, pal, fonts, t("My alliance only"), self.mine_var,
                    command=self._refresh_feeds).pack(anchor="w")
        keep_box = theme.Check(body, pal, fonts, t("Keep history locally"),
                               self._keep, command=self._refresh_feeds)
        keep_box.pack(anchor="w", pady=(4, 0))
        theme.Tooltip(keep_box, t("Unticked, the session database is deleted on\n"
                                  "close: only the JSON exports remain."),
                      pal, fonts)
        lang = tk.Frame(body, background=pal.surface)
        lang.pack(anchor="w", pady=(12, 0))
        theme.label(lang, pal, fonts, t("Language"), kind="muted").pack(side="left")
        box = ttk.Combobox(lang, textvariable=self.lang_var, state="readonly",
                           values=list(i18n.LANGS.values()), width=12)
        box.pack(side="left", padx=(8, 0))
        box.bind("<<ComboboxSelected>>", lambda _e: self._set_lang())
        ttk.Button(body, text="☀  " + t("Light theme") if pal.dark
                   else "☾  " + t("Dark theme"),
                   style="Ghost.TButton",
                   command=self._toggle_theme).pack(anchor="w", pady=(12, 0))

    def _bind_keys(self) -> None:
        # Les raccourcis sont annonces dans les bulles d'aide : un raccourci
        # qui ne se decouvre pas n'existe pas.
        self.root.bind("<F5>", lambda _e: self._toggle_capture())
        self.root.bind("<F4>", lambda _e: self._detect())
        self.root.bind("<Control-e>", lambda _e: self._export())
        self.root.bind("<Control-E>", lambda _e: self._export_all())
        self.root.bind("<Control-Return>", lambda _e: self._send_all())

    def _toggle_theme(self) -> None:
        self.prefs["theme"] = "clair" if self.pal.dark else "sombre"
        self._rebuild()

    def _set_lang(self) -> None:
        code = next(c for c, name in i18n.LANGS.items()
                    if name == self.lang_var.get())
        if code == i18n.current():
            return
        self.prefs["lang"] = code
        i18n.set_lang(code)
        self._rebuild()

    def _rebuild(self) -> None:
        """Reconstruit tous les widgets (theme ou langue), journal conserve."""
        _save_prefs(self.db_path, self.prefs)
        history = self.log.get("1.0", "end-1c")
        self.frame.destroy()
        self.pal, self.fonts = theme.apply(self.root, self.prefs.get("theme", "sombre"))
        self._build()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", history + "\n" if history else "")
        self.log.see("end")
        self.log.configure(state="disabled")
        self._sync_net()
        self._refresh_feeds()
        self._refresh_mail()

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
        self._say(t("log copied to the clipboard."))

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
        self._say(t("detecting: listening on every interface at once, stopping "
                    "as soon as a game frame is recognised. Keep the game open."))
        self._status(t("detecting…"), "busy")
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
            self._status(t("detection found nothing"), "err")
            return
        self.port_var.set(str(found.port))
        self._say(f"-> {found.iface.name or found.iface.device}, port "
                  f"{found.port} — found in {found.seconds:.0f} s.", "ok")
        self._status(t("interface and port found"), "ok")
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
        self._status(t("capturing…"), "busy")
        self._say(t("capture started on port {port}").format(port=port))

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
            self._status(t("stopping…"), "busy")
            self._say(t("stop requested — frames already received are being processed."))
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
            self._status(t("error"), "err")
        elif msg.kind == "busy":
            self._set_busy(msg.payload)
        elif msg.kind == "check":
            ok, text = msg.payload
            self.check_var.set(text)
            self._say(text, "ok" if ok else "err")
        elif msg.kind == "no_tshark":
            self._tshark_missing = True
            self._apply_prereq()
            self._say(t("tshark not found: install Wireshark (see the top of the "
                        "window), then restart lwvs."), "err")
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
            self._status(t("detecting… {s} s max · {n} packets").format(
                s=remaining, n=packets)
                + (" · " + t("{n} game frames").format(n=exact) if exact else ""),
                "busy")
        elif msg.kind == "detected":
            self._detected(msg.payload)
        elif msg.kind == "detect_end":
            self._start_after_detect = False
            self.detect_btn.configure(state="normal")
            if str(self.stop_btn.cget("state")) != "normal":
                self.start_btn.configure(state="normal")
        elif msg.kind == "progress":
            if msg.payload is None:
                self._status(t("ready"))
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
                self._say(t("send complete."), "ok")
                self._status(t("send complete"), "ok")
        elif msg.kind == "done":
            self._finish(msg.payload)
        elif msg.kind == "storm_list":
            self._storm_listed(msg.payload)
        elif msg.kind == "storm_unavailable":
            self._storm_rows = []
            self._drop_tiles("storm:")
            self._render_tiles()
            self._say(f"⚠ {msg.payload}", "err")
            self._sync_buttons()
        elif msg.kind in ("storm_done", "train_done"):
            self._sync_buttons()
            self._status(*msg.payload)
        elif msg.kind == "train_list":
            self._trains_listed(msg.payload)
        elif msg.kind == "train_unavailable":
            self._train_rows = []
            self._drop_tiles("train:")
            self._render_tiles()
            self._sync_buttons()

    def _guard(self, exact: int, decoded: int) -> None:
        pct = 100.0 * exact / decoded if decoded else 0.0
        self.guard_bar.configure(
            value=pct,
            style=("Ok" if pct >= 99 else "Warn") + ".Horizontal.TProgressbar")
        if not decoded:
            self.guard_var.set(t("no capture yet"))
            return
        self.guard_var.set(
            t("safeguard: {exact}/{decoded} decoded down to the last byte "
              "({pct} %)").format(exact=exact, decoded=decoded, pct=f"{pct:.0f}")
            + ("" if pct >= 99
               else "  ⚠ " + t("below 100 %, you are not decoding what you think")))

    def _finish(self, result) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if result is None:      # le worker a echoue : l'erreur suit dans le journal
            return
        self.snapshot_id = None if result.dropped else result.snapshot_id
        st, c = result.stats, result.counters
        self._guard(st.exact, st.decoded)
        self._status(
            t("done — {p} payloads, {d} decoded, {f} failed").format(
                p=st.payloads, d=st.decoded, f=st.failed),
            "err" if result.dropped else "ok")
        self._say(f"capture finished: snapshot #{result.snapshot_id}", "ok")
        if st.failures_by_tag:
            detail = "  ".join(f"0x{t:02x}×{n}" for t, n in st.failures_by_tag.most_common())
            self._say(f"failures by offending type byte: {detail}")
        for name, n in sorted(c.commands.items(), key=lambda kv: -kv[1]):
            self._say(f"  {n:4d}  {name}")
        self._seen = dict(c.commands)
        if result.dropped:
            self._say(t("no usable message: empty snapshot dropped. "
                        "Was the VS screen actually reloaded?"), "err")
        self._refresh_feeds()
        url = normalize_url(self.url_var.get())
        if self.autosend_var.get() and url:
            self._remember_send()
            self._publish_rankings(url, self.token_var.get().strip() or None)

    # -- tableau des flux -------------------------------------------------
    def _update_seen(self, commands: dict[str, int]) -> None:
        # En pleine capture, la base n'a pas encore la nouvelle photo : une tuile
        # apparait des que son message passe, et se remplit a l'arret.
        changed = False
        for feed in exporter.FEEDS:
            if commands.get(feed.command) and not any(
                    e["feed"] is feed for e in self._rows.values()):
                self._feed_tile(feed, None, f"feed:{feed.key}", commands[feed.command],
                                "●  " + t("received"), "", 0)
                changed = True
        if changed:
            self._render_tiles()

    def _feed_tile(self, feed: exporter.Feed, day: int | None, key: str,
                   seen: int, state: str, tag: str, count: int) -> None:
        icon, short = _FEED_LOOK.get(feed.key, ("•", feed.label))
        order = _FEED_ORDER.index(feed.key) if feed.key in _FEED_ORDER else len(_FEED_ORDER)
        tip = t(feed.label) + (f"\n{t('Messages')} : {seen}" if seen else "")
        self._put_tile(key, _FEED_PLACE.get(feed.key, "grp:events"), order + (day or 0) / 100,
                       icon, t(short) + (f" {day}" if day is not None else ""),
                       str(count) if count else "…", state, tag, tip)
        self._rows[key] = {"feed": feed, "day": day, "count": count}

    def _refresh_feeds(self) -> None:
        self._drop_tiles("feed:")
        self._rows: dict[str, dict[str, Any]] = {}
        self.roster_var.set("")

        snapshot = self.snapshot_id or self._latest_snapshot()

        with Store(self._active_db()) as store:
            if self.mine_var.get() and snapshot is not None:
                try:
                    own = exporter.resolve_own_alliance(store, snapshot)
                    self.alliance_var.set(own.label)
                except ValueError:
                    self.alliance_var.set(
                        "⚠ " + t("not identified — open the Duel screen or the "
                                 "member list once, it will be remembered"))
            elif not self.mine_var.get():
                self.alliance_var.set(t("every alliance (see Settings)"))
            else:
                self.alliance_var.set(t("known after the first capture"))
            for feed in exporter.FEEDS:
                alliance = (exporter.MINE
                            if self.mine_var.get() and not feed.inherently_mine
                            else None)
                days = [None]
                if feed.per_day and snapshot is not None:
                    days = self._days(store, snapshot) or [None]
                for day in days:
                    key = f"feed:{feed.key}" + (f":{day}" if day is not None else "")
                    seen = self._seen.get(feed.command, 0)
                    count, state, tag = 0, "●  " + t("received"), ""
                    if snapshot is not None:
                        try:
                            rows = exporter.collect(
                                store, feed.dataset, scope=feed.scope, day=day,
                                alliance=alliance, snapshot=snapshot)
                            count = len(rows)
                            state, tag = self._etat(count, seen)
                            if feed.key == "roles" and rows:
                                ranks = [r.get("alliance_rank") for r in rows]
                                self.roster_var.set(
                                    t("{n} members · R4: {r4} · R5: {r5}").format(
                                        n=len(rows), r4=ranks.count(4), r5=ranks.count(5)))
                        except ValueError as exc:
                            state, tag = "⚠  " + str(exc)[:60], "warn"
                    # Ni donnee ni message recu : rien a montrer.
                    if count or seen:
                        self._feed_tile(feed, day, key, seen, state, tag, count)
        self._render_tiles()
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
            return "✓  " + t("ready"), "ok"
        if not self._seen:
            return "◷  " + t("no rows"), "muted"
        if not seen:
            return "◷  " + t("reopen the screen"), "muted"
        return "⚠  " + t("to report"), "warn"

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
        n = (sum(1 for v in getattr(self, "_rows", {}).values() if v["count"])
             + len(self._storm_rows) + len(self._train_rows))
        self.exportall_btn.configure(state="normal" if n else "disabled")
        self.send_btn.configure(
            state="normal" if n else "disabled",
            text=t("Send {n} to the site").format(n=n) if n else t("Send to the site"))

    # -- export -----------------------------------------------------------
    def _export(self) -> None:
        sel = self._selected
        if not sel or not self._rows.get(sel, {}).get("count"):
            return
        entry = self._rows[sel]
        feed, day = entry["feed"], entry["day"]
        suffix = f"_j{day}" if day is not None else ""
        path = filedialog.asksaveasfilename(
            title=f"Export — {t(feed.label)}",
            defaultextension=".json",
            initialfile=f"lwvs_{feed.key}{suffix}.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        self._write(feed, day, path)

    def _export_all(self) -> None:
        """Un seul dossier pour tout ce qui est pret : classements et mails."""
        if str(self.exportall_btn.cget("state")) != "normal":
            return
        folder = filedialog.askdirectory(
            title=t("Destination folder"),
            initialdir=self.prefs.get("storm_out") or None)
        if not folder:
            return
        self.prefs["storm_out"] = self.prefs["trains_out"] = folder
        _save_prefs(self.db_path, self.prefs)
        if self._storm_rows:
            self._storm_run(folder, None, None)
        if self._train_rows:
            self._trains_run(folder, None, None)
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
            TITLE, t("Name of the new event (e.g. S4 - Another event):"),
            parent=self.root)
        if not libelle or not libelle.strip():
            return
        cree = events_mod.add(libelle.strip())
        self._reload_events()
        self.event_var.set(cree.label)
        self._say(t("event added: {id}  ({label})").format(id=cree.id, label=cree.label))

    def _declared(self) -> tuple[int | None, str | None]:
        """Lu sur le thread UI : ce sont des variables Tk."""
        brut = self.day_var.get().strip()
        jour = int(brut) if brut.isdigit() else None
        return jour, (self.event_var.get().strip() or None)

    def _alliance_for(self, feed: exporter.Feed) -> str | None:
        return (exporter.MINE
                if self.mine_var.get() and not feed.inherently_mine else None)

    def _test_connection(self) -> None:
        # Variables Tk lues ICI, sur le thread UI ; le worker ne recoit que des valeurs.
        url, token = normalize_url(self.url_var.get()), self.token_var.get().strip()
        self.check_var.set(t("testing…"))

        def work():
            ok, text = check_connection(url, token or None)
            self.q.put(Msg("check", (ok, text)))
        self._spawn(work)

    def _site_url(self) -> str:
        """L'URL du site, ou "" apres avoir envoye le joueur la saisir."""
        url = normalize_url(self.url_var.get())
        if not url:
            self._show_page("settings")
            messagebox.showinfo(TITLE, t("Enter the site URL in Settings."))
        return url

    def _send_all(self) -> None:
        """Un seul bouton : classements captures, Storm et trains."""
        if str(self.send_btn.cget("state")) != "normal":
            return
        url = self._site_url()
        if not url:
            return
        self._remember_send()
        token = self.token_var.get().strip() or None
        started = self._publish_rankings(url, token)
        # Les fichiers des mails sont ecrits AVANT l'envoi : un site
        # injoignable ne fait rien perdre.
        if self._storm_rows:
            self._storm_run(self.prefs.get("storm_out")
                            or str(identity.home() / "exports"), url, token)
            started = True
        if self._train_rows:
            self._trains_run(self.prefs.get("trains_out")
                             or str(identity.home() / "exports"), url, token)
            started = True
        if not started:
            messagebox.showinfo(TITLE, t("Nothing to send: no usable capture."))
            return
        self.send_btn.configure(state="disabled")

    def _publish_rankings(self, url: str, token: str | None) -> bool:
        """Envoie les classements prets. Faux s'il n'y avait rien a envoyer."""
        # On lit l'etat de l'UI ICI, sur le thread UI, et on ne passe au worker
        # que des donnees simples.
        # Tout ce que le worker utilisera est lu ICI, sur le thread UI, y
        # compris le filtre d'alliance : `mine_var` est une variable Tk, et la
        # lire depuis un autre thread leve "main thread is not in main loop".
        jobs = [(v["feed"], v["day"], self._alliance_for(v["feed"]))
                for v in self._rows.values() if v["count"]]
        if not jobs:
            return False
        snapshot = self.snapshot_id or self._latest_snapshot()
        db = self._active_db()
        declared = self._declared()
        self.send_btn.configure(state="disabled")
        self._status(t("sending…"), "busy")
        self._say(t("sending {n} ranking(s) to {url}…").format(n=len(jobs), url=url))

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
        return True

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
        self.prefs["post_url"] = normalize_url(self.url_var.get())
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
            self._say(f"[error] {t('export refused')}: {exc}")
            messagebox.showerror(TITLE, str(exc))


    # -- mails du jeu : Storm et trains -----------------------------------
    # Meme discipline que la capture : les variables Tk sont lues ICI, sur le
    # thread UI, et le worker ne recoit que des valeurs simples. Seuls les
    # resumes sans pseudo (`summary_of`) quittent le worker.

    def _refresh_mail(self) -> None:
        text = self.since_var.get()
        try:
            since = storm.since_ms(text)
        except ValueError as exc:
            self._say(f"⚠ {exc}", "err")
            return
        self.prefs["mail_since"] = text.strip()
        _save_prefs(self.db_path, self.prefs)
        db = self.storm_db

        def work():
            try:
                path = db or gamecache.default_db_path()
                if path is None:
                    raise gamecache.CacheUnavailable(
                        t("game database not found under AppData\\LocalLow — has "
                          "the game been run on this PC, with this Windows account?"))
                reports, n, dups, bad = storm.load_reports(path, since_ms=since)
                runs, tn, tbad = trains.load_trains(path, since_ms=since)
            except gamecache.CacheUnavailable as exc:
                # Un prerequis, pas une panne : une phrase, pas une trace de pile.
                self.q.put(Msg("storm_unavailable", str(exc)))
                self.q.put(Msg("train_unavailable", str(exc)))
                return
            self.q.put(Msg("storm_list",
                           ([storm.summary_of(r) for r in reports], n, dups, bad)))
            self.q.put(Msg("train_list",
                           ([trains.summary_of(r) for r in runs], tn, tbad)))
        self._spawn(work)

    def _storm_listed(self, payload) -> None:
        rows, n, dups, bad = payload
        self._storm_rows = rows
        self._drop_tiles("storm:")
        # Une tuile par journee et par type : les deux equipes d'un meme soir
        # se lisent ensemble, le detail de chaque bataille est dans la tuile.
        days: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            days.setdefault((r["date"], r["kind"]), []).append(r)
        for i, ((day, kind), battles) in enumerate(sorted(days.items(), reverse=True)):
            canyon = kind == "canyon_storm"
            wins = sum(1 for r in battles if r["result"] == "win")
            losses = sum(1 for r in battles if r["result"] == "loss")
            detail = "\n".join(
                f"{r['team'] or '?'}{'*' if r['declared'] else ''} · "
                + {"win": "✓", "loss": "✗"}.get(r["result"], "?")
                + f" vs {r['opponents']}"
                for r in sorted(battles, key=lambda r: r["team"] or ""))
            self._put_tile(
                f"storm:{i}", "grp:events", 100 + i, "⛰" if canyon else "☀",
                f"{'Canyon' if canyon else 'Desert'} · {_short_date(day)}",
                str(sum(r["players"] for r in battles)),
                f"✓ {wins}   ✗ {losses}", "ok" if wins else "muted",
                t("players") + "\n" + t("* = team declared, not in the mail"),
                detail)
        state = t("{n} battle(s) in the period").format(n=len(rows))
        if dups:
            state += "  ·  " + t("{n} duplicate mail(s) merged").format(n=dups)
        if bad:
            state += "  ·  " + t("{n} unreadable").format(n=bad)
        if any(r["declared"] for r in rows):
            state += "  ·  " + t("Desert team is declared, not in the mail: "
                                 "evening = A, early afternoon = B")
        if not rows:
            state = t("none in the period — widen « Mail since »")
        self._say(t("Storm") + " : " + state)
        self._render_tiles()
        self._sync_buttons()

    def _trains_listed(self, payload) -> None:
        rows, n, bad = payload
        self._train_rows = rows
        self._drop_tiles("train:")
        # Une tuile par semaine (lundi a dimanche) : un train par jour ferait
        # trente tuiles sur un mois.
        weeks: dict[date, list[dict[str, Any]]] = {}
        for r in rows:
            d = date.fromisoformat(r["date"])
            weeks.setdefault(d - timedelta(days=d.weekday()), []).append(r)
        for i, (monday, runs) in enumerate(sorted(weeks.items(), reverse=True)):
            arrived = sum(1 for r in runs if r["arrived"])
            on_way = len(runs) - arrived
            self._put_tile(
                f"train:{i}", "grp:trains", i, "🚂",
                t("Week of {d}").format(d=_short_date(monday.isoformat())),
                str(len(runs)),
                "✓ " + t("{n} arrived").format(n=arrived)
                + (f"   ◷ {on_way}" if on_way else ""),
                "ok" if arrived else "muted",
                t("trains this week"),
                t("{a} attack(s), {r} robbed").format(
                    a=sum(r["attacks"] or 0 for r in runs),
                    r=sum(r["robbed"] or 0 for r in runs))
                + (f"\n{t('{n} purchased').format(n=sum(1 for r in runs if r['purchased']))}"
                   if any(r["purchased"] for r in runs) else ""))
        state = t("{n} train(s) in the period").format(n=len(rows))
        bought = sum(1 for r in rows if r["purchased"])
        if bought:
            state += "  ·  " + t("{n} purchased").format(n=bought)
        if bad:
            state += "  ·  " + t("{n} unreadable").format(n=bad)
        if not rows:
            state = t("none in the period — widen « Mail since »")
        self._say(t("Trains") + " : " + state)
        self._render_tiles()
        self._sync_buttons()

    def _storm_run(self, folder: str, url: str | None, token: str | None) -> None:
        self._mail_run("storm", storm.export_storm, folder, url, token,
                       t("{n} battle(s) written"))

    def _trains_run(self, folder: str, url: str | None, token: str | None) -> None:
        self._mail_run("trains", trains.export_trains, folder, url, token,
                       t("{n} train(s) written"))

    def _mail_run(self, name: str, export, folder: str, url: str | None,
                  token: str | None, written: str) -> None:
        """Ecrit un JSON par bataille / train, puis l'envoie si `url` est donne."""
        try:
            since = storm.since_ms(self.since_var.get())
        except ValueError as exc:
            self._say(f"[error] {name}: {exc}")
            return
        only_mine = self.mine_var.get()
        db = self.storm_db
        done_kind = "storm_done" if name == "storm" else "train_done"
        self._status(f"{name}: " + (t("sending…") if url else t("writing…")), "busy")

        def work():
            try:
                res = export(folder, db_path=db, since_ms=since,
                             only_mine=only_mine, post_url=url, token=token)
            except (gamecache.CacheUnavailable, ValueError, OSError) as exc:
                self.q.put(Msg("log", f"[error] {name}: {exc}"))
                self.q.put(Msg(done_kind, (f"{name}: " + t("nothing written"), "err")))
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
            done = f"{name}: " + written.format(n=len(res.written))
            if url:
                done += ", " + t("{n} sent").format(n=len(res.written) - failed)
            self.q.put(Msg(done_kind, (done, "err" if failed else "ok")))
        self._spawn(work)


def main(db_path: str | None = None, keep: bool | None = None) -> int:
    root = tk.Tk()
    App(root, db_path, keep=keep)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
