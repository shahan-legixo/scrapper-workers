# Build & deploy the cause-list worker to Cloud Run (one service for scrape + parse).
#
# Usage:
#   .\scripts\deploy-worker.ps1 -ProjectId legixo -Region asia-south1 -Bucket legixo-docs-dev -MongoUri "mongodb+srv://..."

param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "asia-south1",
    [string]$ServiceName = "causelist-worker",
    [Parameter(Mandatory = $true)][string]$Bucket,
    [Parameter(Mandatory = $true)][string]$MongoUri,
    [string]$MongoDb = "causelists"
)

$ErrorActionPreference = "Stop"
gcloud config set project $ProjectId | Out-Null

$EnvVars = @(
    "CLOUD_DRY_RUN=false",
    "LOCAL_INLINE_PARSE=false",
    "GCP_PROJECT_ID=$ProjectId",
    "GCP_LOCATION=$Region",
    "GCS_BUCKET=$Bucket",
    "SCRAPE_QUEUE=scrape-jobs",
    "PARSE_QUEUE=parse-jobs",
    "MONGO_URI=$MongoUri",
    "MONGO_DB=$MongoDb"
) -join ","

Write-Host "Deploying $ServiceName to $Region ..."
gcloud run deploy $ServiceName `
    --source . `
    --region $Region `
    --project $ProjectId `
    --allow-unauthenticated=false `
    --memory 2Gi `
    --timeout 3600 `
    --cpu 2 `
    --max-instances 5 `
    --set-env-vars $EnvVars

$Url = gcloud run services describe $ServiceName --region $Region --project $ProjectId --format='value(status.url)'
Write-Host ""
Write-Host "Deployed: $Url"
Write-Host "Set both SCRAPER_SERVICE_URL and PARSER_SERVICE_URL to this URL in .env"
Write-Host "Then grant Cloud Tasks invoker SA roles/run.invoker on this service."
