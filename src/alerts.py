"""
Détection des dépassements de seuils et envoi d'email d'alerte.
Cooldown : 1 email max par niveau toutes les 4 heures pour éviter le spam.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests
from google.cloud import bigquery

from config import BQ_DATASET, GCP_PROJECT, WHO_LIMITS

logger = logging.getLogger(__name__)

ALERT_TABLE       = f"{GCP_PROJECT}.{BQ_DATASET}.alerts"
SUBSCRIBERS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.subscribers"
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
            WHERE validite IS NOT FALSE AND valeur IS NOT NULL
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


_SUBSCRIBERS_SCHEMA = [
    bigquery.SchemaField("email",         "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("subscribed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("active",        "BOOLEAN",   mode="REQUIRED"),
]


def _ensure_subscribers_table(bq_client: bigquery.Client) -> None:
    """Crée la table subscribers si elle n'existe pas encore."""
    table_ref = bigquery.Table(SUBSCRIBERS_TABLE, schema=_SUBSCRIBERS_SCHEMA)
    bq_client.create_table(table_ref, exists_ok=True)


def get_subscribers(bq_client: bigquery.Client) -> list[str]:
    """Retourne la liste des emails actifs inscrits aux alertes."""
    query = f"SELECT email FROM `{SUBSCRIBERS_TABLE}` WHERE active = TRUE"
    try:
        df = bq_client.query(query).to_dataframe()
        return df["email"].tolist()
    except Exception as exc:
        logger.warning("Impossible de récupérer les abonnés : %s", exc)
        return []


def subscribe(bq_client: bigquery.Client, email: str) -> tuple[str, str]:
    """Inscrit un email. Retourne ('ok'|'already'|'error', détail)."""
    try:
        check_q = f"SELECT COUNT(*) AS n FROM `{SUBSCRIBERS_TABLE}` WHERE email = @email AND active = TRUE"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("email", "STRING", email)]
        )
        df = bq_client.query(check_q, job_config=job_config).to_dataframe()
        if int(df["n"].iloc[0]) > 0:
            return "already", ""
        row = {
            "email":         email,
            "subscribed_at": datetime.now(timezone.utc).isoformat(),
            "active":        True,
        }
        errors = bq_client.insert_rows_json(SUBSCRIBERS_TABLE, [row])
        if errors:
            return "error", str(errors)
        return "ok", ""
    except Exception as exc:
        logger.error("Erreur inscription %s : %s", email, exc)
        return "error", str(exc)


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


def send_alert_email(
    level: str,
    iqa_val: float,
    exceedances: list,
    recipients: list[str] | None = None,
    api_key: str | None = None,
    from_email: str | None = None,
) -> bool:
    """Envoie un email via Brevo aux destinataires indiqués (ou ALERT_EMAIL_TO par défaut). Retourne True si succès."""
    api_key    = api_key    or os.environ.get("BREVO_API_KEY")
    from_email = from_email or os.environ.get("BREVO_FROM_EMAIL")

    if not all([api_key, from_email]):
        logger.warning("Variables Brevo manquantes (BREVO_API_KEY, BREVO_FROM_EMAIL) — email non envoyé.")
        return False

    if recipients is None:
        admin = os.environ.get("ALERT_EMAIL_TO")
        recipients = [admin] if admin else []
    if not recipients:
        logger.warning("Aucun destinataire — email non envoyé.")
        return False

    iqa_cat = _iqa_cat(iqa_val)

    if level == "ok":
        icon, titre, color = "✅", "Rapport de qualité de l'air", "#27AE60"
    elif level == "critical":
        icon, titre, color = "🚨", "ALERTE CRITIQUE", "#E74C3C"
    else:
        icon, titre, color = "⚠️", "Alerte", "#E67E22"

    subject = f"[AirNaoned] {icon} {titre} — IQA {iqa_val:.0f} ({iqa_cat}) à Nantes"

    rows_html = "".join(
        f"<tr><td style='padding:4px 8px;'><b>{POLLUTANT_LABELS.get(n, n)}</b></td>"
        f"<td style='padding:4px 8px;'>{v:.1f} µg/m³</td>"
        f"<td style='padding:4px 8px;'>{l} µg/m³</td>"
        f"<td style='padding:4px 8px; color:{color};'>+{(v - l) / l * 100:.0f}%</td></tr>"
        for n, v, l in exceedances
    )
    if exceedances:
        table_html = f"""
        <table style='border-collapse:collapse; margin:12px 0;'>
            <thead><tr style='background:#f5f5f5;'>
                <th style='padding:4px 8px; text-align:left;'>Polluant</th>
                <th style='padding:4px 8px; text-align:left;'>Valeur mesurée</th>
                <th style='padding:4px 8px; text-align:left;'>Limite OMS</th>
                <th style='padding:4px 8px; text-align:left;'>Écart</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>"""
    elif level == "ok":
        table_html = "<p style='color:#27AE60;'>Tous les polluants sont dans les limites OMS. Qualité de l'air satisfaisante.</p>"
    else:
        table_html = "<p>Aucun dépassement OMS individuel, mais l'IQA global dépasse 100.</p>"

    html_body = f"""
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

    to_list = [{"email": r} for r in recipients]
    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={
                "sender":      {"name": "AirNaoned Alertes", "email": from_email},
                "to":          to_list,
                "subject":     subject,
                "htmlContent": html_body,
            },
            timeout=10,
        )
        if resp.status_code == 201:
            logger.info("Email Brevo envoyé à %s (level=%s, IQA=%.0f)", recipients, level, iqa_val)
            return True
        logger.error("Brevo status %s : %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error("Erreur envoi Brevo : %s", exc)
        return False


def run_check(bq_client: bigquery.Client, test: bool = False) -> str:
    """Point d'entrée principal : vérifie les seuils, envoie l'email si nécessaire.
    Si test=True, simule un scénario de dépassement sans lire la BQ ni respecter le cooldown.
    """
    if test:
        level       = "warning"
        iqa_val     = 118.0
        exceedances = [("PM25", 22.0, 15), ("NO2", 31.0, 25)]
        logger.info("check_alerts : MODE TEST — scénario simulé (IQA=%.0f)", iqa_val)
        email_sent = send_alert_email(level, iqa_val, exceedances)
        return f"[TEST] alerte {level} simulée (IQA={iqa_val:.0f}, email={'oui' if email_sent else 'non'})."

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

    recipients = get_subscribers(bq_client)
    if not recipients:
        admin = os.environ.get("ALERT_EMAIL_TO")
        if admin:
            recipients = [admin]

    email_sent = send_alert_email(level, iqa_val, exceedances, recipients)
    store_alert(bq_client, level, iqa_val, exceedances, email_sent)

    return f"alerte {level} envoyée (IQA={iqa_val:.0f}, email={'oui' if email_sent else 'non'}, destinataires={len(recipients)})."
