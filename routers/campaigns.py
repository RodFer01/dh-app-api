import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, List
from auth import verify_token
from database import execute, execute_returning, query, query_one, IS_HANA
from datetime import date

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


class CampaignCreate(BaseModel):
    name: str
    owner: Optional[str] = None
    objective: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = "active"
    results: Optional[str] = None


class SessionsPatch(BaseModel):
    session_ids: List[str]


def _ensure_sessions_column():
    try:
        if IS_HANA:
            exists = query_one(
                "SELECT COUNT(*) AS cnt FROM TABLE_COLUMNS "
                "WHERE TABLE_NAME='CAMPAIGNS' AND COLUMN_NAME='SPARK_SESSIONS_JSON'"
            )
            cnt = exists.get("cnt") or exists.get("CNT") or 0 if exists else 0
            if not cnt:
                execute("ALTER TABLE campaigns ADD (spark_sessions_json NCLOB DEFAULT NULL)")
        else:
            execute("ALTER TABLE campaigns ADD COLUMN spark_sessions_json NCLOB DEFAULT NULL")
    except Exception:
        pass


class ContactIn(BaseModel):
    prospect_id: str
    contact_name: Optional[str] = None
    account_bp: Optional[str] = None
    account_name: Optional[str] = None
    contact_email: Optional[str] = None


@router.get("/contacts/lookup")
def lookup_campaign_contacts(_=Depends(verify_token)):
    """Returns {prospect_id: [{id, name}...]} for all campaign memberships."""
    rows = query(
        "SELECT cc.prospect_id, cc.campaign_id, c.name as campaign_name "
        "FROM campaign_contacts cc JOIN campaigns c ON cc.campaign_id = c.id"
    )
    result: dict = {}
    for r in rows:
        pid = r["prospect_id"]
        if pid not in result:
            result[pid] = []
        result[pid].append({"id": r["campaign_id"], "name": r["campaign_name"]})
    return result


@router.get("/")
def list_campaigns(_=Depends(verify_token)):
    return query(
        "SELECT id, name, owner, objective, start_date, end_date, status, results, created_at "
        "FROM campaigns ORDER BY created_at DESC"
    )


@router.post("/")
def create_campaign(body: CampaignCreate, current_user=Depends(verify_token)):
    new_id = execute_returning(
        "INSERT INTO campaigns (name, owner, objective, start_date, end_date, status, results, created_by) "
        "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (body.name, body.owner, body.objective, body.start_date, body.end_date,
         body.status, body.results, current_user.get("email", ""))
    )
    return {"ok": True, "id": new_id}


@router.put("/{campaign_id}")
def update_campaign(campaign_id: int, body: CampaignCreate, _=Depends(verify_token)):
    execute(
        "UPDATE campaigns SET name=?, owner=?, objective=?, start_date=?, end_date=?, status=?, results=? "
        "WHERE id=?",
        (body.name, body.owner, body.objective, body.start_date, body.end_date,
         body.status, body.results, campaign_id)
    )
    return {"ok": True}


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int, _=Depends(verify_token)):
    execute("DELETE FROM campaign_contacts WHERE campaign_id=?", (campaign_id,))
    execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    return {"ok": True}


@router.get("/{campaign_id}/contacts")
def list_contacts(campaign_id: int, _=Depends(verify_token)):
    return query(
        "SELECT id, prospect_id, contact_name, account_bp, account_name, added_by, added_at "
        "FROM campaign_contacts WHERE campaign_id=? ORDER BY added_at DESC",
        (campaign_id,)
    )


@router.post("/{campaign_id}/contacts")
def add_contacts(campaign_id: int, contacts: List[ContactIn], current_user=Depends(verify_token)):
    added = 0
    for c in contacts:
        existing = query(
            "SELECT prospect_id FROM campaign_contacts WHERE campaign_id=? AND prospect_id=?",
            (campaign_id, c.prospect_id)
        )
        if not existing:
            try:
                execute(
                    "INSERT INTO campaign_contacts "
                    "(campaign_id, prospect_id, contact_name, account_bp, account_name, contact_email, added_by) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (campaign_id, c.prospect_id, c.contact_name, c.account_bp, c.account_name,
                     c.contact_email, current_user.get("email", ""))
                )
            except Exception:
                # contact_email column may not exist yet (setup-ess not run)
                execute(
                    "INSERT INTO campaign_contacts "
                    "(campaign_id, prospect_id, contact_name, account_bp, account_name, added_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (campaign_id, c.prospect_id, c.contact_name, c.account_bp, c.account_name,
                     current_user.get("email", ""))
                )
            added += 1
    return {"ok": True, "added": added}


@router.delete("/{campaign_id}/contacts/{prospect_id}")
def remove_contact(campaign_id: int, prospect_id: str, _=Depends(verify_token)):
    execute(
        "DELETE FROM campaign_contacts WHERE campaign_id=? AND prospect_id=?",
        (campaign_id, prospect_id)
    )
    return {"ok": True}


class EmailPatch(BaseModel):
    contact_email: str


@router.get("/{campaign_id}/sessions")
def get_campaign_sessions(campaign_id: int, _=Depends(verify_token)):
    _ensure_sessions_column()
    rows = query("SELECT spark_sessions_json FROM campaigns WHERE id=?", (campaign_id,))
    if rows and rows[0].get("spark_sessions_json"):
        try:
            return json.loads(rows[0]["spark_sessions_json"])
        except Exception:
            return []
    return []


@router.patch("/{campaign_id}/sessions")
def update_campaign_sessions(campaign_id: int, body: SessionsPatch, _=Depends(verify_token)):
    _ensure_sessions_column()
    execute(
        "UPDATE campaigns SET spark_sessions_json=? WHERE id=?",
        (json.dumps(body.session_ids), campaign_id)
    )
    return {"ok": True}


@router.patch("/{campaign_id}/contacts/{prospect_id}/email")
def patch_contact_email(campaign_id: int, prospect_id: str, body: EmailPatch, _=Depends(verify_token)):
    execute(
        "UPDATE campaign_contacts SET contact_email=? WHERE campaign_id=? AND prospect_id=?",
        (body.contact_email, campaign_id, prospect_id)
    )
    return {"ok": True}
