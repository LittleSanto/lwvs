"""Langues de la GUI : anglais (texte source), francais, allemand.

La cle est le texte anglais tel qu'il est ecrit dans `gui.py` : un texte sans
traduction s'affiche donc en anglais plutot que de casser. Les `{n}` sont
remplis par `.format()` APRES traduction, chaque langue garde les memes.

Ajouter une langue : une entree dans `LANGS` et un dictionnaire dans `_T`.
Les journaux techniques (codes HTTP, traces) restent en anglais.
"""

from __future__ import annotations

#: Code -> nom affiche dans les reglages, dans sa propre langue.
LANGS = {"en": "English", "fr": "Français", "de": "Deutsch"}

_lang = "en"


def set_lang(code: str) -> None:
    global _lang
    _lang = code if code in LANGS else "en"


def current() -> str:
    return _lang


def t(text: str) -> str:
    return _T.get(_lang, {}).get(text, text)


_FR = {
    # navigation et ecran principal
    "Stats": "Stats",
    "Settings": "Réglages",
    "Capture: open each VS tab in the game and let it load. Storm and trains are "
    "read from the game's mailbox on this PC, no capture needed.":
        "Capture : ouvrez chaque onglet VS dans le jeu et laissez-le charger. "
        "Storm et trains sont lus dans la boîte mail du jeu sur ce PC, sans capture.",
    "Start capture": "Lancer la capture",
    "Stop": "Arrêter",
    "Start capturing": "Lancer la capture",
    "Stop capturing": "Arrêter la capture",
    "The first time, the interface and port are found by\nthemselves before the "
    "capture starts.":
        "La première fois, l'interface et le port sont trouvés\n"
        "tout seuls avant le début de la capture.",
    "Decoded fully": "Décodé en entier",
    "Share of payloads decoded down to their last byte.\nBelow 100 %, you are "
    "not decoding what you think.":
        "Part des messages décodés jusqu'au dernier octet.\n"
        "Sous 100 %, vous ne décodez pas ce que vous croyez.",
    "Network": "Réseau",
    "Your alliance": "Votre alliance",
    "Day": "Jour",
    "Event": "Événement",
    "Day and event are declared by you,\nnot read from the game.":
        "Le jour et l'événement sont déclarés par vous,\npas lus dans le jeu.",
    "Add an event to the catalogue": "Ajouter un événement au catalogue",
    "Mail since": "Mails depuis",
    "Period for Storm and trains. Pick one, or type a date:\n"
    "YYYY-MM-DD = from that day (UTC)":
        "Période pour Storm et trains. Choisissez-en une, ou tapez une date :\n"
        "AAAA-MM-JJ = à partir de ce jour (UTC)",
    "Read the game's mailbox again": "Relire la boîte mail du jeu",
    "Show log": "Afficher le journal",
    "Hide log": "Masquer le journal",
    "Copy": "Copier",
    "Clear": "Effacer",
    "Send to the site": "Envoyer au site",
    "Send {n} to the site": "Envoyer {n} au site",
    "Publishes every ready line (rankings, Storm, trains)":
        "Publie chaque ligne prête (classements, Storm, trains)",
    "This sends the names and ids of every player in them.":
        "Cela envoie les pseudos et identifiants de tous les joueurs concernés.",
    "Save JSON…": "Enregistrer en JSON…",
    "Writes one file per ready line": "Écrit un fichier par ligne prête",
    "Double-click a ranking to save only that one":
        "Double-cliquez un classement pour n'enregistrer que lui",
    "Data": "Données",
    "Messages": "Messages",
    "Status / details": "État / détails",
    "Rows": "Lignes",
    # Wireshark
    "Wireshark is required, and it could not be found":
        "Wireshark est requis et n'a pas été trouvé",
    "lwvs does not sniff the network itself: it delegates to tshark, which ships "
    "with Wireshark.\nInstall Wireshark leaving Npcap ticked, then restart lwvs.\n"
    "When Npcap installs, DO NOT tick \"Restrict Npcap driver's access to "
    "Administrators only\":\nticked, no interface will see a single packet unless "
    "lwvs runs as administrator.":
        "lwvs n'écoute pas le réseau lui-même : il délègue à tshark, fourni avec "
        "Wireshark.\nInstallez Wireshark en laissant Npcap coché, puis relancez lwvs.\n"
        "À l'installation de Npcap, NE cochez PAS « Restrict Npcap driver's access "
        "to Administrators only » :\ncoché, aucune interface ne verra un seul paquet "
        "sauf si lwvs tourne en administrateur.",
    "Open the download page": "Ouvrir la page de téléchargement",
    "Wireshark missing": "Wireshark manquant",
    "tshark not found: install Wireshark (see the top of the window), then "
    "restart lwvs.":
        "tshark introuvable : installez Wireshark (voir en haut de la fenêtre), "
        "puis relancez lwvs.",
    # reglages
    "Stats site": "Site de stats",
    "URL and token: your alliance's R4 Tools → Push feed page on the site.":
        "URL et jeton : page R4 Tools → Push feed de votre alliance sur le site.",
    "Test the connection": "Tester la connexion",
    "Checks the URL and the token. Sends no ranking.":
        "Vérifie l'URL et le jeton. N'envoie aucun classement.",
    "testing…": "test en cours…",
    "Send automatically when a capture stops":
        "Envoyer automatiquement à la fin d'une capture",
    "Remember the token (plain text on this PC)":
        "Retenir le jeton (en clair sur ce PC)",
    "Sending publishes the names and ids of every player in the ranking.":
        "Envoyer publie les pseudos et identifiants de tous les joueurs du classement.",
    "Found by itself when you press Start. Change it only if detection fails.":
        "Trouvé tout seul au lancement. À changer seulement si la détection échoue.",
    "Network interface": "Interface réseau",
    "Game port": "Port du jeu",
    "Detect again": "Détecter à nouveau",
    "Listens on every interface at once and stops as soon\nas a game frame is "
    "recognised — a few seconds if the\ngame is running.":
        "Écoute toutes les interfaces à la fois et s'arrête dès\n"
        "qu'une trame du jeu est reconnue — quelques secondes si\n"
        "le jeu tourne.",
    "Data and display": "Données et affichage",
    "My alliance only": "Mon alliance seulement",
    "Keep history locally": "Garder l'historique en local",
    "Unticked, the session database is deleted on\nclose: only the JSON exports "
    "remain.":
        "Décoché, la base de la session est effacée à la\n"
        "fermeture : seuls les exports JSON restent.",
    "Language": "Langue",
    "Light theme": "Thème clair",
    "Dark theme": "Thème sombre",
    # etats et journal
    "ready": "prêt",
    "error": "erreur",
    "port": "port",
    "no interface": "aucune interface",
    "found by itself when you press Start": "trouvé tout seul au lancement",
    "no capture yet": "pas encore de capture",
    "the JSON exports are the deliverable; nothing is kept locally unless you "
    "tick \"Keep history locally\" in Settings.":
        "les exports JSON sont le livrable ; rien n'est gardé en local sauf si vous "
        "cochez « Garder l'historique en local » dans les Réglages.",
    "log copied to the clipboard.": "journal copié dans le presse-papiers.",
    "detecting: listening on every interface at once, stopping as soon as a game "
    "frame is recognised. Keep the game open.":
        "détection : écoute de toutes les interfaces, arrêt dès qu'une trame du "
        "jeu est reconnue. Gardez le jeu ouvert.",
    "detecting…": "détection…",
    "detecting… {s} s max · {n} packets": "détection… {s} s max · {n} paquets",
    "{n} game frames": "{n} trames du jeu",
    "detection found nothing": "la détection n'a rien trouvé",
    "interface and port found": "interface et port trouvés",
    "capturing…": "capture en cours…",
    "capture started on port {port}": "capture lancée sur le port {port}",
    "stopping…": "arrêt…",
    "stop requested — frames already received are being processed.":
        "arrêt demandé — les trames déjà reçues sont en cours de traitement.",
    "done — {p} payloads, {d} decoded, {f} failed":
        "terminé — {p} messages, {d} décodés, {f} en échec",
    "no usable message: empty snapshot dropped. Was the VS screen actually "
    "reloaded?":
        "aucun message exploitable : capture vide abandonnée. L'écran VS a-t-il "
        "vraiment été rechargé ?",
    "safeguard: {exact}/{decoded} decoded down to the last byte ({pct} %)":
        "garde-fou : {exact}/{decoded} décodés jusqu'au dernier octet ({pct} %)",
    "below 100 %, you are not decoding what you think":
        "sous 100 %, vous ne décodez pas ce que vous croyez",
    "Duel screen": "écran Duel",
    "member list": "liste des membres",
    "memory": "mémoire",
    "not identified — open the Duel screen or the member list once, it will be "
    "remembered":
        "non identifiée — ouvrez une fois l'écran Duel ou la liste des membres, "
        "elle sera retenue",
    "every alliance (see Settings)": "toutes les alliances (voir Réglages)",
    "known after the first capture": "connue après la première capture",
    "day": "jour",
    "not seen yet": "pas encore vu",
    "no rows": "aucune ligne",
    "message never received — reopen the screen":
        "message jamais reçu — rouvrez l'écran",
    "received, but no rows: please report": "reçu, mais aucune ligne : à signaler",
    "Destination folder": "Dossier de destination",
    "Name of the new event (e.g. S4 - Another event):":
        "Nom du nouvel événement (ex. S4 - Autre événement) :",
    "event added: {id}  ({label})": "événement ajouté : {id}  ({label})",
    "Enter the site URL in Settings.": "Saisissez l'URL du site dans les Réglages.",
    "Nothing to send: no usable capture.": "Rien à envoyer : aucune donnée exploitable.",
    "sending…": "envoi…",
    "sending {n} ranking(s) to {url}…": "envoi de {n} classement(s) à {url}…",
    "send complete.": "envoi terminé.",
    "send complete": "envoi terminé",
    "export refused": "export refusé",
    "writing…": "écriture…",
    "nothing written": "rien d'écrit",
    "{n} battle(s) written": "{n} bataille(s) écrite(s)",
    "{n} train(s) written": "{n} train(s) écrit(s)",
    "{n} sent": "{n} envoyé(s)",
    # mails du jeu
    "reading the game's mailbox…": "lecture de la boîte mail du jeu…",
    "game database not found under AppData\\LocalLow — has the game been run on "
    "this PC, with this Windows account?":
        "base du jeu introuvable sous AppData\\LocalLow — le jeu a-t-il été lancé "
        "sur ce PC, avec ce compte Windows ?",
    "team": "équipe",
    "declared": "déclarée",
    "win": "victoire",
    "loss": "défaite",
    "{n} battle(s) in the period": "{n} bataille(s) sur la période",
    "{n} duplicate mail(s) merged": "{n} mail(s) en double fusionné(s)",
    "{n} unreadable": "{n} illisible(s)",
    "Desert team is declared, not in the mail: evening = A, early afternoon = B":
        "l'équipe Desert est déclarée, pas dans le mail : soir = A, début "
        "d'après-midi = B",
    "none in the period — widen « Mail since »":
        "rien sur la période — élargissez « Mails depuis »",
    "{n} train(s) in the period": "{n} train(s) sur la période",
    "{n} purchased": "{n} acheté(s)",
    "purchased": "acheté",
    "en route": "en route",
    "{a} attack(s), {r} robbed": "{a} attaque(s), {r} pillé(s)",
    # libelles des flux (exporter.FEEDS)
    "Kills — alliance roster": "Kills — membres de l'alliance",
    "Donation points — week": "Points de don — semaine",
    "Alliance roles (R1–R5)": "Rangs d'alliance (R1–R5)",
    "THP — server ranking": "THP — classement serveur",
    "Event ranking (camp battle)": "Classement d'événement (bataille de camps)",
    "Canyon Storm — registered, team A": "Canyon Storm — inscrits, équipe A",
    "Canyon Storm — registered, team B": "Canyon Storm — inscrits, équipe B",
    "Desert Storm — team A": "Desert Storm — équipe A",
    "Desert Storm — team B": "Desert Storm — équipe B",
    "VS points — duel total": "Points VS — total du duel",
    "VS points — per day": "Points VS — par jour",
    # tableau : bandeaux et libelles courts
    "Capture": "Capture",
    "Storms": "Tempêtes",
    "Trains": "Trains",
    "{r} / {n} ready": "{r} / {n} prêts",
    "Donations · week": "Dons · semaine",
    "Roles R1–R5": "Rôles R1–R5",
    "THP · server": "THP · serveur",
    "Camp battle": "Bataille de camp",
    "Canyon · team A": "Canyon · équipe A",
    "Canyon · team B": "Canyon · équipe B",
    "Desert · team A": "Desert · équipe A",
    "Desert · team B": "Desert · équipe B",
    "VS · day": "VS · jour",
}

_DE = {
    "Stats": "Statistiken",
    "Settings": "Einstellungen",
    "Capture: open each VS tab in the game and let it load. Storm and trains are "
    "read from the game's mailbox on this PC, no capture needed.":
        "Aufzeichnung: Öffne im Spiel jeden VS-Tab und lass ihn laden. Storm und "
        "Züge werden aus dem Spielpostfach auf diesem PC gelesen, ohne Aufzeichnung.",
    "Start capture": "Aufzeichnung starten",
    "Stop": "Stopp",
    "Start capturing": "Aufzeichnung starten",
    "Stop capturing": "Aufzeichnung beenden",
    "The first time, the interface and port are found by\nthemselves before the "
    "capture starts.":
        "Beim ersten Mal werden Schnittstelle und Port\n"
        "automatisch gefunden, bevor die Aufzeichnung beginnt.",
    "Decoded fully": "Vollständig dekodiert",
    "Share of payloads decoded down to their last byte.\nBelow 100 %, you are "
    "not decoding what you think.":
        "Anteil der bis zum letzten Byte dekodierten Nachrichten.\n"
        "Unter 100 % dekodierst du nicht, was du denkst.",
    "Network": "Netzwerk",
    "Your alliance": "Deine Allianz",
    "Day": "Tag",
    "Event": "Event",
    "Day and event are declared by you,\nnot read from the game.":
        "Tag und Event gibst du selbst an,\nsie werden nicht aus dem Spiel gelesen.",
    "Add an event to the catalogue": "Event zum Katalog hinzufügen",
    "Mail since": "Mails seit",
    "Period for Storm and trains. Pick one, or type a date:\n"
    "YYYY-MM-DD = from that day (UTC)":
        "Zeitraum für Storm und Züge. Wähle einen oder gib ein Datum ein:\n"
        "JJJJ-MM-TT = ab diesem Tag (UTC)",
    "Read the game's mailbox again": "Spielpostfach neu lesen",
    "Show log": "Protokoll zeigen",
    "Hide log": "Protokoll ausblenden",
    "Copy": "Kopieren",
    "Clear": "Leeren",
    "Send to the site": "An die Website senden",
    "Send {n} to the site": "{n} an die Website senden",
    "Publishes every ready line (rankings, Storm, trains)":
        "Veröffentlicht jede fertige Zeile (Ranglisten, Storm, Züge)",
    "This sends the names and ids of every player in them.":
        "Dabei werden Namen und IDs aller enthaltenen Spieler gesendet.",
    "Save JSON…": "Als JSON speichern…",
    "Writes one file per ready line": "Schreibt eine Datei pro fertiger Zeile",
    "Double-click a ranking to save only that one":
        "Doppelklick auf eine Rangliste speichert nur diese",
    "Data": "Daten",
    "Messages": "Nachrichten",
    "Status / details": "Status / Details",
    "Rows": "Zeilen",
    "Wireshark is required, and it could not be found":
        "Wireshark wird benötigt und wurde nicht gefunden",
    "lwvs does not sniff the network itself: it delegates to tshark, which ships "
    "with Wireshark.\nInstall Wireshark leaving Npcap ticked, then restart lwvs.\n"
    "When Npcap installs, DO NOT tick \"Restrict Npcap driver's access to "
    "Administrators only\":\nticked, no interface will see a single packet unless "
    "lwvs runs as administrator.":
        "lwvs liest das Netzwerk nicht selbst mit: Es nutzt tshark, das mit "
        "Wireshark kommt.\nInstalliere Wireshark mit aktiviertem Npcap und starte "
        "lwvs neu.\nBei der Npcap-Installation „Restrict Npcap driver's access to "
        "Administrators only“ NICHT ankreuzen:\nangekreuzt sieht keine Schnittstelle "
        "ein Paket, außer lwvs läuft als Administrator.",
    "Open the download page": "Download-Seite öffnen",
    "Wireshark missing": "Wireshark fehlt",
    "tshark not found: install Wireshark (see the top of the window), then "
    "restart lwvs.":
        "tshark nicht gefunden: Installiere Wireshark (siehe oben im Fenster) und "
        "starte lwvs neu.",
    "Stats site": "Statistik-Website",
    "URL and token: your alliance's R4 Tools → Push feed page on the site.":
        "URL und Token: Seite R4 Tools → Push feed deiner Allianz auf der Website.",
    "Test the connection": "Verbindung testen",
    "Checks the URL and the token. Sends no ranking.":
        "Prüft URL und Token. Sendet keine Rangliste.",
    "testing…": "wird getestet…",
    "Send automatically when a capture stops":
        "Nach Ende einer Aufzeichnung automatisch senden",
    "Remember the token (plain text on this PC)":
        "Token merken (Klartext auf diesem PC)",
    "Sending publishes the names and ids of every player in the ranking.":
        "Senden veröffentlicht Namen und IDs aller Spieler der Rangliste.",
    "Found by itself when you press Start. Change it only if detection fails.":
        "Wird beim Start automatisch gefunden. Nur ändern, wenn die Erkennung "
        "fehlschlägt.",
    "Network interface": "Netzwerkschnittstelle",
    "Game port": "Spielport",
    "Detect again": "Erneut erkennen",
    "Listens on every interface at once and stops as soon\nas a game frame is "
    "recognised — a few seconds if the\ngame is running.":
        "Hört auf allen Schnittstellen gleichzeitig und stoppt,\n"
        "sobald ein Spielpaket erkannt wird — wenige Sekunden,\n"
        "wenn das Spiel läuft.",
    "Data and display": "Daten und Anzeige",
    "My alliance only": "Nur meine Allianz",
    "Keep history locally": "Verlauf lokal behalten",
    "Unticked, the session database is deleted on\nclose: only the JSON exports "
    "remain.":
        "Nicht angekreuzt wird die Sitzungsdatenbank beim\n"
        "Schließen gelöscht: Nur die JSON-Exporte bleiben.",
    "Language": "Sprache",
    "Light theme": "Helles Design",
    "Dark theme": "Dunkles Design",
    "ready": "bereit",
    "error": "Fehler",
    "port": "Port",
    "no interface": "keine Schnittstelle",
    "found by itself when you press Start": "wird beim Start automatisch gefunden",
    "no capture yet": "noch keine Aufzeichnung",
    "the JSON exports are the deliverable; nothing is kept locally unless you "
    "tick \"Keep history locally\" in Settings.":
        "die JSON-Exporte sind das Ergebnis; lokal wird nichts behalten, außer du "
        "kreuzt „Verlauf lokal behalten“ in den Einstellungen an.",
    "log copied to the clipboard.": "Protokoll in die Zwischenablage kopiert.",
    "detecting: listening on every interface at once, stopping as soon as a game "
    "frame is recognised. Keep the game open.":
        "Erkennung: Höre auf allen Schnittstellen, Stopp sobald ein Spielpaket "
        "erkannt wird. Lass das Spiel offen.",
    "detecting…": "Erkennung…",
    "detecting… {s} s max · {n} packets": "Erkennung… max. {s} s · {n} Pakete",
    "{n} game frames": "{n} Spielpakete",
    "detection found nothing": "Erkennung hat nichts gefunden",
    "interface and port found": "Schnittstelle und Port gefunden",
    "capturing…": "Aufzeichnung läuft…",
    "capture started on port {port}": "Aufzeichnung auf Port {port} gestartet",
    "stopping…": "wird beendet…",
    "stop requested — frames already received are being processed.":
        "Stopp angefordert — bereits empfangene Pakete werden verarbeitet.",
    "done — {p} payloads, {d} decoded, {f} failed":
        "fertig — {p} Nachrichten, {d} dekodiert, {f} fehlgeschlagen",
    "no usable message: empty snapshot dropped. Was the VS screen actually "
    "reloaded?":
        "keine verwertbare Nachricht: leere Aufzeichnung verworfen. Wurde der "
        "VS-Bildschirm wirklich neu geladen?",
    "safeguard: {exact}/{decoded} decoded down to the last byte ({pct} %)":
        "Kontrolle: {exact}/{decoded} bis zum letzten Byte dekodiert ({pct} %)",
    "below 100 %, you are not decoding what you think":
        "unter 100 % dekodierst du nicht, was du denkst",
    "Duel screen": "Duell-Bildschirm",
    "member list": "Mitgliederliste",
    "memory": "Gedächtnis",
    "not identified — open the Duel screen or the member list once, it will be "
    "remembered":
        "nicht erkannt — öffne einmal den Duell-Bildschirm oder die "
        "Mitgliederliste, sie wird gemerkt",
    "every alliance (see Settings)": "alle Allianzen (siehe Einstellungen)",
    "known after the first capture": "nach der ersten Aufzeichnung bekannt",
    "day": "Tag",
    "not seen yet": "noch nicht gesehen",
    "no rows": "keine Zeilen",
    "message never received — reopen the screen":
        "Nachricht nie empfangen — Bildschirm erneut öffnen",
    "received, but no rows: please report": "empfangen, aber keine Zeilen: bitte melden",
    "Destination folder": "Zielordner",
    "Name of the new event (e.g. S4 - Another event):":
        "Name des neuen Events (z. B. S4 - Anderes Event):",
    "event added: {id}  ({label})": "Event hinzugefügt: {id}  ({label})",
    "Enter the site URL in Settings.": "Gib die URL der Website in den Einstellungen ein.",
    "Nothing to send: no usable capture.": "Nichts zu senden: keine verwertbaren Daten.",
    "sending…": "wird gesendet…",
    "sending {n} ranking(s) to {url}…": "sende {n} Rangliste(n) an {url}…",
    "send complete.": "Senden abgeschlossen.",
    "send complete": "Senden abgeschlossen",
    "export refused": "Export abgelehnt",
    "writing…": "wird geschrieben…",
    "nothing written": "nichts geschrieben",
    "{n} battle(s) written": "{n} Schlacht(en) geschrieben",
    "{n} train(s) written": "{n} Zug/Züge geschrieben",
    "{n} sent": "{n} gesendet",
    "reading the game's mailbox…": "Spielpostfach wird gelesen…",
    "game database not found under AppData\\LocalLow — has the game been run on "
    "this PC, with this Windows account?":
        "Spieldatenbank unter AppData\\LocalLow nicht gefunden — wurde das Spiel "
        "auf diesem PC mit diesem Windows-Konto gestartet?",
    "team": "Team",
    "declared": "angegeben",
    "win": "Sieg",
    "loss": "Niederlage",
    "{n} battle(s) in the period": "{n} Schlacht(en) im Zeitraum",
    "{n} duplicate mail(s) merged": "{n} doppelte Mail(s) zusammengeführt",
    "{n} unreadable": "{n} unlesbar",
    "Desert team is declared, not in the mail: evening = A, early afternoon = B":
        "Desert-Team ist angegeben, nicht aus der Mail: Abend = A, früher "
        "Nachmittag = B",
    "none in the period — widen « Mail since »":
        "nichts im Zeitraum — „Mails seit“ erweitern",
    "{n} train(s) in the period": "{n} Zug/Züge im Zeitraum",
    "{n} purchased": "{n} gekauft",
    "purchased": "gekauft",
    "en route": "unterwegs",
    "{a} attack(s), {r} robbed": "{a} Angriff(e), {r} ausgeraubt",
    "Kills — alliance roster": "Kills — Allianzmitglieder",
    "Donation points — week": "Spendenpunkte — Woche",
    "Alliance roles (R1–R5)": "Allianzränge (R1–R5)",
    "THP — server ranking": "THP — Server-Rangliste",
    "Event ranking (camp battle)": "Event-Rangliste (Lagerschlacht)",
    "Canyon Storm — registered, team A": "Canyon Storm — angemeldet, Team A",
    "Canyon Storm — registered, team B": "Canyon Storm — angemeldet, Team B",
    "Desert Storm — team A": "Desert Storm — Team A",
    "Desert Storm — team B": "Desert Storm — Team B",
    "VS points — duel total": "VS-Punkte — Duell gesamt",
    "VS points — per day": "VS-Punkte — pro Tag",
    # Tabelle: Abschnitte und Kurzbezeichnungen
    "Capture": "Aufzeichnung",
    "Storms": "Stürme",
    "Trains": "Züge",
    "{r} / {n} ready": "{r} / {n} bereit",
    "Donations · week": "Spenden · Woche",
    "Roles R1–R5": "Ränge R1–R5",
    "THP · server": "THP · Server",
    "Camp battle": "Lagerschlacht",
    "Canyon · team A": "Canyon · Team A",
    "Canyon · team B": "Canyon · Team B",
    "Desert · team A": "Desert · Team A",
    "Desert · team B": "Desert · Team B",
    "VS · day": "VS · Tag",
}

_T = {"fr": _FR, "de": _DE}


if __name__ == "__main__":  # pragma: no cover
    # Auto-controle : memes cles dans chaque langue, memes `{champs}` qu'en anglais.
    import string

    def fields(s: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(s) if f}

    assert _FR.keys() == _DE.keys(), _FR.keys() ^ _DE.keys()
    for table in _T.values():
        for src, dst in table.items():
            assert fields(src) == fields(dst), (src, dst)
    print(f"ok: {len(_FR)} texts x {len(LANGS)} languages")
