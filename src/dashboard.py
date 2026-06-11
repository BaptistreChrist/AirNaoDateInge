import re
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import datetime, timezone
from config import WHO_LIMITS
from alerts import subscribe, get_subscribers, send_alert_email, get_current_data, check_thresholds

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

st.set_page_config(page_title="AirNaoned - Qualité de l'Air", layout="wide")

PROJECT = "airnao-nantes-2026"
DATASET = "airquality"

POLLUTANT_COLORS = {
    "PM25": "#E74C3C",
    "PM10": "#E67E22",
    "SO2": "#9B59B6",
    "NO2": "#17A5C8",
    "O3":  "#27AE60",
}

POLLUTANT_LABELS = {
    "PM25": "PM2.5", "PM10": "PM10",
    "SO2": "SO₂",   "NO2": "NO₂", "O3": "O₃",
}

IQA_CATEGORIES = [
    (0,   50,  "Bon",                                 "#27AE60"),
    (51,  100, "Modéré",                              "#E67E22"),
    (101, 150, "Mauvais pour les groupes sensibles",  "#E67E22"),
    (151, 200, "Mauvais",                             "#E74C3C"),
    (201, 300, "Très mauvais",                        "#8E44AD"),
    (301, 500, "Dangereux",                           "#2C3E50"),
]

HEALTH_RECO = {
    "Bon":       "La qualité de l'air est satisfaisante. Profitez des activités en plein air.",
    "Modéré":    "Les personnes sensibles (enfants, personnes âgées, asthmatiques) devraient limiter les activités physiques intenses en extérieur. Privilégiez les activités en intérieur et aérez votre logement tôt le matin.",
    "Mauvais pour les groupes sensibles": "Les personnes sensibles devraient éviter les efforts prolongés à l'extérieur. Le grand public peut continuer ses activités normales.",
    "Mauvais":   "Tout le monde devrait réduire les activités physiques intenses en extérieur.",
    "Très mauvais": "Évitez toute activité physique à l'extérieur. Restez en intérieur avec les fenêtres fermées.",
    "Dangereux": "Urgence sanitaire. Restez en intérieur.",
}

POLLUTANT_SPECIFIC_RECO = {
    "PM25": "Évitez les zones à fort trafic ; portez un masque FFP2 si vous devez sortir.",
    "PM10": "Limitez votre exposition aux environnements poussiéreux et aux activités physiques en extérieur.",
    "NO2":  "Évitez les axes routiers très fréquentés, notamment aux heures de pointe.",
    "O3":   "Évitez les efforts physiques intenses entre 12h et 19h, heure de pic d'ozone.",
    "SO2":  "Les personnes asthmatiques doivent avoir leur traitement d'urgence à portée de main.",
}


def _situation_text(cur: dict, iqa_cat: str) -> str:
    over, ok_count = [], 0
    for n, r in cur.items():
        if r["valeur"] is not None and n in WHO_LIMITS:
            if r["valeur"] > WHO_LIMITS[n]:
                pct = (r["valeur"] - WHO_LIMITS[n]) / WHO_LIMITS[n] * 100
                over.append((n, pct))
            else:
                ok_count += 1

    if not over:
        if iqa_cat == "Bon":
            return "Tous les polluants sont dans les limites recommandées par l'OMS. La qualité de l'air est excellente."
        return "Les polluants restent dans les limites OMS, bien que la qualité de l'air soit modérée."

    if ok_count == 0:
        noms = ", ".join(POLLUTANT_LABELS[n] for n, _ in over)
        return f"L'ensemble des polluants surveillés ({noms}) dépassent les recommandations de l'OMS. Situation de pollution généralisée."

    parts = []
    for i, (n, pct) in enumerate(over):
        label, limit = POLLUTANT_LABELS[n], WHO_LIMITS[n]
        if i == 0:
            qualifier = "légèrement" if pct < 20 else ("significativement" if pct < 50 else "très fortement")
            parts.append(f"Les niveaux de {label} dépassent {qualifier} la recommandation OMS ({limit} µg/m³).")
        else:
            parts.append(f"Les niveaux de {label} sont également au-dessus du seuil recommandé.")
    parts.append("Les autres polluants restent dans les limites acceptables.")
    return " ".join(parts)


def _health_reco_text(iqa_cat: str, cur: dict) -> str:
    base = HEALTH_RECO.get(iqa_cat, "")
    elevated = [n for n, r in cur.items() if r["valeur"] is not None and n in WHO_LIMITS and r["valeur"] > WHO_LIMITS[n]]
    specific = [POLLUTANT_SPECIFIC_RECO[n] for n in elevated if n in POLLUTANT_SPECIFIC_RECO]
    return (base + " " + " ".join(specific)).strip() if specific else base


def iqa_category(iqa: float) -> tuple[str, str]:
    for low, high, label, color in IQA_CATEGORIES:
        if low <= iqa <= high:
            return label, color
    return "Dangereux", "#2C3E50"


@st.cache_data(ttl=1800)
def get_current(_client):
    # Prend la dernière valeur disponible pour chaque polluant indépendamment.
    # validite IS NOT FALSE : accepte TRUE (validé) et NULL (provisoire récent),
    # rejette seulement FALSE (mesure explicitement invalide par Atmo).
    query = f"""
        WITH derniere_par_polluant AS (
            SELECT notation_polluant,
                   MAX(date_heure_tu) AS max_ts
            FROM `{PROJECT}.{DATASET}.measures_hourly`
            WHERE validite IS NOT FALSE AND valeur IS NOT NULL
            GROUP BY notation_polluant
        )
        SELECT m.notation_polluant,
               AVG(m.valeur)          AS valeur,
               AVG(m.iqa_sous_indice) AS iqa_sub
        FROM `{PROJECT}.{DATASET}.measures_hourly` m
        JOIN derniere_par_polluant d
          ON m.notation_polluant = d.notation_polluant
         AND m.date_heure_tu >= TIMESTAMP_SUB(d.max_ts, INTERVAL 2 HOUR)
        WHERE m.validite IS NOT FALSE AND m.valeur IS NOT NULL
        GROUP BY m.notation_polluant
    """
    return _client.query(query).to_dataframe()


@st.cache_data(ttl=1800)
def get_historical_hourly(_client):
    query = f"""
        SELECT FORMAT_TIMESTAMP('%d/%m %Hh', date_heure_tu) AS periode,
               date_heure_tu,
               notation_polluant,
               AVG(valeur) AS valeur
        FROM `{PROJECT}.{DATASET}.measures_hourly`
        WHERE date_heure_tu >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 12 HOUR)
          AND validite IS NOT FALSE AND valeur IS NOT NULL
        GROUP BY periode, date_heure_tu, notation_polluant
        ORDER BY date_heure_tu
    """
    return _client.query(query).to_dataframe()


@st.cache_data(ttl=1800)
def get_historical_daily(_client):
    query = f"""
        SELECT FORMAT_DATE('%a', DATE(date_heure_tu, 'Europe/Paris')) AS periode,
               DATE(date_heure_tu, 'Europe/Paris')                    AS jour,
               notation_polluant,
               AVG(valeur) AS valeur
        FROM `{PROJECT}.{DATASET}.measures_hourly`
        WHERE date_heure_tu >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
          AND validite IS NOT FALSE AND valeur IS NOT NULL
        GROUP BY periode, jour, notation_polluant
        ORDER BY jour
    """
    return _client.query(query).to_dataframe()


@st.cache_data(ttl=1800)
def get_historical_monthly(_client):
    query = f"""
        SELECT FORMAT_TIMESTAMP('%b %Y', date_heure_tu) AS periode,
               notation_polluant,
               AVG(valeur) AS valeur
        FROM `{PROJECT}.{DATASET}.measures_monthly`
        WHERE date_heure_tu >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 365 DAY)
          AND validite IS NOT FALSE
        GROUP BY periode, notation_polluant
        ORDER BY MIN(date_heure_tu)
    """
    return _client.query(query).to_dataframe()


def trend_arrow(pct: float) -> str:
    if pct > 5:   return "↗"
    if pct < -5:  return "↘"
    return "—"


def concentration_chart(df: pd.DataFrame, x_col: str) -> go.Figure:
    fig = go.Figure()
    for notation, color in POLLUTANT_COLORS.items():
        subset = df[df["notation_polluant"] == notation]
        if subset.empty:
            continue
        fig.add_trace(go.Scatter(
            x=subset[x_col], y=subset["valeur"],
            mode="lines+markers",
            name=POLLUTANT_LABELS[notation],
            line=dict(color=color, width=2),
            marker=dict(size=5),
        ))
    fig.update_layout(
        xaxis_title="", yaxis_title="Concentration (µg/m³)",
        legend=dict(orientation="h", y=-0.2),
        margin=dict(l=10, r=10, t=10, b=40),
        height=340,
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
    )
    return fig


# ── CSS ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.card {
    background: white; border-radius: 12px; padding: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 12px;
}
.iqa-value { font-size: 3rem; font-weight: 700; line-height: 1; }
.iqa-badge {
    display: inline-block; padding: 3px 12px; border-radius: 20px;
    font-size: .85rem; font-weight: 600; margin-top: 6px;
}
.pol-value { font-size: 1.8rem; font-weight: 700; }
.pol-unit  { font-size: .85rem; color: #666; }
.pol-limit { font-size: .75rem; color: #888; margin-top: 4px; }
.pol-pct   { font-size: .8rem; font-weight: 600; }
.section-title { font-size: 1rem; font-weight: 600; color: #555; margin-bottom: 4px; }
.bar-bg {
    background: #eee; border-radius: 4px; height: 6px; margin-top: 8px;
}
.bar-fill { border-radius: 4px; height: 6px; }
h1 { font-size: 1.5rem !important; margin-bottom: 0 !important; }
</style>
""", unsafe_allow_html=True)

# ── Client BQ ───────────────────────────────────────────────────────────────
if "gcp_service_account" not in st.secrets:
    st.error("Secret GCP manquant. Ajoute [gcp_service_account] dans Streamlit Cloud → Settings → Secrets.")
    st.write("Secrets disponibles :", list(st.secrets.keys()))
    st.stop()

credentials = service_account.Credentials.from_service_account_info(
    dict(st.secrets["gcp_service_account"]),
    scopes=["https://www.googleapis.com/auth/bigquery"]
)
client = bigquery.Client(project=PROJECT, credentials=credentials)

BREVO_API_KEY    = st.secrets.get("brevo_api_key", "")
BREVO_FROM_EMAIL = st.secrets.get("brevo_from_email", "")

# ── Données courantes ────────────────────────────────────────────────────────
df_cur = get_current(client)
cur = {
    row["notation_polluant"]: {
        "valeur":  row["valeur"]  if pd.notna(row["valeur"])  else None,
        "iqa_sub": row["iqa_sub"] if pd.notna(row["iqa_sub"]) else None,
    }
    for _, row in df_cur.iterrows()
}

# IQA global = max des sous-indices
# ?test_alert=warning ou ?test_alert=critical dans l'URL pour tester visuellement
_test = st.query_params.get("test_alert", "")
if _test == "warning":
    cur = {"PM25": {"valeur": 22.0, "iqa_sub": 118.0}, "NO2": {"valeur": 31.0, "iqa_sub": 65.0},
           "PM10": {"valeur": 20.0, "iqa_sub": 18.0}, "SO2": {"valeur": 1.0, "iqa_sub": 1.0},
           "O3":   {"valeur": 55.0, "iqa_sub": 26.0}}
elif _test == "critical":
    cur = {"PM25": {"valeur": 80.0, "iqa_sub": 165.0}, "NO2": {"valeur": 200.0, "iqa_sub": 160.0},
           "PM10": {"valeur": 200.0, "iqa_sub": 155.0}, "SO2": {"valeur": 10.0, "iqa_sub": 5.0},
           "O3":   {"valeur": 150.0, "iqa_sub": 125.0}}

iqa_val = max((r["iqa_sub"] for r in cur.values() if r["iqa_sub"] is not None), default=0)
iqa_cat, iqa_color = iqa_category(iqa_val)

# Dernière timestamp disponible dans la base
@st.cache_data(ttl=1800)
def get_last_update(_client):
    query = f"""
        SELECT MAX(date_heure_tu) AS derniere
        FROM `{PROJECT}.{DATASET}.measures_hourly`
        WHERE validite IS NOT FALSE AND valeur IS NOT NULL
    """
    df = _client.query(query).to_dataframe()
    return df["derniere"].iloc[0]

last_update = get_last_update(client)
last_update_str = pd.Timestamp(last_update).strftime("%d/%m/%Y à %Hh%M") if last_update is not None else "inconnue"

# ── Header ───────────────────────────────────────────────────────────────────
now = datetime.now(timezone.utc).astimezone()
st.markdown(f"## 📍 Tableau de Bord - Qualité de l'Air")
st.caption(f"Nantes, France — Dernières données disponibles : **{last_update_str} UTC** • Collecte : {now.strftime('%d/%m/%Y %H:%M')}")
st.markdown("---")

# ── Bandeau d'alerte (IQA > 100) ─────────────────────────────────────────────
if iqa_val > 100:
    if iqa_val > 150:
        alert_color, alert_icon, alert_title = "#E74C3C", "🚨", "ALERTE — Qualité de l'air mauvaise"
    else:
        alert_color, alert_icon, alert_title = "#E67E22", "⚠️", "ATTENTION — Qualité de l'air mauvaise pour les groupes sensibles"
    exceeded = [
        f"{POLLUTANT_LABELS[n]} ({cur[n]['valeur']:.1f} µg/m³, limite {WHO_LIMITS[n]})"
        for n in POLLUTANT_LABELS
        if cur.get(n) and cur[n]["valeur"] and cur[n]["valeur"] > WHO_LIMITS[n]
    ]
    detail = f"Polluants en dépassement : {', '.join(exceeded)}" if exceeded else f"IQA global : {iqa_val:.0f} ({iqa_cat})"
    st.markdown(f"""
    <div style="background:{alert_color}18; border:1.5px solid {alert_color}; border-radius:10px;
                padding:12px 20px; margin-bottom:16px; display:flex; align-items:center; gap:12px;">
        <span style="font-size:1.4rem;">{alert_icon}</span>
        <div>
            <b style="color:{alert_color}; font-size:.95rem;">{alert_title}</b><br>
            <span style="color:#555; font-size:.85rem;">{detail}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

# ── Ligne 1 : IQA + OMS + Recommandations ────────────────────────────────────
col1, col2, col3 = st.columns([1, 1.5, 1.5])

with col1:
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid {iqa_color};">
        <div class="section-title">🌬️ Indice de Qualité de l'Air</div>
        <div class="iqa-value" style="color:{iqa_color};">{iqa_val:.0f}</div>
        <span style="color:#888; font-size:.9rem;"> IQA</span><br>
        <span class="iqa-badge" style="background:{iqa_color}22; color:{iqa_color};">{iqa_cat}</span>
    </div>
    """, unsafe_allow_html=True)

with col2:
    situation = _situation_text(cur, iqa_cat)
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid #E67E22;">
        <div class="section-title">ℹ️ Situation par rapport à l'OMS</div>
        <p style="font-size:.88rem; color:#444; margin:0;">{situation}</p>
    </div>
    """, unsafe_allow_html=True)

with col3:
    reco = _health_reco_text(iqa_cat, cur)
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid #F39C12;">
        <div class="section-title">⚠️ Recommandations de santé</div>
        <p style="font-size:.88rem; color:#444; margin:0;">{reco}</p>
    </div>
    """, unsafe_allow_html=True)

# ── Ligne 2 : Cartes polluants ───────────────────────────────────────────────
cols = st.columns(5)
for i, (notation, label) in enumerate(POLLUTANT_LABELS.items()):
    r = cur.get(notation)
    valeur  = r["valeur"] if r is not None else None
    limit   = WHO_LIMITS[notation]
    color   = POLLUTANT_COLORS[notation]
    pct     = ((valeur - limit) / limit * 100) if valeur else 0
    arrow   = trend_arrow(pct)
    bar_pct = min(int((valeur / limit) * 100), 100) if valeur else 0
    bar_color = color if valeur and valeur > limit else "#27AE60"
    pct_str = f"+{pct:.0f}%" if pct > 0 else ""
    if valeur is None:
        valeur_str = "—"
    elif valeur < 1:
        valeur_str = f"{valeur:.2f}"
    else:
        valeur_str = f"{valeur:.0f}"

    with cols[i]:
        st.markdown(f"""
        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <b>{label}</b>
                <span style="color:{color}; font-size:1.1rem;">{arrow}</span>
            </div>
            <div class="pol-value" style="color:{color};">
                {valeur_str}
                <span class="pol-unit">µg/m³</span>
            </div>
            <div class="bar-bg">
                <div class="bar-fill" style="width:{bar_pct}%; background:{bar_color};"></div>
            </div>
            <div class="pol-limit">
                Limite OMS: {limit} µg/m³
                <span class="pol-pct" style="color:{color}; float:right;">{pct_str}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

# ── Historique des concentrations ─────────────────────────────────────────────
st.markdown("### Historique des Concentrations")
if "granularity" not in st.session_state:
    st.session_state.granularity = "jour"

col_btn1, col_btn2, col_btn3, _ = st.columns([1, 1, 1, 6])
gran = st.session_state.granularity
with col_btn1: btn_h = st.button("🕐 Heure", type="primary" if gran == "heure" else "secondary")
with col_btn2: btn_j = st.button("📅 Jour",  type="primary" if gran == "jour"  else "secondary")
with col_btn3: btn_m = st.button("📈 Mois",  type="primary" if gran == "mois"  else "secondary")

if btn_h: st.session_state.granularity = "heure"; st.rerun()
if btn_j: st.session_state.granularity = "jour";  st.rerun()
if btn_m: st.session_state.granularity = "mois";  st.rerun()

gran = st.session_state.granularity
if gran == "heure":
    df_hist = get_historical_hourly(client)
    x_col = "periode"
elif gran == "mois":
    df_hist = get_historical_monthly(client)
    x_col = "periode"
else:
    df_hist = get_historical_daily(client)
    x_col = "periode"

if not df_hist.empty:
    st.plotly_chart(concentration_chart(df_hist, x_col), use_container_width=True)
else:
    st.info("Pas de données pour cette période.")

st.markdown("---")
st.markdown("### 📧 Alertes par email")

col_sub, col_send = st.columns([2, 1])

with col_sub:
    st.markdown("**Recevoir une alerte quand la qualité de l'air se dégrade**")
    with st.form("subscribe_form", clear_on_submit=True):
        email_input = st.text_input("Votre adresse email", placeholder="vous@exemple.fr")
        submitted = st.form_submit_button("S'inscrire aux alertes")
    if submitted:
        if not email_input or not EMAIL_RE.match(email_input):
            st.error("Adresse email invalide.")
        else:
            status, detail = subscribe(client, email_input)
            if status == "ok":
                st.success(f"**{email_input}** est inscrit. Vous recevrez un email lors des prochains dépassements.")
            elif status == "already":
                st.info(f"**{email_input}** est déjà inscrit.")
            else:
                st.error(f"Erreur lors de l'inscription : {detail}")

with col_send:
    st.markdown("**Démonstration — envoyer un rapport**")
    st.caption("Envoie le statut actuel à tous les abonnés, même sans dépassement.")
    if st.button("📤 Envoyer un rapport maintenant", use_container_width=True):
        if not BREVO_API_KEY:
            st.error("Secret 'brevo_api_key' introuvable dans Streamlit Cloud → Settings → Secrets.")
        elif not BREVO_FROM_EMAIL:
            st.error("Secret 'brevo_from_email' introuvable dans Streamlit Cloud → Settings → Secrets.")
        else:
            subs = get_subscribers(client)
            if not subs:
                st.warning("Aucun abonné pour l'instant. Inscrivez-vous d'abord.")
            else:
                cur_data = get_current_data(client)
                if not cur_data:
                    st.error("Impossible de récupérer les données actuelles.")
                else:
                    level_now, iqa_now, exc_now = check_thresholds(cur_data)
                    sent = send_alert_email(level_now, iqa_now, exc_now, subs, api_key=BREVO_API_KEY, from_email=BREVO_FROM_EMAIL)
                    if sent:
                        st.success(f"Rapport envoyé à {len(subs)} abonné(s).")
                    else:
                        st.error("Échec de l'envoi — vérifie les logs Streamlit pour le détail.")

st.caption(f"Dernières données disponibles : {last_update_str} UTC • Source publiée avec délai par airpl.org • Collecte automatique toutes les heures")
