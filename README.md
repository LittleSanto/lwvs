# lwvs — décodeur VS pour *Last War: Survival*

Capture le trafic réseau local du jeu, décode les messages du classement
Alliance Duel (VS), les accumule dans SQLite, et les exporte en JSON/CSV plats
pour un autre outil.

Le trafic n'est **pas chiffré** : il n'y a rien à déchiffrer, seulement à
décompresser (zstd) et à désérialiser.

Pas d'interface web, pas de serveur, pas de graphiques. La sortie est un
fichier de données.

---

## Installation

```bash
pip install -e ".[dev]"
```

Requiert **Wireshark** (qui fournit `tshark`, et **Npcap** sous Windows).
`tshark` est détecté automatiquement même hors du `PATH` ; sinon `--tshark <chemin>`.

---

## Démarrage

```bash
lwvs detect
```

Une commande, les deux réponses : l'**interface** et le **port**. Les deux se
prouvent par le même signal — une trame du jeu qui décode jusqu'à son dernier
octet — donc `detect` écoute **toutes les interfaces en parallèle** et s'arrête
**à la preuve** au lieu d'aller au bout du chronomètre. Jeu ouvert, c'est
typiquement 2 à 3 secondes ; `--seconds` est un plafond, pas une attente. Il
rend la ligne `--iface … --port …` à copier.

Le seuil est de **deux** trames exactes sur le même port (`--min-exact`) : un
protocole voisin peut se découper correctement une fois par hasard, deux fois
non.

Les deux commandes séparées restent là quand il faut du détail. `ifaces --probe`
compte les paquets sur chaque interface ; `discover` scrute un seul lien en
profondeur :

```bash
lwvs discover --iface "\Device\NPF_{...}" --duration 25
```

Le **port du jeu change à chaque session**. `discover` écoute tous les ports TCP
et **score chaque port en lui appliquant le vrai décodeur de trames** : si les
octets se découpent en trames valides, c'est le bon canal. Pas de plage de ports
ni de nom de processus — ce serait une devinette ; faire tourner le décodeur est
une preuve.

Le trafic web est **compté puis écarté du scoring**, jamais filtré à la capture :
c'est ce qui permet de dire « tout est sur 443 » au lieu de « aucun trafic ». Si
c'est le cas, relance avec `--keep-web` — le trafic du jeu n'est pas chiffré,
seul le numéro de port ressemble à du web.

Quand `discover` ne trouve rien, il dit **pourquoi** : erreur tshark remontée
telle quelle, interface muette, tout-sur-443, ou ports vus mais sans trame
décodable. Ces quatre causes appellent quatre gestes différents.

Pour chaque port il affiche le **détail du découpage en trames** — trames brutes
vs zstd, resyncs, octets sautés, flags vus. C'est ce qui distingue « autre
protocole » de « bon protocole, détail changé » : la magie zstd ne survient pas
par hasard, la voir sans rien décoder est un diagnostic très différent de ne pas
la voir du tout.

`--hex` ajoute les premiers octets de chaque flux, `--pcap-out FICHIER` conserve
la capture brute pour rejouer les hypothèses sans recapturer :

```bash
lwvs discover --iface "\Device\NPF_{...}" --duration 30 --hex --pcap-out captures/disc.pcapng
```

```bash
lwvs ingest --iface "\Device\NPF_{...}" --port 41234 --duration 120 --save captures/session1
```

```bash
lwvs export --dataset players --format csv -o exports/vs.csv
```

Sources interchangeables pour `ingest`, `inspect` et `probe` :

| option | usage |
|---|---|
| `--iface X --port N [--duration S]` | capture live (Ctrl-C sans `--duration`) |
| `--pcap fichier.pcapng [--port N]` | relit une capture Wireshark |
| `--dir DIR` | ré-ingère des payloads sauvés par `--save` |

---

## Trouver les kills (ou n'importe quel autre écran)

> **État au 10/08/2026, après capture réelle du classement VS.**
> `al.battle.rank.info` ne porte **que `score`** — confirmé sur 14 messages
> réels : aucun compteur d'éliminations. En revanche un champ `armyKill` existe
> ailleurs, dans deux commandes hors VS :
>
> - `al.rank` → `p.list[].armyKill` — un par membre de **ton** alliance ;
> - `get.new.user.info` → `p.armyKill` — profil d'un joueur.
>
> Ce n'est **pas** « les kills du VS » : c'est un compteur porté par le joueur,
> pas par le duel, et il ne couvre que ta propre alliance (pas l'adverse).
> Reste à vérifier à l'écran s'il est cumulé à vie ou remis à zéro. S'il est
> cumulatif, la différence de `armyKill` entre deux snapshots donne les kills de
> l'intervalle — c'est précisément ce que l'accumulation par `uid` permet.

La procédure qui a mené là, et qui reste la bonne pour tout autre écran :

```bash
lwvs ingest --iface IFACE --port N --duration 180 --save captures/kills
```

Ouvre l'écran VS pendant la capture, **onglet par onglet**, en laissant charger.
`--save` écrit les payloads bruts (y compris ceux qui n'ont pas décodé, dans
`failed/`) : tu itères ensuite sans recapturer.

```bash
lwvs inspect --dir captures/kills
lwvs inspect --dir captures/kills --values --key score
lwvs inspect --dir captures/kills --command rank --values
```

Lis d'abord **l'index du protocole** (la liste des commandes vues). Si un
classement d'éliminations existe, ce sera soit un `al.battle.rank.info` avec un
autre `type`, soit une commande distincte.

`--values` donne un exemple de valeur par champ : **c'est ce qui ferme la
boucle** — tu connais le chiffre affiché à l'écran, donc le champ qui le porte
s'identifie tout seul. C'est opt-in parce que ce sont de vraies données joueur.

`--command` et `--key` sont des **filtres de sous-chaîne**, donc des devinettes,
et c'est sans risque : ils réduisent un affichage, ils ne nomment pas un champ.
Deviner dans un parseur stockerait un chiffre faux sans jamais lever d'erreur —
c'est la ligne à ne pas franchir.

Regarde aussi le **compte des payloads qui se cadrent mais ne décodent pas,
groupé par octet de type fautif**. Un tiers d'une vraie capture peut être
illisible, et un message illisible est **invisible pour tous les filtres**.

### Attaquer un octet de type inconnu

```bash
lwvs probe --dir captures/kills --tag 0x06
```

`probe` cherche exhaustivement quelle combinaison de layouts (largeur fixe
0/1/2/4/8/16, préfixe de longueur u8/u16/u32, conteneur compté) fait décoder le
plus de payloads **jusqu'à leur dernier octet exactement**.

Il ne classe **pas** par « distance parcourue avant l'échec » : une largeur trop
longue avale l'octet de type suivant et parse donc *plus loin* que la bonne —
ce critère classe à l'envers. Seul « finit exactement à la fin » est
incorruptible.

Trois garde-fous, sinon ce n'est qu'une devinette habillée :

1. **Groupe de contrôle** — il vérifie d'abord que les payloads qui décodent
   déjà passent le même test. Sinon il le dit et refuse de classer.
2. **L'ambiguïté se rapporte** — les combinaisons ex æquo sont toutes également
   compatibles avec les octets ; un octet sur lequel elles divergent est
   marqué `INDETERMINE`, pas tranché à pile ou face.
3. **Rien ne s'écrit tout seul** dans la table de types. `probe` ne modifie
   jamais `lwvs/wire.py` : sa sortie est une **proposition à relire sur le hex**.

La recherche de racine est restreinte aux **offsets où la capture ancre
réellement ses messages**, relevés sur les seuls payloads *exacts*. Un payload
qui décode en laissant une queue n'a pas révélé un ancrage : il a révélé une
racine bidon. Scanner 64 offsets sur un payload qui ne parse nulle part
fabrique des octets de type qui n'existent pas.

---

## Capture → JSON, en une commande

```bash
lwvs grab --iface "\Device\NPF_{...}" --port 18731 --duration 180 -o exports/
```

Écrit un JSON par classement trouvé. **Aucune base sur le disque** : SQLite
tourne en RAM.

```
payloads 198  decodes 188  echecs 10
  garde-fou [OK]: 188/188 payloads decodent jusqu'au dernier octet (100.0 %)
  base en RAM : rien n'a ete ecrit hors des JSON.
  ecrit  exports/lwvs_kills.json         (99 records, mode kill_rank)
  ecrit  exports/lwvs_dons_semaine.json  (99 records, mode donation_weekly_rank)
  ecrit  exports/lwvs_dons_jour.json     (99 records, mode donation_daily_rank)
  ecrit  exports/lwvs_vs_total.json      (96 records, mode weekly_rank)
  ecrit  exports/lwvs_vs_day_j1.json     (99 records, mode daily_rank)
```

Les trois premiers viennent du **même message** (`al.rank`) : un écran capturé
peut alimenter plusieurs classements.

### Pourquoi la base n'est plus le défaut

Elle n'a jamais servi à produire un export. Sa seule justification forte était
les kills : `army_kill` est cumulatif, donc les kills d'une période sont une
**différence entre deux captures**, ce qui exige un historique. Cette
historisation est faite en aval, donc l'historique local n'a plus de
contrepartie — il ne reste que ses coûts (un fichier à gérer, des snapshots en
double, un `--snapshot N` à passer partout).

Ce qui reste vrai : SQLite est encore le moteur de **déduplication**. Le même
classement est retransmis plusieurs fois (14 messages → 379 lignes), et c'est la
clé `(snapshot, scope, day, uid)` qui ramène ça à une vérité unique. Ce
travail-là n'a jamais eu besoin d'un fichier.

`--db FICHIER` accumule quand même, pour qui veut. `--all-alliances` retire le
filtre sur ton alliance ; il ne s'applique de toute façon jamais aux kills, dont
la source (`al.rank`) *est* ton roster.

---

## L'interface graphique

```bash
lwvs gui
```

Tkinter, donc **aucune dépendance** : il est dans la stdlib et natif sous Windows.
Elle ne décide rien — elle appelle `capture`, `ingest` et `exporter` comme la CLI.

1. **Source** — la liste des interfaces se remplit toute seule et affiche les
   **noms usuels** (« Ethernet », « Wi-Fi ») : le `\Device\NPF_{…}` est ce que
   tshark exige, pas ce qu'un humain reconnaît — il reste la valeur réelle,
   visible au survol. **Un seul bouton « Détecter »** (ou `F4`) remplit
   l'interface *et* le port : c'est
   `lwvs detect`, il écoute tous les liens à la fois et s'arrête dès qu'une
   trame du jeu est reconnue, quelques secondes jeu ouvert. L'interface et le
   port sont mémorisés d'une session à l'autre (`lwvs.gui.json`).
2. **Capture** — Démarrer / Arrêter, avec le compteur de payloads et **le
   garde-fou affiché en permanence** : si la part de payloads décodés jusqu'au
   dernier octet passe sous 100 %, c'est écrit à l'écran.
3. **Ce qui a été vu** — une ligne par statistique, avec le nombre de messages
   reçus pendant la capture, son état et son nombre de lignes. Sélectionne et
   « Exporter en JSON… » (ou double-clic), ou « Tout exporter… » vers un
   dossier. « Seulement mon alliance » applique la règle `mine`. Journée et
   événement sont **déclarés par toi** : le message fait toujours foi quand il
   les porte.
4. **Envoi vers le site** — URL, jeton et « Envoyer maintenant », ou envoi
   automatique après chaque capture. Le jeton n'est écrit sur le disque que si
   tu coches explicitement « Retenir le jeton ». Rappel : l'envoi **publie** les
   pseudos et identifiants de tous les joueurs du classement.

**Rien n'est conservé par défaut** : la session travaille dans une base
temporaire effacée à la fermeture. Coche « Conserver l'historique localement »
si tu veux accumuler dans `lwvs.sqlite3`.

Raccourcis : `F5` démarre ou arrête la capture, `Ctrl+E` exporte la ligne
sélectionnée, `Ctrl+Maj+E` exporte tout, `Ctrl+Entrée` envoie. Le bouton en haut
à droite bascule entre thème sombre et clair ; le choix, la taille et la
position de la fenêtre sont mémorisés.

### Ajouter une statistique

La GUI ne code aucun bouton en dur : elle lit le registre `exporter.FEEDS`.
Ajouter une entrée là suffit à la faire apparaître, avec son voyant « vu / pas
vu » et son bouton d'export.

```python
Feed(
    key="ma_stat",
    label="Ma statistique",
    dataset="members",           # ou "players"
    command="al.rank",           # la commande du protocole qui l'alimente
    metric="weekly_progress",    # la colonne classée, si ce n'est pas la défaut
    mode="ma_stat_rank",         # DISTINCT de tout autre mode
)
```

`metric` existe parce qu'un message porte souvent plusieurs classements :
`al.rank` alimente à lui seul les kills et les deux classements de dons. Deux
flux qui partageraient un `mode` empileraient deux séries différentes côté
site.

Si la donnée n'est pas encore décodée, il faut d'abord la trouver
(`inspect --values`), lui ajouter un parseur dans `messages.py`, une table dans
`store.py`, puis le `Feed`.

### Envoyer vers un site de suivi

Voir `lwvs grab --post` plus haut et [`docs/CONTRAT.md`](docs/CONTRAT.md).
La GUI poste elle aussi, par sa carte « Envoi vers le site » : même transport,
même contrat.

---

### Envoyer vers un site de suivi

```bash
lwvs grab --iface ... --port ... -o exports/ --post https://mon-site/api/rankings --token XXX
```

Un `POST` par classement, `Content-Type: application/json; charset=utf-8`,
`Authorization: Bearer` si `--token`, et `Idempotency-Key` = sha256 du corps.
**Le corps HTTP est octet pour octet le contenu du fichier JSON** — un seul
format, deux transports, et c'est vérifié par un test.

Les fichiers sont écrits **avant** l'envoi : un service injoignable ne fait
jamais perdre une capture, elle reste rejouable depuis le disque.

**[`docs/CONTRAT.md`](docs/CONTRAT.md) décrit ce que le site reçoit et ce qu'il
doit en faire.** Il est fait pour être collé dans le prompt qui construit le
site : modes, sémantique du cumul, pourquoi `uid` est la seule clé, idempotence,
pièges d'encodage, ordres de grandeur.

---

## Distribuer la GUI

```powershell
.\build.ps1            # -> dist\lwvs-<version>-windows.zip
```

Le script lance **les tests d'abord** — on n'empaquette pas un arbre qui ne
passe pas —, appelle PyInstaller sur [`lwvs-gui.spec`](lwvs-gui.spec), copie
[`packaging/LISEZMOI.txt`](packaging/LISEZMOI.txt) dans le dossier et zippe le
tout. ~13 Mo. Le destinataire n'a besoin **ni de Python ni de zstandard**.

Il a toujours besoin de **Wireshark** : Npcap est un pilote signé dont la
licence encadre la redistribution, et personne n'installe un pilote en douce.
Le prérequis est donc traité comme une étape de l'application — si `tshark`
manque, la GUI affiche une bannière qui dit quoi installer et quelle case ne
pas cocher, au lieu de déverser une trace de pile dans le journal.

Trois choix, tous délibérés :

| choix | pourquoi |
|---|---|
| **onedir**, pas onefile | onefile se ré-extrait dans `%TEMP%` à chaque lancement, et déclenche bien plus d'heuristiques antivirus — ce programme écoute déjà le réseau, il part avec un handicap. |
| **console masquée** | l'utilisateur n'a rien à lire dans un terminal. En échange `packaging/lwvs_gui.py` attrape les exceptions de démarrage et les écrit dans `%LOCALAPPDATA%\lwvs\lwvs-crash.log`, sinon un échec serait une fenêtre qui n'apparaît jamais. |
| **pas d'UPX** | un drapeau rouge de plus pour les antivirus, pour quelques Mo. |

`zstandard` charge son backend C par un **import dynamique** : PyInstaller ne le
voit pas tout seul. Il est déclaré en `hiddenimports`, et `build.ps1` **vérifie
sa présence dans le build** — sans quoi le `.exe` démarrerait puis échouerait au
premier paquet, bien trop tard.

**Le binaire n'est pas signé.** SmartScreen affichera « Éditeur inconnu » et
certains antivirus le mettront en quarantaine : c'est attendu pour un exécutable
non signé qui écoute le réseau, et le `LISEZMOI` explique la manip. Un
certificat de signature (~200-400 €/an) est la seule vraie réponse.

À tester sur un poste **sans Python et sans Wireshark** : c'est le seul endroit
où le chemin du premier lancement existe vraiment.

---

## L'export

```bash
lwvs export --format json                            # les trois jeux, stdout
lwvs export --dataset players --format csv -o vs.csv
lwvs export --dataset players --scope day --day 3 --alliance TST --format csv -o j3.csv
```

Filtres : `--scope`, `--day`, `--alliance`, `--snapshot`. `--utf8-bom` pour Excel.

### `--format records` — la forme attendue par l'outil de classement en aval

```bash
lwvs export --dataset members --format records --alliance mine --snapshot 3 -o kills.json
lwvs export --dataset players --format records --alliance mine --scope total -o vs.json
```

```json
{
  "exported_at": "2026-08-10T21:58:45+00:00",
  "mode": "kill_rank",
  "records": [
    { "rank": 1, "uid": "1234567890001234", "player_name": "NomadeX", "score": 43602116 }
  ]
}
```

Un `mode` par métrique :

| `--dataset` | `--scope` / `--metric` | `mode` | source du `score` |
|---|---|---|---|
| `members` | — | `kill_rank` | `army_kill` du roster (`al.rank`), **cumulatif à vie** |
| `members` | `--metric weekly_progress` | `donation_weekly_rank` | points de don de la semaine, **remis à zéro** |
| `members` | `--metric today_progress` | `donation_daily_rank` | points de don du jour, **remis à zéro** |
| `players` | `--scope total` | `weekly_rank` | points VS cumulés de la semaine de duel |
| `players` | `--scope day` | `daily_rank` | points VS du jour |
| `server` | — | `thp_rank` | **THP** (`heroPower` de `rank.get`), classement du serveur — un **instantané**, ni cumulatif ni remis à zéro |

`--metric` choisit la colonne classée : un même jeu de lignes en porte
plusieurs. `--mode` force l'étiquette si l'outil en aval en attend une autre.

- **`uid` est ajouté à la forme d'origine.** C'est la seule clé de jointure
  stable : en 8 jours d'observation, des pseudos ont changé (`Nomade` →
  `NomadeX`, `Kairo` → `K a i r o`) et l'abréviation d'alliance aussi.
  Joindre sur le nom perd le joueur dès qu'il se renomme.
- Les champs propres à l'OCR (`confidence`, `known_player_*`, `screen_rank`,
  `issues`) sont absents : ils décrivent la fiabilité d'une lecture d'écran,
  alors que ces valeurs viennent du fil. En fabriquer serait mentir.
- `exported_at` porte l'**instant de capture**, pas l'heure d'écriture : cette
  forme n'a pas de champ par ligne pour le porter.
- `rank` est contigu (1..N). Filtré sur une alliance, ce **n'est pas** le rang
  VS global — celui-ci a des trous puisque les deux alliances sont classées
  ensemble. Il reste dans l'export CSV/JSON normal.
- Le format refuse de mélanger deux captures : un classement porte une date.

`--alliance mine` résout **ton** alliance par la règle du protocole — celle du
groupe dont la `position` égale celle de `duelInfo` — au lieu de figer une
abréviation. Ton `abbr` peut changer ; ta position est relue à chaque capture.
Si la règle n'est pas résoluble (il faut un `season.info` **et** un `group.info`
dans le même snapshot), l'export échoue avec un message explicite plutôt que de
rendre silencieusement tout le monde.

**joueurs** — une ligne par joueur, par capture, par scope :

```
captured_at, snapshot_id, scope, day, raw_type, rank, uid, name,
score, server_id, alliance_id, alliance_name, alliance_abbr
```

**groupe** — une ligne par alliance du groupe de duel :

```
captured_at, snapshot_id, group_code, position, alliance_id, alliance_name,
alliance_abbr, server_id, round_result, rank_type
```

**standing** — ta propre alliance, duel courant et précédent :

```
captured_at, snapshot_id, scope, group_code, position, rank_type, round_result
```

**membres** — le roster de **ton** alliance, seul porteur d'`army_kill` :

```
captured_at, snapshot_id, alliance_id, uid, name, army_kill, power,
alliance_rank, today_progress, weekly_progress, main_city_lv, server_id,
cur_server_id, online, join_time
```

`army_kill` est **cumulatif** (confirmé à l'écran) : les kills d'un intervalle
s'obtiennent par différence entre deux snapshots, joints sur `uid`. Cette
différence n'est **pas** calculée ici — l'export ne sort que des lignes brutes.
`alliance_rank` est le rang dans l'alliance (R1–R5), à ne pas confondre avec
`rank` du jeu de lignes joueurs, qui est le rang VS positionnel.

Ce qui compte pour l'outil en aval :

- **`uid` est conservé.** Seule clé de jointure stable entre captures : les
  pseudos changent, `uid` non. Ses 4 derniers chiffres sur 16 sont le serveur
  d'origine du joueur. Donnée personnelle, mais l'amputer casserait le suivi
  d'un joueur d'un jour à l'autre.
- **`scope` et `day` sont des colonnes, jamais des noms de colonnes.** Format
  long : une ligne par (joueur, scope, jour). L'outil en aval pivotera s'il veut.
- **`captured_at` sur chaque ligne.** Un duel dure plusieurs jours et se capture
  en plusieurs sessions.
- **Aucune agrégation.** Pas de totaux par alliance, pas de moyennes, pas de
  parts — seulement les lignes brutes décodées. L'agrégation est le travail de
  l'autre outil ; la faire ici livrerait deux fois la même vérité avec deux
  arrondis différents.
- **`raw_type` reste à côté de `scope`** pour que la déduction jour/cumul reste
  vérifiable en aval.
- **UTF-8 explicite**, y compris pour le CSV : les pseudos contiennent des
  emoji, de l'arabe et du chinois. Sous Windows, un CSV écrit sans encodage
  explicite sortirait en cp1252 et casserait.

En JSON, un seul `--dataset` produit un tableau ; `all` produit un objet à trois
clés. En CSV, `all` avec `-o vs.csv` écrit `vs.players.csv`, `vs.group.csv`,
`vs.standing.csv` (un CSV ne porte qu'une table).

---

## Le protocole, tel qu'il est confirmé

### Trame

```
brut       [flag u8][len u16 BE][payload : len octets]                -> avancer 3 + len
compressé  [flag u8][len u16 BE][taille_décomp u32 BE][trame zstd]    -> avancer 7 + len
```

Flags observés : `0x80` brut, `0xB0` compressé.

**Piège principal :** le `len` d'une trame compressée ne couvre **que la trame
zstd**, pas les 4 octets de taille décompressée. Avancer de `3 + len` fait
décoder parfaitement la première trame et transforme tout le reste en bouillie —
un décalage de 4 octets qui se propage. C'est testé explicitement
(`test_trame_compressee_avance_de_7_plus_len_pas_3_plus_len`).

Le discriminant compressé/brut n'est **pas le flag** mais la magie zstd
`28 B5 2F FD` : le flag n'est connu que pour deux valeurs, la magie est
auto-évidente. La sonde `+7` passe avant `+3`.

Décompression via `decompressobj()`, **pas** `decompress()` — celui-ci exige une
taille de contenu dans l'en-tête zstd, qui n'est pas garantie ici.

Réassemblage : concaténation **par flux TCP et dans l'ordre**, avec **les deux
sens d'une connexion dans des tampons séparés** (une connexion porte deux flux
indépendants ; les fusionner désynchronise les deux). Resynchronisation avec
lookahead d'une trame sur en-tête incohérent.

### Sérialisation

Big-endian partout. Chaque valeur commence par un octet de type.

| Tag | Type | Encodage |
|---|---|---|
| `0x01` | bool | 1 octet, 00/01 |
| `0x02` | int8 | 1 octet |
| `0x03` | int16 | 2 octets |
| `0x04` | int32 | 4 octets |
| `0x05` | int64 | 8 octets |
| `0x08` | string | `u16` longueur + UTF-8 |
| `0x0A` | bytes | **`u32`** longueur + octets opaques |
| `0x11` | array | `u16` nombre + N valeurs |
| `0x12` | map | `u16` nombre + N paires (clé, valeur) |

Les **clés de map n'ont pas d'octet de type** : juste `u16` longueur + le nom.
`0x0A` utilise un `u32` là où les strings utilisent un `u16` ; son unique
porteur observé est une clé `_proto` contenant du protobuf embarqué, gardé en
bytes bruts. Décodage des strings : UTF-8, puis cp1252, puis latin-1.

`0x07` est un **double IEEE 754 big-endian** (résolu le 20/08/2026 — voir
ci-dessous).

**Inconnus : `0x06`, `0x0C`, `0x0D`.** Vus dans du vrai trafic, layout
non établi. Leur largeur n'est **pas devinée** : le décodage s'arrête proprement
en rapportant l'offset et le hex autour. Une largeur devinée corromprait
silencieusement tous les champs suivants — bien pire qu'un arrêt net.

#### État du sondage (capture réelle, 198 payloads)

| octet | état | détail |
|---|---|---|
| `0x06` | **proposition : `fixed:4`** | groupe de contrôle 188/188 ; l'admettre fait passer 188 → 193 payloads exacts, sans en casser un seul |
| `0x07` | **RÉSOLU : `double`** | voir ci-dessous |
| `0x0C` | vu en aval de `0x07` | bloque encore `lw.camp.battle.vs.info` |
| `0x0D` | pas revu | — |

`fixed:4` pour `0x06` n'est **pas écrit dans la table de types** : c'est une
proposition à relire sur le hex.

#### `0x07` = double IEEE 754 big-endian

Il bloquait `rank.get.preview`, un message qui « décodait sans erreur » à
l'offset 24 en laissant **94 % de son contenu non lu** — seul le garde-fou à
99,0 % le signalait. Quatre preuves, dans l'ordre où elles ont été établies :

1. `probe --dir captures/thp --tag 0x07` propose `fixed:8`, **sans ex aequo**,
   et c'est le seul layout de l'espace qui décode quoi que ce soit ;
2. **relu sur le hex** — la règle du projet : `41 3e 95 9c 22 e0 16 51`, sur une
   clé `stageScore`, vaut **2 004 380,14** lu en double. Lu en int64 :
   4 701 359 558 853 924 433, qui n'est pas un score ;
3. sous l'hypothèse, **plus aucun payload des 4 captures n'est bloqué par
   `0x07`** : il est franchi et le décodage se poursuit jusqu'à `0x06`/`0x0C` ;
4. **falsification** — `fixed:2`, `fixed:4` et `fixed:16` désalignent le
   décodeur et le font retomber sur des octets arbitraires (`0x00` ×13).
   `fixed:8` est la **seule** largeur qui n'en produit aucun.

La position dans la table (`0x02`–`0x05` = int8/16/32/64, puis `0x06`, `0x07`)
faisait de `float32`/`float64` un voisinage plausible. La cohérence n'était pas
une preuve — les quatre points ci-dessus en sont une.

Pour sonder un octet en supposant un autre résolu, sans rien écrire nulle part :

```bash
lwvs probe --dir captures/vs1 --assume 0x06=fixed:4 --tag 0x0c
```

L'hypothèse est réaffichée en tête du rapport : tout ce qui suit en dépend.

### Enveloppe

```
{p: {p: <corps>, c: "<nom de commande>"}, a: int16, c: int8}
```

`c` interne porte le **nom de commande** : c'est le discriminant de type de
message, et l'aiguillage se fait dessus, pas sur la présence d'une clé marqueur.
`a` et `c` externes : rôle inconnu, aucun sens ne leur est inventé.

### Sélection de l'offset racine — contre-intuitif

Le payload peut commencer par un petit en-tête applicatif, et cet en-tête
**décode souvent comme un objet valide lui aussi**. « Le premier offset qui
parse » est donc le mauvais critère : il attrape l'en-tête et rend un objet
plausible avec deux champs bidons.

Bon critère : le **nombre d'octets consommés**. Chaque tag `0x12` dans les 64
premiers octets est essayé, et le décodage le plus glouton gagne. Testé
explicitement (`test_racine_la_plus_gloutonne_et_non_le_premier_offset_qui_parse`).

**Revers connu :** cette règle masque les échecs. Tant qu'un octet de type de
l'enveloppe est refusé, l'enveloppe ne parse pas et la règle retourne
silencieusement la map du dessous — tout a l'air de marcher pendant que
l'enveloppe est jetée. D'où le **garde-fou affiché à chaque commande** : la part
des payloads qui décodent jusqu'à leur **dernier octet exactement**. Si ce n'est
pas ~100 %, tu ne décodes pas ce que tu crois.

### Messages VS

**`al.battle.rank.info`** — le classement des joueurs.

- Le **rang est positionnel** : l'ordre du tableau *est* le classement, aucun
  champ ne le porte.
- **Les deux alliances du match arrivent dans le même message** ; `abbr` sépare
  ton camp du leur. Aucune seconde capture n'est nécessaire pour comparer.
- Jour vs cumul : le scope est déduit de **la présence de `day`**, pas de `type`.
  Un classement qui déclare le jour qu'il couvre est évidemment celui de ce
  jour, tandis que `type` est un entier opaque. **Les deux sont stockés** pour
  qu'une capture ultérieure puisse trancher.
- Le même classement est retransmis plusieurs fois (4× observé). L'ingestion est
  **idempotente** — clé `(snapshot, scope, day, uid)`, écriture en remplacement.
  Les totaux ne sont jamais cumulés entre messages, ce qui rapporterait 4× le
  vrai score.

**`get.alliance.duel.group.info`** — les 16 alliances du groupe.

**`get.alliance.duel.season.info`** — ta propre alliance, duel courant et
précédent. Ton alliance = celle du groupe dont la `position` égale celle de
`duelInfo`.

---

## Architecture

| module | responsabilité |
|---|---|
| `lwvs/wire.py` | **seul module à connaître les octets** : framing, sérialisation, encodeur. Une mise à jour du jeu doit casser ce fichier et rien d'autre. |
| `lwvs/capture.py` | tshark, réassemblage par flux et par sens, découverte de port |
| `lwvs/pipeline.py` | passe de décodage partagée, garde-fou « exact », sauvegarde des payloads |
| `lwvs/messages.py` | enveloppe et messages VS, sur objets déjà décodés |
| `lwvs/store.py` | **seul module à parler à la base** — pas de SQL ailleurs |
| `lwvs/inspection.py` | cartographie du protocole |
| `lwvs/probe.py` | sondage des octets de type inconnus |
| `lwvs/exporter.py` | JSON / CSV plats |

L'**encodeur vit à côté du décodeur** dans `wire.py`, pas dans un module de
données synthétiques : le générateur doit émettre exactement les octets que le
décodeur attend, et les séparer est la façon dont les deux divergent. Les tests
partent tous d'octets de trame et passent par la vraie enveloppe — un test qui
passerait un corps nu au parseur passerait pendant que ce qui arrive réellement
sur le fil échoue.

```bash
python -m pytest -q
```

---

## Confidentialité

- **Aucun contenu de payload n'est jamais logué.** Les diagnostics citent du hex
  et des offsets.
- Les **champs non mappés sont jetés au décodage** — seuls leur nom et leur type
  sont retenus, pour `inspect`.
- `--values` est **opt-in** : c'est le seul mode qui affiche de vraies données
  joueur.
- Captures, base et exports sont dans le `.gitignore` : un export VS contient
  les pseudos et les identifiants de ~200 joueurs réels.
- `--save` écrit des payloads bruts sur disque. C'est le prix de l'itération
  sans recapture ; traite ce répertoire comme des données personnelles.
