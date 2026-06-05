"""
Création du dataset et des 3 tables BigQuery.
Lancer une seule fois : python bq_setup.py
"""

from google.cloud import bigquery
from config import GCP_PROJECT, BQ_DATASET, TABLES, WHO_LIMITS

client = bigquery.Client(project=GCP_PROJECT)

SCHEMA_MEASURES = [
    bigquery.SchemaField("id",                 "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("code_polluant",      "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("notation_polluant",  "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("code_station",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("nom_station",        "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("nom_commune",        "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("code_commune",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("departement_code",   "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("date_heure_tu",      "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("valeur",             "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("validite",           "BOOLEAN",   mode="NULLABLE"),
    bigquery.SchemaField("iqa_sous_indice",    "FLOAT64",   mode="NULLABLE"),
]

# Tables partitionnées par jour sur date_heure_tu pour optimiser les coûts de scan
PARTITION = bigquery.TimePartitioning(
    type_=bigquery.TimePartitioningType.DAY,
    field="date_heure_tu",
)


def create_dataset() -> None:
    dataset_ref = bigquery.Dataset(f"{GCP_PROJECT}.{BQ_DATASET}")
    dataset_ref.location = "EU"
    client.create_dataset(dataset_ref, exists_ok=True)
    print(f"Dataset {BQ_DATASET} prêt.")


def create_table(table_key: str) -> None:
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{TABLES[table_key]}"
    table = bigquery.Table(table_id, schema=SCHEMA_MEASURES)
    table.time_partitioning = PARTITION
    table.clustering_fields = ["code_polluant", "code_station"]
    client.create_table(table, exists_ok=True)
    print(f"Table {TABLES[table_key]} prête.")


SCHEMA_ALERTS = [
    bigquery.SchemaField("alert_id",         "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("sent_at",          "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("level",            "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("iqa_value",        "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("exceedances_json", "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("email_sent",       "BOOLEAN",   mode="NULLABLE"),
]


def create_alerts_table() -> None:
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.alerts"
    table = bigquery.Table(table_id, schema=SCHEMA_ALERTS)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="sent_at"
    )
    client.create_table(table, exists_ok=True)
    print("Table alerts prête.")


if __name__ == "__main__":
    create_dataset()
    for key in TABLES:
        create_table(key)
    create_alerts_table()
    print("Setup BigQuery terminé.")
