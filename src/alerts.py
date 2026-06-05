"""
Détection des dépassements de seuils et envoi d'email d'alerte.
Cooldown : 1 email max par niveau toutes les 4 heures pour éviter le spam.
"""

import json
import logging
import os
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
from google.cloud import bigquery

from config import BQ_DATASET, GCP_PROJECT, WHO_LIMITS

logger = logging.getLogger(__name__)

ALERT_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.alerts"
COOLDOWN_HOURS = 4

POLLUTANT_LABELS = {
    "PM25": "PM2.5", "PM10": "PM10",
    "SO2": "SO₂",   "NO2": "NO₂", "O3": "O₃",
}

IQA_CATEGORIES = [
    (0,   50,  "Bon"),
    (51,  100, "Modéré"),
    (101, 150, "Mauvais pour les groupes sensibles"),
    (151, 200, "Mauvais"),
    (201, 300, "Très mauvais"),
    (301, 500, "Dangereux"),
]


def _iqa_cat(iqa: float) -> str:
    for low, high, label in IQA_CATEGORIES:
        if low <= iqa <= high:
            return label
    return "Dangereux"


def get_current_data(bq_client: bigquery.Client) -> dict:
    query = f"""
        WITH derniere_par_polluant AS (
            SELECT notation_polluant, MAX(date_heure_tu) AS max_ts
            FROM `{GCP_PROJECT}.{BQ_DATASET}.measures_hourly`
            WHERE validite = TRUE AND valeur IS NOT NULL
            GROUP BY notation_polluant
        )
        SELECT m.notation_polluant,
               AVG(m.valeur)          AS valeur,
               AVG(m.iqa_sous_indice) AS iqa_sub
        FROM `{GCP_PROJECT}.{BQ_DATASET}.measures_hourly` m
        JOIN derniere_par_polluant d
          ON m.notation_polluant = d.notation_polluant
         AND m.date_heure_tu >= TIMESTAMP_SUB(d.max_ts, INTERVAL 2 HOUR)
        WHERE m.validite = TRUE AND m.valeur IS NOT NULL
        GROUP BY m.notation_polluant
    """
    df = bq_client.query(query).to_dataframe()
    return {
        row["notation_polluant"]: {
            "valeur":  row["valeur"]  if pd.notna(row["valeur"])  else None,
            "iqa_sub": row["iqa_sub"] if pd.notna(row["iqa_sub"]) else None,
        }
        for _, row in df.iterrows()
    }


def check_thresholds(cur: dict) -> tuple[str, float, list]:
    """
    Retourne (level, iqa_val, exceedances).
    level : 'ok' | 'warning' | 'critical'
    exceedances : [(notation, valeur, limite), ...]
    """
    iqa_val = max((r["iqa_sub"] for r in cur.values() if r["iqa_sub"] is not None), default=0)
    exceedances = [
        (n, r["valeur"], WHO_LIMITS[n])
        for n, r in cur.items()
        if r["valeur"] is not None and n in WHO_LIMITS and r["valeur"] > WHO_LIMITS[n]
    ]
    if iqa_val > 150:
        level = "critical"
    elif iqa_val > 100 or exceedances:
        level = "warning"
    else:
        level = "ok"
    return level, iqa_val, exceedances


def already_alerted(bq_client: bigquery.Client, level: str) -> bool:
    """True si une alerte du même niveau a été envoyée dans les COOLDOWN_HOURS dernières heures."""
    query = f"""
        SELECT COUNT(*) AS cnt
        FROM `{ALERT_TABLE}`
        WHERE level = '{level}'
          AND sent_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {COOLDOWN_HOURS} HOUR)
          AND email_sent = TRUE
    """
    try:
        df = bq_client.query(query).to_dataframe()
        return int(df["cnt"].iloc[0]) > 0
    except Exception as exc:
        logger.warning("Impossible de vérifier le cooldown alerte : %s", exc)
        return False


def store_alert(bq_client: bigquery.Client, level: str, iqa_val: float, exceedances: list, email_sent: bool) -> None:
    row = {
        "alert_id":        str(uuid.uuid4()),
        "sent_at":         datetime.now(timezone.utc).isoformat(),
        "level":           level,
        "iqa_value":       float(iqa_val),
        "exceedances_json": json.dumps([
            {"polluant": n, "valeur": round(v, 2), "limite": l}
            for n, v, l in exceedances
        ]),
        "email_sent": email_sent,
    }
    errors = bq_client.insert_rows_json(ALERT_TABLE, [row])
    if errors:
        logger.error("Erreur stockage alerte BQ : %s", errors)


def send_alert_email(level: str, iqa_val: float, exceedances: list) -> bool:
    """Envoie un email d'alerte via Gmail SMTP. Retourne True si succès."""
    gmail_from     = os.environ.get("GMAIL_FROM")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient      = os.environ.get("ALERT_EMAIL_TO")

    if not all([gmail_from, gmail_password, recipient]):
        logger.warning("Variables email manquantes (GMAIL_FROM, GMAIL_APP_PASSWORD, ALERT_EMAIL_TO) — email non envoyé.")
        return False

    iqa_cat  = _iqa_cat(iqa_val)
    is_crit  = level == "critical"
    icon     = "🚨" if is_crit else "⚠️"
    titre    = "ALERTE CRITIQUE" if is_crit else "Alerte"
    color    = "#E74C3C" if is_crit else "#E67E22"
    subject  = f"[AirNaoned] {icon} {titre} — IQA {iqa_val:.0f} ({iqa_cat}) à Nantes"

    rows_html = "".join(
        f"<tr><td style='padding:4px 8px;'><b>{POLLUTANT_LABELS.get(n, n)}</b></td>"
        f"<td style='padding:4px 8px;'>{v:.1f} µg/m³</td>"
        f"<td style='padding:4px 8px;'>{l} µg/m³</td>"
        f"<td style='padding:4px 8px; color:{color};'>+{(v - l) / l * 100:.0f}%</td></tr>"
        for n, v, l in exceedances
    )
    table_html = f"""
    <table style='border-collapse:collapse; margin:12px 0;'>
        <thead><tr style='background:#f5f5f5;'>
            <th style='padding:4px 8px; text-align:left;'>Polluant</th>
            <th style='padding:4px 8px; text-align:left;'>Valeur</th>
            <th style='padding:4px 8px; text-align:left;'>Limite OMS</th>
            <th style='padding:4px 8px; text-align:left;'>Écart</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
    </table>""" if exceedances else "<p>Aucun dépassement OMS, mais l'IQA global dépasse 100.</p>"

    body = f"""
    <html><body style='font-family:Arial,sans-serif; color:#333; max-width:600px;'>
        <h2 style='color:{color}; border-bottom:2px solid {color}; padding-bottom:8px;'>
            {icon} {titre} — Qualité de l'Air à Nantes
        </h2>
        <p><b>IQA actuel : <span style='color:{color};'>{iqa_val:.0f}</span></b> — {iqa_cat}</p>
        {table_html}
        <p style='font-size:.9rem; color:#666;'>
            Consultez le tableau de bord AirNaoned pour le suivi en temps réel.
        </p>
        <hr style='border:none; border-top:1px solid #eee; margin-top:24px;'>
        <p style='font-size:.75rem; color:#999;'>AirNaoned — Qualité de l'air à Nantes · Alerte automatique</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_from
    msg["To"]      = recipient
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_from, gmail_password)
            smtp.sendmail(gmail_from, recipient, msg.as_string())
        logger.info("Email d'alerte envoyé à %s (level=%s, IQA=%.0f)", recipient, level, iqa_val)
        return True
    except Exception as exc:
        logger.error("Erreur envoi email : %s", exc)
        return False


def run_check(bq_client: bigquery.Client) -> str:
    """Point d'entrée principal : vérifie les seuils, envoie l'email si nécessaire."""
    cur = get_current_data(bq_client)
    if not cur:
        return "Pas de données disponibles."

    level, iqa_val, exceedances = check_thresholds(cur)

    if level == "ok":
        logger.info("check_alerts : tout OK (IQA=%.0f)", iqa_val)
        return f"ok: IQA={iqa_val:.0f}, aucun dépassement."

    if already_alerted(bq_client, level):
        logger.info("check_alerts : alerte %s déjà envoyée dans les %dh (cooldown).", level, COOLDOWN_HOURS)
        return f"cooldown: alerte {level} déjà envoyée récemment."

    email_sent = send_alert_email(level, iqa_val, exceedances)
    store_alert(bq_client, level, iqa_val, exceedances, email_sent)

    return f"alerte {level} envoyée (IQA={iqa_val:.0f}, email={'oui' if email_sent else 'non'})."
