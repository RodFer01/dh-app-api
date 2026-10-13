# dh-app-api — Codebase Reference

> Written for future Claude sessions. Enough context to start any task without re-reading the entire codebase.

---

## What this is

Internal backend for the **DH Portal** — a data analytics and team management platform for SAP Digital HUB LAC. The portal consolidates pipeline data, Melody activities, presales tracking, DDA/VAT data, scheduling, and email campaign tooling for the LAC SA team.

**Production URL:** `https://dh-api.cfapps.br10.hana.ondemand.com`  
**Deployment:** SAP BTP Cloud Foundry, org `presalesbtplac`, space `DSA`, app name `dh-api`  
**Deploy command:** `cf push dh-api` (from the project root, reads `manifest.yml`)

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Web framework | FastAPI (Uvicorn) |
| DB (production) | SAP HANA Cloud via `hdbcli` |
| DB (local dev) | PostgreSQL via `psycopg2` |
| Auth | JWT HS256 (`python-jose`) + Azure AD OAuth2 (`requests`) |
| Password hashing | `bcrypt` cost 12 |
| File uploads | FastAPI `UploadFile` |
| Email | SMTP via `smtplib` (Office 365) |
| ESS integration | HTTP requests to `https://ess.int.sap` with stored cookies |
| Static files | Served from `./static/` (built Vue/React frontend) |

---

## Project structure

```
dh-app-api/
├── main.py                  # App bootstrap, middleware, debug endpoints
├── auth.py                  # JWT, login, Azure SSO
├── database.py              # DB helpers, SQL translation (PG → HANA)
├── routers/
│   ├── admin.py             # User management, permissions, logs
│   ├── ae_analysis.py       # AE territory analysis
│   ├── actions.py           # Team action items
│   ├── calendar.py          # Calendar / team agenda
│   ├── campaigns.py         # Marketing campaigns + contacts
│   ├── comparison.py        # YoY data comparison
│   ├── ess.py               # Email Self-Service (ESS) integration
│   ├── etl.py               # ETL job trigger + status
│   ├── feedback.py          # 1:1 feedback records
│   ├── idea_zone.py         # Team ideas board
│   ├── melody.py            # Melody activity analytics
│   ├── melody_requests.py   # Melody request tracking
│   ├── notes.py             # Account notes
│   ├── outlook_bridge.py    # Outlook calendar proxy
│   ├── outlook_inbox.py     # Outlook inbox reader
│   ├── outreach.py          # Outreach contacts + account solution map
│   ├── pipeline.py          # Core pipeline analytics (largest file — 1856 lines)
│   ├── pptx_export.py       # PPTX deck generation
│   ├── scheduler.py         # QC meeting scheduler
│   ├── spark.py             # Spark meeting utility
│   └── targets.py           # SA quota/target management
├── etl/                     # 15+ ETL scripts (run independently or via API)
│   ├── config.py            # File path config (FILES dict → CSV/XLSX locations)
│   ├── etl_pipeline.py      # Main pipeline CSV loader
│   ├── etl_pipeline_exploded.py
│   ├── etl_melody.py
│   ├── etl_targets.py
│   ├── etl_vat.py
│   ├── etl_presales.py
│   ├── etl_dda.py
│   ├── etl_rvp.py
│   ├── etl_fx.py
│   ├── etl_progression.py
│   ├── etl_innovation.py
│   ├── etl_mastercode.py
│   ├── etl_outreach_contacts.py
│   ├── etl_melody_2025.py
│   ├── etl_pipeline_2025.py
│   ├── etl_vat_2025.py
│   └── run_all.py
├── spark_meeting.py         # Spark meeting model
├── melody_requests.py       # (also at root — duplicate of router)
├── static/                  # Built frontend assets
├── manifest.yml             # BTP CF deployment config (GITIGNORED — contains secrets)
├── manifest.template.yml    # Safe template for manifest.yml
├── .gitignore
├── requirements.txt
├── SECURITY_REVIEW.md       # Security audit findings (2026-10-13)
└── CODEBASE.md              # This file
```

---

## Authentication

Three parallel login paths — tried in order:

### 1. DB login (`POST /api/auth/login`)
- Validates email + bcrypt password against `app_users` table
- Returns JWT (24h TTL by default, configurable via `JWT_TTL_HOURS`)
- On success, updates `last_login_at` and logs to `app_access_logs`

### 2. Azure SSO (`POST /api/auth/azure`)
- Exchanges Azure auth code for access + refresh tokens via Microsoft identity endpoint
- Fetches user profile from Microsoft Graph (`/v1.0/me`)
- Auto-provisions new users as `viewer` with default modules `["pipeline", "melody", "opportunities", "qualification"]`
- Returns JWT + Outlook Bearer token (used by frontend for calendar operations)

### 3. Legacy fallback (`_login_legacy`)
- Reads `~/.config/sap-portal/users.json`
- Hardcodes `role="admin"` regardless of file contents
- Exists only for migration compatibility — **should be removed** (see SECURITY_REVIEW.md #4)

### JWT payload
```json
{
  "sub": "email@sap.com",
  "name": "Display Name",
  "role": "admin|manager|viewer",
  "modules": ["pipeline", "melody", ...],
  "exp": ...,
  "iat": ...
}
```

### Authorization helpers (defined in `routers/admin.py`)
- `verify_token` — any valid JWT (defined in `auth.py`)
- `require_admin` — `role == "admin"`
- `require_admin_or_manager` — `role in ("admin", "manager")`

### Roles and modules
Roles (`admin`, `manager`, `viewer`) control what write operations are allowed.  
Module access (`pipeline`, `melody`, `etl`, etc.) is stored per-user in `app_permissions` and embedded in the JWT.

Predefined profiles: `admin`, `erp_manager`, `lob_manager`, `spoc`, `spoc_lob`, `viewer`

---

## Database

### Dual-DB abstraction

`database.py` supports both HANA Cloud (production) and PostgreSQL (local dev).  
`DB_DRIVER = os.getenv("DH_DB_DRIVER", "hana")` — default is HANA.

The `_adapt_sql()` function (200+ lines) translates PG SQL to HANA at runtime:
- `%s` → `?`
- `array_agg(...)` → `STRING_AGG(..., ',')`
- `FILTER (WHERE ...)` → `CASE WHEN ... END` (character-by-character parser)
- `DISTINCT ON (col)` → `ROW_NUMBER() OVER (PARTITION BY ...)`
- `::text`, `::int`, etc. → `TO_NVARCHAR()`, `TO_INT()`
- `ILIKE` → `LOWER() LIKE LOWER()`
- `NOW()` → `CURRENT_TIMESTAMP`

**No connection pooling** — each query opens and closes a HANA connection via `get_conn()` context manager.

### Key helpers
```python
query(sql, params, array_cols=None) -> list[dict]
query_one(sql, params, array_cols=None) -> dict | None
execute(sql, params) -> rowcount
execute_returning(sql, params) -> new_id
```

`array_cols`: for HANA, `STRING_AGG` returns CSV strings — pass column names here to convert them to Python lists.

### Main tables

| Table | Description |
|---|---|
| `app_users` | Portal users: email, name, role, password_hash, is_active, last_login_at, last_seen_at |
| `app_permissions` | Per-user module grants: user_id, module, can_read, can_write |
| `app_access_logs` | Login and action audit log |
| `pipeline` | Main pipeline data (loaded weekly from SAP pipeline CSV) |
| `pipeline_last_week` | Prior week snapshot for WoW comparison |
| `pipeline_exploded` | One row per opportunity × VAT member (from ETL) |
| `melody` | Melody activity details |
| `targets` | SA quota targets by inumber/area/sub_team |
| `dh_vat` | VAT (Value Added Team) assignments |
| `dh_presales` | Deal Management opportunity details |
| `dda` / `lac_cloud_erp` | DDA/DET data for ERP tracking |
| `rvp` / `rpv` | Rating/progression data |
| `fx_rates` | Currency exchange rates |
| `progression_opps` | Progression Framework opportunity priorities |
| `account_specs` | IAE/SPEC assignments per account BP |
| `account_solution_map` | Account-level solution + competitor mapping |
| `account_installed_base` | Installed base from InnovationDash |
| `calc_rules` | Rules for pipeline_exploded calculation (divide_by_vat flag) |
| `manager_targets` | Aggregate manager quota targets |
| `etl_job_history` | ETL run log (status, rows, log_text) |
| `notes` | Account notes with tags |
| `campaigns` | Marketing campaign records |
| `campaign_contacts` | Contacts per campaign |
| `ess_credentials` | ESS session cookies per user |
| `ess_sends` | ESS email send log |
| `shadow_opp_comments` | Analyst notes linking closed opportunities to DH opportunity IDs |
| `scheduler_period` | Scheduling config (start/end date, frequency) |
| `scheduler_quota_carriers` | QC list for meeting scheduling |
| `scheduler_email_map` | Email overrides for scheduling |
| `scheduler_meetings` | Scheduled meeting records |
| `feedback_1on1` | 1:1 feedback records |
| `actions` | Team action items |
| `idea_zone` | Team ideas |
| `mastercode` | BP → IAE/SPEC mapping from Mastercode XLSX |
| `outreach_contacts` | Lead Intelligence contacts from Databricks |
| `melody_requests` | Melody requests tracking |

---

## ETL Pipeline

### Trigger points

ETL can be triggered three ways:
1. **API endpoint:** `POST /api/etl/run/{source_id}` — uploads file + queues background job
2. **Debug endpoint:** `POST /api/debug/rerun-pipeline-exploded` — reruns exploded ETL without re-uploading CSV
3. **Direct script execution:** `python etl/etl_pipeline.py` (runs independently from the API)

### ETL sources (defined in `routers/etl.py`)

| ID | Script(s) | Input |
|---|---|---|
| `pipeline` | etl_pipeline, etl_pipeline_exploded | CSV weekly |
| `pipeline_lw` | etl_pipeline (last_week=True) | CSV last week |
| `melody` | etl_melody | CSV |
| `presales` | etl_presales | CSV |
| `vat_full` | etl_vat (only=full_vat) | CSV |
| `vat_dh` | etl_vat (only=dh_vat), etl_pipeline_exploded | CSV |
| `targets` | etl_targets | XLSX |
| `dda` / `lac_cloud_erp` | etl_dda | XLSX |
| `rvp` | etl_rvp | XLSX |
| `progression` | etl_progression | XLSX |
| `innovation_dash` | etl_innovation | XLSX |
| `mastercode` | etl_mastercode | XLSX |
| `melody_2025` | etl_melody_2025 | CSV |
| `pipeline_2025` | etl_pipeline_2025 | CSV |
| `vat_full_2025` / `vat_dh_2025` | etl_vat_2025 | CSV |
| `outreach_contacts` | etl_outreach_contacts | (Databricks API) |
| `full_run` | run_all.py (all modules) | (all files) |

### Job tracking

Jobs are tracked in-memory (`_jobs` dict with `threading.Lock`) and persisted to `etl_job_history`. On restart the in-memory dict is empty but history loads from DB.

### Known issue

ETL runs `os.chdir(ETL_DIR)` in a background thread — this mutates the global process CWD. Safe for single-job execution; breaks under concurrent ETL jobs. (See SECURITY_REVIEW.md #7)

### `etl/etl_pipeline.py` — recent restructure

Refactored to support multi-file ingestion:
- `_read_pipeline_csv(path)` — encapsulates CSV parsing per file
- `run_multi(paths, last_week=False)` — accepts a list of paths and ingests them in sequence; replaces the older single-path entry point
- `_ensure_columns(conn)` — runs column presence checks at ETL time (mirrors startup migration logic)
- `_update_weekly_snapshot(snapshot, pipeline_rows)` — updates the last-week snapshot table after a full pipeline run

---

## Key routers

### `routers/pipeline.py`

Core analytics. Aggregates pipeline data by region, area, phase, solution, AE. Heavy use of `FILTER (WHERE ...)` SQL which is translated to `CASE WHEN` for HANA. Most endpoints require `verify_token` + module permission check.

Recent additions:
- **Finance sub-type filtering** — `_apply_fin_sub_type(where, fin_sub_type)` appends conditions to SQL WHERE clauses for `procurement_only`, `fieldglass_only`, or `mixed_finance` Finance opportunity sub-categories
- **Shadow opportunity comments** — `_ensure_shadow_comments_table()` creates `shadow_opp_comments` table; endpoints `GET /api/pipeline/bookings/shadow-comments` and `PUT /api/pipeline/bookings/shadow-comments/{closed_opp_id}/{dh_opp_id}` for tracking analyst notes on closed/matched opportunities
- **New bookings endpoints:**
  - `POST /api/pipeline/bookings/opp-lines` — opportunity line-level detail
  - `GET /api/pipeline/bookings/missed-vat` — opportunities with missing VAT coverage
  - `GET /api/pipeline/bookings/no-dh` — bookings without DH opportunity match
  - `GET /api/pipeline/single-bag-breakdown` — single-bag revenue breakdown
  - `GET /api/pipeline/filters/local-owners-by-flsm` — local owner list filtered by FLSM

### `routers/ess.py`

ESS (Email Self-Service) integration at `https://ess.int.sap`.

Key functions:
- `_build_des_html(first_name, sessions)` — builds personalized HTML block for DES email injection
- `POST /api/ess/send-des` — sends personalized DES invitations to a list of contacts
- `POST /api/ess/send-test` — sends a test email
- ESS uses session cookies stored in `ess_credentials` table (per-user, not shared)
- HTML inserted into ESS's editable block — ESS sanitizer strips `<div>`, `<b>` (with content); safe tags: `<span>`, `<br>`, `<strong>`, `<a>`

DES URL routing:
- `_DES_BASE` — base URL for Digital Discover Expert Series event site
- `_AREA_ANCHOR` — maps LAC area names (e.g., `"Brazil"`, `"South Cone"`) to URL fragment anchors for direct deep-links into the event page per area
- `_SESSION_GROUPS` — ordered list of `(group_name, {area_set})` tuples that map session names to their target areas
- `_area_url(area)` — resolves an area name to a full DES URL with anchor; used by `send-des` to embed personalized per-recipient event links

### `routers/scheduler.py`

Outlook-based meeting scheduler for QC pipeline reviews.
- Each user provides their own Outlook Bearer token in `X-Cal-Auth` header
- Falls back to SAP Portal proxy (`localhost:8766`) if no token present
- Manages quota carrier list, email map, meeting records, period config

### `routers/admin.py`

Full user lifecycle management (create, update, deactivate, delete). Role profiles. Access log viewer. SMTP welcome emails with temporary passwords. All gated by `require_admin` or `require_admin_or_manager`.

### `routers/targets.py`

SA quota/attainment tracking. Joins pipeline_exploded × targets × dh_vat. Complex aggregations for area-level and individual-level attainment.

### `routers/ae_analysis.py`

AE territory analysis. Joins pipeline × VAT × account_specs. Returns AE-level opportunity lists with solution coverage, competitor info, and install base.

Recent additions:
- **Databricks contacts fallback** — `_databricks_contacts_fallback()` queries `outreach_contacts` table when the primary contacts path returns no results; provides AE-level lead intelligence from the Databricks ETL source
- **Shadow opportunity comments** — same `shadow_opp_comments` table and `_ensure_shadow_comments_table()` used by `pipeline.py`; `ShadowComment` Pydantic model for validation
- **Finance sub-type filtering** — `_apply_fin_sub_type()` (also in `pipeline.py`) available here for Finance-specific AE analysis views

---

## Environment variables

All secrets are set in `manifest.yml` (gitignored). Never hardcode or commit.

| Variable | Purpose |
|---|---|
| `DH_DB_DRIVER` | `hana` (prod) or `postgres` (local dev) |
| `DH_DB_HOST` | HANA Cloud host or PG host |
| `DH_DB_PORT` | HANA: 443, PG: 5432 |
| `DH_DB_USER` | DB user |
| `DH_DB_PASSWORD` | DB password |
| `DH_DB_NAME` | PG only — database name |
| `JWT_SECRET` | HS256 signing key — must be set in prod |
| `JWT_TTL_HOURS` | Token lifetime in hours (default: 24) |
| `AZURE_CLIENT_ID` | Azure app registration client ID |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_CLIENT_SECRET` | Azure app secret — must be set for SSO |
| `SMTP_USER` | Office 365 SMTP user |
| `SMTP_PASSWORD` | SMTP password |
| `SMTP_HOST` | Default: smtp.office365.com |
| `SMTP_PORT` | Default: 587 |
| `DH_APP_URL` | Public app URL (used in welcome emails) |
| `PORTAL_BASE` | SAP Portal proxy base URL (default: http://localhost:8766) |
| `DATABRICKS_TOKEN` | For outreach_contacts ETL |

---

## Startup behavior

On each `cf push` / restart, `main.py` runs two `@app.on_event("startup")` handlers:
1. `run_migrations()` — checks for missing columns in `PIPELINE` and `PIPELINE_LAST_WEEK` and applies `ALTER TABLE` if needed (HANA only). Current managed columns: `phase_since_days`, `region_lvl4`, `opp_phase`, `engagement_type`, `iss_group_text`, and several others added during 2025–2026 pipeline schema evolution.
2. `_startup_ensure_account_specs()` — creates `account_specs` table if missing
3. `_startup_campaigns_migrations()` — adds `spark_sessions_json` column to `campaigns` if missing

These are idempotent — they catch exceptions silently and continue.

---

## Middleware

```python
@app.middleware("http")
async def update_last_seen(request, call_next):
```

After every authenticated request, updates `last_seen_at` in `app_users`. Throttled to once per 60 seconds per user via `_last_seen_cache` (in-memory dict, resets on restart).

---

## CORS

```python
allow_origins=["*"], allow_credentials=False
```

Wildcard origins are acceptable here because `allow_credentials=False` means no cookies/sessions are shared. Browsers will not send cross-origin credentials. Consider restricting to the known frontend origin for defense in depth.

---

## Local development setup

1. Copy `manifest.template.yml` → `manifest.yml` and fill in credentials
2. Set `DH_DB_DRIVER=postgres` in `.env` for local PG
3. `pip install -r requirements.txt`
4. `uvicorn main:app --reload --port 8000`

---

## Deployment

```bash
cf login -a https://api.cf.br10.hana.ondemand.com -o presalesbtplac -s DSA
cf push dh-api
```

`manifest.yml` sets: buildpack, memory (512M), disk (1G), routes, and all env vars.  
The app runs on Python buildpack with `uvicorn main:app --host 0.0.0.0 --port $PORT`.

---

## Known technical debt

1. **No connection pooling** — one HANA connection per query. Works at current load; will break under heavy concurrency.
2. **`_adapt_sql()` complexity** — 300+ lines of regex-based PG→HANA translation. Edge cases possible in complex nested queries. Tested empirically; not formally verified.
3. **Debug endpoints** — 14 endpoints in production that should be removed or locked to admin (see SECURITY_REVIEW.md).
4. **`os.chdir()` in ETL thread** — safe for sequential ETL, breaks under concurrent runs.
5. **`users.json` legacy path** — hardcodes admin role; should be removed once confirmed unused.

---

## Files that must never be committed

```
manifest.yml              # DB password, JWT secret, Azure client secret
.env / .env.*             # Local env overrides
dh_backup_*.sql           # Database dumps
migrate_pg_to_hana.py     # Migration script with embedded credentials
_upload_*.xlsx / *.csv    # Temp ETL upload files
```

All of the above are in `.gitignore`.
