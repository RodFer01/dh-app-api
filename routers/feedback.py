"""Feedback Sessions router — 1:1 meeting notes + Outlook draft."""
from typing import Optional
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import requests, os

from database import query, query_one, execute, execute_returning
from auth import verify_token

router = APIRouter(prefix="/api/feedback-sessions", tags=["feedback"])

_PORTAL_BASE = os.getenv("PORTAL_BASE", "http://localhost:8766")


# ── Models ────────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    member_name:  str
    member_area:  Optional[str] = ""
    meeting_date: str           # YYYY-MM-DD
    discussion:   Optional[str] = ""
    feedback:     Optional[str] = ""
    action_plan:  Optional[str] = ""
    next_steps:   Optional[str] = ""
    next_date:    Optional[str] = None  # YYYY-MM-DD


class SessionUpdate(BaseModel):
    member_name:  Optional[str] = None
    member_area:  Optional[str] = None
    meeting_date: Optional[str] = None
    discussion:   Optional[str] = None
    feedback:     Optional[str] = None
    action_plan:  Optional[str] = None
    next_steps:   Optional[str] = None
    next_date:    Optional[str] = None


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("")
def list_sessions(member_name: Optional[str] = None, _user=Depends(verify_token)):
    sql = "SELECT * FROM feedback_sessions WHERE 1=1"
    params = []
    if member_name:
        sql += " AND UPPER(member_name) LIKE UPPER(?)"; params.append(f"%{member_name}%")
    sql += " ORDER BY meeting_date DESC, id DESC"
    rows = query(sql, params or None)
    result = []
    for r in rows:
        rec = dict(r)
        rec["meeting_date"] = str(rec["meeting_date"]) if rec["meeting_date"] else None
        rec["next_date"]    = str(rec["next_date"])    if rec["next_date"]    else None
        rec["created_at"]   = str(rec["created_at"])   if rec["created_at"]  else None
        rec["updated_at"]   = str(rec["updated_at"])   if rec["updated_at"]  else None
        result.append(rec)
    return result


@router.get("/members")
def list_members(_user=Depends(verify_token)):
    rows = query(
        "SELECT inumber, nombre_empleado AS name, area FROM targets ORDER BY area, nombre_empleado"
    )
    return [dict(r) for r in rows]


@router.post("")
def create_session(body: SessionCreate, _user=Depends(verify_token)):
    author = _user.get("name", "")
    new_id = execute_returning("""
        INSERT INTO feedback_sessions
            (member_name, member_area, meeting_date, discussion, feedback,
             action_plan, next_steps, next_date, created_by)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (body.member_name, body.member_area or "", body.meeting_date,
          body.discussion or "", body.feedback or "",
          body.action_plan or "", body.next_steps or "",
          body.next_date or None, author))
    return {"id": new_id, "ok": True}


@router.get("/{session_id}")
def get_session(session_id: int, _user=Depends(verify_token)):
    row = query_one("SELECT * FROM feedback_sessions WHERE id=?", [session_id])
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    rec = dict(row)
    rec["meeting_date"] = str(rec["meeting_date"]) if rec["meeting_date"] else None
    rec["next_date"]    = str(rec["next_date"])    if rec["next_date"]    else None
    return rec


@router.patch("/{session_id}")
def update_session(session_id: int, body: SessionUpdate, _user=Depends(verify_token)):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        return {"ok": True}
    sets   = ", ".join(f"{k}=?" for k in updates) + ", updated_at=CURRENT_TIMESTAMP"
    params = list(updates.values()) + [session_id]
    execute(f"UPDATE feedback_sessions SET {sets} WHERE id=?", params)
    return {"ok": True}


@router.delete("/{session_id}")
def delete_session(session_id: int, _user=Depends(verify_token)):
    execute("DELETE FROM feedback_sessions WHERE id=?", [session_id])
    return {"ok": True}


# ── Email draft via SAP Portal ────────────────────────────────────────────────

@router.post("/{session_id}/draft-email")
def draft_email(session_id: int, _user=Depends(verify_token)):
    row = query_one("SELECT * FROM feedback_sessions WHERE id=?", [session_id])
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    s = dict(row)

    def esc(v): return str(v or "—").replace("<", "&lt;").replace(">", "&gt;")

    meeting_dt = str(s.get("meeting_date") or "")
    try:
        meeting_label = datetime.strptime(meeting_dt, "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        meeting_label = meeting_dt

    next_dt = str(s.get("next_date") or "")
    try:
        next_label = datetime.strptime(next_dt, "%Y-%m-%d").strftime("%B %d, %Y") if next_dt else "—"
    except Exception:
        next_label = next_dt or "—"

    def section(title: str, content: str, color: str = "#0070B8") -> str:
        if not content or content.strip() == "—":
            return ""
        lines = "".join(f"<li>{esc(l)}</li>" for l in content.strip().splitlines() if l.strip())
        body = f"<ul style='margin:6px 0 0 16px;padding:0'>{lines}</ul>" if lines else f"<p style='margin:6px 0 0 0'>{esc(content)}</p>"
        return f"""
        <div style="margin-bottom:16px">
          <div style="font-size:12px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">{title}</div>
          {body}
        </div>"""

    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#32363A;max-width:720px;margin:0 auto">
  <div style="background:#0070B8;padding:16px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">SAP · 1:1 Feedback Session</span>
    <span style="color:rgba(255,255,255,.75);font-size:13px;margin-left:16px">{meeting_label}</span>
  </div>
  <div style="border:1px solid #E1E2E6;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px">
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
      <tr><td style="color:#788fa6;width:140px;padding:3px 0">Team Member</td><td><b>{esc(s.get('member_name'))}</b></td></tr>
      <tr><td style="color:#788fa6;padding:3px 0">Area / Team</td><td>{esc(s.get('member_area'))}</td></tr>
      <tr><td style="color:#788fa6;padding:3px 0">Meeting Date</td><td>{meeting_label}</td></tr>
      <tr><td style="color:#788fa6;padding:3px 0">Next Check-in</td><td>{next_label}</td></tr>
    </table>
    <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 8px;font-size:12px;color:#856404;font-weight:600">&#9998; Add your message here before sending:</p>
      <p style="margin:0 0 4px;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
      <p style="margin:4px 0 0;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
    </div>
    {section("Discussion Notes", s.get("discussion",""), "#0070B8")}
    {section("Feedback", s.get("feedback",""), "#256F3A")}
    {section("Action Plan", s.get("action_plan",""), "#E76500")}
    {section("Next Steps", s.get("next_steps",""), "#AA0808")}
    <p style="color:#788fa6;font-size:11px;margin-top:24px;border-top:1px solid #E1E2E6;padding-top:12px">
      Generated by Digital Hub SA Main App · {datetime.now().strftime("%Y-%m-%d")}
    </p>
  </div>
</body></html>"""

    subject = f"1:1 Feedback — {s.get('member_name')} · {meeting_label}"

    # Return HTML for frontend to generate .eml download
    return {"ok": True, "subject": subject, "html_body": html}
