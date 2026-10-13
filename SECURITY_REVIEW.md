# Security Review — dh-app-api

**Reviewed:** 2026-10-13  
**Scope:** Full codebase audit (main.py, auth.py, database.py, routers/*, etl/)  
**Status:** Findings only — no code changed by this review

---

## HIGH — Fix before next deployment cycle

### 1. Debug endpoints accessible to all authenticated users

**Files:** `main.py` (lines 131–734)  
**Impact:** Any authenticated user (including `viewer` role) can trigger DDL operations and read internal diagnostics.

14 `/api/debug/*` endpoints are gated only by `verify_token` instead of `require_admin`:

| Endpoint | Risk |
|---|---|
| `GET /api/debug/columns/{table_name}` | DB schema disclosure |
| `GET /api/debug/vat-check/{opp_id}` | Pipeline + VAT data read |
| `GET /api/debug/attainment-diagnostics` | Comprehensive internal data dump (table counts, sample rows, ETL history, calc rules) |
| `GET /api/debug/spec-join-check` | account_specs + pipeline read |
| `POST /api/debug/create-calc-rules` | `CREATE TABLE` + seed data |
| `POST /api/debug/add-last-seen-column` | `ALTER TABLE app_users` |
| `POST /api/debug/create-opportunity-priorities` | `CREATE TABLE` |
| `POST /api/debug/alter-columns` | `ALTER TABLE pipeline` (8 columns) |
| `POST /api/debug/rerun-pipeline-exploded` | Triggers ETL + `os.chdir()` |
| `POST /api/debug/create-campaigns` | `CREATE TABLE campaigns` + `campaign_contacts` |
| `POST /api/debug/setup-ess` | `CREATE TABLE ess_credentials`, `ess_sends`; `ALTER TABLE campaign_contacts` |
| `POST /api/debug/create-account-specs` | `CREATE TABLE` |
| `POST /api/debug/create-account-solution-map` | `CREATE TABLE` |
| `POST /api/debug/create-innovation-table` | `CREATE TABLE` |

**Fix:** Add `_admin=Depends(require_admin)` to every debug endpoint (import `require_admin` from `routers.admin`). These endpoints were bootstrapping helpers — they served their purpose and should either be removed or locked to admin.

```python
# Before
def debug_columns(table_name: str, _user=Depends(verify_token)):

# After
from routers.admin import require_admin
def debug_columns(table_name: str, _admin=Depends(require_admin)):
```

---

### 2. ETL execution accessible to all authenticated users

**File:** `routers/etl.py` (line 439)  
**Impact:** Any authenticated user can trigger ETL jobs that overwrite production tables (pipeline, targets, VAT, melody, etc.).

`POST /api/etl/run/{source_id}` uses `verify_token` only. A viewer can upload a malformed CSV and corrupt the pipeline table.

**Fix:** Require manager or admin role:

```python
from routers.admin import require_admin_or_manager

@router.post("/run/{source_id}")
async def run_etl(
    source_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(default=[]),
    _user=Depends(require_admin_or_manager),  # was: verify_token
):
```

---

### 3. ETL file upload: no size limit

**File:** `routers/etl.py` (line 454–459)  
**Impact:** Unrestricted file size can exhaust CF instance disk (1 GB limit on BTP free tier).

**Fix:** Add size validation after reading:

```python
contents = await f.read()
if len(contents) > 50 * 1024 * 1024:  # 50 MB cap
    raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
tmp.write_bytes(contents)
```

---

## MEDIUM — Fix in the next sprint

### 4. Legacy `users.json` hardcodes `role="admin"`

**File:** `auth.py` (line 117)  
**Impact:** Any user listed in `~/.config/sap-portal/users.json` gets admin access, regardless of what's actually stored in the file. The file **exists** on the current machine (`/Users/I816087/.config/sap-portal/users.json`).

```python
# auth.py:117 — hardcoded, not read from file
return {"email": email, "name": user.get("name", email), "role": "admin", "modules": []}
```

If this migration path is still needed, read the role from the file. If not, remove `_login_legacy` entirely.

**Fix (remove the fallback):**

```python
def login(email: str, password: str) -> dict:
    email = email.strip().lower()
    user = _login_db(email, password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # ... rest unchanged
```

---

### 5. Azure SSO degrades to viewer access on DB failure

**File:** `auth.py` (lines 247–253)  
**Impact:** A transient DB error during Azure login silently grants the user `viewer` access with 3 default modules (`pipeline`, `melody`, `opportunities`). An unknown email never in the DB gets in if the DB is unreachable at login time.

```python
except Exception as ex:
    # If DB lookup fails, still allow login as viewer (graceful degradation)
    role    = "viewer"
    modules = ["pipeline", "melody", "opportunities"]
```

**Fix:** Fail hard on unexpected DB errors. Only catch `HTTPException` (account disabled):

```python
except HTTPException:
    raise
# remove the broad except Exception block
```

---

### 6. Scheduler write endpoints accessible to all users

**File:** `routers/scheduler.py` (line 74)  
**Impact:** `PUT /api/scheduler/period` — any authenticated user can modify the scheduling period config (dates, frequency, duration). All scheduler write routes use `verify_token` only.

**Fix:** Apply `require_admin_or_manager` to all `PUT`/`POST`/`DELETE` scheduler endpoints.

---

### 7. `os.chdir()` in background thread

**Files:** `routers/etl.py` (line 316), `main.py` (line 375)  
**Impact:** `_run_etl_job()` calls `os.chdir(ETL_DIR)` before running ETL scripts. `chdir` mutates the global process working directory. If two ETL jobs run concurrently, the second `chdir` call corrupts the first job's relative path resolution. Affects any code that relies on CWD.

**Fix:** Use absolute paths and `sys.path.insert` without `chdir`. Pass the ETL directory as a parameter. The `etl_dir` is already a `Path` object — use it directly:

```python
# Instead of os.chdir(ETL_DIR), keep ETL_DIR in sys.path and use absolute paths:
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))
# Remove: os.chdir(ETL_DIR)
```

This requires any ETL script that opens files with relative paths to be updated to use `Path(__file__).parent / filename` instead.

---

## LOW / INFORMATIONAL

### 8. JWT secret has a predictable default

**File:** `auth.py` (line 18)

```python
_SECRET_KEY = os.getenv("JWT_SECRET", "dh-main-app-secret-2026-change-in-prod")
```

The default is in the source code. If `JWT_SECRET` is not set in BTP CF, any token signed with the default can be forged.

**Action:** Verify `JWT_SECRET` is always present in `manifest.yml` (it is) and that the CF env var is actually applied (`cf env dh-api`). No code change needed if the env var is confirmed.

---

### 9. No rate limiting on `/api/auth/login`

**File:** `main.py` (line 92)  
An attacker can make unlimited login attempts against any email. bcrypt cost 12 slows each attempt to ~100ms, but concurrent requests are not serialized.

**Fix:** Track failed attempts per email in Redis or an in-memory dict with TTL. After 10 failures in 5 minutes, return 429. FastAPI's `slowapi` library adds this with minimal code.

---

### 10. Outlook Bearer token stored in unprotected temp file

**File:** `main.py` (lines 115–127)

```python
cache_file = os.path.join(tempfile.gettempdir(), "m365_mcp_tokens.json")
```

`/tmp/m365_mcp_tokens.json` is world-readable by default on Linux. On BTP CF the container is isolated, but the token is still unencrypted on disk.

**Fix:** Either store in the DB (encrypted column or application-level AES) or at minimum set `os.chmod(cache_file, 0o600)` after writing.

---

### 11. Welcome email sends plaintext temporary password

**File:** `routers/admin.py` (lines 244–249)  
The plaintext temp password is included in the welcome email HTML body. Email servers may log it.

**Acceptable if:** Users are forced to change password on first login. If not, enforce it.

---

### 12. No DB connection pooling

**File:** `database.py` (lines 345–361)  
Every query opens and closes a HANA connection. Under concurrent requests this will exhaust HANA's connection limit (~50 concurrent connections on standard HANA Cloud tier).

**Fix:** Use `hdbcli`'s built-in connection pool or a semaphore-limited pool:

```python
from hdbcli.connectionpool import ConnectionPool
_pool = ConnectionPool(maxsize=10, **HANA_CONFIG)
```

---

### 13. `_last_seen_cache` not thread-safe

**File:** `main.py` (line 68)  
The cache dict is read/written from multiple async request handlers without a lock. Under very high concurrency, two handlers could both decide to write `last_seen_at` in the same 60-second window. Impact is cosmetic (redundant DB writes, not data loss).

---

## Summary

| Severity | Count | Status |
|---|---|---|
| HIGH | 3 | Fix before next deploy cycle |
| MEDIUM | 4 | Fix in next sprint |
| LOW | 6 | Backlog / informational |

The most urgent fix is **#1 (debug endpoints)** — a viewer-role user can run DDL on the production HANA database today. Issues #2 (ETL trigger) and #5 (Azure SSO degradation) are also exploitable with minimal effort.
