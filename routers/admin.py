"""
Admin router — user management, permissions and access logs.
Only accessible to users with role='admin'.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, List

import bcrypt
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel

from database import query, query_one, execute, execute_returning
from auth import verify_token

router = APIRouter(prefix="/api/admin", tags=["admin"])

ALL_MODULES = [
    "pipeline", "melody", "erp_team", "lobs_team", "melody_requests",
    "etl", "scheduler", "yoy", "qualification", "lac_results",
    "opportunities", "feedback", "actions", "team_agenda", "idea_zone", "admin",
]

# Predefined role profiles with default module sets
ROLE_PROFILES = {
    "admin": ALL_MODULES,
    "erp_manager": [
        "pipeline", "melody", "erp_team", "melody_requests",
        "yoy", "qualification", "lac_results", "opportunities",
    ],
    "lob_manager": [
        "pipeline", "melody", "lobs_team", "melody_requests",
        "yoy", "qualification", "lac_results", "opportunities",
    ],
    "spoc": [
        "pipeline", "melody", "erp_team", "qualification", "opportunities",
    ],
    "spoc_lob": [
        "pipeline", "melody", "lobs_team", "qualification", "opportunities",
    ],
    "viewer": [],
    "manager": ALL_MODULES,
}


def require_admin(user=Depends(verify_token)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
def list_users(_admin=Depends(require_admin)):
    rows = query("""
        SELECT u.id, u.email, u.name, u.role, u.is_active,
               u.created_at, u.last_login_at, u.created_by,
               STRING_AGG(CASE WHEN p.module IS NOT NULL THEN p.module END, ',') AS modules
        FROM app_users u
        LEFT JOIN app_permissions p ON p.user_id = u.id AND p.can_read = true
        GROUP BY u.id, u.email, u.name, u.role, u.is_active,
                 u.created_at, u.last_login_at, u.created_by
        ORDER BY u.name
    """, array_cols=['modules'])
    result = []
    for r in rows:
        rec = dict(r)
        rec["created_at"] = str(rec["created_at"]) if rec["created_at"] else None
        rec["last_login_at"] = str(rec["last_login_at"]) if rec["last_login_at"] else None
        rec["modules"] = rec["modules"] or []
        result.append(rec)
    return result


class CreateUserRequest(BaseModel):
    email: str
    name: str
    password: str
    role: str = "viewer"
    profile: str = ""   # predefined profile key — sets modules automatically
    modules: List[str] = []


@router.post("/users")
def create_user(body: CreateUserRequest, admin=Depends(require_admin)):
    email = body.email.strip().lower()
    existing = query_one("SELECT id FROM app_users WHERE email = %s", (email,))
    if existing:
        raise HTTPException(status_code=409, detail=f"User {email} already exists")

    if body.role not in ("admin", "manager", "viewer"):
        raise HTTPException(status_code=400, detail="role must be admin, manager, or viewer")

    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(12)).decode()
    uid = execute_returning("""
        INSERT INTO app_users (email, name, password_hash, role, created_by)
        VALUES (%s, %s, %s, %s, %s)
    """, (email, body.name.strip(), pw_hash, body.role, admin["email"]))

    # Profile overrides manual module list; fallback to explicit modules or role default
    if body.profile and body.profile in ROLE_PROFILES:
        modules = ROLE_PROFILES[body.profile]
    elif body.modules:
        modules = body.modules
    else:
        modules = ALL_MODULES if body.role == "admin" else []
    can_write = body.role in ("admin", "manager")
    execute("DELETE FROM app_permissions WHERE user_id = %s", (uid,))
    for m in modules:
        execute("""
            INSERT INTO app_permissions (user_id, module, can_read, can_write, granted_by)
            VALUES (%s, %s, true, %s, %s)
        """, (uid, m, can_write, admin["email"]))

    _log(admin["email"], uid, "user_created", "admin", f"Created user {email} role={body.role}")
    return {"ok": True, "id": uid, "email": email}


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    modules: Optional[List[str]] = None


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UpdateUserRequest, admin=Depends(require_admin)):
    user = query_one("SELECT * FROM app_users WHERE id = %s", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.name is not None:
        execute("UPDATE app_users SET name = %s WHERE id = %s", (body.name.strip(), user_id))
    if body.role is not None:
        if body.role not in ("admin", "manager", "viewer"):
            raise HTTPException(status_code=400, detail="Invalid role")
        execute("UPDATE app_users SET role = %s WHERE id = %s", (body.role, user_id))
    if body.is_active is not None:
        execute("UPDATE app_users SET is_active = %s WHERE id = %s", (body.is_active, user_id))
    if body.password:
        pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(12)).decode()
        execute("UPDATE app_users SET password_hash = %s WHERE id = %s", (pw_hash, user_id))
    if body.modules is not None:
        execute("DELETE FROM app_permissions WHERE user_id = %s", (user_id,))
        can_write = (body.role or user["role"]) in ("admin", "manager")
        for m in body.modules:
            execute("""
                INSERT INTO app_permissions (user_id, module, can_read, can_write, granted_by)
                VALUES (%s, %s, true, %s, %s)
            """, (user_id, m, can_write, admin["email"]))

    _log(admin["email"], user_id, "user_updated", "admin", f"Updated user id={user_id}")
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin=Depends(require_admin)):
    user = query_one("SELECT email FROM app_users WHERE id = %s", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["email"] == admin["email"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    execute("DELETE FROM app_users WHERE id = %s", (user_id,))
    _log(admin["email"], None, "user_deleted", "admin", f"Deleted user {user['email']}")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Access logs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/logs")
def get_logs(
    limit: int = Query(default=100, le=500),
    action: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    module: Optional[str] = Query(default=None),
    _admin=Depends(require_admin),
):
    clauses, params = [], []
    if action: clauses.append("action = %s"); params.append(action)
    if email:  clauses.append("UPPER(email) LIKE UPPER(%s)"); params.append(f"%{email}%")
    if module: clauses.append("module = %s"); params.append(module)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = query(f"""
        SELECT id, email, action, module, detail, ip_address, created_at
        FROM app_access_logs {where}
        ORDER BY created_at DESC LIMIT %s
    """, params + [limit])
    return [dict(r) | {"created_at": str(r["created_at"])} for r in rows]


@router.get("/modules")
def get_modules(_admin=Depends(require_admin)):
    return ALL_MODULES


@router.get("/profiles")
def get_profiles(_admin=Depends(require_admin)):
    """Return predefined role profiles with their default module sets."""
    return [
        {"key": "admin",       "label": "Admin",        "description": "Full access to all modules", "modules": ROLE_PROFILES["admin"]},
        {"key": "erp_manager", "label": "ERP Manager",  "description": "ADRM Pipeline, Melody, ERP Team, MR, YoY, Qualification, LAC Results, Opp List", "modules": ROLE_PROFILES["erp_manager"]},
        {"key": "lob_manager", "label": "LoB Manager",  "description": "ADRM Pipeline, Melody, LoB Team, MR, YoY, Qualification, LAC Results, Opp List", "modules": ROLE_PROFILES["lob_manager"]},
        {"key": "spoc",        "label": "SPOC",         "description": "ADRM Pipeline, Melody, ERP Team, Qualification, Opp List", "modules": ROLE_PROFILES["spoc"]},
        {"key": "spoc_lob",    "label": "SPOC LoB",     "description": "ADRM Pipeline, Melody, LoB Team, Qualification, Opp List", "modules": ROLE_PROFILES["spoc_lob"]},
        {"key": "viewer",      "label": "Viewer",       "description": "No default modules — customize manually", "modules": []},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(email: str, user_id: Optional[int], action: str, module: str, detail: str = ""):
    try:
        execute("""
            INSERT INTO app_access_logs (user_id, email, action, module, detail)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, email, action, module, detail))
    except Exception:
        pass
