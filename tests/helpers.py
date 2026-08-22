"""Fixtures de test.

Elles passent par le VRAI encodeur (`wire.encode_value` / `wire.encode_frame`)
et par la VRAIE enveloppe. Un test qui passerait un corps nu au parseur
passerait pendant que ce qui arrive reellement sur le fil echoue.
"""

from __future__ import annotations

import struct
from typing import Any, Iterable, Iterator, Sequence

from lwvs import wire
from lwvs.capture import CaptureSource, Payload
from lwvs.wire import i8, i16, i32, i64


def raw_map(pairs: Sequence[tuple[str, bytes]]) -> bytes:
    """Map dont les VALEURS sont deja encodees en octets.

    Sert uniquement a fabriquer des octets de type volontairement inconnus,
    que l'encodeur refuse (a raison) d'emettre.
    """
    out = bytearray([wire.TAG_MAP])
    out += struct.pack(">H", len(pairs))
    for key, value in pairs:
        kb = key.encode("utf-8")
        out += struct.pack(">H", len(kb))
        out += kb
        out += value
    return bytes(out)


def envelope(command: str, body: Any, a: int = 1, c: int = 2) -> bytes:
    """{p: {p: <corps>, c: "<commande>"}, a: int16, c: int8} -- CONFIRME."""
    return wire.encode_value(
        {"p": {"p": body, "c": command}, "a": i16(a), "c": i8(c)}
    )


def envelope_raw(command: str, body_bytes: bytes, a: int = 1, c: int = 2) -> bytes:
    inner = raw_map([("p", body_bytes), ("c", wire.encode_value(command))])
    return raw_map(
        [("p", inner), ("a", wire.encode_value(i16(a))), ("c", wire.encode_value(i8(c)))]
    )


# ---------------------------------------------------------------------------
# messages VS synthetiques
# ---------------------------------------------------------------------------

_NAMES = ["Ω Ravager", "أحمد", "小龍", "Zoé🔥", "plain"]


def rank_entry(i: int, abbr: str, score: int) -> dict[str, Any]:
    return {
        "uid": i64(1000_0000_0000_0000 + i * 7 + int(abbr[0] == "T") * 3),
        "name": _NAMES[i % len(_NAMES)] + str(i),
        "score": i32(score),
        "serverId": i32(1200 + (i % 3)),
        "aid": i64(9000_0000_0000_0000 + (1 if abbr == "TST" else 2)),
        "alName": f"Alliance {abbr}",
        "abbr": abbr,
    }


def rank_body(n: int = 6, day: int | None = 3, raw_type: int | None = None) -> dict[str, Any]:
    """`day` present => classement du jour ; absent => cumul."""
    rows = []
    for i in range(n):
        abbr = "TST" if i % 2 == 0 else "OPP"
        rows.append(rank_entry(i, abbr, 100000 - i * 137))
    body: dict[str, Any] = {"rankInfo": rows}
    if day is not None:
        body["day"] = i8(day)
        body["type"] = i8(0 if raw_type is None else raw_type)
    else:
        body["type"] = i8(1 if raw_type is None else raw_type)
    return body


def group_body(n: int = 16) -> dict[str, Any]:
    return {
        "groupInfos": [
            {
                "position": i8(p + 1),
                "allianceId": i64(9000_0000_0000_0000 + p),
                "name": f"Alliance {p:02d}",
                "abbr": f"A{p:02d}",
                "serverId": i32(1200 + p),
                "roundResult": i8(p % 3),
                "rankType": i8(1),
                "group": "G1",
            }
            for p in range(n)
        ]
    }


def season_body(position: int = 3) -> dict[str, Any]:
    return {
        "duelInfo": {
            "group": "G1",
            "position": i8(position),
            "rankType": i8(1),
            "roundResult": i8(2),
        },
        "lastDuelInfo": {
            "group": "G0",
            "position": i8(position + 1),
            "rankType": i8(2),
            "roundResult": i8(1),
        },
    }


# ---------------------------------------------------------------------------
# source de test
# ---------------------------------------------------------------------------


class ListSource(CaptureSource):
    """Source alimentee par des payloads deja decadres."""

    def __init__(self, payloads: Iterable[bytes], name: str = "test") -> None:
        self._data = list(payloads)
        self.description = f"list {name}"

    def payloads(self) -> Iterator[Payload]:
        for i, data in enumerate(self._data):
            yield Payload(data=data, stream="t/0", seq=i, flag=wire.FLAG_RAW,
                          compressed=False)


class WireSource(CaptureSource):
    """Source qui repasse par la couche trame : octets bruts -> payloads.

    C'est celle qui compte : elle exerce framing + racine + enveloppe.
    """

    def __init__(self, stream_bytes: bytes, name: str = "test") -> None:
        self._bytes = stream_bytes
        self.description = f"wire {name}"
        self._reader = wire.FrameReader(name)

    def payloads(self) -> Iterator[Payload]:
        frames = self._reader.feed(self._bytes) + self._reader.flush()
        for frame in frames:
            yield Payload(
                data=frame.payload, stream="t/0", seq=frame.seq,
                flag=frame.flag, compressed=frame.compressed,
            )

    @property
    def stats(self):
        return self._reader.stats


def server_rank_body(n: int = 8, raw_type: int = 13,
                     self_ranking: int = 2) -> dict[str, Any]:
    """`rank.get` : le classement du serveur, seul porteur du THP.

    Deux traits repris de la capture reelle, parce que chacun casse quelque
    chose s'il est ignore :

    * la liste arrive DEJA TRIEE par `heroPower` decroissant et aucune ligne ne
      porte de rang -- il est positionnel, comme au classement VS ;
    * certains joueurs n'ont PAS d'alliance (5 sur 200 en capture reelle). Ils
      n'ont donc pas d'`allianceId`, et le filtre par alliance doit les ecarter
      sans broncher plutot que de trebucher sur la cle absente.

    `heroPower` est volontairement d'un autre ordre de grandeur que le `power`
    de `members_body` : ce sont deux mesures differentes, et un test qui les
    confondrait passerait sur des valeurs trop proches.
    """
    rows = []
    for i in range(n):
        entry: dict[str, Any] = {
            "uid": i64(1000_0000_0000_0000 + i * 7),
            "name": _NAMES[i % len(_NAMES)] + str(i),
            "heroPower": i64(160_000_000 - i * 1_000_000),
            "country": "FR",
            "srcServer": i32(1234),
            "picVer": i32(1),
        }
        # Un joueur sur quatre est sans alliance : ni id, ni abbr, ni nom.
        if i % 4 != 3:
            abbr = "TST" if i % 2 == 0 else "OPP"
            entry["allianceId"] = i64(9000_0000_0000_0000 + (1 if abbr == "TST" else 2))
            entry["abbr"] = abbr
            entry["alliancename"] = f"Alliance {abbr}"
            entry["level"] = i8(30 + (i % 5))
        rows.append(entry)
    return {
        "serverRanking": rows,
        "serverId": i32(1234),
        "type": i8(raw_type),
        "selfRanking": i8(self_ranking),
        "global": i8(0),
    }


def members_body(n: int = 8, alliance_id: str = "aid-mine",
                 base_kill: int = 100_000) -> dict[str, Any]:
    """`al.rank` : le roster de TON alliance, seul porteur de armyKill.

    Les points de don (`weeklyProgress`, `todayProgress`) croissent avec `i`
    alors que `armyKill` decroit : l'ordre des dons est donc l'INVERSE de celui
    des kills. Un flux qui se tromperait de colonne rendrait le mauvais ordre au
    lieu de rendre le meme classement deux fois.
    """
    return {
        "allianceId": alliance_id,
        "list": [
            {
                "uid": i64(1000_0000_0000_0000 + i * 7),
                "name": _NAMES[i % len(_NAMES)] + str(i),
                "armyKill": i32(base_kill - i * 1337),
                "power": i64(50_000_000 - i * 100_000),
                "rank": i8(5 - (i % 5)),
                "todayProgress": i32(i * 11),
                "weeklyProgress": i32(i * 77),
                "mainCityLv": i8(30 + (i % 5)),
                "serverId": i32(1234),
                "curServerId": i32(1234),
                "online": i % 3 == 0,
                "joinTime": i64(1_700_000_000 + i * 1000),
                "donateTime": i64(1_786_000_000_000 + i * 1000),
                # 0 = n'a pas donne cette semaine, comme le joueur 0 qui est
                # aussi le seul a weeklyProgress == 0.
                "weeklyDonateTime": i64(0 if i == 0 else 1_786_300_000_000 + i),
            }
            for i in range(n)
        ],
    }
