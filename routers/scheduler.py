"""
Pipeline Scheduler router — manages QC meeting scheduling.
Persists quota carriers, email map, meetings and period config in PostgreSQL.

Outlook integration strategy:
  - Each user stores their own Outlook Bearer token in browser localStorage.
  - The frontend sends it in the X-Cal-Auth request header.
  - The API uses that token directly — no shared server-side token needed.
  - Falls back to SAP Portal proxy (localhost:8766) if no token in header (local mode).
  This design works both locally and on SAP BTP.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional, List

import requests
from fastapi import APIRouter, HTTPException, Query, Depends, Header
from pydantic import BaseModel

from database import query, query_one, execute, execute_returning
from auth import verify_token

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

# SAP Portal base URL — fallback when no per-user token in header
_PORTAL_BASE = os.getenv("PORTAL_BASE", "http://localhost:8766")

_OUTLOOK_BASE = "https://outlook.office.com/api/v2.0/me"


def _outlook_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_outlook_token_or_raise(x_cal_auth: Optional[str]) -> Optional[str]:
    """Return the user-supplied token if present, else None (caller uses Portal fallback)."""
    if x_cal_auth and len(x_cal_auth) > 100:
        return x_cal_auth.strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Period config
# ─────────────────────────────────────────────────────────────────────────────

class PeriodConfig(BaseModel):
    start_date: str    # YYYY-MM-DD
    end_date: str
    freq_days: int
    duration_min: int
    meet_time: str     # HH:MM


@router.get("/period")
def get_period(_user=Depends(verify_token)):
    row = query_one("SELECT * FROM scheduler_period WHERE id = 1")
    if not row:
        return {"start_date": "2026-07-01", "end_date": "2026-12-10",
                "freq_days": 20, "duration_min": 60, "meet_time": "10:00"}
    return {
        "start_date":   str(row["start_date"]),
        "end_date":     str(row["end_date"]),
        "freq_days":    row["freq_days"],
        "duration_min": row["duration_min"],
        "meet_time":    str(row["meet_time"])[:5],
    }


@router.put("/period")
def save_period(body: PeriodConfig, _user=Depends(verify_token)):
    execute("""
        INSERT INTO scheduler_period (id, start_date, end_date, freq_days, duration_min, meet_time, updated_at)
        VALUES (1, %s, %s, %s, %s, %s, now())
        ON CONFLICT (id) DO UPDATE SET
            start_date  = EXCLUDED.start_date,
            end_date    = EXCLUDED.end_date,
            freq_days   = EXCLUDED.freq_days,
            duration_min= EXCLUDED.duration_min,
            meet_time   = EXCLUDED.meet_time,
            updated_at  = now()
    """, (body.start_date, body.end_date, body.freq_days, body.duration_min, body.meet_time))
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Email map
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/emails")
def get_emails(_user=Depends(verify_token)):
    rows = query("SELECT name, email FROM scheduler_email_map ORDER BY name")
    return {r["name"]: r["email"] for r in rows}


class EmailUpdate(BaseModel):
    name: str
    email: str


@router.put("/emails")
def upsert_email(body: EmailUpdate, _user=Depends(verify_token)):
    execute("""
        INSERT INTO scheduler_email_map (name, email, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (name) DO UPDATE SET email = EXCLUDED.email, updated_at = now()
    """, (body.name.strip(), body.email.strip()))
    return {"ok": True}


class EmailBulkUpdate(BaseModel):
    emails: dict  # {name: email}


@router.put("/emails/bulk")
def bulk_upsert_emails(body: EmailBulkUpdate, _user=Depends(verify_token)):
    for name, email in body.emails.items():
        execute("""
            INSERT INTO scheduler_email_map (name, email, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (name) DO UPDATE SET email = EXCLUDED.email, updated_at = now()
        """, (name.strip(), email.strip()))
    return {"ok": True, "count": len(body.emails)}


# ─────────────────────────────────────────────────────────────────────────────
# Quota Carriers
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/quota-carriers")
def get_quota_carriers(
    geo:  Optional[str] = Query(None),
    lob:  Optional[str] = Query(None),
    flsm: Optional[str] = Query(None),
    _user=Depends(verify_token),
):
    """
    Returns QC list. Enriches with email_map and current meeting states.
    Falls back to live pipeline data if no uploaded VAT data exists.
    """
    # Prefer uploaded VAT data; if empty fall back to pipeline DB
    count = query_one("SELECT COUNT(*) AS n FROM scheduler_quota_carriers")
    if count and count["n"] > 0:
        sql = "SELECT * FROM scheduler_quota_carriers WHERE 1=1"
        params: list = []
        if geo:  sql += " AND geo = %s";  params.append(geo)
        if lob:  sql += " AND lob = %s";  params.append(lob)
        if flsm: sql += " AND flsm = %s"; params.append(flsm)
        sql += " ORDER BY geo, flsm, qc"
        rows = query(sql, params or None)
    else:
        # Live from pipeline
        sql = """
            SELECT DISTINCT
                p.opp_owner_name  AS qc,
                p.flsm,
                p.region_lvl3     AS geo,
                p.solution_area_l1 AS lob,
                p.vat_dh          AS dsa_spoc,
                ''                AS sde,
                ''                AS dva,
                'pipeline_db'     AS source
            FROM pipeline p
            WHERE p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND p.opp_owner_name IS NOT NULL
              AND p.opp_owner_name NOT IN ('', 'Opp Owner Name')
              AND p.flsm IS NOT NULL AND p.flsm NOT IN ('', 'FLSM')
              AND p.engagement_type IN ('Digital', 'Field')
        """
        params2: list = []
        if geo:  sql += " AND p.region_lvl3 = %s";      params2.append(geo)
        if lob:  sql += " AND p.solution_area_l1 = %s"; params2.append(lob)
        if flsm: sql += " AND p.flsm = %s";             params2.append(flsm)
        sql += " ORDER BY p.region_lvl3, p.flsm, p.opp_owner_name"
        rows = query(sql, params2 or None)

    # Attach emails
    emails = {r["name"]: r["email"] for r in query("SELECT name, email FROM scheduler_email_map")}

    result = []
    for r in rows:
        rec = dict(r)
        rec["emails"] = {
            name: emails.get(name, "")
            for name in [rec.get("qc"), rec.get("sde"), rec.get("dsa_spoc"), rec.get("dva")]
            if name
        }
        result.append(rec)

    return {"quota_carriers": result, "total": len(result)}


@router.get("/quota-carriers/filters")
def get_qc_filters(_user=Depends(verify_token)):
    """Distinct filter values."""
    count = query_one("SELECT COUNT(*) AS n FROM scheduler_quota_carriers")
    if count and count["n"] > 0:
        geos  = query("SELECT DISTINCT geo  FROM scheduler_quota_carriers WHERE geo  IS NOT NULL ORDER BY geo")
        lobs  = query("SELECT DISTINCT lob  FROM scheduler_quota_carriers WHERE lob  IS NOT NULL ORDER BY lob")
        flsms = query("SELECT DISTINCT flsm FROM scheduler_quota_carriers WHERE flsm IS NOT NULL ORDER BY flsm")
    else:
        geos  = query("SELECT DISTINCT region_lvl3    AS geo  FROM pipeline WHERE snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline) AND region_lvl3 IS NOT NULL ORDER BY geo")
        lobs  = query("SELECT DISTINCT solution_area_l1 AS lob FROM pipeline WHERE snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline) AND solution_area_l1 IS NOT NULL ORDER BY lob")
        flsms = query("SELECT DISTINCT flsm FROM pipeline WHERE snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline) AND flsm IS NOT NULL ORDER BY flsm")

    return {
        "geos":  [r["geo"]  for r in geos  if r.get("geo")],
        "lobs":  [r["lob"]  for r in lobs  if r.get("lob")],
        "flsms": [r["flsm"] for r in flsms if r.get("flsm")],
    }


# ─────────────────────────────────────────────────────────────────────────────
# VAT file upload → populate scheduler_quota_carriers
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/quota-carriers")
def clear_quota_carriers(_user=Depends(verify_token)):
    """Delete all uploaded VAT data from scheduler_quota_carriers."""
    n = execute("DELETE FROM scheduler_quota_carriers")
    return {"ok": True, "deleted": n}


from fastapi import UploadFile, File
import io

@router.post("/upload-vat")
async def upload_vat(file: UploadFile = File(...), _user=Depends(verify_token)):
    """
    Upload Headmap/VAT Excel file → parse and store in scheduler_quota_carriers.
    Supports sheets: 'VAT Completo Combinado', 'LoBs VAT Team' (falls back to first sheet).
    """
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed")

    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)

    sheet_priority = ["VAT Completo Combinado", "LoBs VAT Team"]
    ws = None
    for name in sheet_priority:
        if name in wb.sheetnames:
            ws = wb[name]
            break
    if ws is None:
        ws = wb.worksheets[0]

    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(c).strip() if c else "" for c in next(rows_iter, [])]

    def col(row, *names):
        for n in names:
            try:
                idx = headers.index(n)
                val = row[idx]
                if val:
                    return str(val).strip().replace("  ", " ")
            except ValueError:
                pass
        return ""

    BAD = {"TBH", "TBD", "N/A", ""}
    parsed = []
    for row in rows_iter:
        qc = col(row, "Quota carrier", "Quota Carrier")
        if not qc or qc in BAD:
            continue
        geo  = col(row, "Geo")
        lob  = col(row, "LoB", "LoB ")
        flsm = col(row, "FLSM", "FLSM - Validar - ", "FLSM ")
        if not geo and not flsm:
            continue
        parsed.append({
            "qc":       qc,
            "flsm":     flsm,
            "geo":      geo,
            "lob":      lob,
            "sde":      col(row, "SDE", "HUB- SDE"),
            "dsa_spoc": col(row, "DSA- SPOC", "DSA-SPOC"),
            "dva":      col(row, "DVA"),
        })

    # Deduplicate by (qc, flsm, lob)
    seen = set()
    unique = []
    for r in parsed:
        k = (r["qc"], r["flsm"], r["lob"])
        if k not in seen:
            seen.add(k)
            unique.append(r)

    # Clear and reload
    execute("DELETE FROM scheduler_quota_carriers")
    for r in unique:
        execute("""
            INSERT INTO scheduler_quota_carriers (qc, flsm, geo, lob, sde, dsa_spoc, dva, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'vat_upload')
            ON CONFLICT (qc, flsm, lob) DO UPDATE SET
                geo = EXCLUDED.geo, sde = EXCLUDED.sde,
                dsa_spoc = EXCLUDED.dsa_spoc, dva = EXCLUDED.dva,
                source = EXCLUDED.source, created_at = now()
        """, (r["qc"], r["flsm"], r["geo"], r["lob"], r["sde"], r["dsa_spoc"], r["dva"]))

    return {"ok": True, "loaded": len(unique), "filename": file.filename}


# ─────────────────────────────────────────────────────────────────────────────
# Meetings
# ─────────────────────────────────────────────────────────────────────────────

class MeetingUpsert(BaseModel):
    qc: str
    flsm: Optional[str] = ""
    lob: Optional[str] = ""
    meeting_date: str    # YYYY-MM-DD
    status: str = "pending"
    event_id: Optional[str] = None
    web_link: Optional[str] = None


@router.get("/meetings")
def get_meetings(
    qc:   Optional[str] = Query(None),
    flsm: Optional[str] = Query(None),
    lob:  Optional[str] = Query(None),
    _user=Depends(verify_token),
):
    sql = "SELECT * FROM scheduler_meetings WHERE 1=1"
    params: list = []
    if qc:   sql += " AND qc = %s";   params.append(qc)
    if flsm: sql += " AND flsm = %s"; params.append(flsm)
    if lob:  sql += " AND lob = %s";  params.append(lob)
    sql += " ORDER BY qc, meeting_date"
    rows = query(sql, params or None)
    result = []
    for r in rows:
        rec = dict(r)
        rec["meeting_date"] = str(rec["meeting_date"])
        if rec.get("actual_date"):
            rec["actual_date"] = str(rec["actual_date"])
        # attendees_rsvp is already parsed by psycopg2 (jsonb)
        if rec.get("attendees_rsvp") is None:
            rec["attendees_rsvp"] = []
        result.append(rec)
    return result


@router.put("/meetings")
def upsert_meeting(body: MeetingUpsert, _user=Depends(verify_token)):
    execute("""
        INSERT INTO scheduler_meetings (qc, flsm, lob, meeting_date, status, event_id, web_link, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (qc, flsm, lob, meeting_date) DO UPDATE SET
            status    = EXCLUDED.status,
            event_id  = COALESCE(EXCLUDED.event_id,  scheduler_meetings.event_id),
            web_link  = COALESCE(EXCLUDED.web_link,  scheduler_meetings.web_link),
            updated_at = now()
    """, (body.qc, body.flsm or "", body.lob or "", body.meeting_date,
          body.status, body.event_id, body.web_link))
    return {"ok": True}


class MeetingsBulk(BaseModel):
    meetings: List[MeetingUpsert]


@router.put("/meetings/bulk")
def bulk_upsert_meetings(body: MeetingsBulk, _user=Depends(verify_token)):
    for m in body.meetings:
        execute("""
            INSERT INTO scheduler_meetings (qc, flsm, lob, meeting_date, status, event_id, web_link, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (qc, flsm, lob, meeting_date) DO UPDATE SET
                status    = EXCLUDED.status,
                event_id  = COALESCE(EXCLUDED.event_id,  scheduler_meetings.event_id),
                web_link  = COALESCE(EXCLUDED.web_link,  scheduler_meetings.web_link),
                updated_at = now()
        """, (m.qc, m.flsm or "", m.lob or "", m.meeting_date,
              m.status, m.event_id, m.web_link))
    return {"ok": True, "count": len(body.meetings)}


# ─────────────────────────────────────────────────────────────────────────────
# Outlook proxy — forwards to SAP Portal server (has the token)
# ─────────────────────────────────────────────────────────────────────────────

class CreateMeetingRequest(BaseModel):
    date: str
    time: str
    duration: int
    title: str
    qc: str
    lob: Optional[str] = ""
    geo: Optional[str] = ""
    flsm: Optional[str] = ""
    notes: Optional[str] = ""
    attendees: List[dict]
    draft_mode: bool = False  # True = create without attendees for review


@router.post("/create-meeting")
def create_meeting(
    body: CreateMeetingRequest,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Create Outlook calendar event. Uses per-user token from header, falls back to Portal proxy."""
    token = _get_outlook_token_or_raise(x_cal_auth)

    if token:
        # Direct Outlook API call with user's own token
        from datetime import datetime as dt, timedelta
        try:
            dt_start = dt.strptime(f"{body.date} {body.time}", "%Y-%m-%d %H:%M")
            dt_end   = dt_start + timedelta(minutes=body.duration)
        except Exception:
            dt_start = dt.now(); dt_end = dt_start + timedelta(minutes=body.duration)

        attendees_v2 = [
            {"EmailAddress": {"Address": a.get("email","").strip(), "Name": a.get("name","").strip()}, "Type": "Required"}
            for a in body.attendees if (a.get("email") or "").strip()
        ]

        # Build HTML body
        att_lines = "".join(
            f"<li><b>{a.get('role','')}</b>: {a.get('name','')} &lt;{a.get('email','')}&gt;</li>"
            for a in body.attendees
        )
        notes_html = f"<p>{body.notes}</p>" if body.notes else ""
        html_body = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px">
<p>Hi,</p>
<p>I would like to schedule an <b>Opportunity Review</b> to align on open opportunities and pre-sales support.</p>
<table style="border-collapse:collapse;margin:12px 0">
  <tr><td style="padding:4px 16px 4px 0;color:#556b82;font-weight:bold">Date</td><td>{dt_start.strftime('%A, %B %d, %Y')}</td></tr>
  <tr><td style="padding:4px 16px 4px 0;color:#556b82;font-weight:bold">Time</td><td>{dt_start.strftime('%H:%M')} – {dt_end.strftime('%H:%M')}</td></tr>
  <tr><td style="padding:4px 16px 4px 0;color:#556b82;font-weight:bold">Duration</td><td>{body.duration} min</td></tr>
  <tr><td style="padding:4px 16px 4px 0;color:#556b82;font-weight:bold">QC</td><td>{body.qc}</td></tr>
  <tr><td style="padding:4px 16px 4px 0;color:#556b82;font-weight:bold">LoB</td><td>{body.lob}</td></tr>
</table>
<p><b>Attendees:</b><ul>{att_lines}</ul></p>
<p><b>Agenda:</b></p><ul><li>Open opportunities review</li><li>Pre-sales support needed</li></ul>
{notes_html}<p>Please confirm your availability.</p><p>Best regards</p></body></html>"""

        payload = {
            "Subject": body.title,
            "Body": {"ContentType": "HTML", "Content": html_body},
            "Start": {"DateTime": dt_start.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": "America/Sao_Paulo"},
            "End":   {"DateTime": dt_end.strftime("%Y-%m-%dT%H:%M:%S"),   "TimeZone": "America/Sao_Paulo"},
            "Attendees": attendees_v2 if not body.draft_mode else [],
            "ResponseRequested": True,
        }
        r = requests.post(f"{_OUTLOOK_BASE}/events", headers=_outlook_headers(token), json=payload, timeout=25)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text[:400])
        ev = r.json()
        data = {"ok": True, "id": ev.get("Id",""), "webLink": ev.get("WebLink",""), "subject": ev.get("Subject","")}
    else:
        # Fallback: proxy through SAP Portal (local mode)
        try:
            resp = requests.post(f"{_PORTAL_BASE}/api/pipeline/create-draft", json=body.dict(), timeout=30)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Portal unreachable and no Outlook token provided: {e}")
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:400])
        data = resp.json()

    # Save to DB
    if data.get("id") and body.date:
        status = "draft" if body.draft_mode else "scheduled"
        execute("""
            INSERT INTO scheduler_meetings (qc, flsm, lob, meeting_date, status, event_id, web_link, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (qc, flsm, lob, meeting_date) DO UPDATE SET
                status   = EXCLUDED.status,
                event_id = EXCLUDED.event_id,
                web_link = EXCLUDED.web_link,
                updated_at = now()
        """, (body.qc, body.flsm or "", body.lob or "", body.date,
              status, data["id"], data.get("webLink", "")))
    return data


class ConfirmMeetingRequest(BaseModel):
    attendees: List[dict]  # [{role, name, email}]
    qc: str
    flsm: Optional[str] = ""
    lob: Optional[str] = ""
    meeting_date: str


@router.post("/confirm-meeting/{event_id:path}")
def confirm_meeting(event_id: str, body: ConfirmMeetingRequest, _user=Depends(verify_token)):
    """Send invites for a draft event and mark it as scheduled in DB."""
    try:
        resp = requests.post(
            f"{_PORTAL_BASE}/api/pipeline/event/{event_id}/send-invites",
            json={"attendees": body.attendees},
            timeout=30,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Portal unreachable: {e}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:400])

    execute("""
        UPDATE scheduler_meetings
        SET status = 'scheduled', updated_at = now()
        WHERE event_id = %s
    """, (event_id,))

    return {"ok": True, "invited": resp.json().get("invited", 0)}


@router.get("/outlook/sync")
def sync_from_outlook(
    start_date: Optional[str] = Query(None),
    end_date:   Optional[str] = Query(None),
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """
    Fetch Pipeline Review events from Outlook via user token (X-Cal-Auth),
    match them to DB records and mark as scheduled.
    """
    token = _get_outlook_token_or_raise(x_cal_auth)
    if not token:
        raise HTTPException(status_code=401, detail="Outlook token required. Click 'Conectar Outlook' to authenticate.")

    sd = start_date or "2026-01-01"
    ed = end_date   or "2026-12-31"

    # Search Outlook calendar for SA Pipeline Acceleration events
    url = f"{_OUTLOOK_BASE}/me/calendarview"
    params = {
        "startDateTime": f"{sd}T00:00:00Z",
        "endDateTime":   f"{ed}T23:59:59Z",
        "$top": 200,
        "$select": "Id,Subject,Start,End,Attendees,WebLink",
        "$filter": "contains(Subject,'SA Pipeline Acceleration') or contains(Subject,'Pipeline Review')",
    }
    try:
        resp = requests.get(url, headers=_outlook_headers(token), params=params, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Outlook unreachable: {e}")
    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])

    outlook_events = resp.json().get("value", [])
    updated, skipped, not_found = [], [], []

    for ev in outlook_events:
        subj       = ev.get("Subject", "")
        event_date = (ev.get("Start", {}).get("DateTime") or "")[:10]
        event_id   = ev.get("Id", "")
        web_link   = ev.get("WebLink", "")
        attendees  = [
            {"name": a.get("EmailAddress", {}).get("Name", ""),
             "email": a.get("EmailAddress", {}).get("Address", ""),
             "status": a.get("Status", {}).get("Response", "")}
            for a in ev.get("Attendees", [])
        ]

        # Parse QC name from "Pipeline Review — {QC} | {LoB}"
        qc_name = None
        if "—" in subj:
            after_dash = subj.split("—", 1)[1].strip()
            qc_name = after_dash.split("|")[0].strip() if "|" in after_dash else after_dash.strip()

        if not qc_name or not event_date:
            skipped.append({"subject": subj, "reason": "could not parse QC or date"})
            continue

        # First try: exact or ±3 day match on meeting_date
        rows = query("""
            SELECT id, qc, flsm, lob, meeting_date, status, event_id
            FROM scheduler_meetings
            WHERE qc = %s
              AND DAYS_BETWEEN(meeting_date, %s) BETWEEN -3 AND 3
            ORDER BY ABS(DAYS_BETWEEN(meeting_date, %s))
            LIMIT 1
        """, (qc_name, event_date, event_date, event_date))

        # Fallback: nearest pending/draft meeting for this QC in the same month
        if not rows:
            rows = query("""
                SELECT id, qc, flsm, lob, meeting_date, status, event_id
                FROM scheduler_meetings
                WHERE qc = %s
                  AND status IN ('pending', 'draft')
                  AND YEAR(meeting_date) = YEAR(%s) AND MONTH(meeting_date) = MONTH(%s)
                ORDER BY ABS(DAYS_BETWEEN(meeting_date, %s))
                LIMIT 1
            """, (qc_name, event_date, event_date))

        # Last resort: nearest pending/draft meeting for this QC overall
        if not rows:
            rows = query("""
                SELECT id, qc, flsm, lob, meeting_date, status, event_id
                FROM scheduler_meetings
                WHERE qc = %s
                  AND status IN ('pending', 'draft')
                ORDER BY ABS(DAYS_BETWEEN(meeting_date, %s))
                LIMIT 1
            """, (qc_name, event_date))

        if not rows:
            not_found.append({"subject": subj, "date": event_date, "qc": qc_name})
            continue

        for row in rows:
            execute("""
                UPDATE scheduler_meetings
                SET status          = 'scheduled',
                    event_id        = %s,
                    web_link        = %s,
                    actual_date     = %s,
                    actual_time     = %s,
                    attendees_rsvp  = %s,
                    updated_at      = now()
                WHERE id = %s
            """, (event_id, web_link,
                  event_date,
                  (ev.get("Start", {}).get("DateTime") or "")[11:16],
                  __import__('json').dumps(attendees),
                  row["id"]))
            updated.append({
                "qc":              row["qc"],
                "lob":             row["lob"],
                "meeting_date":    str(row["meeting_date"]),
                "actual_date":     event_date,
                "actual_time":     (ev.get("Start", {}).get("DateTime") or "")[11:16],
                "previous_status": row["status"],
                "subject":         subj,
                "attendees":       attendees,
            })

    return {
        "ok": True,
        "outlook_events_found": len(outlook_events),
        "updated": updated,
        "not_found_in_db": not_found,
        "skipped": skipped,
    }


@router.get("/event/{event_id:path}")
def get_event(
    event_id: str,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Fetch Outlook event with attendee responses."""
    token = _get_outlook_token_or_raise(x_cal_auth)
    if token:
        r = requests.get(
            f"{_OUTLOOK_BASE}/events/{event_id}?$select=Subject,Start,End,Attendees,WebLink,IsCancelled",
            headers=_outlook_headers(token), timeout=20,
        )
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        ev = r.json()
        attendees = []
        for a in (ev.get("Attendees") or []):
            addr   = a.get("EmailAddress") or {}
            status = a.get("Status") or {}
            attendees.append({
                "name": addr.get("Name",""), "email": addr.get("Address",""),
                "type": a.get("Type",""), "response": status.get("Response","None"),
                "time": status.get("Time",""),
            })
        return {
            "id": ev.get("Id",""), "subject": ev.get("Subject",""),
            "start": (ev.get("Start") or {}).get("DateTime",""),
            "end":   (ev.get("End")   or {}).get("DateTime",""),
            "webLink": ev.get("WebLink",""), "isCancelled": ev.get("IsCancelled", False),
            "attendees": attendees,
        }
    else:
        try:
            resp = requests.get(f"{_PORTAL_BASE}/api/pipeline/event/{event_id}", timeout=20)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Portal unreachable: {e}")
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])
        return resp.json()


class EventUpdate(BaseModel):
    subject: str
    date: str
    time: str
    duration: int
    body_html: str


@router.patch("/event/{event_id:path}")
def update_event(
    event_id: str,
    body: EventUpdate,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Update existing Outlook event."""
    token = _get_outlook_token_or_raise(x_cal_auth)
    if token:
        from datetime import datetime as dt, timedelta
        dt_start = dt.strptime(f"{body.date} {body.time}", "%Y-%m-%d %H:%M")
        dt_end   = dt_start + timedelta(minutes=body.duration)
        payload = {
            "Subject": body.subject,
            "Body": {"ContentType": "HTML", "Content": body.body_html},
            "Start": {"DateTime": dt_start.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": "America/Sao_Paulo"},
            "End":   {"DateTime": dt_end.strftime("%Y-%m-%dT%H:%M:%S"),   "TimeZone": "America/Sao_Paulo"},
        }
        r = requests.patch(
            f"{_OUTLOOK_BASE}/events/{event_id}",
            headers=_outlook_headers(token), json=payload, timeout=20,
        )
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        return {"ok": True}
    else:
        try:
            resp = requests.patch(f"{_PORTAL_BASE}/api/pipeline/event/{event_id}", json=body.dict(), timeout=20)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Portal unreachable: {e}")
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])
        return resp.json()
