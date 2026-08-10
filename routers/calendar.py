"""
calendar.py — Team Agenda calendar router.
Fetches Microsoft Graph / Outlook calendar events using per-user token (X-Cal-Auth header).
No server-side token storage — each user supplies their own OAuth token from localStorage.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Optional, List
import requests

from fastapi import APIRouter, Depends, Header, HTTPException, Body
from pydantic import BaseModel
from auth import verify_token
from database import query

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_GRAPH_BASE   = "https://graph.microsoft.com/v1.0"
_OUTLOOK_BASE = "https://outlook.office.com/api/v2.0"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _require_token(x_cal_auth: Optional[str]) -> str:
    if not x_cal_auth or len(x_cal_auth) < 50:
        raise HTTPException(
            status_code=401,
            detail="Outlook token required. Click 'Conectar Outlook' to authenticate."
        )
    return x_cal_auth


def _fetch_user_events(token: str, email: str, start: str, end: str) -> list:
    """Fetch calendar events for a user via Outlook REST API."""
    url = f"{_OUTLOOK_BASE}/users/{email}/calendarview"
    params = {
        "startDateTime": f"{start}T00:00:00Z",
        "endDateTime":   f"{end}T23:59:59Z",
        "$top": 200,
        "$select": "Id,Subject,Start,End,IsAllDay,Location,BodyPreview,IsOrganizer",
        "$orderby": "Start/DateTime",
    }
    try:
        r = requests.get(url, headers=_headers(token), params=params, timeout=15)
        if not r.ok:
            return []
        return r.json().get("value", [])
    except Exception:
        return []


def _normalize_event(ev: dict, member_email: str) -> dict:
    start = ev.get("Start", {})
    end   = ev.get("End", {})
    return {
        "id":          ev.get("Id", ""),
        "subject":     ev.get("Subject", ""),
        "start":       start.get("DateTime", ""),
        "end":         end.get("DateTime", ""),
        "isAllDay":    ev.get("IsAllDay", False),
        "location":    ev.get("Location", {}).get("DisplayName", ""),
        "preview":     ev.get("BodyPreview", ""),
        "memberEmail": member_email,
        "isLocal":     False,
    }


@router.get("/team")
def get_team(_user=Depends(verify_token)):
    """Return team members from scheduler_emails (has name+email), fallback to targets."""
    try:
        rows = query("SELECT TOP 100 name, email FROM scheduler_emails WHERE email IS NOT NULL ORDER BY name")
        if rows:
            return [{"name": r["name"], "email": r["email"] or "", "inumber": ""} for r in rows if r.get("name")]
    except Exception:
        pass
    # Fallback: targets table
    try:
        rows = query("SELECT DISTINCT nombre_empleado AS name, inumber FROM targets ORDER BY nombre_empleado")
        return [
            {"name": r["name"], "email": "", "inumber": r.get("inumber", "")}
            for r in rows
            if r.get("name") and r["name"] not in ("Nombre Empleado *", "")
        ]
    except Exception:
        return []


@router.get("/events")
def get_events(
    start: str,
    end: str,
    members: str = "all",
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Fetch calendar events — own calendar via /me, team via scheduler_emails if available."""
    token = _require_token(x_cal_auth)
    user_email = _user.get("email", "")
    all_events = []

    # Fetch user's own calendar
    url_me = f"{_OUTLOOK_BASE}/me/calendarview"
    params_me = {
        "startDateTime": f"{start}T00:00:00Z",
        "endDateTime":   f"{end}T23:59:59Z",
        "$top": 200,
        "$select": "Id,Subject,Start,End,IsAllDay,Location,BodyPreview",
        "$orderby": "Start/DateTime",
    }
    try:
        r = requests.get(url_me, headers=_headers(token), params=params_me, timeout=15)
        if r.ok:
            for ev in r.json().get("value", []):
                norm = _normalize_event(ev, user_email)
                norm["memberName"] = _user.get("name", "Me")
                norm["isMe"] = True
                all_events.append(norm)
    except Exception:
        pass

    # Fetch team member calendars if token allows
    if members != "me_only":
        try:
            email_rows = query("SELECT name, email FROM scheduler_email_map WHERE email <> '' ORDER BY name")
            email_map = {r["name"]: r["email"] for r in email_rows if r.get("email")}
        except Exception:
            email_map = {}

        for name, email in email_map.items():
            if not email or email.lower() == user_email.lower():
                continue
            evs = _fetch_user_events(token, email, start, end)
            for ev in evs:
                norm = _normalize_event(ev, email)
                norm["memberName"] = name
                all_events.append(norm)

    return {"events": all_events, "fetched_at": datetime.utcnow().isoformat()}


@router.get("/snapshot/status")
def snapshot_status(_user=Depends(verify_token)):
    """Return info about last calendar refresh — placeholder for compatibility."""
    return {"refreshed_at": None, "status": "live"}


@router.post("/snapshot/refresh")
def snapshot_refresh(
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Trigger a calendar refresh — with live API there is nothing to cache."""
    _require_token(x_cal_auth)
    return {"ok": True, "message": "Calendar uses live data — no snapshot needed."}


class InvitePayload(BaseModel):
    subject: str
    start: str       # ISO datetime e.g. 2026-07-25T10:00:00
    end: str
    body: str = ""
    location: str = ""
    attendees: List[str] = []
    is_online_meeting: bool = True
    timezone: str = "America/Sao_Paulo"


@router.post("/invite")
def create_calendar_invite(
    payload: InvitePayload,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Create a calendar event via Outlook REST API."""
    token = _require_token(x_cal_auth)

    attendees_v2 = [
        {"EmailAddress": {"Address": a.strip()}, "Type": "Required"}
        for a in payload.attendees if a.strip()
    ]

    event_payload = {
        "Subject": payload.subject,
        "Body": {"ContentType": "HTML", "Content": payload.body or payload.subject},
        "Start": {"DateTime": payload.start, "TimeZone": payload.timezone},
        "End":   {"DateTime": payload.end,   "TimeZone": payload.timezone},
        "Attendees": attendees_v2,
        "IsOnlineMeeting": payload.is_online_meeting,
        "OnlineMeetingProvider": "teamsForBusiness" if payload.is_online_meeting else None,
    }
    if payload.location:
        event_payload["Location"] = {"DisplayName": payload.location}

    try:
        r = requests.post(
            f"{_OUTLOOK_BASE}/me/events",
            headers=_headers(token),
            json=event_payload,
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])

    data = r.json()
    return {
        "ok": True,
        "subject": data.get("Subject", ""),
        "event_id": data.get("Id", ""),
        "web_link": data.get("WebLink", ""),
    }
