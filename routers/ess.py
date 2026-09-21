"""
ESS (Email Self-Service) integration router.

Authentication: ESS uses Microsoft SSO cookies. The user must paste their
`EmailSelfService` session cookie (from browser DevTools) once. We store it
per user in the `ess_credentials` table and reuse it for API calls.

If the BTP server cannot reach ess.int.sap (internal SAP network), the send
endpoints return 503 and the user can fall back to the CLI script (ess_send.py)
which runs locally. Results from the CLI script are pushed back via the
/sends/record endpoint.
"""
import json as _json
import requests as _req
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from auth import verify_token
from database import execute, execute_returning, query, query_one, IS_HANA

router = APIRouter(prefix="/api/ess", tags=["ess"])

ESS_BASE = "https://ess.int.sap"
_PH = "?" if IS_HANA else "%s"

DES_TEMPLATE_ID = "ec22e094-61e0-4e46-9a58-80141bf0373b"
DES_SITE_URL = "https://digital-discover-expert-series-esp-5e2d.sites-sap.com/#home"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ess_session(user_email: str) -> _req.Session:
    row = query_one(
        f"SELECT cookie_value FROM ess_credentials WHERE user_email={_PH} AND cookie_name='EmailSelfService'",
        [user_email]
    )
    if not row:
        raise HTTPException(
            status_code=401,
            detail="ESS not connected. Store your session cookie first via POST /api/ess/cookie."
        )
    s = _req.Session()
    s.cookies.set("EmailSelfService", row["cookie_value"], domain="ess.int.sap")
    return s


def _get_campaign_spark_sessions(campaign_id: int) -> list:
    """Load the selected Spark sessions for a campaign."""
    row = query_one(
        f"SELECT spark_sessions_json FROM campaigns WHERE id={_PH}",
        [campaign_id]
    )
    if not row or not row.get("spark_sessions_json"):
        return []
    raw = row["spark_sessions_json"]
    session_ids = _json.loads(raw) if isinstance(raw, str) else raw
    if not session_ids:
        return []
    try:
        import spark_meeting as sm
        all_sessions = sm._fetch_raw()
        return [s for s in all_sessions if s.get("id") in session_ids]
    except Exception:
        return []


def _get_template_editable_block_id(sess: _req.Session, template_id: str):
    """Fetch template details and return the first editable block ID, or None."""
    try:
        r = sess.get(f"{ESS_BASE}/api/emailTemplate/{template_id}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        for key in ("editableContent", "editableBlocks", "sections", "blocks"):
            blocks = data.get(key)
            if isinstance(blocks, list) and blocks:
                first = blocks[0]
                return first.get("id") or first.get("contentId") or first.get("blockId")
        return None
    except Exception:
        return None


def _build_des_html(first_name: str, sessions: list) -> str:
    """Build personalized HTML for a DES invite email."""
    name_display = first_name or "prezado(a)"
    rows_html = ""
    for s in sessions:
        upcoming = s.get("upcoming_dates") or []
        if upcoming:
            dates_str = ", ".join(str(d)[:10] for d in upcoming[:2])
        elif s.get("next_date"):
            dates_str = str(s["next_date"])[:10]
        else:
            dates_str = "A definir"
        desc = (s.get("description") or "")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        rows_html += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;font-weight:600;color:#1a1a1a;">{s.get("title","")}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;color:#555;max-width:200px;">{desc or "—"}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;color:#555;white-space:nowrap;">{dates_str}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;color:#555;">{s.get("advisor_name") or "—"}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;">'
            f'<a href="{DES_SITE_URL}" style="color:#0070B8;font-weight:600;text-decoration:none;">Inscrever-se →</a>'
            f'</td>'
            f'</tr>'
        )
    return (
        f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#333;margin:0 0 12px;">'
        f'Olá, <strong>{name_display}</strong>,</p>'

        f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#333;line-height:1.6;margin:0 0 12px;">'
        f'É com muito prazer que convidamos você para o <strong>Discover Expert Series</strong> — uma série exclusiva de '
        f'sessões com os especialistas da SAP, criada para explorar em profundidade as soluções disponíveis para '
        f'transformar o seu negócio. Cada encontro combina visão estratégica e conhecimento técnico em um formato '
        f'prático e direto ao ponto, com espaço para perguntas e discussões.</p>'

        f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#333;margin:0 0 16px;">'
        f'Selecionamos as seguintes sessões para você:</p>'

        f'<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;margin:0 0 20px;">'
        f'<thead><tr style="background:#0070B8;color:white;">'
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;white-space:nowrap;">Sessão</th>'
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;">Descrição</th>'
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;white-space:nowrap;">Data</th>'
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;white-space:nowrap;">Especialista</th>'
        f'<th style="padding:10px 12px;text-align:left;font-weight:600;white-space:nowrap;">Inscrição</th>'
        f'</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'

        f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#333;margin:0 0 12px;">'
        f'Acesse o portal para conferir toda a agenda e se inscrever: '
        f'<a href="{DES_SITE_URL}" style="color:#0070B8;font-weight:600;">{DES_SITE_URL}</a></p>'

        f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#333;margin:0;">'
        f'Contamos com a sua presença!</p>'
    )


# ── Pydantic models ───────────────────────────────────────────────────────────

class CookieIn(BaseModel):
    email_self_service: str


class SendIn(BaseModel):
    template_id: str
    template_name: Optional[str] = None
    subject: str
    from_email: str


class RecordIn(BaseModel):
    template_id: str
    template_name: Optional[str] = None
    results: List[dict]  # [{prospect_id?, contact_name?, email, status, error_msg?}]


# ── ESS Connection ────────────────────────────────────────────────────────────

@router.post("/cookie")
def store_cookie(body: CookieIn, current_user=Depends(verify_token)):
    """Store the user's ESS session cookie. Validates by hitting ESS if reachable."""
    email = current_user.get("email")
    # Attempt validation — ignore ConnectionError (BTP may not reach ess.int.sap)
    try:
        s = _req.Session()
        s.cookies.set("EmailSelfService", body.email_self_service, domain="ess.int.sap")
        r = s.get(f"{ESS_BASE}/api/account/getCurrentUserRights", timeout=8)
        if r.status_code == 401:
            raise HTTPException(status_code=400, detail="Cookie is invalid or expired.")
    except _req.exceptions.ConnectionError:
        pass  # store it anyway — CLI script runs locally and can validate itself

    existing = query_one(
        f"SELECT id FROM ess_credentials WHERE user_email={_PH} AND cookie_name='EmailSelfService'",
        [email]
    )
    if existing:
        execute(
            f"UPDATE ess_credentials SET cookie_value={_PH}, stored_at=CURRENT_TIMESTAMP "
            f"WHERE user_email={_PH} AND cookie_name='EmailSelfService'",
            [body.email_self_service, email]
        )
    else:
        execute(
            f"INSERT INTO ess_credentials (user_email, cookie_name, cookie_value) "
            f"VALUES ({_PH},{_PH},{_PH})",
            [email, "EmailSelfService", body.email_self_service]
        )
    return {"ok": True}


@router.get("/cookie/status")
def cookie_status(current_user=Depends(verify_token)):
    row = query_one(
        f"SELECT stored_at FROM ess_credentials WHERE user_email={_PH} AND cookie_name='EmailSelfService'",
        [current_user.get("email")]
    )
    return {
        "connected": bool(row),
        "stored_at": str(row["stored_at"])[:19] if row else None,
    }


@router.delete("/cookie")
def delete_cookie(current_user=Depends(verify_token)):
    execute(
        f"DELETE FROM ess_credentials WHERE user_email={_PH}",
        [current_user.get("email")]
    )
    return {"ok": True}


# ── ESS API proxies ───────────────────────────────────────────────────────────

@router.get("/templates")
def list_templates(current_user=Depends(verify_token)):
    s = _get_ess_session(current_user.get("email"))
    try:
        r = s.get(f"{ESS_BASE}/api/emailTemplate/getList", timeout=15)
        r.raise_for_status()
        return r.json()
    except _req.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="BTP server cannot reach ess.int.sap. Enter the Template ID manually.")
    except _req.exceptions.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))


@router.get("/sender-emails")
def sender_emails(current_user=Depends(verify_token)):
    s = _get_ess_session(current_user.get("email"))
    try:
        r = s.get(f"{ESS_BASE}/api/emailTemplate/getCurrentUserEmails", timeout=15)
        r.raise_for_status()
        return r.json()
    except _req.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="BTP server cannot reach ess.int.sap.")


# ── Send emails ───────────────────────────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: int, body: SendIn, current_user=Depends(verify_token)):
    """Send ESS email to all campaign contacts that have an email address."""
    s = _get_ess_session(current_user.get("email"))

    contacts = query(
        f"SELECT prospect_id, contact_name, contact_email "
        f"FROM campaign_contacts "
        f"WHERE campaign_id={_PH} AND contact_email IS NOT NULL AND contact_email <> ''",
        [campaign_id]
    )
    if not contacts:
        raise HTTPException(status_code=400, detail="No contacts with email address in this campaign.")

    # Load Spark sessions selected for this campaign
    spark_sessions = _get_campaign_spark_sessions(campaign_id)

    # Try once to get the template's first editable block ID
    editable_block_id = None
    if spark_sessions:
        try:
            editable_block_id = _get_template_editable_block_id(s, body.template_id)
        except Exception:
            pass

    user_email = current_user.get("email")
    results = []

    for c in contacts:
        to_email = c["contact_email"]

        # Build personalized editableSendContent per contact
        editable_send_content = []
        if spark_sessions:
            first_name = (c.get("contact_name") or "").split()[0]
            html = _build_des_html(first_name, spark_sessions)
            item = {"content": html}
            if editable_block_id:
                item["id"] = editable_block_id
            editable_send_content = [item]

        payload = {
            "to": to_email,
            "from": body.from_email,
            "subjectLine": body.subject,
            "useBcc": True,
            "editableSendContent": editable_send_content,
        }
        status = "failed"
        err = ""
        try:
            resp = s.post(
                f"{ESS_BASE}/api/emailTemplate/{body.template_id}/send",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                status = "sent"
            else:
                err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except _req.exceptions.ConnectionError:
            status = "failed"
            err = "BTP server cannot reach ess.int.sap — use CLI script for local sends"
        except Exception as e:
            status = "failed"
            err = str(e)[:300]

        execute(
            f"INSERT INTO ess_sends "
            f"(campaign_id, prospect_id, contact_name, email, template_id, template_name, sent_by, status, error_msg) "
            f"VALUES ({','.join([_PH]*9)})",
            [campaign_id, c["prospect_id"], c["contact_name"], to_email,
             body.template_id, body.template_name, user_email, status, err]
        )
        results.append({"email": to_email, "status": status, "error": err})

    sent = sum(1 for r in results if r["status"] == "sent")
    failed = len(results) - sent
    return {"ok": True, "sent": sent, "failed": failed, "results": results}


# ── Send history ──────────────────────────────────────────────────────────────

@router.get("/campaigns/{campaign_id}/sends")
def list_sends(campaign_id: int, _=Depends(verify_token)):
    return query(
        f"SELECT id, prospect_id, contact_name, email, template_id, template_name, "
        f"sent_by, sent_at, status, error_msg "
        f"FROM ess_sends WHERE campaign_id={_PH} ORDER BY sent_at DESC",
        [campaign_id]
    )


@router.post("/campaigns/{campaign_id}/sends/record")
def record_sends(campaign_id: int, body: RecordIn, current_user=Depends(verify_token)):
    """Import send results from the CLI script (ess_send.py --report-to)."""
    for r in body.results:
        execute(
            f"INSERT INTO ess_sends "
            f"(campaign_id, prospect_id, contact_name, email, template_id, template_name, sent_by, status, error_msg) "
            f"VALUES ({','.join([_PH]*9)})",
            [campaign_id, r.get("prospect_id"), r.get("contact_name"), r["email"],
             body.template_id, body.template_name, current_user.get("email"),
             r.get("status", "sent"), r.get("error_msg", "")]
        )
    return {"ok": True, "inserted": len(body.results)}


@router.get("/campaigns/{campaign_id}/contact-status")
def contact_status(campaign_id: int, _=Depends(verify_token)):
    """Campaign contacts joined with their latest ESS send status."""
    try:
        contacts = query(
            f"SELECT cc.id, cc.prospect_id, cc.contact_name, cc.account_bp, "
            f"cc.account_name, cc.contact_email "
            f"FROM campaign_contacts cc WHERE cc.campaign_id={_PH}",
            [campaign_id]
        )
    except Exception:
        # contact_email column may not exist yet if setup-ess hasn't run
        contacts = query(
            f"SELECT cc.id, cc.prospect_id, cc.contact_name, cc.account_bp, "
            f"cc.account_name, NULL AS contact_email "
            f"FROM campaign_contacts cc WHERE cc.campaign_id={_PH}",
            [campaign_id]
        )

    try:
        sends = query(
            f"SELECT prospect_id, MAX(sent_at) AS last_sent_at, "
            f"MAX(CASE WHEN status='sent' THEN sent_at END) AS last_success "
            f"FROM ess_sends WHERE campaign_id={_PH} GROUP BY prospect_id",
            [campaign_id]
        )
        sends_map = {s["prospect_id"]: s for s in sends}
    except Exception:
        sends_map = {}

    result = []
    for c in contacts:
        s = sends_map.get(c["prospect_id"])
        result.append({
            **c,
            "last_sent_at": str(s["last_sent_at"])[:19] if s and s["last_sent_at"] else None,
            "last_success": str(s["last_success"])[:19] if s and s["last_success"] else None,
        })
    return result
