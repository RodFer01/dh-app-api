"""Comments, notes, and next-steps router."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from database import query, query_one, execute, execute_returning
from auth import verify_token
import json, requests as _req
from datetime import datetime, timezone

router = APIRouter(prefix="/api", tags=["notes"])

_OUTLOOK_BASE = "https://outlook.office.com/api/v2.0/me"

def _create_outlook_draft(token: str, subject: str, body_html: str, to_email: str = "") -> dict:
    """Create an email draft in Outlook using a per-user Bearer token."""
    to_recipients = [{"EmailAddress": {"Address": to_email}}] if to_email else []
    payload = {
        "Subject": subject,
        "Body": {"ContentType": "HTML", "Content": body_html},
        "ToRecipients": to_recipients,
    }
    r = _req.post(
        f"{_OUTLOOK_BASE}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    return {"ok": True, "id": r.json().get("Id", "")}

_ALL_FIELDS = [
    "stage", "risk", "next_step", "next_step_date", "blockers", "notes",
    "deal_qual_status", "presales_status_override", "presales_comments",
    "next_actions", "next_actions_date",
    "digital_asset",
    "melody_engagement", "pre_allocated_dsa", "melody_comments",
    "todo_status", "todo_completion_comments",
]


class NotesPayload(BaseModel):
    stage: str = ""
    risk: str = ""
    next_step: str = ""
    next_step_date: str = ""
    blockers: str = ""
    notes: str = ""
    deal_qual_status: str = ""
    presales_status_override: str = ""
    presales_comments: str = ""
    next_actions: str = ""
    next_actions_date: str = ""
    digital_asset: str = ""
    melody_engagement: str = ""
    pre_allocated_dsa: str = ""
    melody_comments: str = ""
    todo_status: str = ""
    todo_completion_comments: str = ""


class NextStepPayload(BaseModel):
    id: Optional[int] = None
    description: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "Open"


# ── Notes (presales comments) ─────────────────────────────────────────────────

@router.get("/presales/{opp_id}")
def get_presales_data(opp_id: str, _user=Depends(verify_token)):
    """Return presales comments from Deal Management upload for an opportunity."""
    opp_id_padded = opp_id.zfill(10)
    row = query_one(
        "SELECT presales_lead_status, presales_lead_comments FROM presales WHERE opp_id = %s",
        (opp_id_padded,)
    )
    if not row:
        row = query_one(
            "SELECT presales_lead_status, presales_lead_comments FROM presales WHERE opp_id = %s",
            (opp_id,)
        )
    if not row:
        return None
    return {
        "presales_lead_status":   row.get("presales_lead_status"),
        "presales_lead_comments": row.get("presales_lead_comments"),
    }


@router.get("/notes/{opp_id}")
def get_notes(opp_id: str, _user=Depends(verify_token)):
    sql = """
        SELECT body, author, updated_at
        FROM comments WHERE opportunity_id = ?
        ORDER BY updated_at DESC LIMIT 1
    """
    row = query_one(sql, [opp_id])
    if not row:
        return None
    try:
        data = json.loads(row["body"])
    except Exception:
        data = {"notes": row["body"]}
    data["opportunity_id"] = opp_id
    data["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else ""
    data["author"] = row["author"] or ""
    return data


@router.put("/notes/{opp_id}")
def upsert_notes(opp_id: str, payload: NotesPayload, _user=Depends(verify_token)):
    """Always inserts a new version — preserves full history."""
    data = payload.model_dump()
    data["opportunity_id"] = opp_id
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    body = json.dumps(data)
    author = _user.get("name", "")
    execute(
        "INSERT INTO comments (opportunity_id, author, body) VALUES (?,?,?)",
        [opp_id, author, body]
    )
    return {"ok": True}


@router.get("/todo-activities")
def get_todo_activities(user_name: Optional[str] = None, _user=Depends(verify_token)):
    """Return all latest notes that have a next_actions_date set, optionally filtered by author."""
    rows = query(
        """SELECT c.opportunity_id, c.author, c.body, c.updated_at
           FROM comments c
           INNER JOIN (
               SELECT opportunity_id, MAX(updated_at) AS max_updated_at
               FROM comments
               GROUP BY opportunity_id
           ) latest ON c.opportunity_id = latest.opportunity_id
                    AND c.updated_at = latest.max_updated_at""",
        None
    )

    placeholders_sql = """
        SELECT opportunity_id, account_name, opp_description, opp_owner_name,
               solution_area_l1, sub_solution_area_l2, closing_qtr, acv_total_opp,
               opp_phase, drm_category, region_lvl3
        FROM (
            SELECT opportunity_id, account_name, opp_description, opp_owner_name,
                   solution_area_l1, sub_solution_area_l2, closing_qtr, acv_total_opp,
                   opp_phase, drm_category, region_lvl3,
                   ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY snapshot_week DESC) AS _rn
            FROM pipeline
        ) _p WHERE _rn = 1
    """
    pip_rows = query(placeholders_sql, None)
    pip_map = {r["opportunity_id"]: r for r in pip_rows}

    result = []
    for row in rows:
        try:
            data = json.loads(row["body"])
        except Exception:
            data = {}

        next_actions_date = data.get("next_actions_date", "")
        if not next_actions_date:
            continue

        if user_name and (row["author"] or "").lower() != user_name.lower():
            continue

        opp_id = row["opportunity_id"]
        pip = pip_map.get(opp_id, {})

        result.append({
            "opportunity_id":      opp_id,
            "author":              row["author"] or "",
            "updated_at":          row["updated_at"].isoformat() if row["updated_at"] else "",
            "next_actions":        data.get("next_actions", ""),
            "next_actions_date":   next_actions_date,
            "presales_comments":   data.get("presales_comments", ""),
            "notes":               data.get("notes", ""),
            "todo_status":         data.get("todo_status", ""),
            "todo_completion_comments": data.get("todo_completion_comments", ""),
            "stage":               data.get("stage", ""),
            "deal_qual_status":    data.get("deal_qual_status", ""),
            "account_name":        pip.get("account_name", ""),
            "opp_description":     pip.get("opp_description", ""),
            "opp_owner_name":      pip.get("opp_owner_name", ""),
            "solution_area_l1":    pip.get("solution_area_l1", ""),
            "closing_qtr":         pip.get("closing_qtr", ""),
            "acv_total_opp":       pip.get("acv_total_opp"),
            "opp_phase":           pip.get("opp_phase", ""),
            "drm_category":        pip.get("drm_category", ""),
        })

    result.sort(key=lambda x: x["next_actions_date"] or "")
    return result


@router.get("/todo-activities/users")
def get_todo_activity_users(_user=Depends(verify_token)):
    """Return distinct author names that have any todo activities."""
    rows = query(
        """SELECT DISTINCT author FROM comments
           WHERE author IS NOT NULL AND author != ''
           ORDER BY author""",
        None
    )
    return [r["author"] for r in rows if r["author"]]


@router.get("/notes/{opp_id}/history")
def get_notes_history(opp_id: str, _user=Depends(verify_token)):
    """Return all saved versions of notes for an opportunity, newest first."""
    rows = query(
        "SELECT id, author, updated_at, body FROM comments WHERE opportunity_id=? ORDER BY updated_at DESC",
        [opp_id]
    )
    result = []
    for row in rows:
        try:
            data = json.loads(row["body"])
        except Exception:
            data = {"notes": row["body"]}
        result.append({
            "id":         row["id"],
            "author":     row["author"] or "",
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
            "data":       data,
        })
    return result


# ── Next Steps ────────────────────────────────────────────────────────────────

@router.get("/next-steps")
def get_all_next_steps(status: Optional[str] = None, _user=Depends(verify_token)):
    clauses = []
    params = []
    if status:
        clauses.append("ns.status = ?"); params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT ns.id, ns.opportunity_id, ns.description, ns.owner,
               TO_NVARCHAR(ns.due_date, 'YYYY-MM-DD') AS due_date, ns.status,
               TO_NVARCHAR(ns.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
               TO_NVARCHAR(ns.updated_at, 'YYYY-MM-DD HH24:MI:SS') AS updated_at,
               p.account_name, p.opp_description, p.opp_owner_name
        FROM next_steps ns
        LEFT JOIN (
            SELECT opportunity_id, account_name, opp_description, opp_owner_name
            FROM (
                SELECT opportunity_id, account_name, opp_description, opp_owner_name,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY snapshot_week DESC) AS _rn
                FROM pipeline
            ) _p WHERE _rn = 1
        ) p ON p.opportunity_id = ns.opportunity_id
        {where}
        ORDER BY ns.due_date ASC, ns.updated_at DESC
    """
    return query(sql, params or None)


@router.get("/next-steps/{opp_id}")
def get_next_steps(opp_id: str, _user=Depends(verify_token)):
    return query(
        """SELECT id, description, owner,
                  TO_NVARCHAR(due_date, 'YYYY-MM-DD') AS due_date, status,
                  TO_NVARCHAR(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
                  TO_NVARCHAR(updated_at, 'YYYY-MM-DD HH24:MI:SS') AS updated_at
           FROM next_steps WHERE opportunity_id = ?
           ORDER BY due_date ASC, created_at ASC""",
        [opp_id]
    )


@router.post("/next-steps/{opp_id}")
def create_next_step(opp_id: str, payload: NextStepPayload, _user=Depends(verify_token)):
    new_id = execute_returning(
        """INSERT INTO next_steps (opportunity_id, description, owner, due_date, status)
           VALUES (?,?,?,?,?)""",
        [opp_id, payload.description, payload.owner, payload.due_date, payload.status]
    )
    return {"id": new_id}


@router.put("/next-steps/{step_id}")
def update_next_step(step_id: int, payload: NextStepPayload, _user=Depends(verify_token)):
    execute(
        """UPDATE next_steps SET description=?, owner=?, due_date=?,
                                  status=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        [payload.description, payload.owner, payload.due_date, payload.status, step_id]
    )
    return {"ok": True}


@router.delete("/next-steps/{step_id}")
def delete_next_step(step_id: int, _user=Depends(verify_token)):
    execute("DELETE FROM next_steps WHERE id=?", [step_id])
    return {"ok": True}


# ── Email ─────────────────────────────────────────────────────────────────────

class EmailPayload(BaseModel):
    to: str
    cc: str = ""
    subject: str
    body_html: str


@router.post("/email/send")
def send_email(payload: EmailPayload, _user=Depends(verify_token)):
    """Return email as {subject, html_body, to} for frontend .eml download."""
    return {
        "ok": True,
        "subject": payload.subject,
        "html_body": payload.body_html,
        "to_email": payload.to,
    }


class SparkSessionInfo(BaseModel):
    id: str
    title: str
    area: str = ""
    audience: str = ""
    language: str = ""
    next_date: str = ""

class PipelineReviewEmailPayload(BaseModel):
    opp_ids: list
    extra_note: str = ""
    spark_sessions: list = []
    cal_token: str = ""


@router.post("/draft/pipeline-review")
def pipeline_review_email(
    payload: PipelineReviewEmailPayload,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """
    Create a draft pipeline review email in Outlook Drafts.
    Uses HTML body (SAP-styled table) + CSV attachment for Spark Meeting contacts.
    No recipients — user adds To/CC in Outlook before sending.
    """
    from database import query
    import json as _json
    from html import escape as esc
    from datetime import datetime
    import base64

    if not payload.opp_ids:
        raise HTTPException(status_code=400, detail="No opportunities selected")

    placeholders = ",".join(["?"] * len(payload.opp_ids))

    # Load latest notes per opp
    notes_rows = query(
        f"SELECT opportunity_id, body FROM comments "
        f"WHERE opportunity_id IN ({placeholders}) "
        f"ORDER BY opportunity_id, updated_at DESC",
        list(payload.opp_ids)
    )
    latest_notes = {}
    for row in notes_rows:
        if row["opportunity_id"] not in latest_notes:
            try:
                latest_notes[row["opportunity_id"]] = _json.loads(row["body"])
            except Exception:
                latest_notes[row["opportunity_id"]] = {"notes": row["body"]}

    # Pipeline data
    pip_rows = query(
        f"""SELECT DISTINCT ON (opportunity_id)
                opportunity_id, account_name, opp_owner_name,
                solution_area_l1, sub_solution_area_l2, drm_category,
                closing_qtr, acv_total_opp, opp_phase, region_lvl3,
                local_acct_owner
            FROM pipeline
            WHERE opportunity_id IN ({placeholders})
              AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
            ORDER BY opportunity_id, acv_total_opp DESC""",
        list(payload.opp_ids)
    )
    pip_map = {r["opportunity_id"]: r for r in pip_rows}

    today = datetime.now().strftime("%d/%m/%Y")

    # ── Build HTML table rows ──────────────────────────────────────────────────
    rows_html = ""
    csv_rows = ["Account,Contact Name,Email/Phone,Spark Meeting Session Interest"]

    for opp_id in payload.opp_ids:
        n = latest_notes.get(str(opp_id), {})
        p = pip_map.get(str(opp_id), {})
        acv = p.get("acv_total_opp") or 0
        try:
            acv_str = f"${float(acv)/1000:,.1f}K"
        except Exception:
            acv_str = "—"

        account = esc(str(p.get("account_name", "—")))
        qual_st = str(n.get("deal_qual_status", "") or "—")
        qual_color = {"GO": "#256f3a", "No-Go": "#BB0000", "Re-Qualify": "#e76500"}.get(qual_st, "#32363a")
        qual_bg   = {"GO": "#d4edda",  "No-Go": "#fde8e8",  "Re-Qualify": "#fff3cd"}.get(qual_st, "transparent")

        # Opp ID as CRM link
        opp_link = f'<a href="https://sapit-core-prod.launchpad.eu10.hana.ondemand.com/site/S4HANA/sap/fiori#/Opportunities/{opp_id}" style="color:#0070F2;font-weight:600;font-family:monospace">{esc(str(opp_id))}</a>'

        solution = esc(str(p.get("solution_area_l1", "—")))
        if p.get("sub_solution_area_l2"):
            solution += f'<br><span style="color:#758ca4;font-size:11px">{esc(str(p.get("sub_solution_area_l2","")))}</span>'

        rows_html += f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;vertical-align:top">{opp_link}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;font-weight:600;vertical-align:top">{account}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;color:#32363A;font-size:12px;vertical-align:top">{esc(str(p.get("opp_owner_name","—")))}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;color:#32363A;font-size:12px;vertical-align:top">{esc(str(p.get("local_acct_owner","—")))}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;color:#6A6D70;font-size:12px;vertical-align:top">{solution}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;vertical-align:top">{esc(str(p.get("drm_category","—")))}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;vertical-align:top">{esc(str(p.get("closing_qtr","—")))}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;text-align:right;font-weight:600;vertical-align:top">{acv_str}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;font-weight:700;color:{qual_color};background:{qual_bg};border-radius:4px;vertical-align:top">{esc(qual_st)}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;color:#32363A;font-size:13px;vertical-align:top">{esc(str(n.get("presales_comments","") or "—")).replace(chr(10),"<br>")}</td>
          <td style="padding:10px;border-bottom:1px solid #E1E2E6;font-size:13px;vertical-align:top">{esc(str(n.get("next_actions","") or "—")).replace(chr(10),"<br>")}<br><span style="font-size:11px;color:#758ca4">{esc(str(n.get("next_actions_date","") or ""))}</span></td>
        </tr>"""

        # CSV row for Spark contacts
        account_raw = str(p.get("account_name", ""))
        csv_rows.append(f'"{account_raw}",,,"(fill in)"')

    extra_section = ""
    if payload.extra_note.strip():
        extra_section = f"""
    <p style="margin:20px 0 6px;font-weight:600;color:#32363A">Additional notes:</p>
    <p style="background:#F5F6F7;border-left:3px solid #0070F2;padding:10px 14px;border-radius:4px;color:#32363A">{esc(payload.extra_note)}</p>"""

    body_html = f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#32363A;max-width:980px;margin:0 auto">
  <div style="background:#0070F2;padding:16px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">SAP &middot; Pipeline Presales Review</span>
    <span style="color:rgba(255,255,255,.75);font-size:13px;margin-left:16px">{today}</span>
  </div>
  <div style="border:1px solid #E1E2E6;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px">
    <p style="margin:0 0 8px;color:#32363A">Hi Team,</p>
    <p style="margin:0 0 20px;color:#32363A">Please find below the presales review summary for the selected opportunities.</p>
        <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 10px;font-size:12px;color:#856404;font-weight:600">✏ Add your message here before sending:</p>
      <p style="margin:0;min-height:20px">    <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 8px;font-size:12px;color:#856404;font-weight:600">&#9998; Add your message here before sending:</p>
      <p style="margin:0 0 4px;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
      <p style="margin:4px 0 0;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
    </div>nbsp;</p>
      <p style="margin:0;min-height:20px">    <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 8px;font-size:12px;color:#856404;font-weight:600">&#9998; Add your message here before sending:</p>
      <p style="margin:0 0 4px;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
      <p style="margin:4px 0 0;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
    </div>nbsp;</p>
    </div>

    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#F5F6F7">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Opp #</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Account</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">AE Owner</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Local Owner</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Solution</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">DRM</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Quarter</th>
          <th style="padding:8px 10px;text-align:right;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">ACV</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Qual Status</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Presales Comments</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Next Actions</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    {extra_section}

    <p style="margin-top:24px;color:#32363A;font-size:13px">
      <strong>📎 Attachment:</strong> <em>spark_meeting_contacts.xlsx</em> — please fill in contact names and emails for each account for Spark Meeting invitations.
    </p>

    <p style="color:#758ca4;font-size:12px;margin-top:16px;border-top:1px solid #E1E2E6;padding-top:12px">
      Generated by Digital Hub SA Main App &middot; {today}
    </p>
  </div>
</body>
</html>"""

    # ── Build Excel attachment ────────────────────────────────────────────────
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spark Meeting Contacts"

    hdr_font  = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="0070F2")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin      = Side(style="thin", color="E1E2E6")
    bdr       = Border(bottom=thin, right=thin)
    dat_font  = Font(name="Calibri", size=10)
    sess_fill = PatternFill("solid", fgColor="E8F0FE")  # light blue for session rows
    sess_font = Font(name="Calibri", size=10, italic=True, color="0070F2")

    headers = ["Account", "Spark Meeting Session", "Session Date",
               "Contact Name", "Email / Phone", "Notes for AE"]
    col_widths = [35, 45, 14, 25, 30, 30]

    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = hdr_font; c.fill = hdr_fill
        c.alignment = hdr_align; c.border = bdr
        ws.column_dimensions[get_column_letter(col)].width = col_widths[col-1]
    ws.row_dimensions[1].height = 28

    sessions = payload.spark_sessions or []  # list of session dicts

    r_idx = 2
    for opp_id in payload.opp_ids:
        pd_ = pip_map.get(str(opp_id), {})
        account_name = str(pd_.get("account_name", ""))

        if sessions:
            # One row per session for this account
            for i, sess in enumerate(sessions):
                fill = sess_fill if i % 2 == 0 else PatternFill("solid", fgColor="F0F4FF")
                for col in range(1, len(headers)+1):
                    c = ws.cell(row=r_idx, column=col)
                    c.font = dat_font; c.fill = fill; c.border = bdr
                    c.alignment = Alignment(vertical="top", wrap_text=True)
                ws.cell(row=r_idx, column=1, value=account_name).font = Font(name="Calibri", size=10, bold=(i==0))
                ws.cell(row=r_idx, column=2, value=str(sess.get("title",""))).font = sess_font
                ws.cell(row=r_idx, column=3, value=str(sess.get("next_date",""))).font = dat_font
                ws.row_dimensions[r_idx].height = 18
                r_idx += 1
        else:
            # No sessions selected — one blank row per account
            fill = PatternFill("solid", fgColor="FFFFFF" if r_idx % 2 == 0 else "EEF3FF")
            for col in range(1, len(headers)+1):
                c = ws.cell(row=r_idx, column=col)
                c.font = dat_font; c.fill = fill; c.border = bdr
                c.alignment = Alignment(vertical="top", wrap_text=True)
            ws.cell(row=r_idx, column=1, value=account_name)
            ws.cell(row=r_idx, column=2, value="(no session selected)")
            ws.row_dimensions[r_idx].height = 18
            r_idx += 1

    buf = io.BytesIO(); wb.save(buf)
    xlsx_b64 = base64.b64encode(buf.getvalue()).decode()

    subject = f"[Pipeline Review] {len(payload.opp_ids)} Opportunit{'y' if len(payload.opp_ids)==1 else 'ies'} — {today}"

    # Return HTML + xlsx for frontend to call Outlook directly (avoids BTP WAF)
    attachment = None
    if xlsx_b64:
        attachment = {
            "@odata.type": "#Microsoft.OutlookServices.FileAttachment",
            "Name": "spark_meeting_contacts.xlsx",
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ContentBytes": xlsx_b64,
        }

    return {
        "ok": True,
        "subject": subject,
        "html_body": body_html,
        "attachment": attachment,
    }


class AEReminderPayload(BaseModel):
    opp_ids: list
    note:    str = ""
    ae_email: str = ""
    cal_token: str = ""   # Outlook Bearer token — sent in body to avoid BTP header filtering


@router.post("/draft/ae-reminder")
def ae_reminder_email(
    payload: AEReminderPayload,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """Returns subject + html_body for the frontend to call Outlook directly."""
    from database import query
    from html import escape as esc
    from datetime import datetime

    if not payload.opp_ids:
        raise HTTPException(status_code=400, detail="No opportunities selected")

    placeholders = ",".join(["?"] * len(payload.opp_ids))
    opps = query(
        f"""SELECT DISTINCT ON (opportunity_id)
                opportunity_id, account_name, opp_description, opp_owner_name,
                solution_area_l1, sub_solution_area_l2, drm_category,
                closing_qtr, fc_qualification, engagement_type,
                acv_total_opp, opp_phase, local_acct_owner, vat_dh
            FROM pipeline
            WHERE opportunity_id IN ({placeholders})
              AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
            ORDER BY opportunity_id, acv_total_opp DESC""",
        list(payload.opp_ids)
    )

    if not opps:
        raise HTTPException(status_code=404, detail="No opportunities found")

    today = datetime.now().strftime("%B %d, %Y")
    author = _user.get("name", "Solution Advisor")

    def fmt_acv(v):
        if not v: return "—"
        k = float(v) / 1000
        return f"${k:,.1f}K"

    rows_html = ""
    for i, p in enumerate(opps):
        bg = "#F5F6F7" if i % 2 == 0 else "#FFFFFF"
        badge_color = "#0070F2" if str(p.get("engagement_type","")).lower() == "digital" else "#788fa6"
        badge = f'<span style="font-size:10px;background:{badge_color}22;color:{badge_color};padding:1px 6px;border-radius:8px;font-weight:700">{esc(str(p.get("engagement_type","—")))}</span>'
        rows_html += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-family:monospace;font-size:11px;white-space:nowrap">{esc(str(p.get("opportunity_id","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-weight:600">{esc(str(p.get("account_name","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("opp_owner_name","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("local_acct_owner","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("opp_description","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("solution_area_l1","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("closing_qtr","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;text-align:right;font-weight:600">{fmt_acv(p.get("acv_total_opp"))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6">{badge}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("opp_phase","—")))}</td>
        </tr>"""

    note_section = f"""
    <div style="margin:20px 0;padding:12px 16px;background:#FFFDE7;border-left:4px solid #F0AB00;border-radius:4px;font-size:13px;color:#32363A">
      <b>Note from {esc(author)}:</b><br>{esc(payload.note).replace(chr(10),'<br>')}
    </div>""" if payload.note.strip() else ""

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#32363A;max-width:1000px;margin:0 auto">
  <div style="background:#0070B8;padding:16px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">SAP · Pipeline Opportunity Reminder</span>
    <span style="color:rgba(255,255,255,.75);font-size:13px;margin-left:16px">{today}</span>
  </div>
  <div style="border:1px solid #E1E2E6;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px">
    <p style="margin:0 0 8px">Hi,</p>
    <p style="margin:0 0 20px">I wanted to follow up on the opportunities below and align on next steps for our Solution Advisory support.</p>
    {note_section}
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px">
      <thead><tr style="background:#F5F6F7">
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Opp #</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Account</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">AE Owner</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Local Owner</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Description</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Solution</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Quarter</th>
        <th style="padding:8px 10px;text-align:right;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">ACV</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Engagement</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p style="margin-top:24px;color:#32363A">Looking forward to connecting.</p>
    <p style="color:#788fa6;font-size:11px;margin-top:16px;border-top:1px solid #E1E2E6;padding-top:12px">Sent via Digital Hub SA Main App · {today}</p>
  </div>
</body></html>"""

    subject = f"Pipeline Opportunity Follow-up — {len(opps)} Opportunit{'y' if len(opps)==1 else 'ies'} | {today}"

    return {
        "ok": True,
        "subject": subject,
        "html_body": html_body,
        "to_email": payload.ae_email or "",
    }

    today = datetime.now().strftime("%B %d, %Y")
    author = _user.get("name", "Solution Advisor")

    def fmt_acv(v):
        if not v: return "—"
        k = float(v) / 1000
        return f"${k:,.1f}K"

    rows_html = ""
    for i, p in enumerate(opps):
        bg = "#F5F6F7" if i % 2 == 0 else "#FFFFFF"
        badge_color = "#0070F2" if str(p.get("engagement_type","")).lower() == "digital" else "#788fa6"
        badge = f'<span style="font-size:10px;background:{badge_color}22;color:{badge_color};padding:1px 6px;border-radius:8px;font-weight:700">{esc(str(p.get("engagement_type","—")))}</span>'
        rows_html += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-family:monospace;font-size:11px;white-space:nowrap">{esc(str(p.get("opportunity_id","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-weight:600">{esc(str(p.get("account_name","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("opp_owner_name","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("local_acct_owner","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("opp_description","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("solution_area_l1","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(p.get("closing_qtr","—")))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;text-align:right;font-weight:600">{fmt_acv(p.get("acv_total_opp"))}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6">{badge}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(p.get("opp_phase","—")))}</td>
        </tr>"""

    note_section = f"""
    <div style="margin:20px 0;padding:12px 16px;background:#FFFDE7;border-left:4px solid #F0AB00;border-radius:4px;font-size:13px;color:#32363A">
      <b>Note from {esc(author)}:</b><br>{esc(payload.note).replace(chr(10),'<br>')}
    </div>""" if payload.note.strip() else ""

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#32363A;max-width:1000px;margin:0 auto">
  <div style="background:#0070B8;padding:16px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">SAP · Pipeline Opportunity Reminder</span>
    <span style="color:rgba(255,255,255,.75);font-size:13px;margin-left:16px">{today}</span>
  </div>
  <div style="border:1px solid #E1E2E6;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px">
    <p style="margin:0 0 8px">Hi,</p>
    <p style="margin:0 0 20px">I wanted to follow up on the opportunities below and align on next steps for our Solution Advisory support. Please let me know your availability for a quick sync.</p>
    {note_section}
    <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 8px;font-size:12px;color:#856404;font-weight:600">&#9998; Add your message here before sending:</p>
      <p style="margin:0 0 4px;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
      <p style="margin:4px 0 0;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px">
      <thead>
        <tr style="background:#F5F6F7">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Opp #</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Account</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Description</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Solution</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Quarter</th>
          <th style="padding:8px 10px;text-align:right;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">ACV</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Engagement</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070B8;color:#556b82;font-size:11px;text-transform:uppercase">Phase</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p style="margin-top:24px;color:#32363A">Looking forward to connecting.</p>
    <p style="color:#788fa6;font-size:11px;margin-top:16px;border-top:1px solid #E1E2E6;padding-top:12px">
      Sent via Digital Hub SA Main App · {today}
    </p>
  </div>
</body></html>"""

    subject = f"Pipeline Opportunity Follow-up — {len(opps)} Opportunit{'y' if len(opps)==1 else 'ies'} | {today}"

    token = (payload.cal_token or x_cal_auth or "").strip()
    if not token or len(token) < 100:
        raise HTTPException(status_code=503, detail="Token expired. Use the 'Conectar Outlook' button to renew.")

    return _create_outlook_draft(token, subject, html_body, payload.ae_email or "")
