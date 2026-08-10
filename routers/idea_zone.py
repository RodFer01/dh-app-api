"""IdeaZone router — feature request & voting board."""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from database import query, query_one, execute, execute_returning
from auth import verify_token

router = APIRouter(prefix="/api/idea-zone", tags=["idea-zone"])


def _ensure_tables():
    """Create tables if they don't exist yet."""
    try:
        execute("""
            CREATE TABLE idea_zone (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title NVARCHAR(300) NOT NULL,
                description NVARCHAR(2000),
                indicators NVARCHAR(1000),
                data_needed NVARCHAR(1000),
                status NVARCHAR(50) DEFAULT 'open',
                author NVARCHAR(200),
                author_email NVARCHAR(200),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception:
        pass
    try:
        execute("""
            CREATE TABLE idea_zone_votes (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                idea_id INTEGER NOT NULL,
                voter_email NVARCHAR(200) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception:
        pass
    try:
        execute("""
            CREATE TABLE idea_zone_comments (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                idea_id INTEGER NOT NULL,
                author NVARCHAR(200),
                body NVARCHAR(2000),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception:
        pass


class IdeaCreate(BaseModel):
    title: str
    description: str = ""
    indicators: str = ""
    data_needed: str = ""


class IdeaUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    indicators: Optional[str] = None
    data_needed: Optional[str] = None
    status: Optional[str] = None


class CommentCreate(BaseModel):
    body: str


def _serialize(row: dict) -> dict:
    r = dict(row)
    for f in ("created_at", "updated_at"):
        if r.get(f):
            r[f] = str(r[f])
    r["user_voted"] = bool(r.get("user_voted", 0))
    r["vote_count"] = int(r.get("vote_count") or 0)
    r["comment_count"] = int(r.get("comment_count") or 0)
    return r


@router.get("")
def list_ideas(_user=Depends(verify_token)):
    _ensure_tables()
    email = _user.get("email", "")
    rows = query("""
        SELECT i.id, i.title, i.description, i.indicators, i.data_needed,
               i.status, i.author, i.author_email,
               TO_NVARCHAR(i.created_at, 'YYYY-MM-DD HH24:MI') AS created_at,
               TO_NVARCHAR(i.updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at,
               COUNT(DISTINCT v.id) AS vote_count,
               COUNT(DISTINCT CASE WHEN v.voter_email = %s THEN v.id END) AS user_voted,
               COUNT(DISTINCT c.id) AS comment_count
        FROM idea_zone i
        LEFT JOIN idea_zone_votes v ON v.idea_id = i.id
        LEFT JOIN idea_zone_comments c ON c.idea_id = i.id
        GROUP BY i.id, i.title, i.description, i.indicators, i.data_needed,
                 i.status, i.author, i.author_email, i.created_at, i.updated_at
        ORDER BY vote_count DESC, i.created_at DESC
    """, [email])
    return [_serialize(r) for r in rows]


@router.post("")
def create_idea(body: IdeaCreate, _user=Depends(verify_token)):
    _ensure_tables()
    new_id = execute_returning(
        """INSERT INTO idea_zone (title, description, indicators, data_needed, author, author_email)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [body.title.strip(), body.description, body.indicators, body.data_needed,
         _user.get("name", ""), _user.get("email", "")]
    )
    return {"ok": True, "id": new_id}


@router.put("/{idea_id}")
def update_idea(idea_id: int, body: IdeaUpdate, _user=Depends(verify_token)):
    idea = query_one("SELECT author_email, status FROM idea_zone WHERE id = %s", [idea_id])
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    if _user.get("role") != "admin" and idea["author_email"] != _user.get("email"):
        raise HTTPException(status_code=403, detail="Only the author or admin can edit this idea")

    updates, params = [], []
    if body.title is not None:
        updates.append("title = %s"); params.append(body.title.strip())
    if body.description is not None:
        updates.append("description = %s"); params.append(body.description)
    if body.indicators is not None:
        updates.append("indicators = %s"); params.append(body.indicators)
    if body.data_needed is not None:
        updates.append("data_needed = %s"); params.append(body.data_needed)
    if body.status is not None:
        updates.append("status = %s"); params.append(body.status)

    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        execute(f"UPDATE idea_zone SET {', '.join(updates)} WHERE id = %s", params + [idea_id])
    return {"ok": True}


@router.delete("/{idea_id}")
def delete_idea(idea_id: int, _user=Depends(verify_token)):
    idea = query_one("SELECT author_email FROM idea_zone WHERE id = %s", [idea_id])
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    if _user.get("role") != "admin" and idea["author_email"] != _user.get("email"):
        raise HTTPException(status_code=403, detail="Only the author or admin can delete this idea")
    execute("DELETE FROM idea_zone_comments WHERE idea_id = %s", [idea_id])
    execute("DELETE FROM idea_zone_votes WHERE idea_id = %s", [idea_id])
    execute("DELETE FROM idea_zone WHERE id = %s", [idea_id])
    return {"ok": True}


@router.post("/{idea_id}/vote")
def toggle_vote(idea_id: int, _user=Depends(verify_token)):
    _ensure_tables()
    email = _user.get("email", "")
    existing = query_one(
        "SELECT id FROM idea_zone_votes WHERE idea_id = %s AND voter_email = %s",
        [idea_id, email]
    )
    if existing:
        execute("DELETE FROM idea_zone_votes WHERE idea_id = %s AND voter_email = %s", [idea_id, email])
        voted = False
    else:
        execute("INSERT INTO idea_zone_votes (idea_id, voter_email) VALUES (%s, %s)", [idea_id, email])
        voted = True

    count_row = query_one("SELECT COUNT(*) AS n FROM idea_zone_votes WHERE idea_id = %s", [idea_id])
    return {"voted": voted, "count": int(count_row["n"]) if count_row else 0}


@router.get("/{idea_id}/comments")
def get_comments(idea_id: int, _user=Depends(verify_token)):
    rows = query(
        "SELECT id, author, body, TO_NVARCHAR(created_at, 'YYYY-MM-DD HH24:MI') AS created_at "
        "FROM idea_zone_comments WHERE idea_id = %s ORDER BY created_at ASC",
        [idea_id]
    )
    return [dict(r) for r in rows]


@router.post("/{idea_id}/comments")
def add_comment(idea_id: int, body: CommentCreate, _user=Depends(verify_token)):
    new_id = execute_returning(
        "INSERT INTO idea_zone_comments (idea_id, author, body) VALUES (%s, %s, %s)",
        [idea_id, _user.get("name", ""), body.body.strip()]
    )
    return {"ok": True, "id": new_id}


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: int, _user=Depends(verify_token)):
    execute("DELETE FROM idea_zone_comments WHERE id = %s", [comment_id])
    return {"ok": True}
