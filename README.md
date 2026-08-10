# DH App API

FastAPI backend for the Digital Hub SA Main App, deployed on SAP BTP Cloud Foundry.

## Stack

- Python 3.11 / FastAPI
- SAP HANA Cloud (production) / PostgreSQL (local dev)
- SAP BTP Cloud Foundry (br10 region)

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in your local values
cp manifest.template.yml manifest.yml   # then edit with real credentials
# OR use environment variables directly:
export DH_DB_DRIVER=hana
export DH_DB_HOST=...
export DH_DB_PASSWORD=...
export JWT_SECRET=...

uvicorn main:app --reload --port 8000
```

## Deploy to BTP

```bash
# Build manifest from template (never commit manifest.yml)
cp manifest.template.yml manifest.yml
# Edit manifest.yml with real values, then:
cf push dh-api

# Set secrets (do this once, they persist in CF):
cf set-env dh-api DH_DB_PASSWORD "your-password"
cf set-env dh-api AZURE_CLIENT_SECRET "your-secret"
cf set-env dh-api JWT_SECRET "your-jwt-secret"
cf restage dh-api
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DH_DB_DRIVER` | yes | `hana` or `postgres` |
| `DH_DB_HOST` | yes | HANA Cloud host |
| `DH_DB_PORT` | yes | `443` for HANA |
| `DH_DB_USER` | yes | DB user |
| `DH_DB_PASSWORD` | yes (secret) | DB password — use `cf set-env` |
| `AZURE_CLIENT_ID` | yes | App Registration client ID |
| `AZURE_TENANT_ID` | yes | SAP Azure tenant ID |
| `AZURE_CLIENT_SECRET` | yes (secret) | App Registration secret — use `cf set-env` |
| `JWT_SECRET` | yes (secret) | JWT signing key — use `cf set-env` |
| `JWT_TTL_HOURS` | no | Token TTL, default 168h |

## Project structure

```
main.py          — FastAPI app, auth endpoints, debug endpoints
auth.py          — Azure SSO + JWT logic
database.py      — HANA/Postgres abstraction layer
routers/         — Feature routers (pipeline, outreach, ae_analysis, ...)
etl/             — ETL scripts for CSV/Excel ingestion
```
