# One image, three roles via APP_MODULE:
#   APP_MODULE=server:app              # producer + /jobs + workers (default)
#   APP_MODULE=server:app              # Worker 1 / Worker 2 same app (Cloud Tasks hits /tasks/*)
#
# Example:
#   docker build -t causelist-workers .
#   docker run -e CLOUD_DRY_RUN=false -e MONGO_URI=... -e GCS_BUCKET=... \
#     -e SCRAPER_SERVICE_URL=https://... -p 8080:8080 causelist-workers

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY all_causelist ./all_causelist
COPY db ./db
COPY server.py run_all.py load_env.py ./

RUN pip install --no-cache-dir .

ENV PORT=8080
ENV APP_MODULE=server:app
ENV CLOUD_DRY_RUN=false

EXPOSE 8080

CMD exec uvicorn ${APP_MODULE} --host 0.0.0.0 --port ${PORT}
