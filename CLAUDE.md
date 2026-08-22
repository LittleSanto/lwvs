# lwvs — contexte pour Claude Code

Outil en ligne de commande (+ GUI Tkinter) qui capture le trafic réseau local de
*Last War: Survival*, décode les classements, et les exporte en JSON pour un site
de statistiques externe.

Le trafic n'est **pas chiffré** : rien à déchiffrer, seulement à décompresser
(zstd) et désérialiser. Réponds en français.

```bash
python -m pytest -q          # 161 tests, ~45 s
.\build.ps1                  # -> dist\lwvs-<version>-windows.zip
python -m pyflakes lwvs tests packaging
```

Dépendance unique : `zstandard`. Tout le reste est stdlib. **Ne pas en ajouter**
sans raison forte — l'outil doit tourner sur un poste de joueur.
`tshark` (Wireshark + Npcap) est requis à l'exécution, détecté hors PATH.

---

## Règles d'architecture — elles ont été payées cher

| module | responsabilité, exclusive |
|---|---|
| `wire.py` | **seul à connaître les octets** : framing, sérialisation, encodeur. Une mise à jour du jeu doit casser ce fichier et rien d'autre. |
| `store.py` | **seul à parler à SQLite**. Aucun SQL ailleurs. |
| `capture.py` | tshark, réassemblage, découverte de port. Ne connaît pas le domaine. |
| `messages.py` | enveloppe et messages, sur objets **déjà décodés**. Aucun octet. |
| `exporter.py` | formats de sortie **et** registre `FEEDS` (voir plus bas). |
| `gui.py` | ne décide rien : appelle `capture`/`ingest`/`exporter` comme la CLI. |
| `packaging/` + `lwvs-gui.spec` + `build.ps1` | fabrication du `.exe`. PyInstaller est une dépendance de **build**, jamais embarquée : la règle « une seule dépendance à l'exécution » reste entière. |
| `theme.py` | **seul à connaître les couleurs** : palette, styles ttk, cartes, cases à cocher dessinées, bulles d'aide. Aucune logique métier. `gui.py` pose des widgets, `theme.py` les peint. |

L'**encodeur vit dans `wire.py`**, à côté du décodeur. Les séparer est la façon
dont les deux divergent. Les tests l'utilisent pour fabriquer de vraies trames.

---

## Les pièges qui reviennent

Chacun a coûté un bug réel. Ils sont tous couverts par un test nommé.

**Framing.** Une trame compressée avance de `7 + len`, pas `3 + len` : le `len`
ne couvre que la trame zstd, pas les 4 octets de taille décompressée. Le
discriminant est la **magie zstd** `28 B5 2F FD`, pas le flag. Décompression via
`decompressobj()`, jamais `decompress()`.

**Racine la plus gloutonne.** Un en-tête applicatif décode souvent comme un objet
valide. « Le premier offset qui parse » attrape l'en-tête. Le bon critère est le
**nombre d'octets consommés**. Revers : cette règle masque les échecs, d'où le
**garde-fou affiché partout** — la part de payloads qui décodent jusqu'à leur
dernier octet exactement doit être ~100 %. Vu en vrai le 20/08/2026 :
`rank.get.preview` décodait « sans erreur » à l'offset 24 en laissant **4 077
octets sur 4 346 non lus** — 94 % du message invisible, et un faux « hors
enveloppe » dans l'index des commandes. Seul le garde-fou à 99,0 % le disait.

**Ne jamais deviner la largeur d'un octet de type inconnu.** Une largeur devinée
corrompt silencieusement tout ce qui suit. Le décodage s'arrête net avec offset
et hex. `probe` propose, il n'écrit jamais dans `wire.DEFAULT_TABLE` (qui est et
reste vide).

**UTF-8 explicite partout, fichier ET stdout.** La console Windows est en cp1252
et casse sur le premier pseudo non latin. Le chemin stdout avait été oublié : il
rendait un export vide sans erreur visible.

**Un nombre n'arrive pas toujours en nombre.** `score` du classement
d'événement est une **chaîne** sur le fil. `find --value` ne comparait que les
entiers (et ne cherchait que des motifs int32/int64 dans les octets bruts) : il
répondait « NON trouvé » sur un message pourtant décodé à 100 %, et renvoyait
relire le hex d'un payload sans rapport. Les deux passes cherchent maintenant
aussi la forme **texte** du nombre.

**« Vide » et « jamais reçu » ne sont pas la même panne.** Le tableau de la GUI
affichait « aucune ligne » dans les deux cas. Le premier est un défaut de
décodage, le second se répare en jeu en rouvrant l'écran — les confondre fait
chercher un bug là où il n'y en a pas. Le tableau les sépare désormais, sur la
foi des commandes vues pendant la capture.

**Le poste d'un autre n'a ni ton PATH ni ton répertoire courant.**
`store.DEFAULT_DB` est **relatif** : lancé depuis un raccourci, le `.exe` posait
sa base et ses préférences dans un répertoire imprévisible — ou nulle part, si
c'était `Program Files`. La GUI ancre les deux dans `identity.home()`, avec une
migration unique des préférences déjà écrites. Et `tshark` absent n'est pas une
panne du programme mais un **prérequis** : `_spawn` en faisait une trace de pile
dans le journal, illisible pour un joueur. C'est désormais une bannière qui dit
quoi installer — et surtout quelle case de Npcap **ne pas** cocher.

**Tkinter n'est pas thread-safe.** Lire une variable Tk depuis un thread de
travail lève `main thread is not in main loop`, parfois seulement par
intermittence. Toutes les variables Tk sont lues sur le thread UI et passées au
worker en valeurs simples. Un test rend cette faute fatale.

**Les tests ne doivent jamais toucher l'état réel du poste.** `conftest.py`
isole `LWVS_HOME`. `filterwarnings = ["error"]` est volontaire : il a déjà
attrapé des fuites de threads et de timers Tk.

---

## Protocole — ce qui est établi

Enveloppe : `{p: {p: <corps>, c: "<commande>"}, a: int16, c: int8}`. On aiguille
sur le **nom de commande**. Certains messages n'en ont pas (battements de cœur) :
ce n'est pas un échec de décodage.

Sérialisation, big-endian, octet de type en tête : `01` bool, `02`–`05` int8/16/32/64,
`07` **double IEEE 754**, `08` string (`u16` + UTF-8), `0A` bytes (**`u32`**),
`11` array, `12` map. Les **clés de map n'ont pas d'octet de type**.

**Inconnus : `0x06`, `0x0C`, `0x0D`.** (`0x07` est résolu : double IEEE 754
big-endian.) `probe` propose `0x06 = fixed:4`
(groupe de contrôle 188/188, +5 payloads débloqués) — **proposition non écrite**,
à relire sur le hex. `0x0C` bloque `lw.camp.battle.vs.info`, qui porte peut-être
le numéro de journée des événements.

### Commandes décodées

| commande | contenu |
|---|---|
| `al.battle.rank.info` | classement VS. Rang **positionnel**. `day` présent ⇒ journée, absent ⇒ cumul. Les **deux alliances** dans le même message. |
| `get.alliance.duel.group.info` | les 16 alliances du groupe |
| `get.alliance.duel.season.info` | ta position (`duelInfo`, `lastDuelInfo`) |
| `al.rank` | roster de **ton** alliance ; porte `armyKill`, les **points de don**, `power` (**pas** le THP) et `allianceId` |
| `rank.get` | classement du **serveur** : ~200 joueurs, 15 alliances, seul porteur du **THP** (`heroPower`). Rang **positionnel**, `type` = onglet (13 observé, les autres inconnus). Certains joueurs n'ont pas d'`allianceId`. |
| `lw.camp.battle.user.score.rank` | classement d'événement. `score` en **chaîne**, `rank` réel, **pas d'`aid`**, aucune journée. |

### Faits contre-intuitifs sur les données

- **`armyKill` est cumulatif à vie.** Les kills d'une période sont une
  **différence** entre deux captures, jointe sur `uid`. Calculée en aval.
- **Les points de don sont l'inverse : remis à zéro.** `weeklyProgress` (écran
  « Points de don ») et `todayProgress` sont **déjà** la valeur de la période —
  aucune différence à calculer, et soustraire deux captures produirait des
  négatifs à chaque lundi. Établi sur la capture du lundi 10/08/2026, 99
  membres : `todayProgress == weeklyProgress` pour les 99 (même remise à zéro),
  et `weeklyDonateTime == 0` exactement quand `weeklyProgress == 0` (99/99),
  ce qui rattache « progress » aux dons et à rien d'autre.
- **Le THP est `heroPower` de `rank.get`, PAS `power` d'`al.rank`.** Le faux
  ami a coûté un flux entier, livré sur le mauvais champ. Le rapport entre les
  deux va de **0,512 à 0,749 selon le joueur** (42 joueurs communs) : 23,7
  points d'écart, donc pas un changement d'unité — deux mesures distinctes.
  Vérifié à l'écran via `selfRanking`, qui désigne la ligne du capteur.
  `power` reste une colonne du roster et **n'alimente aucun flux** : on
  n'exporte pas une métrique qu'on ne sait pas nommer.
- **Le THP est un instantané**, ni cumulatif ni remis à zéro : il monte *et*
  descend. Aucune différence à calculer, une baisse n'est pas une anomalie.
- **Un message porte plusieurs classements.** `al.rank` en alimente trois. La
  colonne classée est `Feed.metric`, pas une propriété du jeu de lignes, et
  chaque métrique a son propre `mode` — deux métriques sous un même `mode`
  empileraient deux séries chez le site sans que personne le voie.
- **`uid` est une chaîne de 16 chiffres**, jamais un nombre (précision flottante).
  Seule clé stable : pseudos et abréviations d'alliance changent (observé :
  `Nomade`→`NomadeX`, `KRKN`→`ORCA` en 8 jours).
- **Ton alliance se résout en trois niveaux** : écran Duel → `al.rank` → mémoire
  persistante. La mémoire n'est acceptée que si l'alliance apparaît réellement
  dans la capture (par id **ou** par abbr).
- **Journée mesurée ≠ déclarée.** `context.day_source` vaut `"message"` ou
  `"declared"`. Le message fait toujours foi.

---

## Ajouter une statistique

Le registre `exporter.FEEDS` est **la** liste : la GUI et `grab` le lisent, rien
n'est codé en dur ailleurs. Ajouter une entrée suffit à la faire apparaître.

1. La trouver : `grab --save captures/x` puis `find --dir captures/x --value <chiffre lu à l'écran>`.
   `find` cherche dans les objets décodés **et** dans les octets bruts — un
   payload illisible est invisible à la première passe.
2. Parseur dans `messages.py`, table dans `store.py`, `Feed(...)` dans `exporter.py`.
3. Mettre à jour **`docs/CONTRAT.md`** : c'est la spec que le site consomme.

---

## État hors dépôt

`%LOCALAPPDATA%\lwvs\` (ou `$LWVS_HOME`) : `identity.json` (ton alliance
mémorisée), `events.json` (catalogue des événements déclarables).

`context.event` est un **identifiant stable** (`s3_spice_wars`), pas du texte
libre — sinon « S3 - Spice Wars » et « S3 – Spice Wars » deviennent deux
événements côté site. Un nom absent du catalogue est **refusé**, pas créé au vol.

---

## Confidentialité

Un export contient les pseudos et identifiants de ~200 joueurs réels.

- **Aucun contenu de payload n'est jamais logué** : les diagnostics citent du hex
  et des offsets.
- Les champs non mappés sont **jetés au décodage** ; seuls nom et type remontent.
- `inspect --values` est opt-in — c'est le seul mode qui affiche des données joueur.
- Captures, base, exports et `*.gui.json` sont dans `.gitignore`.
- `--post` **publie** ces données chez un tiers : ce n'est pas la même chose
  qu'écrire un fichier local.

---

## Chantiers ouverts

- **`0x07` : RÉSOLU le 20/08/2026** — **double IEEE 754 big-endian**
  (`TAG_DOUBLE`), décodé et encodé, trois tests nommés. Quatre preuves, sur
  la capture `captures/thp` : (1) `probe --dir captures/thp --tag 0x07` le
  propose sans ex aequo ; (2) relu sur le hex, `41 3e 95 9c 22 e0 16 51` vaut
  2 004 380,14 en double sur une clé `stageScore` — 4,7×10¹⁸ en int64, absurde ;
  (3) sous l'hypothèse, **plus aucun payload des 4 captures n'est bloqué par
  `0x07`**, le décodage continue jusqu'à `0x06`/`0x0C` ; (4) falsification :
  `fixed:2/4/16` désalignent et font tomber sur des octets arbitraires (`0x00`
  ×13), `fixed:8` est la **seule** largeur qui n'en produit aucun.
  Débloque `rank.get.preview` (ventilation de puissance : `fightpower`,
  `buildingPower`, `heroHighestPower`) — mais une seule ligne par onglet.
- **`0x06`, `0x0C`, `0x0D`** non résolus. `0x06` est de loin le plus fréquent
  (17 payloads bloqués sur les 4 captures).
- **Journée des événements** : déclarée à la main faute de mieux.
  `lw.camp.battle.vs.info` est le candidat, bloqué par `0x0C`.
- **Côté site** : les documents arrivent (`applied: 0`, `pending_uids: N`),
  l'appariement `uid` ↔ `player_id` reste à faire — les joueurs y ont été créés
  par OCR, appariés par nom.
