"""
Logique d'ingestion partagée + entry points Cloud Functions.

Cloud Scheduler appelle chaque fonction via HTTP trigger :
  - ingest_hourly   : toutes les heures   (0 * * * *)
  - ingest_daily    : tous les jours       (0 6 * * *)
  - ingest_monthly  : le 1er du mois       (0 6 1 * *)
"""

import logging
from datetime import datetime, timedelta, timezone, date
from typing import Iterable

import functions_framework
from google.cloud import bigquery

from config import GCP_PROJECT, BQ_DATASET, TABLES, POLLUTANTS
from airpl_client import fetch_all_pollutants
from iqa import _sub_index

logger = logging.getLogger(__name__)
bq = bigquery.Client(project=GCP_PROJECT)

CODE_TO_NOTATION = {v: k for k, v in POLLUTANTS.items()}


def _row(record: dict) -> dict:
    notation = CODE_TO_NOTATION.get(record.get("code_polluant"), "UNKNOWN")
    valeur = record.get("valeur")
    return {
        "id":               record["id"],
        "code_polluant":    record.get("code_polluant"),
        "notation_polluant": notation,
        "code_station":     record.get("code_station"),
        "nom_station":      record.get("nom_station"),
        "nom_commune":      record.get("nom_commune"),
        "code_commune":     record.get("code_commune"),
        "departement_code": record.get("departement_code"),
        "date_heure_tu":    record.get("date_heure_tu"),
        "valeur":           valeur,
        "validite":         record.get("validite"),
        "iqa_sous_indice":  _sub_index(notation, valeur) if valeur is not None else None,
    }


def _insert(table_key: str, rows: Iterable[dict]) -> int:
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{TABLES[table_key]}"
    batch, total = [], 0
    for row in rows:
        batch.append(_row(row))
        if len(batch) >= 500:
            errors = bq.insert_rows_json(table_id, batch)
            if errors:
                logger.error("BQ insert errors: %s", errors)
            total += len(batch)
            batch = []
    if batch:
        errors = bq.insert_rows_json(table_id, batch)
        if errors:
            logger.error("BQ insert errors: %s", errors)
        total += len(batch)
    return total


# ── Cloud Function entry points ──────────────────────────────────────────────

@functions_framework.http
def ingest_hourly(request):
    now = datetime.now(timezone.utc)
    # Fenêtre glissante : 2 dernières heures pour absorber les retards de publication
    date_end = now.date()
    date_start = (now - timedelta(hours=2)).date()
    rows = fetch_all_pollutants("hourly", date_start, date_end)
    count = _insert("hourly", rows)
    msg = f"hourly: {count} lignes insérées ({date_start} → {date_end})"
    logger.info(msg)
    return msg, 200


@functions_framework.http
def ingest_daily(request):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    rows = fetch_all_pollutants("daily", yesterday, yesterday)
    count = _insert("daily", rows)
    msg = f"daily: {count} lignes insérées ({yesterday})"
    logger.info(msg)
    return msg, 200


@functions_framework.http
def ingest_monthly(request):
    today = datetime.now(timezone.utc).date()
    # Premier jour du mois précédent → premier jour du mois courant
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    rows = fetch_all_pollutants("monthly", last_month_start, last_month_end)
    count = _insert("monthly", rows)
    msg = f"monthly: {count} lignes insérées ({last_month_start} → {last_month_end})"
    logger.info(msg)
    return msg, 200
