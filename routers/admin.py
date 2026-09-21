"""
Admin router — user management, permissions and access logs.
Only accessible to users with role='admin'.
"""
from __future__ import annotations
import logging
import os
import secrets
import smtplib
import string
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, List

import bcrypt
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel

from database import query, query_one, execute, execute_returning
from auth import verify_token

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

MODULE_LABELS = {
    "pipeline":        "ADRM Pipeline",
    "melody":          "Melody Analysis",
    "erp_team":        "ERP Team",
    "lobs_team":       "LoBs Team",
    "melody_requests": "Melody Requests",
    "etl":             "Atualização de Dados",
    "scheduler":       "Pipeline Scheduler",
    "yoy":             "2025 vs 2026",
    "qualification":   "Deal Qualification",
    "lac_results":     "LAC Results",
    "opportunities":   "Opportunity List",
    "feedback":        "Feedback 1:1",
    "actions":         "Actions",
    "team_agenda":     "Team Agenda",
    "idea_zone":       "IdeaZone",
    "ae_analysis":     "AE Territory Analysis",
    "campaigns":       "Campanhas",
    "admin":           "Administration",
}

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


def require_admin_or_manager(user=Depends(verify_token)):
    if user.get("role") not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin or Manager access required")
    return user


@router.get("/active-users")
def get_active_users(user=Depends(require_admin_or_manager)):
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = query("""
        SELECT email, name, role, last_seen_at, last_login_at
        FROM app_users
        WHERE is_active = true
          AND last_seen_at > %s
          AND email != %s
        ORDER BY last_seen_at DESC
    """, (cutoff, user["email"]))
    result = []
    for r in rows:
        rec = dict(r)
        rec["last_seen_at"] = str(rec["last_seen_at"]) if rec["last_seen_at"] else None
        rec["last_login_at"] = str(rec["last_login_at"]) if rec["last_login_at"] else None
        result.append(rec)
    return result


def require_admin(user=Depends(verify_token)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _gen_temp_password() -> str:
    chars = string.ascii_letters + string.digits
    return "Dh" + "".join(secrets.choice(chars) for _ in range(8)) + "!"


def _send_welcome_email(to: str, name: str, modules: list, temp_password: str, cc: str = "") -> None:
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    smtp_host = os.getenv("SMTP_HOST", "smtp.office365.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    app_url = os.getenv("DH_APP_URL", "https://dh-app.cfapps.us10-002.hana.ondemand.com")

    if not smtp_user or not smtp_pass:
        log.warning("SMTP not configured — skipping welcome email for %s", to)
        return

    module_items = "".join(
        f'<li style="line-height:2;">{MODULE_LABELS.get(m, m)}</li>'
        for m in modules
    )
    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:0 auto;">
      <div style="background:#0070F2;padding:24px 32px;">
        <h2 style="color:white;margin:0;font-size:22px;">DH Portal — Acesso liberado</h2>
      </div>
      <div style="padding:32px;">
        <p>Olá, <strong>{name}</strong>,</p>
        <p>Você agora tem acesso ao <strong>DH Portal</strong>, a plataforma do time de Digital HUB LAC.</p>
        <h3 style="color:#0070F2;margin-top:24px;">Suas credenciais</h3>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
          <tr><td style="padding:8px 0;color:#555;width:140px;">Portal</td>
              <td><a href="{app_url}" style="color:#0070F2;">{app_url}</a></td></tr>
          <tr><td style="padding:8px 0;color:#555;">Usuário</td>
              <td><strong>{to}</strong></td></tr>
          <tr><td style="padding:8px 0;color:#555;">Senha temporária</td>
              <td><code style="background:#f4f4f4;padding:3px 10px;border-radius:4px;font-size:14px;">{temp_password}</code></td></tr>
        </table>
        <p style="font-size:12px;color:#888;margin-top:4px;">Recomendamos alterar sua senha no primeiro acesso.</p>
        <h3 style="color:#0070F2;margin-top:24px;">Funcionalidades habilitadas</h3>
        <ul style="margin:0;padding-left:20px;">{module_items}</ul>
        <p style="margin-top:32px;font-size:13px;color:#555;">
          Em caso de dúvidas, entre em contato com o administrador do portal.
        </p>
      </div>
      <div style="background:#f4f4f4;padding:16px 32px;font-size:11px;color:#999;text-align:center;">
        SAP Digital HUB LAC &middot; Portal interno &middot; Não responda este e-mail
      </div>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Acesso ao DH Portal — bem-vindo(a), {name}"
    msg["From"] = smtp_user
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(html, "html", "utf-8"))

    recipients = [to] + ([cc] if cc else [])
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
        s.ehlo()
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, recipients, msg.as_string())


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
    profile: str = ""
    modules: List[str] = []
    send_email: bool = False
    cc_email: str = ""


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

    if body.send_email:
        try:
            _send_welcome_email(email, body.name.strip(), modules, body.password, body.cc_email)
        except Exception as e:
            log.warning("Welcome email failed for %s: %s", email, e)

    return {"ok": True, "id": uid, "email": email}


class BulkCreateItem(BaseModel):
    email: str
    name: str


class BulkCreateRequest(BaseModel):
    users: List[BulkCreateItem]
    profile: str = ""
    role: str = "viewer"
    modules: List[str] = []
    send_email: bool = True
    cc_email: str = ""


@router.post("/users/bulk")
def bulk_create_users(body: BulkCreateRequest, admin=Depends(require_admin)):
    if body.profile and body.profile in ROLE_PROFILES:
        modules = ROLE_PROFILES[body.profile]
    elif body.modules:
        modules = body.modules
    else:
        modules = []

    role = body.role
    can_write = role in ("admin", "manager")
    created, failed = [], []

    for u in body.users:
        email = u.email.strip().lower()
        name = u.name.strip()
        if not email or not name:
            failed.append({"email": email or "(blank)", "reason": "Nome e email são obrigatórios"})
            continue
        if query_one("SELECT id FROM app_users WHERE email = %s", (email,)):
            failed.append({"email": email, "reason": "Usuário já existe"})
            continue

        temp_pw = _gen_temp_password()
        pw_hash = bcrypt.hashpw(temp_pw.encode(), bcrypt.gensalt(12)).decode()
        try:
            uid = execute_returning(
                "INSERT INTO app_users (email, name, password_hash, role, created_by) VALUES (%s, %s, %s, %s, %s)",
                (email, name, pw_hash, role, admin["email"]),
            )
            execute("DELETE FROM app_permissions WHERE user_id = %s", (uid,))
            for m in modules:
                execute(
                    "INSERT INTO app_permissions (user_id, module, can_read, can_write, granted_by) VALUES (%s, %s, true, %s, %s)",
                    (uid, m, can_write, admin["email"]),
                )
            _log(admin["email"], uid, "user_created", "admin", f"Bulk created {email} role={role}")

            if body.send_email:
                try:
                    _send_welcome_email(email, name, modules, temp_pw, body.cc_email)
                except Exception as mail_err:
                    log.warning("Welcome email failed for %s: %s", email, mail_err)

            created.append({"email": email, "name": name, "id": uid})
        except Exception as e:
            failed.append({"email": email, "reason": str(e)})

    return {"ok": True, "created": created, "failed": failed}


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
