"""Envoi des classements vers un service HTTP.

Volontairement minimal, et volontairement dependant de RIEN : `urllib` de la
stdlib. Ce module ne connait pas le protocole du jeu, il poste un document JSON
deja fabrique par `exporter.build_records`.

Le contrat est documente dans `docs/CONTRAT.md` : le service recoit EXACTEMENT
le document que `--format records` ecrit dans un fichier. Un seul format, deux
transports.

Idempotence : l'en-tete `Idempotency-Key` porte le sha256 du corps. Deux envois
du meme document -- un rejeu, un double-clic -- portent la meme cle et le
service doit les traiter comme un seul. Deux captures distinctes ont un
`exported_at` different, donc un corps different, donc une cle differente : ce
sont bien deux mesures et elles doivent toutes les deux compter.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = ["PublishResult", "body_of", "idempotency_key", "publish"]

DEFAULT_TIMEOUT = 20.0


def body_of(payload: dict[str, Any]) -> bytes:
    """Corps canonique : c'est lui qui est hashe ET envoye, jamais deux variantes."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def idempotency_key(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class PublishResult:
    url: str
    status: int
    key: str
    bytes_sent: int
    response: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def publish(
    payload: dict[str, Any],
    url: str,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> PublishResult:
    """Poste un document de classement. Ne leve pas : rend un resultat lisible.

    Un echec reseau ne doit pas faire perdre la capture : l'appelant a deja les
    JSON sur disque et peut rejouer l'envoi.
    """
    body = body_of(payload)
    key = idempotency_key(body)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": key,
        "User-Agent": "lwvs",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            text = resp.read(2048).decode("utf-8", "replace")
            return PublishResult(url=url, status=resp.status, key=key,
                                 bytes_sent=len(body), response=text)
    except urllib.error.HTTPError as exc:
        text = exc.read(2048).decode("utf-8", "replace") if exc.fp else ""
        return PublishResult(url=url, status=exc.code, key=key,
                             bytes_sent=len(body), response=text,
                             error=f"HTTP {exc.code}")
    except Exception as exc:   # DNS, refus, timeout, TLS...
        return PublishResult(url=url, status=0, key=key, bytes_sent=len(body),
                             error=f"{type(exc).__name__}: {exc}")
