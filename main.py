"""
dh-app-api — FastAPI backend for Digital Hub SA Main App.
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time

from auth import login, login_azure, verify_token, decode_token_email
from fastapi import Depends
from routers import pipeline, melody, targets, notes, melody_requests, spark, etl, comparison, scheduler, admin, feedback, pptx_export, actions, calendar, idea_zone, ae_analysis, outreach, outlook_inbox, outlook_bridge, campaigns, ess, pipeline_progression, partner_analysis

app = FastAPI(title="DH SA Main App API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*", "X-Outlook-Token", "Authorization", "Content-Type"],
)

@app.on_event("startup")
async def run_migrations():
    """Ensure HANA table columns added after initial schema deployment exist."""
    try:
        from database import query, execute, IS_HANA
        if not IS_HANA:
            return

        def _migrate_table(table_upper: str, pending: list):
            rows = query(
                "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = ?",
                [table_upper]
            )
            existing = {r["column_name"].lower() for r in rows}
            for col, ddl in pending:
                if col not in existing:
                    execute(ddl)
                    print(f"[migration] Applied: added {col.upper()} to {table_upper}", flush=True)
                else:
                    print(f"[migration] OK: {col.upper()} already in {table_upper}", flush=True)

        _migrate_table("MELODY", [
            ("opp_main_solution_l2", 'ALTER TABLE "MELODY" ADD ("OPP_MAIN_SOLUTION_L2" NVARCHAR(200))'),
            ("opp_main_solution_l3", 'ALTER TABLE "MELODY" ADD ("OPP_MAIN_SOLUTION_L3" NVARCHAR(200))'),
            ("opp_main_solution_l4", 'ALTER TABLE "MELODY" ADD ("OPP_MAIN_SOLUTION_L4" NVARCHAR(200))'),
        ])
        _migrate_table("PIPELINE", [
            ("phase_since_days", 'ALTER TABLE "PIPELINE" ADD ("PHASE_SINCE_DAYS" INTEGER)'),
            ("region_lvl4",      'ALTER TABLE "PIPELINE" ADD ("REGION_LVL4" NVARCHAR(100))'),
        ])
        _migrate_table("PIPELINE_LAST_WEEK", [
            ("region_lvl4",                    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("REGION_LVL4" NVARCHAR(100))'),
            ("opp_phase",                      'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("OPP_PHASE" NVARCHAR(100))'),
            ("engagement_type",                'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ENGAGEMENT_TYPE" NVARCHAR(50))'),
            ("iss_group_text",                 'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ISS_GROUP_TEXT" NVARCHAR(200))'),
            ("account_planned_archetype_text", 'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACCOUNT_PLANNED_ARCHETYPE_TEXT" NVARCHAR(200))'),
            ("deal_qualification_decision",    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("DEAL_QUALIFICATION_DECISION" NVARCHAR(100))'),
            ("flsm",                           'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("FLSM" NVARCHAR(100))'),
            ("vat_dh",                         'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_DH" NVARCHAR(2000))'),
            ("vat_full",                       'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_FULL" NVARCHAR(2000))'),
            ("vat_size",                       'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_SIZE" INTEGER)'),
            ("acv_total_opp",                  'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACV_TOTAL_OPP" DOUBLE)'),
            ("acv_per_vat",                    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACV_PER_VAT" DOUBLE)'),
        ])

    except Exception as exc:
        print(f"[migration] FAILED (non-fatal): {exc}", flush=True)


_last_seen_cache: dict[str, float] = {}  # email -> unix timestamp, throttle DB writes

@app.middleware("http")
async def update_last_seen(request: Request, call_next):
    response = await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            email = decode_token_email(auth[7:])
            if email:
                now = time.time()
                if now - _last_seen_cache.get(email, 0) > 60:
                    _last_seen_cache[email] = now
                    from database import execute
                    execute("UPDATE app_users SET last_seen_at = now() WHERE email = %s", (email,))
        except Exception:
            pass
    return response

# ── Auth endpoint ─────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login", tags=["auth"])
def auth_login(body: LoginRequest):
    return login(body.email, body.password)


class AzureCallbackRequest(BaseModel):
    code: str
    redirect_uri: str

@app.post("/api/auth/azure", tags=["auth"])
def auth_azure(body: AzureCallbackRequest):
    """Exchange Azure auth code for DH App JWT token."""
    return login_azure(body.code, body.redirect_uri)


class OutlookTokenSync(BaseModel):
    outlook_token: str
    expires_in: int = 3600

@app.post("/api/auth/sync-outlook-token", tags=["auth"])
def sync_outlook_token(body: OutlookTokenSync, _user=Depends(verify_token)):
    """Save Outlook token server-side so background services can use it."""
    import json, tempfile, time, os
    cache_file = os.path.join(tempfile.gettempdir(), "m365_mcp_tokens.json")
    try:
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        exp = time.time() + body.expires_in - 60
        scope_key = "https://outlook.office.com/Calendars.ReadWrite"
        cache[scope_key] = {"secret": body.outlook_token, "expiresOn": exp}
        with open(cache_file, "w") as f:
            json.dump(cache, f)
        return {"ok": True, "expires_in": body.expires_in}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/debug/columns/{table_name}", tags=["auth"])
def debug_columns(table_name: str, _user=Depends(verify_token)):
    from database import query
    rows = query(
        "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = ? ORDER BY POSITION",
        [table_name.upper()]
    )
    return rows


@app.get("/api/debug/vat-check/{opp_id}", tags=["auth"])
def vat_check(opp_id: str, _user=Depends(verify_token)):
    """Check dh_vat and pipeline data for a specific opportunity."""
    from database import query
    vat_rows = query(
        "SELECT sales_team_member_id, sales_team_member_name, sales_team_role FROM dh_vat WHERE opportunity_id = %s",
        [opp_id]
    )
    pipeline_rows = query(
        """SELECT opportunity_id, account_name, drm_category, opp_phase, closing_qtr,
                  snapshot_week, acv_total_opp
           FROM pipeline
           WHERE opportunity_id = %s
           ORDER BY snapshot_week DESC
           LIMIT 3""",
        [opp_id]
    )
    return {"opp_id": opp_id, "vat_rows": vat_rows, "pipeline_rows": pipeline_rows}

@app.post("/api/debug/create-calc-rules", tags=["auth"])
def create_calc_rules(_user=Depends(verify_token)):
    """Create and seed calc_rules table in HANA if missing."""
    from database import execute, query_one
    try:
        execute("""
            CREATE TABLE calc_rules (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                area NVARCHAR(100),
                solution_l1 NVARCHAR(200),
                solution_l2 NVARCHAR(200),
                divide_by_vat BOOLEAN,
                description NVARCHAR(500),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception:
        pass  # already exists

    existing = query_one("SELECT COUNT(*) AS n FROM calc_rules")
    if existing and existing["n"] > 0:
        return {"ok": True, "msg": "already seeded", "rows": existing["n"]}

    rows = [
        ('ERP Cloud BR',   None,                         None,                True,  'All lines ÷ VAT_Size'),
        ('ERP Cloud MCLA', None,                         None,                True,  'All lines ÷ VAT_Size'),
        ('ERP MX MCLA IB', None,                         None,                True,  'All lines ÷ VAT_Size'),
        ('SCM',            'Supply Chain Management',    None,                False, 'Full value, no division'),
        ('SCM',            'Finance & Spend Management', 'Cloud ERP Finance', True,  'Value ÷ VAT_Size'),
        ('SCM',            'Cloud ERP Private',          None,                True,  'Value ÷ VAT_Size'),
    ]
    for area, sol1, sol2, div, desc in rows:
        execute(
            "INSERT INTO calc_rules (area, solution_l1, solution_l2, divide_by_vat, description) VALUES (?,?,?,?,?)",
            (area, sol1, sol2, div, desc)
        )
    return {"ok": True, "msg": "created and seeded", "rows": len(rows)}

@app.post("/api/debug/add-last-seen-column", tags=["auth"])
def add_last_seen_column(_user=Depends(verify_token)):
    from database import execute
    try:
        execute("ALTER TABLE app_users ADD (last_seen_at TIMESTAMP)")
    except Exception:
        pass  # column already exists
    return {"ok": True}


@app.post("/api/debug/create-opportunity-priorities", tags=["auth"])
def create_opportunity_priorities(_user=Depends(verify_token)):
    """Create opportunity_priorities table for Progression Framework data."""
    from database import execute, IS_HANA
    try:
        if IS_HANA:
            execute("""
                CREATE TABLE opportunity_priorities (
                    opportunity_id NVARCHAR(50) NOT NULL PRIMARY KEY,
                    priority_t1    INTEGER,
                    priority       INTEGER,
                    priority_adj   INTEGER,
                    probability_pct DECIMAL(5,2)
                )
            """)
        else:
            execute("""
                CREATE TABLE IF NOT EXISTS opportunity_priorities (
                    opportunity_id TEXT NOT NULL PRIMARY KEY,
                    priority_t1    INTEGER,
                    priority       INTEGER,
                    priority_adj   INTEGER,
                    probability_pct NUMERIC(5,2)
                )
            """)
        return {"ok": True, "msg": "table created"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/debug/alter-columns", tags=["auth"])
def alter_columns(_user=Depends(verify_token)):
    """Widen text columns in PIPELINE table that are too narrow for real data."""
    from database import execute
    alters = [
        "ALTER TABLE pipeline ALTER (vat_dh NVARCHAR(2000))",
        "ALTER TABLE pipeline ALTER (vat_full NVARCHAR(2000))",
        "ALTER TABLE pipeline ALTER (opp_description NVARCHAR(2000))",
        "ALTER TABLE pipeline ALTER (account_name NVARCHAR(500))",
        "ALTER TABLE pipeline ALTER (opp_owner_name NVARCHAR(500))",
        "ALTER TABLE pipeline ALTER (flsm NVARCHAR(500))",
        "ALTER TABLE pipeline ALTER (local_acct_owner NVARCHAR(500))",
        "ALTER TABLE pipeline ALTER (channel_partner NVARCHAR(2000))",
    ]
    done = []
    errors = []
    for sql in alters:
        try:
            execute(sql)
            done.append(sql)
        except Exception as e:
            errors.append({"sql": sql, "error": str(e)})
    return {"done": len(done), "errors": errors}

@app.get("/api/data-status", tags=["auth"])
def data_status(_user=Depends(verify_token)):
    """Return the latest snapshot_week from pipeline and melody table row counts."""
    from database import query_one
    pipeline = query_one("SELECT MAX(snapshot_week) AS last_week FROM pipeline")
    melody   = query_one("SELECT MAX(activ_start_date_utc) AS last_activity FROM melody")
    return {
        "pipeline_snapshot": str(pipeline["last_week"])[:10] if pipeline and pipeline["last_week"] else None,
        "melody_last_activity": str(melody["last_activity"])[:10] if melody and melody["last_activity"] else None,
    }


@app.get("/api/debug/attainment-diagnostics", tags=["auth"])
def attainment_diagnostics(_user=Depends(verify_token)):
    """Diagnose why attainment pages may show zeros. Checks key table states."""
    from database import query_one, query

    def safe_one(sql, params=None):
        try:
            return query_one(sql, params)
        except Exception as e:
            return {"error": str(e)}

    def safe_many(sql, params=None):
        try:
            return query(sql, params)
        except Exception as e:
            return [{"error": str(e)}]

    pipeline_snap   = safe_one("SELECT MAX(snapshot_week) AS v, COUNT(*) AS n FROM pipeline")
    pe_snap         = safe_one("SELECT MAX(snapshot_week) AS v, COUNT(*) AS n FROM pipeline_exploded")
    targets_count   = safe_one("SELECT COUNT(*) AS n FROM targets")
    vat_count       = safe_one("SELECT COUNT(*) AS n FROM dh_vat")
    calc_rules      = safe_one("SELECT COUNT(*) AS n FROM calc_rules")
    fx_rate         = safe_one("SELECT rate FROM fx_rates WHERE from_currency='USD' AND to_currency='EUR' ORDER BY rate_date DESC LIMIT 1")

    try:
        mgr_targets = query("SELECT area_label, quota_eur, quota_q1, quota_q2, quota_q3, quota_q4 FROM manager_targets")
    except Exception as e:
        mgr_targets = [{"error": str(e)}]

    targets_sample = safe_many("SELECT inumber, nombre_empleado, area, sub_team, acv_booked_annual_target FROM targets ORDER BY area LIMIT 10")

    # How many rows exist in pipeline for the max snapshot specifically
    p_week  = str(pipeline_snap.get("v") or "")[:10] if pipeline_snap else None
    pe_week = str(pe_snap.get("v") or "")[:10] if pe_snap else None
    weeks_in_sync = p_week == pe_week and p_week is not None

    latest_pipeline_rows = safe_one(
        "SELECT COUNT(*) AS n FROM pipeline WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)"
    )

    # How many opportunity IDs in the latest pipeline snapshot have a matching SA in dh_vat+targets
    sa_opp_matches = safe_one("""
        SELECT COUNT(DISTINCT p.opportunity_id) AS n
        FROM pipeline p
        JOIN dh_vat v ON v.opportunity_id = p.opportunity_id
        JOIN targets t ON t.inumber = v.sales_team_member_id
        WHERE p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND (t.area IN ('ERP Cloud BR','ERP Cloud MCLA','ERP MX MCLA IB')
               OR t.sub_team = 'SCM')
    """)

    # Distinct area values in targets (to detect unexpected values like manager names)
    targets_areas = safe_many("SELECT area, COUNT(*) AS n FROM targets GROUP BY area ORDER BY n DESC")

    # LoBs SA check
    lobs_sa_count = safe_one("SELECT COUNT(*) AS n FROM targets WHERE area = 'LoBs Team'")
    lobs_sub_teams = safe_many(
        "SELECT sub_team, COUNT(*) AS n FROM targets WHERE area = 'LoBs Team' GROUP BY sub_team"
    )

    # Full calc_rules contents (critical for pipeline_exploded logic)
    calc_rules_rows = safe_many("SELECT id, area, solution_l1, solution_l2, divide_by_vat FROM calc_rules ORDER BY id")

    # Recent ETL job history for pipeline source
    etl_recent = safe_many("""
        SELECT job_id, source_id, source_label, status,
               started_at, finished_at, rows_affected, error_msg, log_text
        FROM etl_job_history
        WHERE source_id = 'pipeline'
        ORDER BY started_at DESC
        LIMIT 5
    """)

    return {
        "pipeline":             {"max_snapshot_week": p_week, "row_count": pipeline_snap.get("n"),
                                 "latest_snapshot_rows": latest_pipeline_rows.get("n") if latest_pipeline_rows else None},
        "pipeline_exploded":    {"max_snapshot_week": pe_week, "row_count": pe_snap.get("n")},
        "snapshot_weeks_in_sync": weeks_in_sync,
        "sa_opp_matches_in_latest_pipeline": sa_opp_matches.get("n") if sa_opp_matches else None,
        "targets":              {"row_count": targets_count.get("n") if targets_count else None,
                                 "area_distribution": targets_areas,
                                 "lobs_sa_count": lobs_sa_count.get("n") if lobs_sa_count else None,
                                 "lobs_sub_teams": lobs_sub_teams},
        "dh_vat":               {"row_count": vat_count.get("n") if vat_count else None},
        "calc_rules":           {"row_count": calc_rules.get("n") if calc_rules else None,
                                 "rows": calc_rules_rows},
        "fx_rate_usd_eur":      fx_rate.get("rate") if fx_rate and "error" not in fx_rate else fx_rate,
        "manager_targets":      mgr_targets,
        "targets_sample":       targets_sample,
        "recent_pipeline_etl_jobs": etl_recent,
    }


@app.post("/api/debug/rerun-pipeline-exploded", tags=["auth"])
def rerun_pipeline_exploded(_user=Depends(verify_token)):
    """Re-run etl_pipeline_exploded against already-loaded pipeline data. Safe to call without re-uploading CSV."""
    import sys, os
    from pathlib import Path
    etl_dir = Path(__file__).parent / "etl"
    if str(etl_dir) not in sys.path:
        sys.path.insert(0, str(etl_dir))
    os.chdir(etl_dir)
    import importlib
    import etl_pipeline_exploded
    importlib.reload(etl_pipeline_exploded)
    rows = etl_pipeline_exploded.run()
    return {"ok": True, "rows_inserted": rows}


@app.post("/api/debug/setup-past-pipeline", tags=["auth"])
def setup_past_pipeline(_user=Depends(verify_token)):
    """Create past_pipeline table (if not exists) and populate an initial snapshot from current pipeline data."""
    import sys, os
    from pathlib import Path
    etl_dir = Path(__file__).parent / "etl"
    if str(etl_dir) not in sys.path:
        sys.path.insert(0, str(etl_dir))
    os.chdir(etl_dir)
    import importlib
    import etl_past_pipeline
    importlib.reload(etl_past_pipeline)
    etl_past_pipeline._ensure_table()
    n = etl_past_pipeline.run()
    # Invalidate cached table check in pipeline_progression router
    from routers import pipeline_progression
    pipeline_progression._TABLE_EXISTS = None
    return {"ok": True, "rows_inserted": n}


@app.get("/api/auth/me", tags=["auth"])
def auth_me(current_user=Depends(verify_token)):
    """Return fresh user profile + permissions from DB (bypasses stale localStorage)."""
    from database import query_one
    user = query_one(
        "SELECT id, email, name, role FROM app_users WHERE email = %s AND is_active = true",
        (current_user["email"],)
    )
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="User not found or inactive")
    perms = query_one(
        "SELECT array_agg(module ORDER BY module) AS modules FROM app_permissions WHERE user_id = %s AND can_read = true",
        (user["id"],), array_cols=["modules"]
    )
    modules = (perms["modules"] or []) if perms else []
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.split(',') if m.strip()]
    return {"email": user["email"], "name": user["name"], "role": user["role"], "modules": modules}

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(pipeline.router)
app.include_router(melody.router)
app.include_router(targets.router)
app.include_router(notes.router)
app.include_router(melody_requests.router)
app.include_router(spark.router)
app.include_router(etl.router)
app.include_router(comparison.router)
app.include_router(scheduler.router)
app.include_router(admin.router)
app.include_router(feedback.router)
app.include_router(pptx_export.router)
app.include_router(actions.router)
app.include_router(calendar.router)
app.include_router(idea_zone.router)
app.include_router(ae_analysis.router)
app.include_router(outreach.router)
app.include_router(outlook_inbox.router)
app.include_router(outlook_bridge.router)
app.include_router(campaigns.router)
app.include_router(ess.router)
app.include_router(pipeline_progression.router)
app.include_router(partner_analysis.router)

@app.post("/api/debug/create-campaigns", tags=["auth"])
def create_campaigns_tables(_user=Depends(verify_token)):
    """Create campaigns and campaign_contacts tables."""
    from database import execute, IS_HANA
    results = {}
    if IS_HANA:
        for table, ddl in [
            ("campaigns", """
                CREATE TABLE campaigns (
                    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    name         NVARCHAR(200) NOT NULL,
                    owner        NVARCHAR(200),
                    objective    NVARCHAR(1000),
                    start_date   DATE,
                    end_date     DATE,
                    status       NVARCHAR(50) DEFAULT 'active',
                    results      NVARCHAR(2000),
                    created_by   NVARCHAR(200),
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """),
            ("campaign_contacts", """
                CREATE TABLE campaign_contacts (
                    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    campaign_id  INTEGER NOT NULL,
                    prospect_id  NVARCHAR(100) NOT NULL,
                    contact_name NVARCHAR(300),
                    account_bp   NVARCHAR(50),
                    account_name NVARCHAR(300),
                    added_by     NVARCHAR(200),
                    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """),
        ]:
            try:
                execute(ddl)
                results[table] = "created"
            except Exception as e:
                results[table] = f"skipped: {e}"
    else:
        for table, ddl in [
            ("campaigns", """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    owner       TEXT,
                    objective   TEXT,
                    start_date  DATE,
                    end_date    DATE,
                    status      TEXT DEFAULT 'active',
                    results     TEXT,
                    created_by  TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """),
            ("campaign_contacts", """
                CREATE TABLE IF NOT EXISTS campaign_contacts (
                    id           SERIAL PRIMARY KEY,
                    campaign_id  INTEGER NOT NULL,
                    prospect_id  TEXT NOT NULL,
                    contact_name TEXT,
                    account_bp   TEXT,
                    account_name TEXT,
                    added_by     TEXT,
                    added_at     TIMESTAMP DEFAULT NOW()
                )
            """),
        ]:
            try:
                execute(ddl)
                results[table] = "created"
            except Exception as e:
                results[table] = f"skipped: {e}"
    return {"ok": True, "tables": results}


@app.post("/api/debug/setup-ess", tags=["auth"])
def setup_ess(_user=Depends(verify_token)):
    """Create ess_credentials and ess_sends tables; add contact_email to campaign_contacts."""
    from database import execute, IS_HANA
    results = {}

    # ess_credentials
    try:
        if IS_HANA:
            execute("""
                CREATE TABLE ess_credentials (
                    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_email   NVARCHAR(200) NOT NULL,
                    cookie_name  NVARCHAR(100) NOT NULL,
                    cookie_value NVARCHAR(2000) NOT NULL,
                    stored_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            execute("""
                CREATE TABLE IF NOT EXISTS ess_credentials (
                    id           SERIAL PRIMARY KEY,
                    user_email   TEXT NOT NULL,
                    cookie_name  TEXT NOT NULL,
                    cookie_value TEXT NOT NULL,
                    stored_at    TIMESTAMP DEFAULT NOW()
                )
            """)
        results["ess_credentials"] = "created"
    except Exception as e:
        results["ess_credentials"] = f"skipped: {e}"

    # ess_sends
    try:
        if IS_HANA:
            execute("""
                CREATE TABLE ess_sends (
                    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    campaign_id   INTEGER,
                    prospect_id   NVARCHAR(100),
                    contact_name  NVARCHAR(300),
                    email         NVARCHAR(300),
                    template_id   NVARCHAR(100),
                    template_name NVARCHAR(200),
                    sent_by       NVARCHAR(200),
                    sent_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status        NVARCHAR(20),
                    error_msg     NVARCHAR(500)
                )
            """)
        else:
            execute("""
                CREATE TABLE IF NOT EXISTS ess_sends (
                    id            SERIAL PRIMARY KEY,
                    campaign_id   INTEGER,
                    prospect_id   TEXT,
                    contact_name  TEXT,
                    email         TEXT,
                    template_id   TEXT,
                    template_name TEXT,
                    sent_by       TEXT,
                    sent_at       TIMESTAMP DEFAULT NOW(),
                    status        TEXT,
                    error_msg     TEXT
                )
            """)
        results["ess_sends"] = "created"
    except Exception as e:
        results["ess_sends"] = f"skipped: {e}"

    # contact_email column on campaign_contacts
    try:
        if IS_HANA:
            execute("ALTER TABLE campaign_contacts ADD (contact_email NVARCHAR(300))")
        else:
            execute("ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS contact_email TEXT")
        results["contact_email_column"] = "added"
    except Exception as e:
        results["contact_email_column"] = f"skipped: {e}"

    return {"ok": True, "tables": results}


@app.get("/api/debug/spec-join-check", tags=["auth"])
def spec_join_check(_user=Depends(verify_token)):
    """Diagnose account_specs → pipeline JOIN. Shows sample BPs from each table and match count."""
    from database import query
    specs_sample  = query("SELECT TOP 5 account_bp FROM account_specs ORDER BY account_bp")
    pipe_sample   = query("SELECT TOP 5 account_bp FROM pipeline ORDER BY account_bp")
    match_count   = query(
        "SELECT COUNT(*) AS n FROM pipeline p "
        "WHERE EXISTS (SELECT 1 FROM account_specs s "
        "WHERE LPAD(TO_NVARCHAR(s.account_bp), 10, '0') = p.account_bp)"
    )
    specs_count   = query("SELECT COUNT(*) AS n FROM account_specs")
    return {
        "account_specs_sample": [r["account_bp"] for r in specs_sample],
        "pipeline_sample":      [r["account_bp"] for r in pipe_sample],
        "account_specs_total":  specs_count[0]["n"] if specs_count else 0,
        "pipeline_rows_with_spec_match": match_count[0]["n"] if match_count else 0,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


_ACCOUNT_SPECS_DDL = (
    'CREATE TABLE account_specs ('
    '  account_bp          NVARCHAR(50)  NOT NULL,'
    '  iae                 NVARCHAR(200),'
    '  spec_cx             NVARCHAR(200),'
    '  spec_hxm            NVARCHAR(200),'
    '  spec_procurement    NVARCHAR(200),'
    '  spec_dsc            NVARCHAR(200),'
    '  spec_btp            NVARCHAR(200),'
    '  spec_bdc            NVARCHAR(200),'
    '  spec_fin            NVARCHAR(200),'
    '  spec_pro            NVARCHAR(200),'
    '  PRIMARY KEY (account_bp)'
    ')'
)


@app.on_event("startup")
def _startup_ensure_account_specs():
    """Create account_specs table if it doesn't exist yet (ETL will populate it)."""
    try:
        from database import execute
        execute(_ACCOUNT_SPECS_DDL)
    except Exception:
        pass  # already exists


@app.on_event("startup")
def _startup_campaigns_migrations():
    """Add columns to campaigns table that may be missing in older deployments."""
    from database import execute, query_one, IS_HANA
    migrations = [
        "ALTER TABLE campaigns ADD (spark_sessions_json NCLOB DEFAULT NULL)" if True else "",
    ]
    # HANA uses ADD (col type) syntax; check if column exists first
    try:
        if IS_HANA:
            exists = query_one(
                "SELECT COUNT(*) AS cnt FROM TABLE_COLUMNS "
                "WHERE TABLE_NAME='CAMPAIGNS' AND COLUMN_NAME='SPARK_SESSIONS_JSON'"
            )
            if not exists or (exists.get("cnt") or exists.get("CNT") or 0) == 0:
                execute("ALTER TABLE campaigns ADD (spark_sessions_json NCLOB DEFAULT NULL)")
        else:
            execute("ALTER TABLE campaigns ADD COLUMN spark_sessions_json NCLOB DEFAULT NULL")
    except Exception:
        pass


@app.post("/api/debug/create-account-specs", tags=["auth"])
def create_account_specs(_user=Depends(verify_token)):
    """Create empty account_specs table in HANA if missing. Run ETL after to populate."""
    from database import execute
    try:
        execute(_ACCOUNT_SPECS_DDL)
        return {"ok": True, "msg": "table created — upload Mastercode to populate"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/debug/create-account-solution-map", tags=["auth"])
def create_account_solution_map(_user=Depends(verify_token)):
    """Create account_solution_map table in HANA if missing."""
    from database import execute, IS_HANA
    try:
        if IS_HANA:
            execute("""
                CREATE TABLE DBADMIN.ACCOUNT_SOLUTION_MAP (
                    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    account_bp   NVARCHAR(50)   NOT NULL,
                    account_name NVARCHAR(500),
                    solution     NVARCHAR(200)  NOT NULL,
                    competitors  NVARCHAR(2000),
                    notes        NVARCHAR(1000),
                    updated_by   NVARCHAR(200),
                    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (account_bp, solution)
                )
            """)
        else:
            execute("""
                CREATE TABLE IF NOT EXISTS account_solution_map (
                    id           SERIAL PRIMARY KEY,
                    account_bp   TEXT NOT NULL,
                    account_name TEXT,
                    solution     TEXT NOT NULL,
                    competitors  TEXT,
                    notes        TEXT,
                    updated_by   TEXT,
                    updated_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE (account_bp, solution)
                )
            """)
        return {"ok": True, "msg": "table created"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/debug/create-innovation-table", tags=["auth"])
def create_innovation_table(_user=Depends(verify_token)):
    """Create account_installed_base table for InnovationDash ETL."""
    from database import execute, IS_HANA
    try:
        if IS_HANA:
            execute("""
                CREATE TABLE DBADMIN.ACCOUNT_INSTALLED_BASE (
                    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    account_bp    NVARCHAR(50)  NOT NULL,
                    product_label NVARCHAR(300) NOT NULL,
                    solution_l1   NVARCHAR(200) NOT NULL,
                    status        NVARCHAR(50)  NOT NULL,
                    UNIQUE (account_bp, product_label)
                )
            """)
        else:
            execute("""
                CREATE TABLE IF NOT EXISTS account_installed_base (
                    id            SERIAL PRIMARY KEY,
                    account_bp    TEXT NOT NULL,
                    product_label TEXT NOT NULL,
                    solution_l1   TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    UNIQUE (account_bp, product_label)
                )
            """)
        return {"ok": True, "msg": "table created"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
