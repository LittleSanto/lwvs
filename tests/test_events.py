"""Catalogue des evenements : une cle stable contre un libelle libre."""

from __future__ import annotations

import pytest

from lwvs import events, exporter


def test_le_catalogue_est_amorce_avec_spice_wars():
    catalogue = events.load()
    assert [e.id for e in catalogue] == ["s3_spice_wars"]
    assert catalogue[0].label == "S3 - Spice Wars"


def test_ajouter_puis_retirer():
    cree = events.add("S4 - Autre Event")
    assert cree.id == "s4_autre_event"
    assert {e.id for e in events.load()} == {"s3_spice_wars", "s4_autre_event"}

    assert events.remove("s4_autre_event") is True
    assert events.remove("s4_autre_event") is False
    assert {e.id for e in events.load()} == {"s3_spice_wars"}


def test_un_meme_libelle_ne_cree_pas_de_doublon():
    events.add("S3 - Spice Wars")
    assert len(events.load()) == 1


@pytest.mark.parametrize("saisie", [
    "s3_spice_wars",        # l'identifiant
    "S3 - Spice Wars",      # le libelle exact
    "s3 - spice wars",      # la casse
    "S3 – Spice Wars",      # tiret LONG : le piege qui fragmente en aval
    "S3  -  Spice   Wars",  # espaces multiples
])
def test_la_resolution_absorbe_les_variantes_de_ponctuation(saisie):
    """Sans ca, chaque variante deviendrait un evenement distinct cote site."""
    resolu = events.resolve(saisie)
    assert resolu is not None
    assert resolu.id == "s3_spice_wars"


def test_un_evenement_inconnu_est_refuse_pas_fabrique():
    """Fabriquer l'evenement au vol ferait revenir par la fenetre la faute de
    frappe qu'on voulait eviter."""
    assert events.resolve("S9 - Jamais Declare") is None

    with pytest.raises(ValueError, match="evenement inconnu"):
        exporter.build_records([], "members", event="S9 - Jamais Declare")


def test_le_document_porte_l_identifiant_ET_le_libelle():
    doc = exporter.build_records([], "members", event="S3 – Spice Wars")
    assert doc["context"]["event"] == "s3_spice_wars"
    assert doc["context"]["event_label"] == "S3 - Spice Wars"


def test_le_catalogue_survit_a_un_fichier_illisible(isole_l_identite):
    events.path().write_text("{ pas du json", encoding="utf-8")
    assert [e.id for e in events.load()] == ["s3_spice_wars"]
