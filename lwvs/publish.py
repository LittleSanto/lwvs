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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULT_URL", "PublishResult", "body_of", "check_connection",
           "idempotency_key", "normalize_url", "publish"]

DEFAULT_TIMEOUT = 20.0
# Valeur par defaut de la GUI : l'alliance GolD. Une autre alliance colle l'adresse
# et le jeton que ses R4 lisent dans R4 Tools -> Push feed.
DEFAULT_URL = "https://gold.kwet.eu/api/rankings"
_PATH = "/api/rankings"


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


def normalize_url(raw: str) -> str:
    """Rend utilisable l'adresse telle qu'un R4 la colle : `gold.kwet.eu`,
    `https://gold.kwet.eu/` -> `https://gold.kwet.eu/api/rankings`.

    Une adresse qui porte deja un chemin est laissee telle quelle.
    """
    url = raw.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.path in ("", "/"):
        url = urllib.parse.urlunsplit(parts._replace(path=_PATH))
    return url


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
    return _post(body_of(payload), url, token, timeout, extra_headers)


def _post(body: bytes, url: str, token: str | None, timeout: float,
          extra_headers: dict[str, str] | None = None) -> PublishResult:
    url = normalize_url(url)
    key = idempotency_key(body)
    if not url.lower().startswith(("http://", "https://")):
        # urlopen lirait aussi file:// ; on ne poste que vers du HTTP(S).
        return PublishResult(url=url, status=0, key=key, bytes_sent=len(body),
                             error="the address must start with https://")
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


def check_connection(url: str, token: str | None,
                     timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Verifie l'adresse et le jeton SANS publier le moindre classement.

    Poste un corps VIDE. Le service controle le jeton avant de lire le corps :
    401 jeton faux, 503 envoi pas encore active, puis 422 « corps vide » pour un
    jeton accepte. Ce 422 est donc le « tout va bien » : rien n'est enregistre.
    """
    if not url.strip():
        return False, "Enter the site address first."
    if not token:
        return False, "Paste the token first (R4 Tools → Push feed on the site)."
    res = _post(b"", url, token, timeout)
    if res.ok or res.status == 422:
        return True, f"Connected: {res.url} accepts this token. Nothing was sent."
    if res.status == 401:
        return False, ("Token refused (401). Generate it again in R4 Tools → "
                       "Push feed and paste the new one.")
    if res.status == 503:
        return False, ("This site has no push token yet (503). An R4 must "
                       "generate one in R4 Tools → Push feed.")
    if res.status == 404:
        return False, (f"Nothing at {res.url} (404). The address looks like "
                       "https://<your-alliance>.kwet.eu/api/rankings.")
    if res.status == 0:
        return False, f"Cannot reach {res.url}: {res.error}"
    return False, f"Unexpected answer, HTTP {res.status}: {res.response.strip()[:120]}"
