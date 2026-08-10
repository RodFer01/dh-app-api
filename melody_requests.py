"""
melody_requests.py — DB access layer for melody_requests, comments and upload log.
HANA-compatible version: uses database.py helpers instead of psycopg2 directly.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Any
import pandas as pd
from database import query, query_one, execute, execute_returning

# Maps Excel column header → DB column name
COLUMN_MAP: dict[str, str] = {
    "Req Create Date":          "req_create_date",
    "Req ID Sub":               "req_id_sub",
    "Req ID":                   "req_id",
    "Req Type":                 "req_type",
    "Opportunity ID":           "opportunity_id",
    "Req Status":               "req_status",
    "Req Status Sub":           "sub_status",
    "Sub Status":               "sub_status",
    "Req Role":                 "req_role",
    "Req Solution":             "req_solution",
    "Req Industry":             "req_industry",
    "Req Requestor":            "requestor",
    "Requestor":                "requestor",
    "Requestor ID":             "requestor_id",
    "Req Processor":            "req_processor",
    "Req Resource":             "req_resource",
    "Acct ID":                  "acct_id",
    "Acct Name":                "acct_name",
    "Req Create Time":          "req_create_time",
    "Req Engagement":           "req_engagement",
    "Req ID Main":              "req_id_main",
    "Req Last Action Date":     "req_last_action_date",
    "Req Last Action Time":     "req_last_action_time",
    "Req Profit Center":        "req_profit_center",
    "Req Role Area":            "req_role_area",
    "Req Solution Digital":     "req_solution_digital",
    "Req Status Main":          "req_status_main",
    "Req Task":                 "req_task",
    "Req Task - Action Taken":  "req_task_action_taken",
    "Req Task - Owner":         "req_task_owner",
    "Req Task - Owner Role":    "req_task_owner_role",
    "Req Task Owner Role":      "req_task_owner_role",
}

DATE_COLS: set[str] = {"req_create_date", "req_last_action_date"}
_DIFF_COLS = [c for c in COLUMN_MAP.values() if c != "req_id_sub"]


def _clean(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return None if s in ("", "nan", "NaT", "None", "NaN") else s


def _clean_date(val: Any) -> date | None:
    if val is None:
        return None
    try:
        import pandas as _pd
        if _pd.isna(val):
            return None
    except Exception:
        pass
    if isinstance(val, (date, datetime)):
        return val.date() if isinstance(val, datetime) else val
    s = str(val).strip()
    if s in ("", "nan", "NaT", "None", "NaN", "nat"):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load_from_excel(file) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    df = pd.read_excel(file, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    normalised = {c.lower().strip(): c for c in df.columns}
    rename: dict[str, str] = {}
    missing: list[str] = []

    for excel_col, db_col in COLUMN_MAP.items():
        key = excel_col.lower().strip()
        if excel_col in df.columns:
            rename[excel_col] = db_col
        elif key in normalised:
            rename[normalised[key]] = db_col
        else:
            missing.append(excel_col)

    if missing:
        warnings.append(f"Columns not found (will be NULL): {', '.join(missing)}")

    df = df.rename(columns=rename)
    df = df.loc[:, ~df.columns.duplicated(keep='first')]

    all_db_cols = list(dict.fromkeys(COLUMN_MAP.values()))
    for c in all_db_cols:
        if c not in df.columns:
            df[c] = None
    df = df[all_db_cols].copy()

    for col in DATE_COLS:
        df[col] = df[col].apply(_clean_date)
    for col in df.columns:
        if col not in DATE_COLS:
            df[col] = df[col].apply(_clean)

    before = len(df)
    df = df[df["req_id_sub"].notna()].reset_index(drop=True)
    dropped = before - len(df)
    if dropped:
        warnings.append(f"{dropped} rows skipped (missing Req ID Sub)")

    dupes = df["req_id_sub"].duplicated(keep="last").sum()
    if dupes:
        df = df.drop_duplicates(subset=["req_id_sub"], keep="last").reset_index(drop=True)
        warnings.append(f"{dupes} duplicate Req ID Sub values — kept last occurrence")

    return df, warnings


def diff_with_db(df_new: pd.DataFrame) -> dict:
    incoming_ids = set(df_new["req_id_sub"].dropna())

    if not incoming_ids:
        return {"to_insert": [], "to_update": [], "unchanged": [],
                "only_in_db": [], "changed_cols": {}}

    try:
        ph = ",".join(["%s"] * len(incoming_ids))
        existing_rows = query(
            f"SELECT * FROM melody_requests WHERE req_id_sub IN ({ph})",
            list(incoming_ids)
        )
        df_existing = pd.DataFrame(existing_rows) if existing_rows else pd.DataFrame()
        all_db_ids = {r["req_id_sub"] for r in query("SELECT req_id_sub FROM melody_requests")}
    except Exception:
        # Table may not exist yet — treat all incoming rows as new
        df_existing = pd.DataFrame()
        all_db_ids = set()

    existing_ids = set(df_existing["req_id_sub"].tolist()) if not df_existing.empty else set()
    to_insert  = sorted(incoming_ids - existing_ids)
    only_in_db = sorted(all_db_ids - incoming_ids)

    to_update: list[str] = []
    unchanged: list[str] = []
    changed_cols: dict[str, list[str]] = {}

    if not df_existing.empty:
        df_existing = df_existing.set_index("req_id_sub")
        for _, new_row in df_new[df_new["req_id_sub"].isin(existing_ids)].iterrows():
            rid = new_row["req_id_sub"]
            old_row = df_existing.loc[rid]
            diffs = []
            for col in _DIFF_COLS:
                new_val = _clean(new_row.get(col))
                old_val = _clean(old_row.get(col)) if col in old_row.index else None
                if col in DATE_COLS:
                    new_val = _clean_date(new_row.get(col))
                    old_val = _clean_date(old_row.get(col)) if col in old_row.index else None
                if str(new_val) != str(old_val):
                    diffs.append(col)
            if diffs:
                to_update.append(rid)
                changed_cols[rid] = diffs
            else:
                unchanged.append(rid)

    return {
        "to_insert":    to_insert,
        "to_update":    to_update,
        "unchanged":    unchanged,
        "only_in_db":   only_in_db,
        "changed_cols": changed_cols,
    }


def get_summary() -> dict:
    try:
        r = query_one("""
            SELECT
                COUNT(*) AS total_rows,
                COUNT(DISTINCT opportunity_id) AS total_opps,
                COUNT(DISTINCT req_id_sub) AS total_requests,
                COUNT(CASE WHEN req_status_main NOT IN ('Closed','Cancelled') THEN 1 END) AS open_count,
                COUNT(CASE WHEN req_status_main IN ('Closed','Cancelled') THEN 1 END) AS closed_count
            FROM melody_requests
        """)
        comments = query_one("SELECT COUNT(*) AS n FROM melody_request_comments")
        result = dict(r) if r else {}
        result["total_comments"] = comments["n"] if comments else 0
        return result
    except Exception:
        return {"total_rows": 0, "total_opps": 0, "total_requests": 0,
                "open_count": 0, "closed_count": 0, "total_comments": 0}


def _get_table_cols(table: str) -> set[str]:
    """Return actual column names (lowercase) from HANA metadata."""
    rows = query(
        "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = %s ORDER BY POSITION",
        [table.upper()]
    )
    return {r["column_name"].lower() for r in rows}


def _safe_val(v):
    """Convert NaN/NaT to None for HANA compatibility."""
    if v is None:
        return None
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    return v


def _insert_rows(rows: list[dict], all_db_cols: list[str]):
    if not rows:
        return
    actual = _get_table_cols("melody_requests")
    cols = [c for c in all_db_cols if c.lower() in actual]
    col_str = ", ".join(c.upper() for c in cols)
    ph      = ", ".join("%s" for _ in cols)
    sql     = f"INSERT INTO melody_requests ({col_str}) VALUES ({ph})"
    for row in rows:
        execute(sql, [_safe_val(row.get(c)) for c in cols])


def _update_rows(rows: list[dict], all_db_cols: list[str]):
    if not rows:
        return
    actual = _get_table_cols("melody_requests")
    update_cols = [c for c in all_db_cols if c != "req_id_sub" and c.lower() in actual]
    set_str = ", ".join(f"{c.upper()} = %s" for c in update_cols)
    sql = f"UPDATE melody_requests SET {set_str}, UPLOADED_AT = CURRENT_TIMESTAMP WHERE REQ_ID_SUB = %s"
    for row in rows:
        params = [_safe_val(row.get(c)) for c in update_cols] + [row["req_id_sub"]]
        execute(sql, params)


def upsert_requests(df: pd.DataFrame, filename: str = "", uploaded_by: str = "") -> dict:
    if df.empty:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    diff = diff_with_db(df)
    rows_map = df.set_index("req_id_sub").to_dict("index")
    all_db_cols = list(dict.fromkeys(COLUMN_MAP.values()))

    insert_rows = [{"req_id_sub": rid, **rows_map[rid]} for rid in diff["to_insert"] if rid in rows_map]
    update_rows = [{"req_id_sub": rid, **rows_map[rid]} for rid in diff["to_update"] if rid in rows_map]
    unchanged   = len(diff["unchanged"])

    _insert_rows(insert_rows, all_db_cols)
    _update_rows(update_rows, all_db_cols)

    _log_upload(filename=filename, mode="merge", rows_in_file=len(df),
                rows_inserted=len(insert_rows), rows_updated=len(update_rows),
                rows_unchanged=unchanged, rows_removed=0, uploaded_by=uploaded_by)

    return {"inserted": len(insert_rows), "updated": len(update_rows), "unchanged": unchanged}


def replace_requests(df: pd.DataFrame, filename: str = "", uploaded_by: str = "") -> dict:
    if df.empty:
        return {"inserted": 0, "removed": 0, "comments_preserved": 0}

    all_db_cols = list(dict.fromkeys(COLUMN_MAP.values()))

    # Step 1 — snapshot comments
    all_comments = query(
        "SELECT req_id_sub, author, body, created_at, updated_at FROM melody_request_comments"
    )

    # Step 2 — count existing rows
    removed_row = query_one("SELECT COUNT(*) AS n FROM melody_requests")
    removed = removed_row["n"] if removed_row else 0

    # Step 3 — delete all (HANA doesn't support TRUNCATE CASCADE reliably)
    execute("DELETE FROM melody_request_comments WHERE 1=1")
    execute("DELETE FROM melody_requests WHERE 1=1")

    # Step 4 — insert new rows
    rows = df.where(df.notna(), None).to_dict("records")
    _insert_rows(rows, all_db_cols)

    # Step 5 — restore comments for rows still in file
    incoming_ids = set(df["req_id_sub"].dropna())
    comments_to_restore = [c for c in all_comments if c["req_id_sub"] in incoming_ids]
    orphan_count = len(all_comments) - len(comments_to_restore)

    for c in comments_to_restore:
        execute(
            "INSERT INTO melody_request_comments (req_id_sub, author, body, created_at, updated_at) VALUES (%s,%s,%s,%s,%s)",
            (c["req_id_sub"], c["author"], c["body"], c["created_at"], c["updated_at"])
        )

    _log_upload(filename=filename, mode="replace", rows_in_file=len(df),
                rows_inserted=len(rows), rows_updated=0,
                rows_unchanged=0, rows_removed=removed, uploaded_by=uploaded_by)

    return {
        "inserted":           len(rows),
        "removed":            removed,
        "comments_preserved": len(comments_to_restore),
        "comments_orphaned":  orphan_count,
    }


def _log_upload(filename: str, mode: str, rows_in_file: int,
                rows_inserted: int, rows_updated: int, rows_unchanged: int,
                rows_removed: int, uploaded_by: str):
    try:
        execute(
            """INSERT INTO melody_request_upload_log
               (FILENAME, MODE, ROWS_IN_FILE, ROWS_INSERTED, ROWS_UPDATED,
                ROWS_UNCHANGED, ROWS_REMOVED, UPLOADED_BY)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (filename, mode, rows_in_file, rows_inserted, rows_updated,
             rows_unchanged, rows_removed, uploaded_by or None)
        )
    except Exception:
        pass  # log table may not exist — don't fail the upload


def get_upload_log(limit: int = 20) -> list[dict]:
    try:
        rows = query(
            f"SELECT TOP {int(limit)} id, uploaded_at, filename, mode, rows_in_file, rows_inserted, rows_updated, rows_unchanged, rows_removed, uploaded_by FROM melody_request_upload_log ORDER BY uploaded_at DESC"
        )
    except Exception:
        return []
    result = []
    for r in rows:
        rec = dict(r)
        if rec.get("uploaded_at"):
            rec["uploaded_at"] = str(rec["uploaded_at"])
        result.append(rec)
    return result


def get_comments(req_id_sub: str) -> list[dict]:
    rows = query(
        "SELECT id, author, body, created_at, updated_at FROM melody_request_comments WHERE req_id_sub = %s ORDER BY created_at ASC",
        (req_id_sub,)
    )
    result = []
    for r in rows:
        rec = dict(r)
        for f in ("created_at", "updated_at"):
            if rec.get(f):
                rec[f] = str(rec[f])
        result.append(rec)
    return result


def add_comment(req_id_sub: str, body: str, author: str = "") -> int:
    new_id = execute_returning(
        "INSERT INTO melody_request_comments (req_id_sub, author, body) VALUES (%s, %s, %s)",
        (req_id_sub, author, body)
    )
    return new_id


def update_comment(comment_id: int, body: str):
    execute(
        "UPDATE melody_request_comments SET body = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (body, comment_id)
    )


def delete_comment(comment_id: int):
    execute("DELETE FROM melody_request_comments WHERE id = %s", (comment_id,))


def set_engagement_status(req_id_sub: str, status: str | None):
    """MERGE INTO engagement status table — creates if not exists."""
    try:
        existing = query_one(
            "SELECT req_id_sub FROM mr_engagement_status WHERE req_id_sub = %s",
            (req_id_sub,)
        )
        if existing:
            execute(
                "UPDATE mr_engagement_status SET engagement_status = %s, updated_at = CURRENT_TIMESTAMP WHERE req_id_sub = %s",
                (status, req_id_sub)
            )
        else:
            execute(
                "INSERT INTO mr_engagement_status (req_id_sub, engagement_status) VALUES (%s, %s)",
                (req_id_sub, status)
            )
    except Exception:
        # Table may not exist — create it and retry
        execute("""
            CREATE TABLE IF NOT EXISTS mr_engagement_status (
                req_id_sub NVARCHAR(50) PRIMARY KEY,
                engagement_status NVARCHAR(50),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        execute(
            "INSERT INTO mr_engagement_status (req_id_sub, engagement_status) VALUES (%s, %s)",
            (req_id_sub, status)
        )
