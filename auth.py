"""
auth.py — JWT auth for FastAPI.
Login validates against app_users table in PostgreSQL.
Falls back to legacy users.json for backwards compatibility during migration.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

_USERS_FILE = Path.home() / ".config" / "sap-portal" / "users.json"
_SECRET_KEY = os.getenv("JWT_SECRET", "dh-main-app-secret-2026-change-in-prod")
_ALGORITHM  = "HS256"
_TOKEN_TTL  = int(os.getenv("JWT_TTL_HOURS", "24"))

security = HTTPBearer()


def create_token(email: str, name: str, role: str = "viewer", modules: list = None) -> str:
    payload = {
        "sub":     email,
        "name":    name,
        "role":    role,
        "modules": modules or [],
        "exp":     datetime.now(timezone.utc) + timedelta(hours=_TOKEN_TTL),
        "iat":     datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_token_email(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload.get("sub")
    except Exception:
        return None


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        payload = jwt.decode(credentials.credentials, _SECRET_KEY, algorithms=[_ALGORITHM])
        return {
            "email":   payload["sub"],
            "name":    payload.get("name", ""),
            "role":    payload.get("role", "viewer"),
            "modules": payload.get("modules", []),
        }
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _login_db(email: str, password: str) -> Optional[dict]:
    """Try login against PostgreSQL app_users table."""
    try:
        from database import query_one, execute
        user = query_one(
            "SELECT id, email, name, password_hash, role, is_active FROM app_users WHERE email = %s",
            (email,)
        )
        if not user:
            return None
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="Account disabled. Contact your administrator.")
        ok = bcrypt.checkpw(password.encode(), user["password_hash"].encode())
        if not ok:
            return None

        # Fetch granted modules
        perms = query_one(
            "SELECT array_agg(module ORDER BY module) AS modules FROM app_permissions WHERE user_id = %s AND can_read = true",
            (user["id"],), array_cols=["modules"]
        )
        modules = (perms["modules"] or []) if perms else []
        # Ensure modules is always a list (HANA may return CSV string)
        if isinstance(modules, str):
            modules = [m.strip() for m in modules.split(',') if m.strip()]

        # Update last_login_at
        execute("UPDATE app_users SET last_login_at = now() WHERE id = %s", (user["id"],))

        # Log access
        try:
            execute("""
                INSERT INTO app_access_logs (user_id, email, action, module, detail)
                VALUES (%s, %s, 'login', 'auth', 'Login successful')
            """, (user["id"], email))
        except Exception:
            pass

        return {"email": email, "name": user["name"], "role": user["role"], "modules": modules}
    except HTTPException:
        raise
    except Exception:
        return None


def _login_legacy(email: str, password: str) -> Optional[dict]:
    """Fallback: validate against users.json for backwards compatibility."""
    try:
        users = json.loads(_USERS_FILE.read_text())
        user = users.get(email)
        if not user:
            return None
        ok = bcrypt.checkpw(password.encode(), user["password_hash"].encode())
        if not ok:
            return None
        return {"email": email, "name": user.get("name", email), "role": "admin", "modules": []}
    except Exception:
        return None


def login(email: str, password: str) -> dict:
    """Validate credentials. Tries PostgreSQL app_users first, then legacy users.json."""
    email = email.strip().lower()

    user = _login_db(email, password)
    if user is None:
        user = _login_legacy(email, password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(user["email"], user["name"], user["role"], user["modules"])
    return {
        "access_token":          token,
        "token_type":            "bearer",
        "name":                  user["name"],
        "email":                 user["email"],
        "role":                  user["role"],
        "modules":               user["modules"],
        "outlook_token":         user.get("outlook_token", ""),
        "outlook_expires_in":    user.get("outlook_expires_in", 3600),
        "outlook_refresh_token": user.get("outlook_refresh_token", ""),
    }


# ── Azure AD / Microsoft SSO ──────────────────────────────────────────────────

_AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "d05149f3-407b-4029-b257-57fc36c06e59")
_AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID", "42f7676c-f455-423c-82f6-dc2d99791af7")
_AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")

def _get_azure_user(auth_code: str, redirect_uri: str) -> dict:
    """
    Exchange Azure auth code for access token, then fetch user profile.
    Returns dict with email, name, role, modules.
    """
    import requests as _req

    # 1. Exchange code for tokens
    token_url = f"https://login.microsoftonline.com/{_AZURE_TENANT_ID}/oauth2/v2.0/token"
    resp = _req.post(token_url, data={
        "grant_type":    "authorization_code",
        "client_id":     _AZURE_CLIENT_ID,
        "client_secret": _AZURE_CLIENT_SECRET,
        "code":          auth_code,
        "redirect_uri":  redirect_uri,
        "scope":         "openid profile email User.Read offline_access",
    }, timeout=15)

    if not resp.ok:
        raise HTTPException(status_code=401, detail=f"Azure token exchange failed: {resp.text[:200]}")

    tokens = resp.json()
    access_token    = tokens.get("access_token", "")
    outlook_token   = access_token   # same token works for Outlook REST API
    refresh_token   = tokens.get("refresh_token", "")
    expires_in      = tokens.get("expires_in", 3600)

    # 2. Get user profile from Microsoft Graph
    graph_resp = _req.get("https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"}, timeout=10)

    if not graph_resp.ok:
        raise HTTPException(status_code=401, detail="Failed to fetch user profile from Microsoft Graph")

    profile = graph_resp.json()
    email = (profile.get("mail") or profile.get("userPrincipalName") or "").lower().strip()
    name  = profile.get("displayName") or email.split("@")[0]

    if not email:
        raise HTTPException(status_code=401, detail="Could not determine user email from Microsoft profile")

    # 3. Look up or auto-create user in app_users
    try:
        from database import query_one, execute
        user = query_one("SELECT id, name, role, is_active FROM app_users WHERE email = %s", (email,))

        if user:
            if not user.get("is_active", True):
                raise HTTPException(status_code=403, detail="Account disabled. Contact your administrator.")
            # Update last login
            execute("UPDATE app_users SET last_login_at = now() WHERE email = %s", (email,))
            user_id = user["id"]
            role    = user["role"]
        else:
            # Auto-provision: create as viewer with basic modules
            default_modules = ["pipeline", "melody", "opportunities", "qualification"]
            import bcrypt as _bc, secrets
            pw_hash = _bc.hashpw(secrets.token_hex(32).encode(), _bc.gensalt()).decode()
            execute("""
                INSERT INTO app_users (email, name, password_hash, role, is_active)
                VALUES (%s, %s, %s, 'viewer', true)
            """, (email, name, pw_hash))
            new_user = query_one("SELECT id FROM app_users WHERE email = %s", (email,))
            user_id = new_user["id"] if new_user else None
            role    = "viewer"
            # Grant default modules
            if user_id:
                for mod in default_modules:
                    try:
                        execute("INSERT INTO app_permissions (user_id, module, can_read) VALUES (%s, %s, true)",
                                (user_id, mod))
                    except Exception:
                        pass

        # Fetch modules
        if user_id:
            perms = query_one(
                "SELECT array_agg(module ORDER BY module) AS modules FROM app_permissions WHERE user_id = %s AND can_read = true",
                (user_id,), array_cols=["modules"]
            )
            modules = (perms["modules"] or []) if perms else []
            if isinstance(modules, str):
                modules = [m.strip() for m in modules.split(',') if m.strip()]
        else:
            modules = []

        # Log
        try:
            execute("""INSERT INTO app_access_logs (user_id, email, action, module, detail)
                       VALUES (%s, %s, 'login', 'auth', 'Azure SSO login')""", (user_id, email))
        except Exception:
            pass

    except HTTPException:
        raise
    except Exception as ex:
        # If DB lookup fails, still allow login as viewer (graceful degradation)
        role    = "viewer"
        modules = ["pipeline", "melody", "opportunities"]

    return {"email": email, "name": name, "role": role, "modules": modules,
            "outlook_token": outlook_token, "outlook_expires_in": expires_in,
            "outlook_refresh_token": refresh_token}


def login_azure(auth_code: str, redirect_uri: str) -> dict:
    """Handle Azure SSO login. Returns same shape as regular login()."""
    user  = _get_azure_user(auth_code, redirect_uri)
    token = create_token(user["email"], user["name"], user["role"], user["modules"])
    return {
        "access_token":          token,
        "token_type":            "bearer",
        "name":                  user["name"],
        "email":                 user["email"],
        "role":                  user["role"],
        "modules":               user["modules"],
        "outlook_token":         user.get("outlook_token", ""),
        "outlook_expires_in":    user.get("outlook_expires_in", 3600),
        "outlook_refresh_token": user.get("outlook_refresh_token", ""),
    }
