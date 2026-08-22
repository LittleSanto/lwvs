"""Interface en ligne de commande.

Pas d'interface web, pas de serveur, pas de graphiques : la sortie est un
fichier de donnees.
"""

from __future__ import annotations

import argparse
import sys

from . import capture, exporter, inspection, probe as probe_mod
from .capture import DirSource, LiveSource, PcapSource, TsharkNotFound
from .ingest import ingest
from .pipeline import PayloadSaver
from .store import DEFAULT_DB, Store

PROG = "lwvs"


# ---------------------------------------------------------------------------
# options communes
# ---------------------------------------------------------------------------


def _add_source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("source (une seule)")
    g.add_argument("--iface", help="interface tshark (voir `lwvs ifaces`)")
    g.add_argument("--port", type=int, help="port TCP du jeu (voir `lwvs discover`)")
    g.add_argument("--duration", type=int, default=None,
                   help="duree de capture en secondes (defaut: jusqu'a Ctrl-C)")
    g.add_argument("--pcap", help="fichier .pcapng a relire")
    g.add_argument("--dir", dest="from_dir",
                   help="repertoire de payloads sauvegardes par `inspect --save`")
    g.add_argument("--tshark", help="chemin de tshark si hors PATH")


def _build_source(args: argparse.Namespace) -> capture.CaptureSource:
    chosen = [bool(args.from_dir), bool(args.pcap), bool(args.iface)]
    if sum(chosen) != 1:
        raise SystemExit(
            "choisis exactement une source : --dir DIR, --pcap FICHIER, "
            "ou --iface IFACE --port N"
        )
    if args.from_dir:
        return DirSource(args.from_dir)
    if args.pcap:
        return PcapSource(args.pcap, port=args.port, tshark=args.tshark)
    if not args.port:
        raise SystemExit("--iface exige --port (lance `lwvs discover` pour le trouver)")
    return LiveSource(args.iface, args.port, duration=args.duration, tshark=args.tshark)


# ---------------------------------------------------------------------------
# commandes
# ---------------------------------------------------------------------------


def cmd_ifaces(args: argparse.Namespace) -> int:
    if not args.probe:
        for iface in capture.list_interfaces(args.tshark):
            print(f"{iface.number:>3}  {iface.label}")
        print("\n(--probe compte les paquets sur chacune : choisir a l'oeil est une"
              " devinette,\n une machine declare souvent Wi-Fi et Ethernet alors qu'un"
              " seul porte du trafic.)")
        return 0

    print(f"Comptage de {args.seconds}s sur chaque interface...\n", file=sys.stderr)
    results = capture.probe_interfaces(seconds=args.seconds, tshark=args.tshark)
    print(f"{'paquets':>9}  interface")
    for iface, count, err in results:
        shown = "erreur" if count < 0 else str(count)
        print(f"{shown:>9}  {iface.label}" + (f"   [{err}]" if err else ""))
    live = [(i, n) for i, n, _ in results if n > 0]
    if live:
        best = live[0][0]
        print(f"\n-> interface a utiliser : {best.name or best.device}")
        print(f'   --iface "{best.device}"')
    else:
        print("\n-> aucune interface ne voit de paquet. Npcap est-il installe, et"
              " cette session a-t-elle les droits de capture ?")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """Interface ET port en une passe. `ifaces --probe` puis `discover` etaient
    deux attentes a la file pour une seule et meme preuve."""
    print(
        f"Ecoute de toutes les interfaces a la fois, {args.seconds}s au plus.\n"
        "Chaque port est score par le vrai decodeur, et l'ecoute s'arrete des\n"
        "qu'un port produit des trames du jeu. Ouvre le jeu maintenant.\n",
        file=sys.stderr,
    )
    found = capture.autodetect(
        seconds=args.seconds, tshark=args.tshark, keep_web=args.keep_web,
        min_exact=args.min_exact,
    )
    if found.attempts:
        print(f"{'exacts':>6}  {'trames':>6}  {'paquets':>8}  {'port':>6}  interface")
        for att in found.attempts:
            best = att.best
            print(f"{(best.exact if best else 0):>6}  "
                  f"{(best.frames if best else 0):>6}  "
                  f"{att.report.total_packets:>8}  "
                  f"{(best.port if best else 0):>6}  "
                  f"{att.iface.label}" + (f"   [{att.error}]" if att.error else ""))

    problem = found.diagnosis()
    if problem:
        print(f"\n-> {problem}", file=sys.stderr)
        return 1
    print(f"\n-> trouve en {found.seconds:.1f}s : {found.iface.label}")
    print(f'   --iface "{found.iface.device}" --port {found.port}')
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    scope = ("tous ports TCP" if args.keep_web else
             f"tous ports TCP sauf {', '.join(str(p) for p in capture.WEB_PORTS)}")
    print(
        f"Ecoute de {args.duration}s sur {args.iface}, {scope}.\n"
        "Chaque port est score en lui appliquant le vrai decodeur de trames :\n"
        "si les octets se decoupent en trames valides, c'est le bon canal.\n"
        "Ouvre le jeu et l'ecran VS maintenant.\n",
        file=sys.stderr,
    )
    rep = capture.discover_ports(
        args.iface, duration=args.duration, tshark=args.tshark, top=args.top,
        keep_web=args.keep_web, pcap_out=args.pcap_out,
    )

    print(f"paquets TCP avec charge utile : {rep.total_packets}"
          + (f"   (dont {rep.web_packets} sur des ports web, exclus du scoring)"
             if rep.web_packets else ""))
    if rep.scores:
        print()
        print(f"{'port':>6}  {'paquets':>8}  {'octets':>9}  {'trames':>7}  "
              f"{'decodes':>7}  {'exacts':>7}  commandes")
        for sc in rep.scores:
            cmds = ", ".join(sorted(sc.commands)[:3]) or "-"
            print(f"{sc.port:>6}  {sc.packets:>8}  {sc.bytes_seen:>9}  {sc.frames:>7}  "
                  f"{sc.decoded:>7}  {sc.exact:>7}  {cmds}")
        print("\ndetail du decoupage en trames :")
        for sc in rep.scores:
            print(f"  {sc.port:>6}  {sc.verdict()}")
            print(f"          {sc.framing_note()}")
            if args.hex:
                for sample in sc.hex_samples:
                    print(f"          {sample}")
        if not args.hex:
            print("\n(--hex montre les premiers octets de chaque flux : c'est ce qui")
            print(" tranche entre 'autre protocole' et 'bon protocole, detail change'.)")

    if rep.pcap_path:
        print(f"\ncapture brute conservee : {rep.pcap_path}")
        print(f"   rejouable sans recapturer : {PROG} inspect --pcap {rep.pcap_path}")

    for warning in rep.warnings:
        print(f"\n[avertissement] {warning}", file=sys.stderr)
        print("   les paquets lus avant l'incident restent valides : ce qui a ete"
              " decode l'a ete pour de bon.", file=sys.stderr)

    problem = rep.diagnosis(args.keep_web)
    if problem:
        print(f"\n-> {problem}", file=sys.stderr)
        return 1
    best = rep.scores[0]
    print(f"\n-> port du jeu tres probable : {best.port} "
          f"({best.exact} payloads decodes jusqu'au dernier octet)")
    print(f"   --port {best.port}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    source = _build_source(args)
    saver = PayloadSaver(args.save) if args.save else None
    try:
        with Store(args.db) as store:
            result = ingest(source, store, note=args.note, saver=saver, progress=True)
    finally:
        if saver:
            saver.close()

    st = result.stats
    c = result.counters
    print(f"\nsnapshot #{result.snapshot_id}  source: {source.description}")
    print(f"  payloads {st.payloads}  decodes {st.decoded}  exacts {st.exact}  "
          f"echecs {st.failed}")
    if st.failures_by_tag:
        detail = "  ".join(f"0x{t:02x}x{n}" for t, n in st.failures_by_tag.most_common())
        print(f"  echecs par octet de type fautif : {detail}")
    print(f"  {st.guardrail()}")
    s = result.stored
    print(f"  classements  : {c.rank_messages} message(s), {c.rank_rows} ligne(s) vues"
          f" -> {s.get('players', 0)} stockee(s)")
    print(f"  groupe       : {c.group_messages} message(s), {c.group_rows} ligne(s) vues"
          f" -> {s.get('group', 0)} stockee(s)")
    print(f"  standing     : {c.season_messages} message(s), {c.standing_rows} ligne(s) vues"
          f" -> {s.get('standing', 0)} stockee(s)")
    print(f"  membres      : {c.member_messages} message(s), {c.member_rows} ligne(s) vues"
          f" -> {s.get('members', 0)} stockee(s)")
    # Le classement d'evenement manquait a cet inventaire : 100 lignes pouvaient
    # entrer en base sans qu'une seule ligne du resume le dise, et son absence
    # ne se distinguait pas d'un echec.
    print(f"  evenement    : {c.camp_messages} message(s), {c.camp_rows} ligne(s) vues"
          f" -> {s.get('camp', 0)} stockee(s)")
    print(f"  THP serveur  : {c.server_rank_messages} message(s),"
          f" {c.server_rank_rows} ligne(s) vues"
          f" -> {s.get('server', 0)} stockee(s)")
    if c.rank_rows > s.get("players", 0):
        print("  (l'ecart est normal : les retransmissions du meme classement sont"
              " ecrasees, jamais cumulees.)")
    if c.commands:
        print("  commandes vues :")
        for cmd, n in sorted(c.commands.items(), key=lambda kv: -kv[1]):
            print(f"    {n:5d}  {cmd}")
    if result.dropped:
        print("  aucun message VS : snapshot vide supprime.")
    return 0


def cmd_grab(args: argparse.Namespace) -> int:
    from .grab import MEMORY_DB, grab

    source = _build_source(args)
    result = grab(
        source, out_dir=args.out, db_path=args.db or MEMORY_DB,
        only_mine=not args.all_alliances, prefix=args.prefix, note=args.note,
        post_url=args.post, token=args.token, save_dir=args.save,
        declared_day=args.declare_day, event=args.event,
    )
    st = result.ingest.stats
    print(f"\npayloads {st.payloads}  decodes {st.decoded}  echecs {st.failed}")
    print(f"  {st.guardrail()}")
    if not args.db:
        print("  base en RAM : rien n'a ete ecrit hors des JSON.")
    if result.own_source:
        origine = {"duel": "ecran Duel", "roster": "liste des membres (al.rank)",
                   "memoire": "memorisee d'une capture precedente"}
        print(f"  ton alliance : {result.own_label}"
              f"  (source : {origine.get(result.own_source, result.own_source)})")
    for w in result.written:
        print(f"  ecrit  {w.path}  ({w.records} records, mode {w.feed.mode})")
        if w.published is not None:
            pub = w.published
            verdict = f"HTTP {pub.status}" if pub.ok else f"ECHEC {pub.error or pub.status}"
            print(f"         envoi {verdict}  cle {pub.key[:12]}...")
            # La reponse du service est LA reponse a "est-ce que c'est arrive ?".
            # Un 200 dit qu'il a recu, pas qu'il a applique. On la montre telle
            # quelle plutot que d'interpreter un format qui ne nous appartient pas.
            if pub.response.strip():
                print(f"         reponse {pub.response.strip()[:200]}")
    for key, why in result.skipped:
        print(f"  ignore {key} : {why}")
    for clash in result.conflicts:
        print(f"  [conflit] {clash}")
    if not result.written:
        print("\n-> aucun classement dans cette capture. L'ecran VS a-t-il ete"
              " recharge, onglet par onglet ?")
        return 1
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    source = _build_source(args)
    # Le saver est branche en amont des filtres : il sauve TOUT, y compris les
    # payloads qui n'ont pas decode (dans failed/). Un message illisible ne peut
    # pas matcher un filtre de cle, et ce sont justement ceux-la qu'il faut garder.
    saver = PayloadSaver(args.save) if args.save else None
    try:
        report = inspection.inspect_source(source, command_filter=args.command, saver=saver)
    finally:
        if saver:
            saver.close()
    print(inspection.render_report(report, key_filter=args.key, show_values=args.values))
    if args.save:
        print(f"\npayloads bruts ecrits dans {args.save}/ (ok/ et failed/).")
        print(f"re-ingestion : {PROG} inspect --dir {args.save}")
    if not args.values:
        print("\n(--values ajoute un exemple de valeur par champ : c'est ce qui ferme")
        print(" la boucle avec le chiffre affiche a l'ecran. Opt-in car ce sont de")
        print(" vraies donnees joueur.)")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    from .finder import find_values, render_find

    source = _build_source(args)
    numbers = [int(str(v).replace(" ", "").replace(",", "").replace(" ", ""))
               for v in (args.value or [])]
    report = find_values(source, numbers=numbers, texts=args.text or [])
    print(render_find(report, show_values=args.values))
    return 0 if report.by_path or report.raw_hits else 1


def cmd_probe(args: argparse.Namespace) -> int:
    source = _build_source(args)
    tags = [int(t, 0) for t in args.tag] if args.tag else None
    assume = dict(probe_mod.parse_assumption(a) for a in (args.assume or []))
    result = probe_mod.probe_source(
        source, tags=tags, max_payloads=args.max_payloads,
        max_combos=args.max_combos, progress=True, assume=assume,
    )
    print(probe_mod.render_probe(result))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        written = exporter.run_export(
            store,
            dataset=args.dataset,
            fmt=args.format,
            out=args.out,
            scope=args.scope,
            day=args.day,
            alliance=args.alliance,
            snapshot=args.snapshot,
            bom=args.utf8_bom,
            mode=args.mode,
            declared_day=args.declare_day,
            event=args.event,
            metric=args.metric,
        )
    if written != ["-"]:
        for target in written:
            print(f"ecrit: {target}", file=sys.stderr)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main   # import tardif : tkinter absent sur serveur
    # `--db` explicite = intention de conserver. Sans lui, la GUI travaille dans
    # une base temporaire effacee a la fermeture.
    return gui_main(args.db, keep=args.db is not None)


def cmd_events(args: argparse.Namespace) -> int:
    from . import events as events_mod

    if args.add:
        created = events_mod.add(args.add, event_id=args.id)
        print(f"ajoute : {created.id}  ({created.label})")
    if args.remove:
        ok = events_mod.remove(args.remove)
        print(f"{'retire' if ok else 'inconnu'} : {args.remove}")

    catalogue = events_mod.load()
    print(f"\ncatalogue ({events_mod.path()}) :")
    for event in catalogue:
        print(f"  {event.id:<24} {event.label}")
    print("\nL'identifiant est la cle de regroupement en aval ; le libelle n'est")
    print("qu'un affichage. Un libelle libre fragmenterait le suivi, d'où ce")
    print("catalogue : `--event` n'accepte que ce qui y figure.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        snaps = store.snapshots()
        if not snaps:
            print(f"base {args.db} : vide.")
            return 0
        print(f"base {args.db} -- {len(snaps)} snapshot(s)\n")
        print(f"{'id':>4}  {'captured_at':<25}  {'payl':>5} {'dec':>5} {'exa':>5}  source")
        for s in snaps:
            print(f"{s.id:>4}  {s.captured_at:<25}  {s.payloads:>5} {s.decoded:>5} "
                  f"{s.exact:>5}  {s.source}")
        print("\nlignes de classement par (snapshot, scope, jour) :")
        print(f"{'snap':>4}  {'scope':<6} {'day':>4} {'type':>4}  {'joueurs':>7}  alliances")
        for row in store.summary():
            print(f"{row['snapshot_id']:>4}  {row['scope']:<6} "
                  f"{row['day'] if row['day'] is not None else '-':>4} "
                  f"{row['raw_type'] if row['raw_type'] is not None else '-':>4}  "
                  f"{row['players']:>7}  {row['alliances']}")
        cmds = store.commands_seen()
        if cmds:
            print("\nindex du protocole (toutes captures) :")
            for cmd, n in cmds:
                print(f"  {n:6d}  {cmd}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Decodeur du classement Alliance Duel (VS) de Last War: Survival.",
        epilog="Le trafic n'est pas chiffre : il n'y a rien a dechiffrer, "
               "seulement a decompresser (zstd) et a deserialiser.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("ifaces", help="liste les interfaces tshark")
    q.add_argument("--probe", action="store_true",
                   help="compte les paquets sur chaque interface pour trouver la bonne")
    q.add_argument("--seconds", type=int, default=8, help="duree du comptage (--probe)")
    q.add_argument("--tshark")
    q.set_defaults(func=cmd_ifaces)

    q = sub.add_parser("detect", help="trouve interface ET port en une passe")
    q.add_argument("--seconds", type=int, default=20,
                   help="plafond de l'ecoute ; elle s'arrete a la premiere preuve")
    q.add_argument("--min-exact", type=int, default=2,
                   help="trames decodees jusqu'au dernier octet valant preuve")
    q.add_argument("--keep-web", action="store_true",
                   help="score aussi les ports web (si le jeu parle en 443)")
    q.add_argument("--tshark")
    q.set_defaults(func=cmd_detect)

    q = sub.add_parser("discover", help="trouve le port du jeu en scorant les ports")
    q.add_argument("--iface", required=True)
    q.add_argument("--duration", type=int, default=25)
    q.add_argument("--top", type=int, default=10)
    q.add_argument("--keep-web", action="store_true",
                   help="score aussi les ports web (si le jeu parle en 443)")
    q.add_argument("--hex", action="store_true",
                   help="montre les premiers octets de chaque flux (diagnostic)")
    q.add_argument("--pcap-out", help="ecrit aussi la capture brute dans ce fichier")
    q.add_argument("--tshark")
    q.set_defaults(func=cmd_discover)

    q = sub.add_parser("ingest", help="capture, decode et stocke les messages VS")
    _add_source_args(q)
    q.add_argument("--db", default=DEFAULT_DB)
    q.add_argument("--note", help="note libre attachee au snapshot")
    q.add_argument("--save", help="sauve aussi les payloads bruts dans ce repertoire")
    q.set_defaults(func=cmd_ingest)

    q = sub.add_parser(
        "grab",
        help="capture -> JSON en une commande, sans base sur le disque")
    _add_source_args(q)
    q.add_argument("-o", "--out", default="exports", help="dossier de sortie")
    q.add_argument("--prefix", default="lwvs", help="prefixe des noms de fichier")
    q.add_argument("--db", default=None,
                   help="accumule aussi dans cette base (defaut : RAM, rien "
                        "n'est conserve)")
    q.add_argument("--all-alliances", action="store_true",
                   help="n'applique pas le filtre sur ton alliance")
    q.add_argument("--declare-day", type=int, metavar="N",
                   help="journee de l'evenement, quand le message ne la porte "
                        "pas. Marquee `declared` dans le document : c'est une "
                        "affirmation, pas une mesure.")
    q.add_argument("--event", metavar="NOM",
                   help="nom de l'evenement (ex: \"Saison 3 - Spice Wars\"). "
                        "Jamais transmis par le jeu, toujours declare.")
    q.add_argument("--save", metavar="DIR",
                   help="garde aussi les payloads bruts (y compris ceux qui n'ont "
                        "pas decode) pour explorer avec `find` et `inspect`")
    q.add_argument("--post", metavar="URL",
                   help="envoie aussi chaque classement a ce service "
                        "(voir docs/CONTRAT.md). Publie des donnees joueur.")
    q.add_argument("--token", help="jeton Bearer pour --post")
    q.add_argument("--note")
    q.set_defaults(func=cmd_grab)

    q = sub.add_parser("inspect", help="cartographie le protocole (commandes, formes)")
    _add_source_args(q)
    q.add_argument("--command", help="filtre sous-chaine sur le nom de commande")
    q.add_argument("--key", help="filtre sous-chaine sur les chemins de cles")
    q.add_argument("--values", action="store_true",
                   help="montre un exemple de valeur par champ (vraies donnees joueur)")
    q.add_argument("--save", help="ecrit les payloads bruts, y compris les echecs")
    q.set_defaults(func=cmd_inspect)

    q = sub.add_parser(
        "find",
        help="retrouve un chiffre vu a l'ecran : dit quelle commande le porte")
    _add_source_args(q)
    q.add_argument("--value", action="append",
                   help="un nombre lu a l'ecran (espaces et virgules tolerees), "
                        "repetable")
    q.add_argument("--text", action="append", help="un pseudo lu a l'ecran, repetable")
    q.add_argument("--values", action="store_true",
                   help="montre chaque correspondance (vraies donnees joueur)")
    q.set_defaults(func=cmd_find)

    q = sub.add_parser("probe", help="sonde les octets de type inconnus")
    _add_source_args(q)
    q.add_argument("--tag", action="append",
                   help="restreint a un octet de type (ex: --tag 0x06), repetable")
    q.add_argument("--assume", action="append", metavar="TAG=LAYOUT",
                   help="suppose un octet resolu pour sonder les autres "
                        "(ex: --assume 0x06=fixed:4). Hypothese, pas un fait.")
    q.add_argument("--max-payloads", type=int, default=20)
    q.add_argument("--max-combos", type=int, default=500)
    q.set_defaults(func=cmd_probe)

    q = sub.add_parser("export", help="exporte en JSON ou CSV")
    q.add_argument("--db", default=DEFAULT_DB)
    q.add_argument("--dataset", choices=("all",) + exporter.DATASETS, default="all")
    q.add_argument("--format", choices=("json", "csv", "records"), default="json",
                   help="records = la forme {exported_at, mode, records[]} "
                        "attendue par l'outil de classement en aval")
    q.add_argument("-o", "--out", help="fichier de sortie ('-' ou absent : stdout)")
    q.add_argument("--scope", help="players: day|total ; standing: current|previous")
    q.add_argument("--day", type=int)
    q.add_argument("--alliance",
                   help="sous-chaine sur abbr/nom, id exact, ou 'mine' pour "
                        "resoudre TON alliance par la regle du protocole")
    q.add_argument("--snapshot", type=int)
    q.add_argument("--declare-day", type=int, metavar="N",
                   help="format records : journee declaree quand le message ne "
                        "la porte pas")
    q.add_argument("--event", metavar="NOM",
                   help="format records : nom de l'evenement (toujours declare)")
    q.add_argument("--metric", metavar="COLONNE",
                   help="format records : colonne classee. Un meme jeu de "
                        "lignes porte plusieurs classements -- `al.rank` donne "
                        "les kills (army_kill, defaut) ET les points de don "
                        "(weekly_progress, today_progress).")
    q.add_argument("--mode", help="format records : valeur du champ `mode` "
                                  "(defaut deduit du dataset et de la metrique)")
    q.add_argument("--utf8-bom", action="store_true",
                   help="ajoute un BOM au CSV (Excel sous Windows)")
    q.set_defaults(func=cmd_export)

    q = sub.add_parser("gui", help="interface graphique : capturer, voir, exporter")
    q.add_argument("--db", default=None,
                   help="conserve l'historique dans cette base (defaut : base "
                        "temporaire effacee a la fermeture)")
    q.set_defaults(func=cmd_gui)

    q = sub.add_parser("events", help="catalogue des evenements declarables")
    q.add_argument("--add", metavar="LIBELLE", help='ex: --add "S4 - Autre event"')
    q.add_argument("--id", help="identifiant force (defaut : deduit du libelle)")
    q.add_argument("--remove", metavar="ID")
    q.set_defaults(func=cmd_events)

    q = sub.add_parser("status", help="resume de la base accumulee")
    q.add_argument("--db", default=DEFAULT_DB)
    q.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TsharkNotFound as exc:
        print(f"erreur: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"erreur: fichier introuvable: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"erreur: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
