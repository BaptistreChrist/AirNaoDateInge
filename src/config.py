from datetime import date

GCP_PROJECT = "airnao-nantes-2026"
BQ_DATASET = "airquality"

WHO_LIMITS = {"PM25": 15, "PM10": 45, "SO2": 40, "NO2": 25, "O3": 100}

TABLES = {
    "hourly":   "measures_hourly",
    "daily":    "measures_daily",
    "monthly":  "measures_monthly",
}

# 5 polluants cibles avec leur code API airpl.org
POLLUTANTS = {
    "O3":   "08",
    "PM10": "24",
    "PM25": "39",
    "NO2":  "03",
    "SO2":  "01",
}

COMMUNE_CODE = "44109"  # Nantes

API_BASE = "https://data.airpl.org/api/v1/"
API_PAGE_SIZE = 1000

GRANULARITY_API = {
    "hourly":  "horaire",
    "daily":   "journaliere",
    "monthly": "mensuelle",
}

# Périodes historiques (backfill)
BACKFILL_START = {
    "monthly": date(2025, 5, 1),
    "daily":   date(2026, 4, 1),
    "hourly":  date(2026, 5, 1),
}
