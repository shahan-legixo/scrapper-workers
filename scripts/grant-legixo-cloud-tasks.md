# Unlock real Cloud Tasks + Cloud Run (project `legixo`)

Your laptop SA can write GCS (`legixo-docs-dev`) but **cannot** create Cloud Tasks,
and your user account has **no** `run.*` / `cloudtasks.*` on `legixo`.
Your personal GCP projects also have **closed billing**, so you cannot host queues there.

Ask a `legixo` project Owner to run:

```bash
PROJECT=legixo
REGION=asia-south1
USER=shahansiddique101@gmail.com
SA=legixo-service-account@legixo.iam.gserviceaccount.com
INVOKER=cloud-tasks-invoker@$PROJECT.iam.gserviceaccount.com

gcloud services enable cloudtasks.googleapis.com run.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com --project=$PROJECT

# You (developer) — create queues, deploy, enqueue
gcloud projects add-iam-policy-binding $PROJECT \
  --member="user:$USER" --role="roles/cloudtasks.admin"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="user:$USER" --role="roles/run.admin"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="user:$USER" --role="roles/iam.serviceAccountUser"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="user:$USER" --role="roles/artifactregistry.writer"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="user:$USER" --role="roles/cloudbuild.builds.editor"

# Runtime SA — Worker 1 enqueues parse tasks + reads/writes PDFs
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/cloudtasks.enqueuer"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/run.invoker"

# Invoker SA used in task OIDC tokens
gcloud iam service-accounts create cloud-tasks-invoker \
  --display-name="Cloud Tasks invoker" --project=$PROJECT || true
gcloud iam service-accounts add-iam-policy-binding $INVOKER \
  --member="serviceAccount:$SA" --role="roles/iam.serviceAccountUser" --project=$PROJECT
gcloud iam service-accounts add-iam-policy-binding $INVOKER \
  --member="user:$USER" --role="roles/iam.serviceAccountUser" --project=$PROJECT
```

Then on this machine:

```powershell
cd causelists
.\scripts\setup-cloud-tasks.ps1 -ProjectId legixo -Region asia-south1
.\scripts\deploy-worker.ps1 -ProjectId legixo -Bucket legixo-docs-dev -MongoUri "<atlas-or-reachable-uri>"
# copy Run URL into .env SCRAPER_SERVICE_URL + PARSER_SERVICE_URL
# set CLOUD_DRY_RUN=false, GCP_PROJECT_ID=legixo, TASK_SERVICE_ACCOUNT_EMAIL=cloud-tasks-invoker@legixo.iam.gserviceaccount.com
uv run uvicorn server:app --reload --port 8000
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" -d "{\"court\":\"bmhc\",\"date\":\"2026-10-14\"}"
```

Ngrok path (worker stays local, Tasks still real): after queues exist, set
`SCRAPER_SERVICE_URL` / `PARSER_SERVICE_URL` to your ngrok URL and leave
`TASK_SERVICE_ACCOUNT_EMAIL` empty (no OIDC). Leave `GOOGLE_APPLICATION_CREDENTIALS`
or `GCS_CREDENTIALS_JSON` for GCS uploads.
