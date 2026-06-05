#!/bin/bash
# Déploiement complet sur GCP
# Prérequis : gcloud auth login && gcloud config set project data-engineering

set -e

PROJECT="airnao-nantes-2026"
REGION="europe-west1"
DATASET="airquality"
SRC="src"

echo "==> Activation des APIs GCP..."
gcloud services enable cloudfunctions.googleapis.com \
                       cloudscheduler.googleapis.com \
                       bigquery.googleapis.com \
                       cloudbuild.googleapis.com \
                       run.googleapis.com \
                       --project=$PROJECT

echo "==> Setup BigQuery..."
cd $SRC && python3 bq_setup.py && cd ..

echo "==> Déploiement Cloud Function : ingest_hourly..."
gcloud functions deploy ingest_hourly \
  --gen2 \
  --runtime=python312 \
  --region=$REGION \
  --source=$SRC \
  --entry-point=ingest_hourly \
  --project=$PROJECT \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=256Mi \
  --timeout=300s \
  --set-env-vars=GCP_PROJECT=$PROJECT

echo "==> Déploiement Cloud Function : ingest_daily..."
gcloud functions deploy ingest_daily \
  --gen2 \
  --runtime=python312 \
  --region=$REGION \
  --source=$SRC \
  --entry-point=ingest_daily \
  --project=$PROJECT \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=256Mi \
  --timeout=300s \
  --set-env-vars=GCP_PROJECT=$PROJECT

echo "==> Déploiement Cloud Function : ingest_monthly..."
gcloud functions deploy ingest_monthly \
  --gen2 \
  --runtime=python312 \
  --region=$REGION \
  --source=$SRC \
  --entry-point=ingest_monthly \
  --project=$PROJECT \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=256Mi \
  --timeout=300s \
  --set-env-vars=GCP_PROJECT=$PROJECT

echo "==> Déploiement Cloud Function : check_alerts..."
gcloud functions deploy check_alerts \
  --gen2 \
  --runtime=python312 \
  --region=$REGION \
  --source=$SRC \
  --entry-point=check_alerts \
  --project=$PROJECT \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=256Mi \
  --timeout=120s \
  --set-env-vars=GCP_PROJECT=$PROJECT,GMAIL_FROM=REMPLACER_PAR_VOTRE_EMAIL,ALERT_EMAIL_TO=REMPLACER_PAR_EMAIL_DESTINATAIRE
  # Ajouter GMAIL_APP_PASSWORD via Secret Manager ou --set-env-vars (ne pas mettre en clair dans ce script)

# Récupération des URLs des fonctions
HOURLY_URL=$(gcloud functions describe ingest_hourly  --region=$REGION --gen2 --format='value(serviceConfig.uri)')
DAILY_URL=$(gcloud functions describe ingest_daily    --region=$REGION --gen2 --format='value(serviceConfig.uri)')
MONTHLY_URL=$(gcloud functions describe ingest_monthly --region=$REGION --gen2 --format='value(serviceConfig.uri)')
ALERTS_URL=$(gcloud functions describe check_alerts   --region=$REGION --gen2 --format='value(serviceConfig.uri)')

echo "==> Création des Cloud Schedulers..."
gcloud scheduler jobs create http airnaoned-hourly \
  --location=$REGION \
  --schedule="0 * * * *" \
  --uri=$HOURLY_URL \
  --http-method=GET \
  --oidc-service-account-email="777906359882-compute@developer.gserviceaccount.com" \
  --time-zone="Europe/Paris" || echo "Job hourly déjà existant."

gcloud scheduler jobs create http airnaoned-daily \
  --location=$REGION \
  --schedule="0 6 * * *" \
  --uri=$DAILY_URL \
  --http-method=GET \
  --oidc-service-account-email="777906359882-compute@developer.gserviceaccount.com" \
  --time-zone="Europe/Paris" || echo "Job daily déjà existant."

gcloud scheduler jobs create http airnaoned-monthly \
  --location=$REGION \
  --schedule="0 6 1 * *" \
  --uri=$MONTHLY_URL \
  --http-method=GET \
  --oidc-service-account-email="777906359882-compute@developer.gserviceaccount.com" \
  --time-zone="Europe/Paris" || echo "Job monthly déjà existant."

gcloud scheduler jobs create http airnaoned-check-alerts \
  --location=$REGION \
  --schedule="5 * * * *" \
  --uri=$ALERTS_URL \
  --http-method=GET \
  --oidc-service-account-email="777906359882-compute@developer.gserviceaccount.com" \
  --time-zone="Europe/Paris" || echo "Job check-alerts déjà existant."

echo ""
echo "==> Déploiement terminé."
echo "Lance le backfill avec :"
echo "  cd src && python backfill.py --granularity monthly"
echo "  cd src && python backfill.py --granularity daily"
echo "  cd src && python backfill.py --granularity hourly"
