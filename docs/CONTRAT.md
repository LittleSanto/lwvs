# Contrat de données — `lwvs` → site de statistiques

> Ce fichier est fait pour être **collé tel quel** dans le prompt qui construit
> le site. Il décrit ce que le site reçoit, ce qu'il doit en faire, et les
> pièges qui font échouer une intégration naïve.
>
> Le producteur est `lwvs`, un outil qui capture le trafic réseau local de
> *Last War: Survival* et décode les classements. Le site ne parle jamais au
> jeu : il ne voit que les documents décrits ici.

---

## 1. Le document

Un document = **un classement, à un instant, pour une métrique**. C'est l'unité
d'échange, en fichier comme en HTTP.

```json
{
  "exported_at": "2026-08-10T21:58:45+00:00",
  "mode": "kill_rank",
  "records": [
    { "rank": 1, "uid": "1234567890001234", "player_name": "NomadeX", "score": 43602116 },
    { "rank": 2, "uid": "1234567890001250", "player_name": "Comete", "score": 13505959 }
  ]
}
```

| champ | type | sens |
|---|---|---|
| `exported_at` | ISO 8601 UTC | **instant de capture**, pas d'écriture. C'est la date de la mesure. |
| `mode` | string | quelle métrique — voir §2 |
| `records[].rank` | int | position dans **ce** document, 1..N contigu |
| `records[].uid` | **string** | identifiant joueur, 16 chiffres — voir §3 |
| `records[].player_name` | string | pseudo **à cet instant** — volatil, voir §3 |
| `records[].score` | int | la valeur de la métrique |

Il n'y a **pas** de champ de confiance, de statut de reconnaissance ni de liste
d'anomalies. Ces notions viennent d'un outil OCR ; ici les valeurs sont lues sur
le fil, elles sont exactes ou absentes. Le site ne doit pas les attendre.

---

## 2. Les modes

| `mode` | ce que `score` mesure | cumulatif ? |
|---|---|---|
| `kill_rank` | éliminations du joueur (`armyKill`) | **oui, à vie** |
| `weekly_rank` | points VS cumulés sur la semaine de duel | oui, dans la semaine |
| `daily_rank` | points VS d'une journée de duel | non |
| `camp_battle_rank` | points d'un événement à camps (Spice Wars…) | non |
| `donation_weekly_rank` | points de don de la semaine (`weeklyProgress`) | remis à zéro chaque semaine |
| `donation_daily_rank` | points de don du jour (`todayProgress`) | remis à zéro chaque jour |
| `thp_rank` | THP du joueur — `heroPower` du classement du serveur | **ni l'un ni l'autre** — instantané |
| `alliance_role` | **grade** dans l'alliance : `1`..`5` pour R1..R5 — pas une performance | ni l'un ni l'autre — un état |
| `desert_storm_rank` | points personnels d'**une** bataille de Desert Storm | non — une bataille |
| `canyon_storm_rank` | points totaux d'**une** bataille de Canyon Storm (bataille + assistance + stratégie) | non — une bataille |
| `alliance_train` | **pas de `score`** : qui conduisait **un** train, et son VIP ou Guardian | non — un train |
| `canyon_signup_a`, `canyon_signup_b` | **pas de `score`** : qui est **inscrit** au Canyon Storm dans l'équipe A / B, titulaire ou remplaçant | non — un état à l'instant de la capture |

D'autres modes apparaîtront. **Le site doit accepter un `mode` inconnu** en le
stockant tel quel plutôt qu'en rejetant le document : de nouvelles statistiques
seront ajoutées côté capture sans que le site change.

### `kill_rank` est cumulatif — c'est le point le plus important

`score` est un compteur **à vie**, jamais remis à zéro. « 43 602 116 » n'est pas
une performance, c'est un total historique.

**Les kills d'une période sont une différence entre deux documents**, jointe sur
`uid` :

```
kills(joueur, T1→T2) = score(joueur, T2) − score(joueur, T1)
```

C'est le site qui fait ce calcul ; `lwvs` n'agrège rien et n'envoie que des
valeurs brutes. Conséquences pour le modèle de données :

- il faut **conserver chaque mesure**, pas seulement la dernière ;
- une différence négative signale une anomalie (mauvais joueur, reset côté jeu),
  pas un joueur qui « perd » des kills ;
- un joueur absent d'un document n'est pas à zéro : il n'a pas été mesuré.
  Absence ≠ zéro, et les traiter pareil fabriquera de faux effondrements.

### `camp_battle_rank` — les événements saisonniers

Source : `lw.camp.battle.user.score.rank`. Le nom de la commande est **générique**
(« camp battle ») : d'autres événements de saison passeront très probablement
par le même message. Le site ne doit donc pas coder en dur « Spice Wars » — c'est
`context.event` qui porte le nom, et il est **déclaré par l'utilisateur** (§2ter).

Trois particularités, chacune vérifiée sur capture réelle :

- le classement contient **les deux camps** ; les documents sont filtrés sur ton
  alliance, comme pour le VS ;
- le message n'a **aucun identifiant d'alliance**, seulement l'abréviation. Le
  filtrage se fait donc sur `abbr` — moins solide qu'un identifiant, mais c'est
  tout ce que le jeu transmet ;
- le message **ne porte aucun numéro de journée**.

### `donation_*` — l'inverse exact de `kill_rank`

Source : `al.rank`, **le même message que `kill_rank`**. Le roster transporte
deux compteurs sans rapport l'un avec l'autre, et l'écran « Points de don » est
`weeklyProgress`.

**Ce sont des compteurs remis à zéro.** `score` est déjà la valeur de la
période : il n'y a **aucune différence à calculer**. Appliquer la règle de
`kill_rank` à un `donation_*` produirait des valeurs négatives à chaque remise à
zéro, interprétées comme des anomalies alors que c'est le fonctionnement normal.

| | `kill_rank` | `donation_weekly_rank` |
|---|---|---|
| remise à zéro | jamais | hebdomadaire (lundi) |
| valeur de la période | `score(T2) − score(T1)` | `score` directement |
| une baisse signifie | anomalie | remise à zéro, ou capture de la semaine suivante |

**Conséquence pour le site : ne jamais soustraire deux `donation_*`.** Deux
documents de semaines différentes sont deux mesures indépendantes ; la clé de
regroupement est la semaine, pas l'écart. Un `score` en baisse entre deux
captures n'est pas un joueur qui perd des points, c'est un lundi.

Ce que la capture du lundi 10/08/2026 établit, sur 99 membres : `todayProgress`
et `weeklyProgress` étaient **égaux pour les 99** — les deux compteurs partagent
bien la remise à zéro du lundi. Et `weeklyDonateTime == 0` exactement quand
`weeklyProgress == 0` : un score à zéro veut dire « n'a pas donné », pas « pas
mesuré ». C'est la seule métrique où le zéro est une information.

`donation_daily_rank` **se vide chaque nuit** : une capture faite le lendemain
ne rattrape pas la veille. Une journée non capturée est perdue, alors que
`donation_weekly_rank` reste rattrapable jusqu'au dimanche soir.

### `thp_rank` — ni cumulatif, ni remis à zéro : un instantané

Source : **`rank.get`**, le classement du serveur — pas le roster d'alliance.
`score` est le **THP** (*Total Hero Power*), c'est-à-dire le chiffre affiché sur
la fiche de profil du joueur. La correspondance a été **vérifiée à l'écran** :
le message porte un champ `selfRanking` qui désigne la ligne du joueur qui
capture, et la valeur de cette ligne est celle qu'il lisait en jeu.

#### Le faux ami qui rend deux séries incomparables

Le roster d'alliance (`al.rank`) transporte un champ nommé `power`. **Ce n'est
pas le THP.** Sur les 42 joueurs présents dans les deux captures, le rapport
`heroPower / power` se répartit ainsi :

| | rapport `heroPower` / `power` |
|---|---|
| minimum | 0,512 |
| médiane | 0,613 |
| maximum | 0,749 |

**23,7 points d'écart d'un joueur à l'autre.** Un changement d'unité serait
constant ; celui-ci ne l'est pas. Ce sont deux mesures différentes, et `power`
agrège quelque chose de plus large dont la composition n'est pas établie.

Conséquence côté producteur : **`power` n'alimente aucun document.** Il reste
une colonne de l'export `members` pour qui veut creuser, mais aucun `mode` ne le
publie, parce qu'on n'exporte pas une métrique qu'on ne sait pas nommer. Si un
document `thp_rank` arrive un jour avec des valeurs 40 % plus hautes que
d'habitude, ce n'est pas une progression : c'est un producteur qui s'est trompé
de champ, et le site peut le détecter ainsi.

#### C'est un état, pas un compteur

Troisième comportement possible, et il ne ressemble à aucun des deux autres :

| | `kill_rank` | `donation_weekly_rank` | `thp_rank` |
|---|---|---|---|
| peut descendre | non, jamais | seulement à la remise à zéro | **oui, à tout moment** |
| valeur de la période | `score(T2) − score(T1)` | `score` directement | **aucune** — c'est un état |
| une baisse signifie | anomalie | c'est lundi | troupes perdues, soin en cours, équipement changé — **normal** |

**Ce que le site doit en faire : stocker la valeur telle quelle, horodatée, et
ne jamais la soustraire.** Un graphe du THP dans le temps est légitime ; un
« gain de puissance sur la semaine » calculé par différence ne l'est pas.

#### Le seul document qui déborde de ton alliance

Tous les autres modes sont filtrés sur ta propre alliance. Celui-ci vient d'un
classement qui couvre **tout le serveur** — 200 lignes et 15 alliances dans la
capture de référence. Deux conséquences :

- le filtrage par alliance s'appuie sur un **vrai `allianceId`**, pas sur
  l'abréviation comme `camp_battle_rank`. C'est donc un filtre solide ;
- **certains joueurs n'ont pas d'alliance du tout** (5 sur 200). Ils sont
  écartés du document filtré, et c'est voulu.

Si le site veut la puissance des joueurs **adverses**, elle est disponible —
c'est le seul endroit du contrat où elle le soit — mais il faut la demander
explicitement côté producteur, qui filtre par défaut.

#### Croiser le THP avec un classement

C'est l'usage principal. Les documents `weekly_rank` / `daily_rank` classent au
score VS mais ne portent aucune puissance ; `thp_rank` porte la puissance. La
jointure se fait **sur `uid`**, jamais sur le pseudo (§3), de préférence entre
deux documents de la même capture — `exported_at` les rapproche.

Deux limites à respecter :

- **couverture partielle.** Un joueur du classement VS absent de `thp_rank` n'a
  pas un THP de zéro : il n'est pas dans le top 200 du serveur, ou l'écran n'a
  pas été capturé. **Absence ≠ zéro**, ici comme ailleurs ;
- **fraîcheur.** Sans écran du classement serveur pendant la capture, il n'y a
  pas de document `thp_rank` du tout. Un THP affiché à côté d'un classement doit
  être daté de **sa propre** capture, pas de celle du classement.

#### `rank.get` porte plusieurs onglets

Le message a un champ `type` qui désigne l'onglet du classement. Un seul (13) a
été observé, et **on ne sait pas ce que valent les autres** — le producteur le
transporte sans l'interpréter. Le site n'a rien à en faire aujourd'hui, mais
qu'il sache que deux onglets différents pourraient un jour porter deux
métriques sous le même nom de champ.

---

### `alliance_role` — le grade, pas un score

Source : `al.rank`, **le même message que `kill_rank`** — le champ `rank` de
chaque membre. `score` vaut `1` à `5` pour R1 à R5. Ce n'est pas une
performance : ne jamais l'additionner, le moyenner ni le soustraire.

Sur les captures des 10 et 11/08/2026 (99 membres) : 1 R5, 9 R4, 1 R3,
42 R2, 46 R1 — les deux fois identique.

**Le document porte tout le roster, pas seulement les officiers.** C'est ce qui
permet de voir une rétrogradation : un R4 redevenu R3 arrive avec `score: 3`,
alors que dans un document limité aux R4/R5 il aurait simplement disparu — et
une absence ne se distingue pas d'un écran non capturé. Pour ne garder que les
officiers, le site filtre `score >= 4`.

**Ce que le site doit en faire :** pour chaque `uid` du document, **remplacer**
le grade enregistré par `score`. Le plus récent `exported_at` fait foi. Un
joueur absent du document n'a pas changé de grade : il n'a pas été mesuré (ou
a quitté l'alliance — le document ne permet pas de trancher).

`rank` est trié R5 en tête, puis dans l'ordre du roster : entre deux membres de
même grade, il ne veut rien dire.

---

### `desert_storm_rank` / `canyon_storm_rank` — un document par bataille

Source : les **mails de résultat** du jeu, lus dans le cache local du client
(`lwvs storm`), pas sur le réseau — un mail ne passe sur le fil qu'une fois, à
sa réception. Chaque semaine produit **quatre documents** : Canyon A et B le
jeudi, Desert A et B le vendredi. Un document = une bataille = une équipe.

```json
{
  "exported_at": "2026-09-24T14:30:46+00:00",
  "mode": "canyon_storm_rank",
  "context": {
    "battle_date": "2026-09-24",
    "mail_uid": "66fffbc8b4f74d38a14e936a99d4c029",
    "team": "B",
    "team_source": "message",
    "result": "win",
    "side":          { "score": 322631, "members": 20 },
    "opponent_side": { "score": 185981, "members": 38 },
    "alliance":  { "id": "6671…", "alliance_abbr": "GolD", "alliance_name": "Golden Fire", "server_id": 1719 },
    "opponents": [ { "id": "ee1b…", "alliance_abbr": "VOXM", "…": "…" }, { "…": "TIWA" } ]
  },
  "records": [
    { "rank": 1, "uid": "…", "player_name": "…", "score": 15570471, "game_rank": 1,
      "battle_score": 14194371, "assist_score": 375100, "strategy_score": 1001000 }
  ]
}
```

| champ | sens |
|---|---|
| `exported_at` | émission du mail = **fin de la bataille**. Réexporter le même mail donne un document **identique octet pour octet**, donc la même `Idempotency-Key` : renvoyer une semaine déjà envoyée est sans danger. |
| `context.battle_date` | jour de la bataille (UTC). La semaine se déduit de là. |
| `context.team` | `"A"` ou `"B"`. **Absent** si inconnu, jamais deviné. |
| `context.team_source` | `"message"` (Canyon : lu dans le titre du mail) ou `"declared"` (Desert, voir plus bas) |
| `context.result` | `"win"` / `"loss"`, du point de vue de ton camp |
| `context.side`, `opponent_side` | score du **camp** (et effectif en Canyon — le « 38/40 » de l'écran) |
| `context.alliance` | ton alliance. `allies` : les autres alliances de ton camp (Canyon) |
| `context.opponents` | **liste** : en Canyon, le camp adverse peut réunir deux alliances. Ce n'est pas le `opponent` singulier du duel VS. |
| `records[].game_rank` | le rang **du jeu**, celui de l'écran — distinct de `rank` (§4) |
| `battle_score`, `assist_score`, `strategy_score` | Canyon seulement : la **ventilation** de `score`, qui en est la somme exacte (vérifié sur 206 lignes). Une métrique décomposée, pas trois métriques. |

**Participation.** Figurer dans `records`, c'est avoir participé à cette
bataille. Le mail **ne porte pas la liste des inscrits** : un membre absent du
document n'a pas joué (ou n'était pas inscrit), sans que l'on puisse trancher.
En Canyon, `records` peut dépasser l'effectif affiché : 44 lignes pour un
« 38/40 », parce qu'il compte aussi ceux qui sont entrés puis sortis.

**`game_rank` a des trous en Canyon, et c'est juste.** Le camp peut réunir ton
alliance et une alliée ; le jeu les classe ensemble, et le document ne garde que
ton alliance. On obtient 5, 6, 8, 9, 10, 13… — exactement l'écran quand la case
« Mon Alliance » est cochée. En Desert, le camp est une seule alliance :
`game_rank` vaut 1..N.

**L'équipe du Desert Storm est déclarée, pas mesurée.** Le mail ne porte aucun
marqueur d'équipe. La règle vient de l'alliance : le créneau du **soir** est
l'équipe **A**, celui du **début d'après-midi** l'équipe **B** (20:30 et 11:30
UTC, soit 22:30 et 13:30 l'été à Paris). Le site doit traiter
`team_source: "declared"` comme une saisie humaine : si l'alliance change de
créneaux, ce champ devient faux sans que rien ne le signale.

**Ce que le site doit en faire :** stocker chaque document comme une bataille
distincte, clé `(mode, battle_date, team)` — ou `mail_uid`, unique. Ne jamais
additionner deux batailles pour faire « la semaine » sans le dire : A et B sont
deux équipes différentes, pas deux manches.

### `alliance_train` — un document par train

Source : les **mails de train** du cache local (`lwvs trains`), comme Storm.
Chaque membre reçoit les mails de **chaque** train de son alliance : un train
quotidien, plus les trains **achetés**. Le but côté site : confronter les
trains **réellement partis** au planning des conducteurs, et compter VIP et
Guardians.

```json
{
  "exported_at": "2026-10-09T23:34:01+00:00",
  "mode": "alliance_train",
  "context": {
    "train_id": "1425382470714832757",
    "train_date": "2026-10-09",
    "mail_uid": "8d8995fc…",
    "arrived": true,
    "departed_at": "2026-10-09T22:08:00+00:00",
    "arrived_at": "2026-10-09T23:34:01+00:00",
    "purchased": false,
    "escort": "guardian",
    "self_select": true,
    "passengers": 28,
    "rob_times": 1, "completeness": 0.9,
    "attacks": 2, "attacks_repelled": 1,
    "alliance": { "id": "6671…", "alliance_abbr": "GolD", "alliance_name": "Golden Fire", "server_id": 1719 }
  },
  "records": [
    { "rank": 1, "uid": "…", "player_name": "…", "role": "conductor", "alliance_abbr": "GolD" },
    { "rank": 2, "uid": "…", "player_name": "…", "role": "guardian",  "alliance_abbr": "GolD" }
  ]
}
```

| champ | sens |
|---|---|
| `records` | le **conducteur** (`role: "conductor"`), puis l'**escorte** : `"vip"` ou `"guardian"`. Un seul record si aucune escorte n'a été désignée. **Pas de `score`** : ce document dit *qui*, pas *combien*. |
| `records[].role` | **absent** si le jeu envoie un type d'escorte inconnu : le joueur reste, son rôle n'est pas deviné. |
| `context.train_id` | identifiant du train, **en texte** (19 chiffres, au-delà de la précision d'un flottant — même règle que `uid`, §3). **C'est la clé.** |
| `context.train_date` | le **jour du jeu** (heure du serveur, UTC−2 : la journée change à 02:00 UTC). Un train parti à 01:14 UTC compte pour la **veille**. C'est la date à confronter au planning. |
| `context.purchased` | `true` : train **acheté**, en plus du train quotidien. Un jour peut donc avoir deux trains ; le train « planifié » est celui à `false`. |
| `context.escort` | `"vip"` ou `"guardian"`, le même que le `role` du record 2. Absent s'il n'y en a pas. |
| `context.arrived` | `false` : le train roulait encore quand la boîte a été lue. Voir plus bas. |
| `context.departed_at`, `arrived_at` | instants UTC. `expected_arrival_at` remplace `arrived_at` tant que `arrived` vaut `false`. |
| `context.passengers` | passagers, conducteur **non** compris. |
| `context.attacks`, `attacks_repelled` | arrivée seulement : attaques subies, et celles que la défense a repoussées. |
| `context.rob_times`, `completeness` | arrivée seulement : pillages réussis (= `attacks − attacks_repelled`), et part de cargaison restante (1,0 − 0,1 par pillage). |
| `context.self_select` | **brut, sens non confirmé.** `false` 3 fois sur 71 trains. Hypothèse : `true` = conducteur désigné par un R4/R5, `false` = tiré au sort par le jeu. Ne pas l'afficher comme un fait avant confirmation. |

**VIP ou Guardian : établi, pas supposé.** Le Guardian défend le train avec sa
propre escouade ; le VIP n'est qu'un bénéficiaire. Le mail porte l'escouade de
défense, dont les héros sont propres à un joueur : sur 43 paires de trains
partageant des héros, les 43 désignent bien le même Guardian (ou le même
conducteur quand l'escorte est un VIP).

**Un même train peut arriver deux fois.** Lu pendant le trajet, il produit un
document `arrived: false` ; relu après l'arrivée, un document complet, avec un
autre `exported_at` — donc une autre `Idempotency-Key` (§5). **Le site
rapproche sur `train_id` et garde le document le plus récent** (`arrived: true`
l'emporte).

**Ce que le site doit en faire :** une ligne par `train_id`. Pour valider le
planning : joindre `(train_date, purchased = false)` au conducteur prévu ce
jour-là, sur `uid` — jamais sur le pseudo. Un jour sans document n'est pas
forcément un jour sans train : la boîte du client expire, et ne contient que ce
qu'elle a reçu.

### `canyon_signup_a` / `canyon_signup_b` — les inscrits d'une équipe

Source : le **fil**, pas un mail. Le message `quarantine.act.player.list` arrive
quand le joueur ouvre l'écran d'inscription du Canyon Storm ; il liste les
membres de **ton** alliance, chacun avec son équipe. Un document par équipe.

```json
{
  "exported_at": "2026-10-13T11:10:14+00:00",
  "mode": "canyon_signup_a",
  "context": { "team": "A", "team_source": "message" },
  "records": [
    { "rank": 1, "uid": "…", "player_name": "…", "role": "starter" },
    { "rank": 2, "uid": "…", "player_name": "…", "role": "substitute" }
  ]
}
```

| champ | sens |
|---|---|
| `records` | les joueurs inscrits dans cette équipe. **Pas de `score`** : ce document dit *qui*, pas *combien*. Le message porte des puissances, mais aucune n'est exportée — on n'exporte pas une métrique qu'on ne sait pas nommer (cf. le faux ami du THP). |
| `records[].rank` | position `1..N` **dans le document**, triée par pseudo. Le jeu ne classe personne ici. |
| `records[].role` | `"starter"` ou `"substitute"`. **Absent** si le jeu envoie une valeur inconnue : le joueur reste, son rôle n'est pas deviné. |
| `context.team` | `"A"` ou `"B"`. Lu sur le message (`group` 1 / 2). La correspondance 1 = A, 2 = B vient d'un effectif **différent** par équipe : 22 et 24 à l'écran, 22 et 24 dans le message. |

**Établi le 13/10/2026** sur une capture de l'écran d'inscription : 98 membres,
22 en A, 24 en B, 52 non inscrits (absents du document).

**Titulaire / remplaçant : très probable, pas confirmé à l'écran.** Chaque
équipe a exactement 20 joueurs au même `state` (le plafond de titulaires), plus
un reste de 2 (A) et 4 (B) à l'autre valeur, sous le plafond de 10 remplaçants.

**Ce que le site doit savoir :**

- **C'est un instantané des inscriptions** à l'ouverture de l'écran, pas la
  composition finale : on peut s'inscrire ou se désinscrire ensuite.
- **Seuls les membres de ton alliance** y figurent. Un allié placé dans la même
  équipe n'apparaît pas dans ce message.
- Deux ouvertures de l'écran à des moments différents donnent deux documents
  (autre `exported_at`, donc autre `Idempotency-Key`, §5) : garder le plus récent
  par `(mode)`.

---

## 2ter. Déclaré par un humain, ou mesuré sur le fil

Certaines informations n'existent nulle part dans le protocole. Les inventer
serait les faire passer pour des mesures, alors elles sont **marquées**.

| champ | origine possible |
|---|---|
| `context.day` | mesurée (`daily_rank`) **ou** déclarée (événements) |
| `context.day_source` | `"message"` ou `"declared"` — toujours présent si `day` l'est |
| `context.event` | **toujours déclaré** — identifiant stable, absent si rien n'a été saisi |
| `context.event_label` | libellé d'affichage du même événement |

```json
"context": {
  "day": 3,
  "day_source": "declared",
  "event": "s3_spice_wars",
  "event_label": "S3 - Spice Wars"
}
```

**`event` est un identifiant, pas du texte libre.** C'est la même discipline que
`uid` / `player_name` : la clé ne bouge pas, l'étiquette peut changer. Sans ça,
« S3 - Spice Wars », « S3 – Spice Wars » (tiret long) et « s3 spice wars »
deviendraient **trois événements distincts** côté site, et personne ne s'en
apercevrait avant que les courbes se coupent en morceaux.

Côté producteur, un catalogue local (`lwvs events`) impose cette clé : un nom
absent du catalogue est **refusé**, pas créé au vol. Le site doit donc :

- **regrouper sur `event`**, jamais sur `event_label` ;
- **accepter un `event` inconnu** et le stocker tel quel — de nouveaux
  événements seront déclarés sans que le site change ;
- afficher `event_label`, et le traiter comme un attribut daté.

**Règle de priorité :** quand le message porte une journée, elle **fait foi** et
`day_source` vaut `"message"`, même si l'utilisateur en a annoncé une autre. Le
désaccord lui est signalé au moment de la capture.

**Ce que le site doit en faire.** Traiter `day_source: "declared"` comme une
saisie humaine : corrigeable, faillible, et à ne pas utiliser comme clé
d'unicité seule. Deux captures de la même journée déclarée ne sont pas
forcément la même mesure — c'est `exported_at` qui les distingue.

---

## 2bis. `context` — qui affronte qui

Chaque document porte un bloc `context` **quand l'information est connue** :

```json
"context": {
  "alliance":  { "id": "90000000…", "alliance_abbr": "ORCA", "alliance_name": "ΘRCA ARMADA", "server_id": 1234, "position": 11 },
  "opponent":  { "id": "91000000…", "alliance_abbr": "VIPR", "alliance_name": "Vipers Nine",        "server_id": 1250, "position": 13 },
  "group_code": "30_3_1",
  "day": 1
}
```

**Comment l'adversaire est obtenu.** Le classement VS transmet **les deux
alliances du match** dans le même message. L'adversaire est donc celle dont
l'identifiant n'est pas le tien — ce n'est pas une supposition, c'est une
soustraction. Ta propre alliance est identifiée par la règle du protocole : la
position du groupe qui égale celle de `duelInfo`.

**Le bloc est omis quand il n'est pas su, jamais rempli de `null`.** Une clé
absente dit « pas mesuré » ; une clé à `null` invite à la traiter comme une
valeur. Concrètement :

- un document `kill_rank` issu d'une capture sans écran VS n'a **pas** de
  `context` — le roster seul ne dit pas qui on affronte ;
- si le classement contient autre chose que deux alliances, `opponent` est
  absent : trois voudrait dire deux duels mélangés, et deviner serait pire que
  se taire.

**Ce que le site doit en faire.**

1. **Ne pas rendre `context` obligatoire.** Un document sans contexte reste
   valide et ses records doivent être appliqués normalement.
2. **Clé d'un duel : `(group_code, alliance.id, opponent.id)`.** Pas les
   abréviations : `KRKN` est devenu `ORCA` en 8 jours, et un tag d'alliance est
   réutilisable par n'importe qui. Les `id` sont stables.
3. **`day`** numérote la journée. Sur `daily_rank` elle vient du jeu ; sur les
   événements elle est déclarée par l'utilisateur. `day_source` tranche —
   voir §2ter.
4. Les libellés (`alliance_abbr`, `alliance_name`) sont des **attributs datés**,
   à historiser comme le pseudo d'un joueur — pas comme une identité.

---

## 3. `uid` est la seule clé, et c'est non négociable

Tout le reste change. Observé sur 8 jours de données réelles :

- des pseudos changent — `Nomade` → `NomadeX`, `Kairo` → `K a i r o` ;
- l'abréviation d'alliance change — `KRKN` → `ORCA` ;
- un joueur change de serveur.

Une jointure sur `player_name` perdrait ces joueurs et **compterait le nouveau
nom comme un nouveau venu**, avec 43 M de kills apparus de nulle part.

Deux règles :

1. **La clé métier est `uid`.** `player_name` est un libellé d'affichage, à
   historiser comme un attribut daté, jamais comme une identité.
2. **`uid` est une chaîne, pas un nombre.** 16 chiffres dépassent la précision
   d'un flottant IEEE 754 : `JSON.parse` en JavaScript corrompt silencieusement
   la valeur. Colonne `TEXT` / `VARCHAR`, pas `BIGINT` si un JS touche la donnée
   au passage.

Les 4 derniers chiffres de `uid` sont le serveur d'origine du joueur — utile
pour repérer les migrations, mais ce n'est pas une clé.

---

## 4. `rank` ne veut pas dire ce qu'on croit

`rank` est la **position dans ce document**, recalculée à l'export, contiguë de
1 à N.

Les documents `weekly_rank` et `daily_rank` sont filtrés sur une seule alliance.
Or le jeu classe **les deux alliances du duel ensemble**. Donc ce `rank` n'est
pas le rang réel dans le duel — le rang réel a des trous.

Si le site veut afficher un rang, il doit choisir : le rang interne à
l'alliance (celui du document) ou le rang du duel (pas transmis ici). Ne
présente pas l'un pour l'autre. En cas de doute, **classe par `score`** et
ignore `rank`.

---

## 5. Idempotence

Une capture peut être rejouée : l'utilisateur relance l'envoi, un fichier est
reposté. Le site **doit** dédupliquer.

L'en-tête HTTP `Idempotency-Key` porte le **sha256 du corps**. Sémantique :

- même clé → même document, déjà appliqué : répondre 200 sans réappliquer ;
- clé différente → mesure différente, à conserver.

Deux captures réelles à des instants différents ont un `exported_at` différent,
donc un corps différent, donc une clé différente : elles comptent toutes les
deux. C'est voulu — deux mesures d'un compteur cumulatif sont deux points de
la série.

Si le site préfère une clé métier plutôt que le hash, la clé naturelle est
`(mode, exported_at, uid)`.

---

## 6. Transport

Deux transports, **un seul format** — le corps HTTP est octet pour octet le
contenu du fichier JSON, c'est vérifié par un test.

**Fichier :**

```bash
lwvs grab --iface "\Device\NPF_{...}" --port 18731 --duration 180 -o exports/
```

Écrit `lwvs_kills.json`, `lwvs_dons_semaine.json`, `lwvs_dons_jour.json`,
`lwvs_roles.json`, `lwvs_thp.json`, `lwvs_vs_total.json`, `lwvs_vs_day_j1.json`,
`lwvs_camp_battle.json`…

Pour un événement, la journée et le nom se déclarent :

```bash
lwvs grab --iface ... --port ... -o exports/   --declare-day 3 --event "Saison 3 - Spice Wars"
```

Desert Storm et Canyon Storm se lisent dans le cache du jeu, sans capture :

```bash
lwvs storm -o exports/                      # les 7 derniers jours
lwvs storm --since 2026-09-14 -o exports/   # depuis une date
lwvs storm --list --all                     # ce que contient le cache, sans pseudo
```

Écrit `lwvs_canyon_storm_2026-09-24_A.json`, `lwvs_desert_storm_2026-09-25_B.json`…

**HTTP :**

```bash
$env:LWVS_TOKEN = "XXX"     # une fois par session PowerShell
lwvs grab --iface ... --port ... --post https://mon-site/api/rankings
lwvs storm --post https://mon-site/api/rankings
```

- `POST` par document (pas de lot), `Content-Type: application/json; charset=utf-8`
- `Authorization: Bearer <token>` si `LWVS_TOKEN` ou `--token`. Préférer la
  variable : tapé en argument, le jeton reste en clair dans l'historique du shell.
- `Idempotency-Key: <sha256 du corps>`
- `User-Agent: lwvs`
- Le site répond 2xx pour accepter. Tout le reste est signalé à l'utilisateur.

**Les fichiers sont écrits avant l'envoi.** Un service injoignable ne fait
jamais perdre une capture : elle est rejouable depuis le disque.

---

## 7. Encodage

UTF-8 partout, sans échappement `\uXXXX`. Les pseudos contiennent des emoji, de
l'arabe, du chinois, et des caractères comme `Θ` (`ΘRCA ARMADA`).

Base de données en `utf8mb4` si MySQL — `utf8` n'y contient pas les emoji et
tronque au premier.

---

## 8. Ordres de grandeur

Pour dimensionner sans se tromper :

- **~100 lignes** par document pour une alliance, ~200 si les deux camps ;
- **7 documents** par capture aujourd'hui, davantage à mesure que des
  statistiques s'ajoutent. Quatre d'entre eux (`kill_rank`, `donation_*`,
  `alliance_role`)
  viennent du **même message** : un seul écran capturé en alimente plusieurs ;
- **~200 lignes** pour `thp_rank` avant filtrage — c'est le classement du
  serveur entier, le plus gros document de la série ;
- **quelques captures par jour** au plus — un duel dure une semaine. Ce n'est
  pas un flux temps réel, c'est une série de photos.

Aucune raison de prévoir du streaming, une file, ou un cache. Un `POST` et une
table suffisent.

---

## 9. Données personnelles

Un document contient les **pseudos et identifiants de ~200 joueurs réels** qui
n'ont pas consenti à figurer dans une base tierce.

À trancher avant la mise en ligne : qui accède au site, combien de temps les
données sont conservées, et ce qui se passe si un joueur demande son retrait.
Ce sont des questions de conception, pas de code — mais un site public exposant
ces données n'est pas la même chose qu'un tableau de bord d'alliance derrière
authentification.

---

## 10. Ce qui n'est pas dans le contrat

Pour éviter que le site les attende :

- **Pas de kills ni de dons adverses.** `armyKill`, `weeklyProgress` et
  `todayProgress` viennent tous du roster de ton alliance (`al.rank`) ; le jeu
  ne transmet pas celui de l'adversaire.
- **Pas de décomposition du THP.** `thp_rank` porte un seul nombre. Le jeu
  transmet bien une ventilation ailleurs (puissance de combat, de bâtiments,
  héros le plus fort), mais dans un message qui n'expose qu'une ligne par
  onglet — inexploitable comme classement, donc non publié.
- **La puissance adverse existe, mais n'est pas envoyée par défaut.** C'est la
  seule exception à la règle « records filtrés sur ton alliance » : le
  classement du serveur couvre tout le monde. Le producteur filtre quand même
  par défaut. Si le site veut comparer joueur par joueur avec l'adversaire,
  c'est une option à activer côté capture — dis-le.
- **Pas d'agrégats.** Ni totaux d'alliance, ni moyennes, ni parts. C'est le
  travail du site, et le faire des deux côtés livrerait deux fois la même
  vérité avec deux arrondis différents.
- **Pas de données de l'alliance adverse au niveau joueur.** Le `context`
  nomme l'adversaire, mais les `records` restent filtrés sur ton alliance.
  Le classement VS contient pourtant les deux camps : si le site veut comparer
  joueur par joueur, c'est un flux à ajouter côté producteur — dis-le.
