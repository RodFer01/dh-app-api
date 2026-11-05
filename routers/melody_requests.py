"""Melody Requests router — upload, comments, explorer."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Body, Header
from pydantic import BaseModel
from database import query, query_one, execute
from auth import verify_token
import io

# Reuse existing melody_requests logic
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "presales-pipeline-app"))
import melody_requests as mr

router = APIRouter(prefix="/api/melody-requests", tags=["melody-requests"])


class CommentPayload(BaseModel):
    body: str


class CommentUpdatePayload(BaseModel):
    body: str


@router.get("/summary")
def get_summary(_user=Depends(verify_token)):
    return mr.get_summary()


@router.get("/upload-log")
def get_upload_log(limit: int = 20, _user=Depends(verify_token)):
    return mr.get_upload_log(limit=limit)


@router.get("")
def get_requests(
    status: Optional[str] = None,
    req_type: Optional[str] = None,
    role_area: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 1000,
    # Multi-select
    statuses: List[str] = Query(default=[]),
    req_types: List[str] = Query(default=[]),
    role_areas: List[str] = Query(default=[]),
    requestors: List[str] = Query(default=[]),
    resources: List[str] = Query(default=[]),
    profit_centers: List[str] = Query(default=[]),
    task_owners: List[str] = Query(default=[]),
    processors: List[str] = Query(default=[]),
    sub_statuses: List[str] = Query(default=[]),
    action_takens: List[str] = Query(default=[]),
    lob_teams: List[str] = Query(default=[]),
    engagement_statuses: List[str] = Query(default=[]),
    solutions: List[str] = Query(default=[]),
    # Date range filters
    create_date_from: Optional[str] = Query(default=None),   # YYYY-MM-DD
    create_date_to:   Optional[str] = Query(default=None),
    last_action_from: Optional[str] = Query(default=None),
    last_action_to:   Optional[str] = Query(default=None),
    _user=Depends(verify_token),
):
    def multi(field, vals, single):
        all_vals = vals or ([single] if single else [])
        return all_vals

    clauses, params = [], []

    def add_in(field, vals):
        if not vals: return
        ph = ",".join(["%s"] * len(vals))
        clauses.append(f"{field} IN ({ph})")
        params.extend(vals)

    add_in("req_status_main",    multi("req_status_main", statuses, status))
    add_in("req_type",           multi("req_type", req_types, req_type))
    add_in("req_role_area",      multi("req_role_area", role_areas, role_area))
    add_in("requestor",          requestors)
    add_in("req_resource",       resources)
    add_in("req_profit_center",  profit_centers)
    add_in("req_task_owner",     task_owners)
    add_in("req_task_action_taken",   action_takens)

    # LoB team filter — match any member of the selected sub-teams in req_task_owner
    if lob_teams:
        members = query(
            "SELECT nombre_empleado FROM targets WHERE sub_team = ANY(%s) AND nombre_empleado IS NOT NULL",
            (lob_teams,)
        )
        names = [r["nombre_empleado"] for r in members]
        if names:
            or_clauses = " OR ".join(["req_task_owner ILIKE %s"] * len(names))
            clauses.append(f"({or_clauses})")
            params.extend([f"%{n}%" for n in names])

    # req_processor can hold comma-separated names — use ILIKE for each selected value
    if processors:
        or_clauses = " OR ".join(["req_processor ILIKE %s"] * len(processors))
        clauses.append(f"({or_clauses})")
        params.extend([f"%{p}%" for p in processors])

    add_in("sub_status", sub_statuses)
    add_in("req_solution", solutions)

    if engagement_statuses:
        has_none = "(Sem engagement)" in engagement_statuses
        real_vals = [v for v in engagement_statuses if v != "(Sem engagement)"]
        if has_none and real_vals:
            ph = ",".join(["%s"] * len(real_vals))
            clauses.append(f"(es.engagement_status IN ({ph}) OR es.engagement_status IS NULL)")
            params.extend(real_vals)
        elif has_none:
            clauses.append("es.engagement_status IS NULL")
        else:
            ph = ",".join(["%s"] * len(real_vals))
            clauses.append(f"es.engagement_status IN ({ph})")
            params.extend(real_vals)

    if search:
        search_fields = [
            "r.req_id_sub", "r.opportunity_id", "r.acct_name",
            "r.requestor", "r.req_resource", "r.req_processor", "r.req_task_owner",
            "r.req_task_owner_role", "r.req_role_area", "r.req_profit_center",
            "r.req_type", "r.req_status", "r.req_status_main", "r.sub_status",
            "r.req_task", "r.req_task_action_taken",
        ]
        clauses.append("(" + " OR ".join(f"{f} ILIKE %s" for f in search_fields) + ")")
        like = f"%{search}%"
        params.extend([like] * len(search_fields))

    if create_date_from:
        clauses.append("req_create_date >= %s"); params.append(create_date_from)
    if create_date_to:
        clauses.append("req_create_date <= %s"); params.append(create_date_to)
    if last_action_from:
        clauses.append("req_last_action_date >= %s"); params.append(last_action_from)
    if last_action_to:
        clauses.append("req_last_action_date <= %s"); params.append(last_action_to)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT r.req_id_sub, r.req_type, r.req_status_main, r.req_status, r.sub_status,
               r.opportunity_id, r.acct_name, r.requestor, r.req_resource,
               r.req_role_area, r.req_profit_center, r.req_task_owner, r.req_task_owner_role,
               r.req_processor, r.req_solution,
               TO_NVARCHAR(r.req_create_date, 'YYYY-MM-DD') AS req_create_date,
               TO_NVARCHAR(r.req_last_action_date, 'YYYY-MM-DD') AS req_last_action_date,
               r.req_task, r.req_task_action_taken,
               COUNT(c.id) AS comment_count,
               es.engagement_status
        FROM melody_requests r
        LEFT JOIN melody_request_comments c ON c.req_id_sub = r.req_id_sub
        LEFT JOIN mr_engagement_status es ON es.req_id_sub = r.req_id_sub
        {where}
        GROUP BY r.req_id_sub, r.req_type, r.req_status_main, r.req_status, r.sub_status,
                 r.opportunity_id, r.acct_name, r.requestor, r.req_resource,
                 r.req_role_area, r.req_profit_center,
                 r.req_task_owner, r.req_task_owner_role, r.req_processor, r.req_solution,
                 r.req_create_date, r.req_last_action_date, r.req_task, r.req_task_action_taken,
                 es.engagement_status
        ORDER BY r.req_create_date DESC
        LIMIT {int(limit)}
    """
    rows = query(sql, params or None)

    # Pre-Allocated: check if any DSA name from targets appears in req_processor
    # Exclude managers — they are not SAs
    MANAGERS = {'rodrigo ribeiro', 'marmileth', 'carlos flores', 'angela beltran', 'rafael morcelli'}
    try:
        dsa_names = [r["nombre_empleado"].strip() for r in
                     query("SELECT nombre_empleado FROM targets WHERE nombre_empleado IS NOT NULL")
                     if r.get("nombre_empleado")
                     and not any(m in r["nombre_empleado"].lower() for m in MANAGERS)]
    except Exception:
        dsa_names = []

    for row in rows:
        proc = (row.get("req_processor") or "").lower()
        matched = [n for n in dsa_names if n and n.lower() in proc]
        row["pre_allocated"] = matched[0] if matched else None

    return rows


@router.get("/filters")
def get_filters(_user=Depends(verify_token)):
    array_col_names = ['statuses','req_types','role_areas','requestors','resources',
                       'profit_centers','task_owners','processors','sub_statuses','action_takens','solutions']
    row = query_one("""
        SELECT
            array_agg(DISTINCT req_status_main   ORDER BY req_status_main)   AS statuses,
            array_agg(DISTINCT req_type          ORDER BY req_type)           AS req_types,
            array_agg(DISTINCT req_role_area     ORDER BY req_role_area)      AS role_areas,
            array_agg(DISTINCT requestor         ORDER BY requestor)          AS requestors,
            array_agg(DISTINCT req_resource      ORDER BY req_resource)       AS resources,
            array_agg(DISTINCT req_profit_center ORDER BY req_profit_center)  AS profit_centers,
            array_agg(DISTINCT req_task_owner      ORDER BY req_task_owner)       AS task_owners,
            array_agg(DISTINCT req_processor       ORDER BY req_processor)         AS processors,
            array_agg(DISTINCT sub_status          ORDER BY sub_status)            AS sub_statuses,
            array_agg(DISTINCT req_task_action_taken ORDER BY req_task_action_taken) AS action_takens,
            array_agg(DISTINCT req_solution        ORDER BY req_solution)          AS solutions
        FROM melody_requests
    """, array_cols=array_col_names) or {}
    result = {k: [v for v in (arr if isinstance(arr, list) else []) if v is not None] for k, arr in row.items()}

    # Explode comma-separated processor/task_owner strings into individual names
    for field in ("processors", "task_owners"):
        raw = result.get(field, [])
        unique: set = set()
        for entry in raw:
            for name in str(entry).split(","):
                n = name.strip()
                if n:
                    unique.add(n)
        result[field] = sorted(unique)

    # LoB sub-teams from targets table
    lob_rows = query(
        "SELECT DISTINCT sub_team FROM targets WHERE sub_team IS NOT NULL ORDER BY sub_team"
    )
    result["lob_teams"] = [r["sub_team"] for r in lob_rows]

    # Engagement statuses from mr_engagement_status table
    eng_rows = query(
        "SELECT DISTINCT engagement_status FROM mr_engagement_status WHERE engagement_status IS NOT NULL ORDER BY engagement_status"
    )
    result["engagement_statuses"] = ["(Sem engagement)"] + [r["engagement_status"] for r in eng_rows]

    return result


@router.get("/{req_id_sub}/comments")
def get_comments(req_id_sub: str, _user=Depends(verify_token)):
    return mr.get_comments(req_id_sub)


@router.post("/{req_id_sub}/comments")
def add_comment(req_id_sub: str, payload: CommentPayload, _user=Depends(verify_token)):
    author = _user.get("name", "")
    new_id = mr.add_comment(req_id_sub, payload.body, author=author)
    return {"id": new_id}


@router.put("/comments/{comment_id}")
def update_comment(comment_id: int, payload: CommentUpdatePayload, _user=Depends(verify_token)):
    mr.update_comment(comment_id, payload.body)
    return {"ok": True}


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: int, _user=Depends(verify_token)):
    mr.delete_comment(comment_id)
    return {"ok": True}


@router.post("/upload/diff")
async def diff_upload(
    file: UploadFile = File(...),
    _user=Depends(verify_token),
):
    """Parse file and return diff preview without committing."""
    content = await file.read()
    df, warnings = mr.load_from_excel(io.BytesIO(content))
    if df.empty:
        return {"error": "No valid rows found", "warnings": warnings}
    diff = mr.diff_with_db(df)
    return {
        "warnings":   warnings,
        "to_insert":  len(diff["to_insert"]),
        "to_update":  len(diff["to_update"]),
        "unchanged":  len(diff["unchanged"]),
        "only_in_db": len(diff["only_in_db"]),
        "changed_cols": diff["changed_cols"],
    }


@router.post("/upload")
async def upload_requests(
    file: UploadFile = File(...),
    mode: str = Form(default="merge"),
    _user=Depends(verify_token),
):
    content  = await file.read()
    df, warnings = mr.load_from_excel(io.BytesIO(content))
    if df.empty:
        raise HTTPException(status_code=400, detail="No valid rows found")

    author = _user.get("name", "")
    if mode == "replace":
        result = mr.replace_requests(df, filename=file.filename or "", uploaded_by=author)
    else:
        result = mr.upsert_requests(df, filename=file.filename or "", uploaded_by=author)

    return {"ok": True, "warnings": warnings, **result}


class MRReviewEmailPayload(BaseModel):
    req_ids: list
    extra_note: str = ""
    cal_token: str = ""


@router.post("/draft/review")
def mr_review_email(
    payload: MRReviewEmailPayload,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    """
    Create an Outlook draft with the Melody Request review summary.
    One row per selected request with status, comments, processor and resource.
    """
    from database import query as db_query
    from html import escape as esc
    from datetime import datetime
    import base64, subprocess, sys, os, json as _j, tempfile

    if not payload.req_ids:
        raise HTTPException(status_code=400, detail="No requests selected")

    placeholders = ",".join(["%s"] * len(payload.req_ids))

    reqs = db_query(
        f"""SELECT req_id_sub, req_type, req_status_main, sub_status,
                   opportunity_id, acct_name, requestor, req_resource, req_processor,
                   req_profit_center, req_role_area, req_task_owner,
                   TO_NVARCHAR(req_create_date, 'YYYY-MM-DD') AS req_create_date, req_task
            FROM melody_requests
            WHERE req_id_sub IN ({placeholders})""",
        list(payload.req_ids)
    )
    req_map = {r["req_id_sub"]: r for r in reqs}

    # Pre-allocated DSA check — exclude managers
    MANAGERS = {'rodrigo ribeiro', 'marmileth', 'carlos flores', 'angela beltran', 'rafael morcelli'}
    try:
        dsa_names = [r["nombre_empleado"].strip() for r in
                     db_query("SELECT nombre_empleado FROM targets WHERE nombre_empleado IS NOT NULL")
                     if r.get("nombre_empleado")
                     and not any(m in r["nombre_empleado"].lower() for m in MANAGERS)]
    except Exception:
        dsa_names = []

    def _pre_allocated(processor: str) -> str | None:
        proc = (processor or "").lower()
        matched = [n for n in dsa_names if n and n.lower() in proc]
        return matched[0] if matched else None

    # Load latest comment per request
    comments_rows = db_query(
        f"""SELECT mc.req_id_sub, mc.body, mc.author, mc.created_at
            FROM melody_request_comments mc
            JOIN (SELECT req_id_sub, MAX(created_at) AS mx FROM melody_request_comments
                  WHERE req_id_sub IN ({placeholders}) GROUP BY req_id_sub) latest
              ON mc.req_id_sub = latest.req_id_sub AND mc.created_at = latest.mx""",
        list(payload.req_ids)
    )
    latest_comment = {r["req_id_sub"]: r for r in comments_rows}

    today = datetime.now().strftime("%d/%m/%Y")

    rows_html = ""
    for rid in payload.req_ids:
        r = req_map.get(str(rid), {})
        cm = latest_comment.get(str(rid), {})

        sub_st = str(r.get("sub_status") or "—")
        main_st = str(r.get("req_status_main") or "—")
        st_color = {"Closed": "#256f3a", "In Review": "#0070f2"}.get(main_st, "#32363a")
        st_bg    = {"Closed": "#d4edda",  "In Review": "#e1f4ff"}.get(main_st, "transparent")

        comment_text = esc(str(cm.get("body") or "—")).replace("\n", "<br>")
        comment_author = esc(str(cm.get("author") or ""))
        comment_date = ""
        if cm.get("created_at"):
            try:
                comment_date = datetime.fromisoformat(str(cm["created_at"])).strftime("%d/%m/%Y")
            except Exception:
                pass

        pre_alloc = _pre_allocated(str(r.get("req_processor") or ""))
        pre_alloc_html = (
            f'<span style="color:#1E8449;font-weight:700;background:#D4EDDA;border-radius:4px;padding:2px 6px;font-size:11px">✓ {esc(pre_alloc)}</span>'
            if pre_alloc else '<span style="color:#aaa;font-size:11px">—</span>'
        )

        rows_html += f"""
        <tr>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-family:monospace;font-size:12px;white-space:nowrap">{esc(str(rid))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-weight:600">{esc(str(r.get("acct_name","—")))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(r.get("req_type","—")))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px;color:#6A6D70">{esc(str(r.get("requestor","—")))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(r.get("req_resource","—")))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{esc(str(r.get("req_processor","—")))}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{pre_alloc_html}</td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;text-align:center;font-weight:700;color:{st_color};background:{st_bg};border-radius:4px;white-space:nowrap">{esc(main_st)}<br><span style="font-size:10px;font-weight:400;color:#758ca4">{esc(sub_st)}</span></td>
          <td style="padding:9px 10px;border-bottom:1px solid #E1E2E6;font-size:12px">{comment_text}<br><span style="font-size:10px;color:#758ca4">{comment_author} {comment_date}</span></td>
        </tr>"""

    extra_section = ""
    if payload.extra_note.strip():
        extra_section = f"""<p style="margin:20px 0 6px;font-weight:600;color:#32363A">Additional notes:</p>
        <p style="background:#F5F6F7;border-left:3px solid #0070F2;padding:10px 14px;border-radius:4px;color:#32363A">{esc(payload.extra_note)}</p>"""

    body_html = f"""<!DOCTYPE html><html><body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#32363A;max-width:1000px;margin:0 auto">
  <div style="background:#0070F2;padding:16px 24px;border-radius:8px 8px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">SAP &middot; Melody Request Review</span>
    <span style="color:rgba(255,255,255,.75);font-size:13px;margin-left:16px">{today}</span>
  </div>
  <div style="border:1px solid #E1E2E6;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px">
    <p style="margin:0 0 20px;color:#32363A">Hi Team,<br>Please find below the Melody Request review summary for the selected requests.</p>
    <div style="margin:20px 0 24px 0;border:1px dashed #F0AB00;border-radius:6px;background:#FFFDE7;padding:14px 16px">
      <p style="margin:0 0 8px;font-size:12px;color:#856404;font-weight:600">&#9998; Add your message here before sending:</p>
      <p style="margin:0 0 4px;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
      <p style="margin:4px 0 0;min-height:24px;border-bottom:1px solid #FFD54F">&nbsp;</p>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#F5F6F7">
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase;white-space:nowrap">Req ID</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Account</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Type</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Requestor</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Resource</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Processor</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#1E8449;font-size:11px;text-transform:uppercase">Pre-Allocated DSA</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Status</th>
        <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #0070F2;color:#556b82;font-size:11px;text-transform:uppercase">Last Comment</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    {extra_section}
    <p style="margin-top:24px;color:#32363A;font-size:13px">
      <strong>📎 Attachment:</strong> <em>mr_review_{today.replace('/','')}.xlsx</em> — full detail of the selected requests.
    </p>
    <p style="color:#758ca4;font-size:12px;margin-top:16px;border-top:1px solid #E1E2E6;padding-top:12px">Generated by Digital Hub SA Main App &middot; {today}</p>
  </div>
</body></html>"""

    # ── Build Excel attachment ────────────────────────────────────────────────
    import io as _io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MR Review"

    hdr_font  = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="0070F2")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin      = Side(style="thin", color="E1E2E6")
    bdr       = Border(bottom=thin, right=thin)
    dat_font  = Font(name="Calibri", size=10)

    headers = [
        ("Req ID Sub", 18), ("Req ID Main", 16), ("Req Type", 14),
        ("Account", 30), ("Requestor", 22), ("Resource", 22), ("Processor", 28),
        ("Status Main", 14), ("Sub-Status", 18),
        ("Profit Center", 18), ("Role Area", 18), ("Task Owner", 22),
        ("Opp ID", 16), ("Created", 12), ("Task", 30), ("Last Comment", 40), ("Comment By", 20),
    ]
    fields = [
        "req_id_sub", "req_id_main", "req_type",
        "acct_name", "requestor", "req_resource", "req_processor",
        "req_status_main", "sub_status",
        "req_profit_center", "req_role_area", "req_task_owner",
        "opportunity_id", "req_create_date", "req_task", "_last_comment", "_comment_author",
    ]

    for col, (label, width) in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=label)
        c.font = hdr_font; c.fill = hdr_fill
        c.alignment = hdr_align; c.border = bdr
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    fill_even = PatternFill("solid", fgColor="F0F4FF")
    fill_odd  = PatternFill("solid", fgColor="FFFFFF")

    for ri, rid in enumerate(payload.req_ids, 2):
        r = req_map.get(str(rid), {})
        cm = latest_comment.get(str(rid), {})
        fill = fill_even if ri % 2 == 0 else fill_odd
        row_data = {**r, "_last_comment": cm.get("body", ""), "_comment_author": cm.get("author", "")}
        for ci, field in enumerate(fields, 1):
            c = ws.cell(row=ri, column=ci, value=str(row_data.get(field) or ""))
            c.font = dat_font; c.fill = fill; c.border = bdr
            c.alignment = Alignment(vertical="top", wrap_text=(field in ("req_task", "_last_comment", "req_processor")))

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    buf = _io.BytesIO(); wb.save(buf)
    xlsx_b64 = base64.b64encode(buf.getvalue()).decode()
    xlsx_name = f"mr_review_{today.replace('/', '')}.xlsx"

    subject = f"[MR Review] {len(payload.req_ids)} Request{'s' if len(payload.req_ids)!=1 else ''} — {today}"

    return {"ok": True, "subject": subject, "html_body": body_html}


@router.get("/comments-by-opportunity/{opp_id}")
def get_all_comments_by_opp(opp_id: str, _user=Depends(verify_token)):
    """All comments for every MR request linked to a given opportunity_id."""
    from database import query as db_query
    rows = db_query("""
        SELECT c.id, c.req_id_sub, c.body, c.author,
               TO_NVARCHAR(c.created_at) AS created_at
        FROM melody_request_comments c
        JOIN melody_requests mr ON mr.req_id_sub = c.req_id_sub
        WHERE mr.opportunity_id = ?
        ORDER BY c.req_id_sub, c.created_at ASC
    """, [opp_id])
    return rows


@router.get("/by-opportunity/{opp_id}")
def get_requests_by_opp(opp_id: str, _user=Depends(verify_token)):
    """All MR requests + latest comment for a given opportunity_id."""
    from database import query as db_query
    rows = db_query("""
        SELECT mr.req_id_sub, mr.req_type, mr.req_status_main, mr.sub_status,
               mr.requestor, mr.req_resource, mr.req_processor,
               mr.req_role_area, mr.req_task_owner,
               TO_NVARCHAR(mr.req_create_date)     AS req_create_date,
               TO_NVARCHAR(mr.req_last_action_date) AS req_last_action_date,
               lc.body        AS last_comment,
               lc.author      AS last_comment_author,
               TO_NVARCHAR(lc.created_at) AS last_comment_date,
               COUNT(c2.id)  AS comment_count
        FROM melody_requests mr
        LEFT JOIN (
            SELECT req_id_sub, body, author, created_at
            FROM (
                SELECT req_id_sub, body, author, created_at,
                       ROW_NUMBER() OVER (PARTITION BY req_id_sub ORDER BY created_at DESC) AS _rn
                FROM melody_request_comments
            ) _lc WHERE _rn = 1
        ) lc ON lc.req_id_sub = mr.req_id_sub
        LEFT JOIN melody_request_comments c2 ON c2.req_id_sub = mr.req_id_sub
        WHERE mr.opportunity_id = ?
        GROUP BY mr.req_id_sub, mr.req_type, mr.req_status_main, mr.sub_status,
                 mr.requestor, mr.req_resource, mr.req_processor,
                 mr.req_role_area, mr.req_task_owner,
                 mr.req_create_date, mr.req_last_action_date,
                 lc.body, lc.author, lc.created_at
        ORDER BY mr.req_create_date DESC
    """, [opp_id])
    return rows


# ── Engagement status ─────────────────────────────────────────────────────────

VALID_ENGAGEMENT_STATUSES = ("assigned", "waiting_definition", "return_to")

@router.put("/{req_id_sub}/engagement-status")
def set_engagement_status(req_id_sub: str, payload: dict = Body(...), _user=Depends(verify_token)):
    status = payload.get("engagement_status")
    if status not in VALID_ENGAGEMENT_STATUSES and status is not None:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_ENGAGEMENT_STATUSES}")
    mr.set_engagement_status(req_id_sub, status)
    return {"ok": True, "req_id_sub": req_id_sub, "engagement_status": status}
