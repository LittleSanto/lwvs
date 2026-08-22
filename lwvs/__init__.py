"""lwvs -- decodeur du classement Alliance Duel (VS) de Last War: Survival.

Le trafic n'est pas chiffre : il n'y a rien a dechiffrer, seulement a
decompresser (zstd) et a deserialiser.

Architecture (regles transverses) :

* `wire`     -- SEUL module a connaitre les octets : framing, serialisation,
                encodeur. Une mise a jour du jeu casse ce fichier et rien d'autre.
* `capture`  -- tshark, reassemblage par flux et par sens, decouverte de port.
* `pipeline` -- passe de decodage partagee + garde-fou "exact".
* `messages` -- enveloppe et messages VS, sur objets deja decodes.
* `store`    -- SEUL module a parler a la base (SQLite, accumulation).
* `inspection` / `probe` -- cartographie du protocole et sondage des octets
                de type inconnus.
* `exporter` -- JSON / CSV plats.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
