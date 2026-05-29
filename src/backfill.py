"""
Chargement historique one-shot.
  python backfill.py --granularity monthly
  python backfill.py --granularity daily
  python backfill.py --granularity hourly

Lance chaque granularité séparément pour pouvoir reprendre en cas d'erreur.
"""

import argparse
import logging
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from google.cloud import bigquery

from config import GCP_PROJECT, BQ_DATASET, TABLES, BACKFILL_START, POLLUTANTS
from airpl_client import fetch_all_pollutants
from iqa import _sub_index

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


def _insert_batch(table_key: str, rows: list[dict]) -> None:
    if not rows:
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{TABLES[table_key]}"
    errors = bq.insert_rows_json(table_id, rows)
    if errors:
        logger.error("BQ insert errors: %s", errors[:3])


def _date_windows_monthly(start: date, end: date):
    """Génère des fenêtres mois par mois."""
    cursor = start.replace(day=1)
    while cursor <= end:
        next_month = cursor + relativedelta(months=1)
        yield cursor, min(next_month - timedelta(days=1), end)
        cursor = next_month


def _date_windows_daily(start: date, end: date, chunk_days: int = 7):
    """Génère des fenêtres de N jours."""
    cursor = start
    while cursor <= end:
        yield cursor, min(cursor + timedelta(days=chunk_days - 1), end)
        cursor += timedelta(days=chunk_days)


def _date_windows_hourly(start: date, end: date, chunk_days: int = 2):
    """Fenêtres de 2 jours pour les données horaires (volume élevé)."""
    return _date_windows_daily(start, end, chunk_days)


def run_backfill(granularity: str) -> None:
    today = date.today()
    start = BACKFILL_START[granularity]
    table_key = granularity

    if granularity == "monthly":
        windows = list(_date_windows_monthly(start, today))
    elif granularity == "daily":
        windows = list(_date_windows_daily(start, today))
    else:
        windows = list(_date_windows_hourly(start, today))

    logger.info("Backfill %s : %d fenêtres à traiter (%s → %s)", granularity, len(windows), start, today)

    total = 0
    for i, (w_start, w_end) in enumerate(windows, 1):
        logger.info("[%d/%d] %s → %s", i, len(windows), w_start, w_end)
        batch = []
        for record in fetch_all_pollutants(granularity, w_start, w_end):
            batch.append(_row(record))
            if len(batch) >= 500:
                _insert_batch(table_key, batch)
                total += len(batch)
                batch = []
        _insert_batch(table_key, batch)
        total += len(batch)

    logger.info("Backfill %s terminé : %d lignes insérées au total.", granularity, total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--granularity", choices=["monthly", "daily", "hourly"], required=True)
    args = parser.parse_args()
    run_backfill(args.granularity)
