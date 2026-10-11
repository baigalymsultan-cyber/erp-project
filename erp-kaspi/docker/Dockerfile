# syntax=docker/dockerfile:1.7
# MASTER PROMPT §12 / §132 — production image for the ERP backend.
# The image contains the application code and runs migrations (Alembic) on
# start; the PostgreSQL server is a separate service (see docker-compose.yml).
#
# Build (from the repository root):
#   docker build -t erp-backend -f docker/Dockerfile .
# Run:
#   docker run --rm --env-file .env erp-backend

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System dependencies: postgresql-client for scripts/apply_schema.sh (§87) and
# for the health/backup scripts; libpq is needed by psycopg/psycopg2 wheels
# (the wheels bundle it, the client tools do not).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first — this layer is cached between code changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir "psycopg[binary]==3.3.5"

# Application code.
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY app /app/app
COPY database /app/database
COPY scripts /app/scripts
COPY etl /app/etl

# Non-root user (§131: the container never runs as root).
RUN useradd --system --create-home --uid 10001 erp \
    && chown -R erp:erp /app
USER erp

ENV PYTHONPATH=/app

# §59/§134: the container is alive only while the API answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:8000/health" | grep -q '"status":"ok"' || exit 1

EXPOSE 8000

# uvicorn reads DATABASE_* from the environment (§10/§11 — see .env.example).
# Baseline-audit item 2 (§55): migrations actually run on start — the comment
# above promised it, the old CMD did not. The baseline revision is idempotent
# (§87: it can re-apply over a schema.sql-provisioned database and just stamps
# alembic_version), so this is safe on first start and on every restart.
#
# TD-008: the runtime env carries DATABASE_USER=erp_app (DML only). Migrations
# must run as erp_migrator (the schema owner), so the alembic step below
# overrides DATABASE_USER/DATABASE_PASSWORD for its own process only; uvicorn
# then exec's with the original erp_app environment.
CMD ["sh", "-c", "DATABASE_USER=erp_migrator DATABASE_PASSWORD=$DB_MIGRATOR_PASSWORD alembic upgrade head && exec uvicorn app.api:app --host 0.0.0.0 --port 8000 --proxy-headers"]
