"""
outlook_inbox.py — Outlook flagged emails via Outlook REST API v2.
Uses outlook.office.com/api/v2.0 — mesmo token do bookmarklet usado pelo calendar.py.
"""
from __future__ import annotations
from typing import Optional
import logging
import requests
from fastapi import APIRouter, Depends, Header, HTTPException
from auth import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/outlook", tags=["outlook"])

_BASE = "https://outlook.office.com/api/v2.0"
_MSG_SELECT = "Id,Subject,From,ReceivedDateTime,BodyPreview,IsRead,WebLink,Importance,HasAttachments,Flag"
_MSG_FILTER = "Flag/FlagStatus eq 'Flagged'"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _require_token(x_cal_auth: Optional[str]) -> str:
    if not x_cal_auth or len(x_cal_auth) < 50:
        raise HTTPException(status_code=401, detail="Outlook token required.")
    return x_cal_auth


def _normalize(m: dict, folder_name: str = "") -> dict:
    sender = m.get("From", {}).get("EmailAddress", {})
    return {
        "id":              m.get("Id", ""),
        "subject":         m.get("Subject", ""),
        "from_name":       sender.get("Name", ""),
        "from_email":      sender.get("Address", ""),
        "received":        m.get("ReceivedDateTime", ""),
        "preview":         m.get("BodyPreview", ""),
        "is_read":         m.get("IsRead", False),
        "web_link":        m.get("WebLink", ""),
        "importance":      m.get("Importance", "Normal"),
        "has_attachments": m.get("HasAttachments", False),
        "folder":          folder_name,
    }


def _get_folders(token: str) -> list[dict]:
    folders = []

    def fetch(url: str):
        try:
            r = requests.get(url, headers=_headers(token), timeout=15)
            if not r.ok:
                logger.error("MailFolders error %s: %s", r.status_code, r.text[:300])
                return
            data = r.json()
            for f in data.get("value", []):
                folders.append({"id": f["Id"], "name": f.get("DisplayName", "")})
                fetch(f"{_BASE}/me/MailFolders/{f['Id']}/ChildFolders?$top=100")
            if data.get("@odata.nextLink"):
                fetch(data["@odata.nextLink"])
        except Exception as e:
            logger.exception("Exception fetching folders: %s", e)

    fetch(f"{_BASE}/me/MailFolders?$top=100")
    return folders


def _flagged_in_folder(token: str, folder_id: str, folder_name: str) -> list[dict]:
    try:
        r = requests.get(
            f"{_BASE}/me/MailFolders/{folder_id}/Messages",
            headers=_headers(token),
            params={"$filter": _MSG_FILTER, "$select": _MSG_SELECT,
                    "$orderby": "ReceivedDateTime desc", "$top": 50},
            timeout=15,
        )
        if not r.ok:
            return []
        return [_normalize(m, folder_name) for m in r.json().get("value", [])]
    except Exception:
        return []


@router.get("/flagged")
def get_flagged_emails(
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    token = _require_token(x_cal_auth)
    folders = _get_folders(token)
    if not folders:
        # Return detail with Outlook API hint instead of generic 502
        raise HTTPException(
            status_code=502,
            detail="Could not list mail folders. Token may lack Mail.Read scope or be expired. Check API logs for details."
        )

    seen: set[str] = set()
    messages = []
    for f in folders:
        for msg in _flagged_in_folder(token, f["id"], f["name"]):
            if msg["id"] not in seen:
                seen.add(msg["id"])
                messages.append(msg)

    messages.sort(key=lambda m: m.get("received", ""), reverse=True)
    logger.info("Flagged emails: %d messages across %d folders", len(messages), len(folders))
    return {"messages": messages, "folders_searched": len(folders)}


@router.patch("/unflag/{message_id}")
def unflag_message(
    message_id: str,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Remove o flag de uma mensagem específica no Outlook."""
    token = _require_token(x_cal_auth)
    try:
        r = requests.patch(
            f"{_BASE}/me/Messages/{message_id}",
            headers=_headers(token),
            json={"Flag": {"FlagStatus": "NotFlagged"}},
            timeout=10,
        )
        if r.ok:
            return {"ok": True}
        logger.warning("Unflag failed %s: %s %s", message_id, r.status_code, r.text[:200])
        raise HTTPException(status_code=r.status_code, detail=f"Outlook unflag failed: {r.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unflag exception: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
