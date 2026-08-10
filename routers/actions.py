"""Actions (To-Do) router — CRUD + timeline feed. HANA-compatible version."""
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel
from database import query, execute, execute_returning
from auth import verify_token
import json
import logging
import requests

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/actions", tags=["actions"])

REGIONS = ["Brazil", "Mexico", "Multi-Country LAC", "Argentina", "Chile", "Colombia",
           "Peru", "Other LAC"]

STATUS_COLORS = {"not_started": "Red", "in_progress": "Yellow", "completed": "Green"}

_OUTLOOK_BASE = "https://outlook.office.com/api/v2.0"


def _row(r: dict) -> dict:
    d = dict(r)
    for f in ("created_at", "completed_at"):
        if d.get(f):
            d[f] = d[f].isoformat() if hasattr(d[f], 'isoformat') else str(d[f])
    if d.get("due_date"):
        d["due_date"] = str(d["due_date"])
    # extra_activities stored as JSON string in HANA
    if isinstance(d.get("extra_activities"), str):
        try:
            d["extra_activities"] = json.loads(d["extra_activities"])
        except Exception:
            d["extra_activities"] = []
    # tags stored as comma-separated string in HANA
    if isinstance(d.get("tags"), str):
        d["tags"] = [t.strip() for t in d["tags"].split(",") if t.strip()] if d["tags"] else []
    return d


def _unflag_outlook_message(token: str, message_id: str) -> bool:
    """Remove o flag de uma mensagem no Outlook. Fire-and-forget — não bloqueia a resposta."""
    try:
        r = requests.patch(
            f"{_OUTLOOK_BASE}/me/Messages/{message_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"Flag": {"FlagStatus": "NotFlagged"}},
            timeout=10,
        )
        if r.ok:
            logger.info("Unflagged Outlook message %s", message_id)
            return True
        logger.warning("Unflag failed %s: %s %s", message_id, r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.exception("Unflag exception for %s: %s", message_id, e)
        return False


@router.get("")
def list_actions(
    status: List[str] = Query(default=[]),
    region: List[str] = Query(default=[]),
    search: Optional[str] = None,
    due_from: Optional[str] = None,
    due_to: Optional[str] = None,
    _user=Depends(verify_token),
):
    clauses, params = [], []
    if status:
        ph = ",".join(["%s"] * len(status))
        clauses.append(f"status IN ({ph})"); params.extend(status)
    if region:
        ph = ",".join(["%s"] * len(region))
        clauses.append(f"region IN ({ph})"); params.extend(region)
    if search:
        clauses.append("(title ILIKE %s OR requester ILIKE %s OR account_name ILIKE %s OR opp_id ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if due_from:
        clauses.append("due_date >= %s"); params.append(due_from)
    if due_to:
        clauses.append("due_date <= %s"); params.append(due_to)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = query(f"SELECT * FROM actions {where} ORDER BY due_date ASC NULLS LAST, created_at DESC", params or None)
    return [_row(r) for r in rows]


@router.get("/meta")
def actions_meta(_user=Depends(verify_token)):
    """Distinct values for filter dropdowns."""
    requesters = query("SELECT DISTINCT requester FROM actions WHERE requester IS NOT NULL AND requester != '' ORDER BY requester")
    tag_rows = query("SELECT DISTINCT tags FROM actions WHERE tags IS NOT NULL AND tags != ''")
    tag_set = set()
    for r in tag_rows:
        for t in (r.get("tags") or "").split(","):
            t = t.strip()
            if t:
                tag_set.add(t)
    return {
        "regions": REGIONS,
        "statuses": list(STATUS_COLORS.keys()),
        "requesters": [r["requester"] for r in requesters],
        "tags": sorted(tag_set),
    }


class ActionCreate(BaseModel):
    title: str
    description: str = ""
    region: str = ""
    requester: str = ""
    objective: str = ""
    comments: str = ""
    status: str = "not_started"
    opp_id: Optional[str] = None
    account_name: Optional[str] = None
    extra_activities: list = []
    due_date: Optional[str] = None
    created_by: str = ""
    tags: list = []
    outlook_message_id: Optional[str] = None


@router.post("")
def create_action(body: ActionCreate, _user=Depends(verify_token)):
    new_id = execute_returning(
        """INSERT INTO actions
           (title, description, region, requester, objective, comments, status,
            opp_id, account_name, extra_activities, due_date, created_by, tags,
            outlook_message_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [
            body.title, body.description, body.region, body.requester,
            body.objective, body.comments, body.status,
            body.opp_id, body.account_name,
            json.dumps(body.extra_activities),
            body.due_date or None, body.created_by or _user.get("name", ""),
            ",".join(str(t) for t in body.tags),
            body.outlook_message_id,
        ]
    )
    row = query("SELECT * FROM actions WHERE id = %s", [new_id])
    return _row(dict(row[0])) if row else {"id": new_id}


class ActionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    region: Optional[str] = None
    requester: Optional[str] = None
    objective: Optional[str] = None
    comments: Optional[str] = None
    status: Optional[str] = None
    opp_id: Optional[str] = None
    account_name: Optional[str] = None
    extra_activities: Optional[list] = None
    due_date: Optional[str] = None
    tags: Optional[list] = None


@router.put("/{action_id}")
def update_action(
    action_id: int,
    body: ActionUpdate,
    background_tasks: BackgroundTasks,
    _user=Depends(verify_token),
    x_cal_auth: Optional[str] = Header(default=None),
):
    updates, params = [], []
    mapping = {
        "title": body.title, "description": body.description, "region": body.region,
        "requester": body.requester, "objective": body.objective, "comments": body.comments,
        "status": body.status, "opp_id": body.opp_id, "account_name": body.account_name,
        "due_date": body.due_date,
    }
    for col, val in mapping.items():
        if val is not None:
            updates.append(f"{col} = %s"); params.append(val)
    if body.tags is not None:
        updates.append("tags = %s")
        params.append(",".join(str(t) for t in body.tags))
    if body.extra_activities is not None:
        updates.append("extra_activities = %s")
        params.append(json.dumps(body.extra_activities))
    if body.status == "completed":
        updates.append("completed_at = CURRENT_TIMESTAMP")
    elif body.status in ("not_started", "in_progress"):
        updates.append("completed_at = NULL")
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(action_id)
    execute(f"UPDATE actions SET {', '.join(updates)} WHERE id = %s", params)
    rows = query("SELECT * FROM actions WHERE id = %s", [action_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Action not found")

    action = _row(dict(rows[0]))

    # Auto-unflag no Outlook quando concluído, em background
    if body.status == "completed" and x_cal_auth and action.get("outlook_message_id"):
        background_tasks.add_task(
            _unflag_outlook_message, x_cal_auth, action["outlook_message_id"]
        )

    return action


@router.delete("/{action_id}")
def delete_action(action_id: int, _user=Depends(verify_token)):
    n = execute("DELETE FROM actions WHERE id = %s", [action_id])
    if n == 0:
        raise HTTPException(status_code=404, detail="Action not found")
    return {"ok": True}
