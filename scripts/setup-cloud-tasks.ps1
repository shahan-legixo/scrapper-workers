# Setup Cloud Tasks queues + invoker SA for cause-list workers (asia-south1).
# Prerequisites: gcloud auth login && gcloud config set project <PROJECT_ID>
#
# Usage:
#   .\scripts\setup-cloud-tasks.ps1 -ProjectId legixo -Region asia-south1 -ServiceName causelist-worker

param(
    [string]$ProjectId = $env:GCP_PROJECT_ID,
    [string]$Region = $(if ($env:GCP_LOCATION) { $env:GCP_LOCATION } else { "asia-south1" }),
    [string]$ServiceName = "causelist-worker",
    [string]$InvokerSaName = "cloud-tasks-invoker",
    [string]$ScrapeQueue = $(if ($env:SCRAPE_QUEUE) { $env:SCRAPE_QUEUE } else { "scrape-jobs" }),
    [string]$ParseQueue = $(if ($env:PARSE_QUEUE) { $env:PARSE_QUEUE } else { "parse-jobs" })
)

if (-not $ProjectId) {
    Write-Error "Pass -ProjectId or set GCP_PROJECT_ID"
    exit 1
}

$ErrorActionPreference = "Stop"
gcloud config set project $ProjectId | Out-Null

Write-Host "== Enable APIs =="
gcloud services enable run.googleapis.com cloudtasks.googleapis.com storage.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com --project $ProjectId

$InvokerEmail = "$InvokerSaName@$ProjectId.iam.gserviceaccount.com"

Write-Host "== Invoker SA: $InvokerEmail =="
$exists = gcloud iam service-accounts describe $InvokerEmail --project $ProjectId 2>$null
if (-not $exists) {
    gcloud iam service-accounts create $InvokerSaName `
        --display-name "Cloud Tasks invoker for cause-list workers" `
        --project $ProjectId
}

Write-Host "== Create queues =="
function Ensure-Queue([string]$Name) {
    $q = gcloud tasks queues describe $Name --location $Region --project $ProjectId 2>$null
    if (-not $q) {
        gcloud tasks queues create $Name --location $Region --project $ProjectId
    } else {
        Write-Host "queue exists: $Name"
    }
}
Ensure-Queue $ScrapeQueue
Ensure-Queue $ParseQueue

Write-Host @"

Next steps:
1) Deploy Cloud Run:
   gcloud run deploy $ServiceName --source . --region $Region --allow-unauthenticated=false --set-env-vars CLOUD_DRY_RUN=false,GCP_PROJECT_ID=$ProjectId,GCS_BUCKET=YOUR_BUCKET,MONGO_URI=YOUR_MONGO

2) Grant invoker:
   gcloud run services add-iam-policy-binding $ServiceName --region $Region --member=serviceAccount:$InvokerEmail --role=roles/run.invoker

3) Copy service URL:
   gcloud run services describe $ServiceName --region $Region --format='value(status.url)'

4) Put into .env:
   CLOUD_DRY_RUN=false
   LOCAL_INLINE_PARSE=false
   GCP_PROJECT_ID=$ProjectId
   GCP_LOCATION=$Region
   SCRAPE_QUEUE=$ScrapeQueue
   PARSE_QUEUE=$ParseQueue
   SCRAPER_SERVICE_URL=<url from step 3>
   PARSER_SERVICE_URL=<same url>
   TASK_SERVICE_ACCOUNT_EMAIL=$InvokerEmail
   GCS_BUCKET=<your bucket>

5) From a machine with GCP creds that can create tasks:
   uv run uvicorn server:app --reload
   curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" -d "{\"court\":\"bmhc\",\"date\":\"2026-10-14\"}"

Cloud Tasks will POST to {SCRAPER_SERVICE_URL}/tasks/scrape then {PARSER_SERVICE_URL}/tasks/parse.
"@
