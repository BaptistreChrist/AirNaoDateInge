import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from google.cloud import bigquery
from datetime import datetime, timezone

st.set_page_config(page_title="AirNaoned - Qualité de l'Air", layout="wide")

PROJECT = "airnao-nantes-2026"
DATASET = "airquality"

WHO_LIMITS = {"PM25": 15, "PM10": 45, "SO2": 40, "NO2": 25, "O3": 100}

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


def iqa_category(iqa: float) -> tuple[str, str]:
    for low, high, label, color in IQA_CATEGORIES:
        if low <= iqa <= high:
            return label, color
    return "Dangereux", "#2C3E50"


@st.cache_data(ttl=1800)
def get_current(_client):
    # Prend la dernière valeur disponible pour chaque polluant indépendamment
    query = f"""
        WITH derniere_par_polluant AS (
            SELECT notation_polluant,
                   MAX(date_heure_tu) AS max_ts
            FROM `{PROJECT}.{DATASET}.measures_hourly`
            WHERE validite = TRUE AND valeur IS NOT NULL
            GROUP BY notation_polluant
        )
        SELECT m.notation_polluant,
               AVG(m.valeur)          AS valeur,
               AVG(m.iqa_sous_indice) AS iqa_sub
        FROM `{PROJECT}.{DATASET}.measures_hourly` m
        JOIN derniere_par_polluant d
          ON m.notation_polluant = d.notation_polluant
         AND m.date_heure_tu >= TIMESTAMP_SUB(d.max_ts, INTERVAL 2 HOUR)
        WHERE m.validite = TRUE AND m.valeur IS NOT NULL
        GROUP BY m.notation_polluant
    """
    return _client.query(query).to_dataframe()


@st.cache_data(ttl=1800)
def get_historical_hourly(_client):
    query = f"""
        WITH dernier_jour AS (
            SELECT MAX(DATE(date_heure_tu)) AS max_date
            FROM `{PROJECT}.{DATASET}.measures_hourly`
            WHERE validite = TRUE
        )
        SELECT FORMAT_TIMESTAMP('%Hh', date_heure_tu) AS periode,
               notation_polluant,
               AVG(valeur) AS valeur
        FROM `{PROJECT}.{DATASET}.measures_hourly`
        WHERE DATE(date_heure_tu) = (SELECT max_date FROM dernier_jour)
          AND validite = TRUE
        GROUP BY periode, notation_polluant
        ORDER BY periode
    """
    return _client.query(query).to_dataframe()


@st.cache_data(ttl=1800)
def get_historical_daily(_client):
    query = f"""
        SELECT FORMAT_DATE('%a', DATE(date_heure_tu, 'Europe/Paris')) AS periode,
               DATE(date_heure_tu, 'Europe/Paris')                    AS jour,
               notation_polluant,
               AVG(valeur) AS valeur
        FROM `{PROJECT}.{DATASET}.measures_daily`
        WHERE date_heure_tu >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
          AND validite = TRUE
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
          AND validite = TRUE
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
client = bigquery.Client(project=PROJECT)

# ── Données courantes ────────────────────────────────────────────────────────
df_cur = get_current(client)
cur = {row["notation_polluant"]: {"valeur": row["valeur"], "iqa_sub": row["iqa_sub"]} for _, row in df_cur.iterrows()}

# IQA global = max des sous-indices
iqa_val = max((r["iqa_sub"] for r in cur.values() if r["iqa_sub"] is not None), default=0)
iqa_cat, iqa_color = iqa_category(iqa_val)

# ── Header ───────────────────────────────────────────────────────────────────
now = datetime.now(timezone.utc).astimezone()
st.markdown(f"## 📍 Tableau de Bord - Qualité de l'Air")
st.caption(f"Nantes, France — {now.strftime('%A %d %B %Y, %H:%M')}")
st.markdown("---")

# ── Ligne 1 : IQA + OMS + Recommandations ───────────────────────────────────
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
    depassements = [
        f"Les niveaux de **{POLLUTANT_LABELS[n]}** dépassent la recommandation OMS ({WHO_LIMITS[n]} µg/m³)."
        for n, r in cur.items()
        if r["valeur"] is not None and WHO_LIMITS.get(n) and r["valeur"] > WHO_LIMITS[n]
    ]
    situation = " ".join(depassements) if depassements else "Tous les polluants sont dans les limites recommandées par l'OMS."
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid #E67E22;">
        <div class="section-title">ℹ️ Situation par rapport à l'OMS</div>
        <p style="font-size:.88rem; color:#444; margin:0;">{situation}</p>
    </div>
    """, unsafe_allow_html=True)

with col3:
    reco = HEALTH_RECO.get(iqa_cat, "")
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
col_btn1, col_btn2, col_btn3, _ = st.columns([1, 1, 1, 6])
with col_btn1: btn_h = st.button("🕐 Heure")
with col_btn2: btn_j = st.button("📅 Jour",  type="primary")
with col_btn3: btn_m = st.button("📈 Mois")

if "granularity" not in st.session_state:
    st.session_state.granularity = "jour"
if btn_h: st.session_state.granularity = "heure"
if btn_j: st.session_state.granularity = "jour"
if btn_m: st.session_state.granularity = "mois"

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

st.caption("Données mises à jour toutes les heures • Source : Réseau de surveillance de la qualité de l'air")
