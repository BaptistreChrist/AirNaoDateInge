"""
Client HTTP pour l'API data.airpl.org.
Gère la pagination automatique et le filtrage par département/polluant.
"""

import logging
import time
import urllib.request
import json
import ssl
from datetime import date
from typing import Iterator

from config import API_BASE, API_PAGE_SIZE, COMMUNE_CODE, GRANULARITY_API, POLLUTANTS

logger = logging.getLogger(__name__)

# Bypass SSL si le certificat local n'est pas à jour
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "AirNaoned/1.0"})
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as r:
        return json.load(r)


def fetch_measures(
    granularity: str,
    date_start: date,
    date_end: date,
    pollutant_notation: str | None = None,
) -> Iterator[dict]:
    """
    Itère sur toutes les mesures pour une granularité et une plage de dates.
    Filtre sur le département 44 et optionnellement sur un polluant.
    """
    api_gran = GRANULARITY_API[granularity]
    code_pol = POLLUTANTS.get(pollutant_notation) if pollutant_notation else None

    base_url = (
        f"{API_BASE}mesure/{api_gran}/?format=json"
        f"&code_commune={COMMUNE_CODE}"
        f"&date_heure_tu__range={date_start},{date_end}"
        f"&limit={API_PAGE_SIZE}"
    )
    if code_pol:
        base_url += f"&code_polluant={code_pol}"

    url = base_url + "&offset=0"
    fetched = 0

    while url:
        try:
            data = _get(url)
        except Exception as exc:
            logger.error("Erreur API %s : %s", url, exc)
            raise

        results = data.get("results", [])
        yield from results
        fetched += len(results)

        # L'API retourne toujours un `next` même sur page vide (bug) → on s'arrête
        # dès que la page est incomplète ou vide.
        if len(results) < API_PAGE_SIZE:
            url = None
        else:
            url = data.get("next")

        if url:
            time.sleep(0.1)  # politesse envers l'API

    logger.info("fetch_measures(%s, %s→%s, %s) : %d enregistrements", granularity, date_start, date_end, pollutant_notation, fetched)


def fetch_all_pollutants(granularity: str, date_start: date, date_end: date) -> Iterator[dict]:
    """Itère sur les 5 polluants cibles pour une granularité et une plage de dates."""
    for notation in POLLUTANTS:
        yield from fetch_measures(granularity, date_start, date_end, notation)
