"""
dh-app-api — FastAPI backend for Digital Hub SA Main App.
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth import login, login_azure, verify_token
from fastapi import Depends
from routers import pipeline, melody, targets, notes, melody_requests, spark, etl, comparison, scheduler, admin, feedback, pptx_export, actions, calendar, idea_zone, ae_analysis, outreach, outlook_inbox, outlook_bridge

app = FastAPI(title="DH SA Main App API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*", "X-Outlook-Token", "Authorization", "Content-Type"],
)

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

@app.get("/api/health")
def health():
    return {"status": "ok"}

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
