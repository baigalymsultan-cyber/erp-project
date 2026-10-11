# Local verification gate (FIN-OPS-0001): mirrors .github/workflows/ci.yml.
# bookworm ships postgresql-client 15, matching the PostgreSQL 15 server
# (a newer pg_dump emits settings PostgreSQL 15 rejects in restore drills).
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /deps
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt
WORKDIR /repo
