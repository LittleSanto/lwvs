"""Couche octets : framing, serialisation, encodeur.

Les deux regles CONTRE-INTUITIVES ont leur test explicite : ce sont elles qui
reviendront (`7 + len` et la racine la plus gloutonne).
"""

from __future__ import annotations

import struct

import pytest
import zstandard

from lwvs import wire
from lwvs.wire import i8, i16, i32, i64, raw_bytes

from .helpers import envelope, raw_map


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def test_roundtrip_tous_les_tags():
    value = {
        "b": True,
        "i8": i8(-12),
        "i16": i16(-3000),
        "i32": i32(123456),
        "i64": i64(1234567890123456),
        "s": "Ω أحمد 小龍 🔥",
        "by": raw_bytes(b"\x00\x01\xff" * 10),
        "arr": [i8(1), "deux", {"k": i16(3)}],
        "map": {"nested": {"deep": i32(7)}},
    }
    data = wire.encode_value(value)
    got, end = wire.decode_value_at(data, 0)
    assert end == len(data)
    assert got == {
        "b": True,
        "i8": -12, "i16": -3000, "i32": 123456, "i64": 1234567890123456,
        "s": "Ω أحمد 小龍 🔥",
        "by": b"\x00\x01\xff" * 10,
        "arr": [1, "deux", {"k": 3}],
        "map": {"nested": {"deep": 7}},
    }


def test_bytes_utilise_un_u32_la_ou_string_utilise_un_u16():
    """0x0A -> u32, 0x08 -> u16. La difference est la source de tous les decalages."""
    s = wire.encode_value("ab")
    assert s[0] == wire.TAG_STRING
    assert s[1:3] == struct.pack(">H", 2)

    b = wire.encode_value(raw_bytes(b"ab"))
    assert b[0] == wire.TAG_BYTES
    assert b[1:5] == struct.pack(">I", 2)


def test_cles_de_map_sans_octet_de_type():
    data = wire.encode_value({"ab": i8(1)})
    # 0x12 | u16 count | u16 keylen | 'a' 'b' | 0x02 0x01
    assert data == bytes([wire.TAG_MAP]) + struct.pack(">H", 1) + \
        struct.pack(">H", 2) + b"ab" + bytes([wire.TAG_INT8, 0x01])


def test_proto_reste_en_bytes_bruts():
    """Le porteur observe de 0x0A est une cle `_proto` de protobuf embarque.

    Le decoder comme du texte le massacrerait : on le garde opaque.
    """
    blob = bytes(range(256)) * 3
    data = wire.encode_value({"_proto": raw_bytes(blob)})
    got, _ = wire.decode_value_at(data, 0)
    assert isinstance(got["_proto"], bytes)
    assert got["_proto"] == blob


@pytest.mark.parametrize("payload,expected", [
    ("héllo".encode("utf-8"), "héllo"),
    ("héllo".encode("cp1252"), "héllo"),
    (b"\x81\x8d\x90", None),  # ni utf-8 ni cp1252 : latin-1 rattrape
])
def test_decodage_string_utf8_puis_cp1252_puis_latin1(payload, expected):
    data = bytes([wire.TAG_STRING]) + struct.pack(">H", len(payload)) + payload
    got, end = wire.decode_value_at(data, 0)
    assert end == len(data)
    if expected is not None:
        assert got == expected
    else:
        assert got == payload.decode("latin-1")


def test_octet_de_type_inconnu_arrete_net_avec_offset_et_hex():
    """Une largeur devinee corrompt silencieusement tout ce qui suit.

    Un arret net avec offset + hexa est strictement preferable.
    """
    data = raw_map([("x", b"\x06\xde\xad")])
    with pytest.raises(wire.UnknownTypeByte) as exc:
        wire.decode_value_at(data, 0)
    err = exc.value
    assert err.tag == 0x06
    assert err.offset == data.index(b"\x06\xde\xad")
    assert "06" in err.hex
    assert "0x06" in str(err)


@pytest.mark.parametrize("tag", sorted(wire.UNKNOWN_TAGS))
def test_les_quatre_inconnus_ne_sont_pas_devines(tag):
    data = raw_map([("x", bytes([tag]) + b"\x00" * 8)])
    with pytest.raises(wire.UnknownTypeByte) as exc:
        wire.decode_value_at(data, 0)
    assert exc.value.tag == tag


def test_valeur_tronquee():
    data = bytes([wire.TAG_STRING]) + struct.pack(">H", 99) + b"ab"
    with pytest.raises(wire.TruncatedValue):
        wire.decode_value_at(data, 0)


# ---------------------------------------------------------------------------
# framing -- LA regle contre-intuitive n°1
# ---------------------------------------------------------------------------


def test_trame_compressee_avance_de_7_plus_len_pas_3_plus_len():
    """PIEGE PRINCIPAL, teste explicitement.

    Le `len` d'une trame compressee ne couvre QUE la trame zstd, pas les 4
    octets de taille decompressee. Avancer de 3 + len decode parfaitement la
    premiere trame et transforme tout le reste en bouillie.
    """
    p1 = wire.encode_value({"one": "x" * 400})
    p2 = wire.encode_value({"two": "y" * 400})
    f1 = wire.encode_frame(p1, compress=True)
    f2 = wire.encode_frame(p2, compress=True)

    declared_len = int.from_bytes(f1[1:3], "big")
    # La preuve du piege : 3 + len tombe 4 octets AVANT la vraie frontiere.
    assert 3 + declared_len == len(f1) - 4
    assert 7 + declared_len == len(f1)

    frames = list(wire.iter_frames(f1 + f2))
    assert [f.payload for f in frames] == [p1, p2]
    assert all(f.compressed for f in frames)


def test_magie_zstd_et_non_le_flag_est_le_discriminant():
    payload = wire.encode_value({"k": "v" * 300})
    frame = bytearray(wire.encode_frame(payload, compress=True))
    assert frame[7:11] == wire.ZSTD_MAGIC
    # Flag inconnu : la magie doit suffire a trancher.
    frame[0] = 0x42
    frames = list(wire.iter_frames(bytes(frame)))
    assert len(frames) == 1
    assert frames[0].compressed is True
    assert frames[0].payload == payload


def test_frame_brute():
    payload = wire.encode_value({"k": i8(1)})
    frame = wire.encode_frame(payload, compress=False)
    assert frame[0] == wire.FLAG_RAW
    assert int.from_bytes(frame[1:3], "big") == len(payload)
    frames = list(wire.iter_frames(frame))
    assert [f.payload for f in frames] == [payload]
    assert frames[0].compressed is False


def test_zstd_sans_taille_de_contenu_dans_l_entete():
    """decompressobj() et non decompress() : ce dernier exige une taille de
    contenu dans l'en-tete zstd, qui n'est pas garantie ici."""
    payload = wire.encode_value({"k": "z" * 500})
    cobj = zstandard.ZstdCompressor().compressobj()
    body = cobj.compress(payload) + cobj.flush()
    # decompress() sans taille de contenu leve : c'est bien le cas teste.
    with pytest.raises(zstandard.ZstdError):
        zstandard.ZstdDecompressor().decompress(body)
    frame = struct.pack(">BHI", wire.FLAG_ZSTD, len(body), len(payload)) + body
    frames = list(wire.iter_frames(frame))
    assert [f.payload for f in frames] == [payload]


def test_reassemblage_a_travers_les_segments_tcp():
    """Jamais paquet par paquet : les messages traversent plusieurs segments."""
    payloads = [wire.encode_value({"i": i16(i), "pad": "p" * 300}) for i in range(5)]
    stream = b"".join(wire.encode_frame(p, compress=(i % 2 == 0))
                      for i, p in enumerate(payloads))

    reader = wire.FrameReader("split")
    got = []
    for i in range(0, len(stream), 7):   # segments minuscules et desalignes
        got += reader.feed(stream[i:i + 7])
    got += reader.flush()
    assert [f.payload for f in got] == payloads


def test_les_deux_sens_restent_separes():
    """Fusionner les deux sens entrelace deux sequences et desynchronise tout."""
    up = [wire.encode_value({"up": i16(i)}) for i in range(3)]
    down = [wire.encode_value({"down": i16(i), "pad": "d" * 200}) for i in range(3)]
    s_up = b"".join(wire.encode_frame(p) for p in up)
    s_down = b"".join(wire.encode_frame(p, compress=True) for p in down)

    r_up, r_down = wire.FrameReader("up"), wire.FrameReader("down")
    # Arrivee entrelacee, comme sur le fil.
    out_up, out_down = [], []
    for i in range(0, max(len(s_up), len(s_down)), 11):
        out_up += r_up.feed(s_up[i:i + 11])
        out_down += r_down.feed(s_down[i:i + 11])
    out_up += r_up.flush()
    out_down += r_down.flush()
    assert [f.payload for f in out_up] == up
    assert [f.payload for f in out_down] == down

    # Le meme flux fusionne dans un seul tampon ne rend PAS les memes trames.
    merged = wire.FrameReader("merged")
    fused = merged.feed(s_up + s_down[:5]) + merged.flush()
    assert [f.payload for f in fused] != up + down


def test_resynchronisation_apres_octets_parasites():
    payloads = [wire.encode_value({"k": i16(i), "pad": "q" * 120}) for i in range(3)]
    good = b"".join(wire.encode_frame(p) for p in payloads)
    stream = b"\x00\x00\x00\x00\x00\x00" + good

    reader = wire.FrameReader("resync")
    frames = reader.feed(stream) + reader.flush()
    assert [f.payload for f in frames] == payloads
    assert reader.stats.resyncs >= 1


def test_taille_decompressee_incoherente_declenche_une_resync():
    """C'est la signature exacte d'un decalage de 4 octets."""
    payload = wire.encode_value({"k": "z" * 400})
    frame = bytearray(wire.encode_frame(payload, compress=True))
    frame[3:7] = (len(payload) + 1).to_bytes(4, "big")
    reader = wire.FrameReader("bad")
    frames = reader.feed(bytes(frame)) + reader.flush()
    assert frames == []
    assert reader.stats.size_mismatches == 1


# ---------------------------------------------------------------------------
# racine -- LA regle contre-intuitive n°2
# ---------------------------------------------------------------------------


def test_racine_la_plus_gloutonne_et_non_le_premier_offset_qui_parse():
    """Un en-tete applicatif decode souvent comme un objet valide lui aussi.

    "Le premier offset qui parse" attrape l'en-tete et rend un objet plausible
    avec deux champs bidons. Le bon critere est le nombre d'octets consommes.
    """
    header = wire.encode_value({"hd": i8(7)})     # un vrai objet, mais pas la racine
    assert header[0] == wire.TAG_MAP
    body = envelope("al.battle.rank.info", {"rankInfo": [], "type": i8(1)})
    payload = header + body

    # "Premier offset qui parse" : offset 0, deux champs bidons.
    first, first_end = wire.decode_value_at(payload, 0)
    assert first == {"hd": 7}
    assert first_end == len(header)

    root = wire.decode_payload(payload)
    assert root.root_offset == len(header)
    assert root.exact is True
    assert root.value["p"]["c"] == "al.battle.rank.info"


def test_exact_est_le_garde_fou():
    payload = envelope("x.y", {"a": i8(1)})
    assert wire.decode_payload(payload).exact is True
    assert wire.decode_payload(payload + b"\x99\x99").exact is False
    assert wire.decode_payload(payload + b"\x99\x99").trailing == 2


def test_payload_sans_racine():
    with pytest.raises(wire.RootDecodeError) as exc:
        wire.decode_payload(b"\x01\x02\x03\x04")
    assert exc.value.total == 4


def test_root_decode_error_expose_l_octet_fautif():
    payload = envelope_with_unknown()
    with pytest.raises(wire.RootDecodeError) as exc:
        wire.decode_payload(payload)
    assert exc.value.unknown_tags == {0x0C}
    assert exc.value.blocking_tag == 0x0C


def envelope_with_unknown() -> bytes:
    inner = raw_map([("p", raw_map([("x", b"\x0c\x01\x02")])),
                     ("c", wire.encode_value("some.command"))])
    return raw_map([("p", inner),
                    ("a", wire.encode_value(i16(1))),
                    ("c", wire.encode_value(i8(2)))])


def test_offsets_racine_restreints():
    payload = wire.encode_value({"a": i8(1)}) + envelope("x.y", {"b": i8(2)})
    anchored = wire.decode_payload(payload, offsets=[0])
    assert anchored.root_offset == 0
    assert anchored.exact is False


# ---------------------------------------------------------------------------
# layouts hypothetiques (utilises seulement par le sondeur)
# ---------------------------------------------------------------------------


def test_layout_hypothetique_ne_touche_pas_la_table_de_production():
    data = raw_map([("x", b"\x06\xde\xad")])
    with pytest.raises(wire.UnknownTypeByte):
        wire.decode_value_at(data, 0)          # table de production : refus

    table = wire.TypeTable(extra={0x06: wire.Fixed(2)})
    got, end = wire.decode_value_at(data, 0, table)
    assert end == len(data)
    assert got["x"].tag == 0x06
    assert got["x"].body == b"\xde\xad"

    # La table de production est restee vide : rien ne s'ecrit tout seul.
    assert wire.DEFAULT_TABLE.extra == {}


# ---------------------------------------------------------------------------
# 0x07 : double IEEE 754 big-endian
# ---------------------------------------------------------------------------


def test_0x07_est_un_double_big_endian():
    """ETABLI le 20/08/2026. La preuve qui tranche est la LECTURE : les memes
    8 octets valent un score plausible en double, et 4.7e18 en int64.

    Les octets sont ceux de la capture reelle, sur une cle `stageScore`.
    """
    brut = bytes.fromhex("41 3e 95 9c 22 e0 16 51".replace(" ", ""))
    data = raw_map([("stageScore", b"\x07" + brut)])
    got, end = wire.decode_value_at(data, 0)
    assert end == len(data), "un double consomme exactement 8 octets"
    assert got["stageScore"] == pytest.approx(2_004_380.1362, rel=1e-9)
    # La lecture concurrente, celle qu'on ecarte.
    assert struct.unpack(">q", brut)[0] == 4_701_359_558_853_924_433


def test_0x07_fait_l_aller_retour_par_l_encodeur():
    """L'encodeur vit a cote du decodeur pour que les tests fabriquent de VRAIES
    trames. Un tag decodable mais non encodable ne serait testable que sur des
    octets ecrits a la main."""
    payload = wire.encode_value({"stageScore": 2_004_380.1362, "n": i32(7)})
    got, end = wire.decode_value_at(payload, 0)
    assert end == len(payload)
    assert got["stageScore"] == pytest.approx(2_004_380.1362)
    assert got["n"] == 7


def test_0x07_tronque_s_arrete_net_au_lieu_de_deviner():
    """Moins de 8 octets : on refuse, on ne complete pas."""
    with pytest.raises(wire.TruncatedValue):
        wire.decode_value_at(raw_map([("x", b"\x07\x01\x02\x03")]), 0)
