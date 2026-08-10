"""
Shared utilities for ETL — HANA-compatible version.
Uses hdbcli (same connection as the main app via env vars).
"""
import logging
import os
import re
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def get_conn():
    from hdbcli import dbapi
    conn = dbapi.connect(
        address=os.environ["DH_DB_HOST"],
        port=int(os.environ.get("DH_DB_PORT", "443")),
        user=os.environ["DH_DB_USER"],
        password=os.environ["DH_DB_PASSWORD"],
        encrypt=True,
        sslValidateCertificate=False,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(conn, table: str, rows: list[dict], conflict_cols: list[str]) -> int:
    """
    Bulk INSERT for HANA. Caller does TRUNCATE/DELETE before calling.
    - Deduplicates by conflict_cols (last occurrence wins)
    - Filters row keys to only columns that exist in the target table
    - Column names uppercased to match HANA storage
    """
    if not rows:
        return 0

    # Fetch actual table columns from HANA metadata
    cur = conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = ? ORDER BY POSITION",
        [table.upper()]
    )
    table_cols = {r[0].lower() for r in cur.fetchall()}

    # Filter row keys to only those that exist in the table
    all_keys = list(rows[0].keys())
    cols = [c for c in all_keys if c.lower() in table_cols]

    if not cols:
        raise ValueError(f"No matching columns found for table {table}")

    # Deduplicate by conflict_cols (last occurrence wins)
    seen: dict = {}
    for row in rows:
        key = tuple(row.get(c) for c in conflict_cols)
        seen[key] = row
    rows = list(seen.values())

    col_str = ", ".join(c.upper() for c in cols)
    ph      = ", ".join("?" for _ in cols)
    sql     = f"INSERT INTO {table} ({col_str}) VALUES ({ph})"

    cur2 = conn.cursor()
    for i in range(0, len(rows), 500):
        for row in rows[i:i + 500]:
            cur2.execute(sql, [row[c] for c in cols])
    return len(rows)


def clean_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if s in ("", "nan", "(Nenhum valor)", "Not Applicable", "N/A"):
        return None
    return s


def clean_num(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", ".")
    if s in ("", "nan", "(Nenhum valor)"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_int(val: Any) -> int | None:
    n = clean_num(val)
    return int(n) if n is not None else None


_DATE_PATTERNS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
    "%b %d, %Y", "%d %b %Y", "%Y%m%d",
]


def clean_date(val: Any) -> date | None:
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
    if s in ("", "nan", "NaT", "None", "NaN", "nat", "(Nenhum valor)"):
        return None
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def truncate(conn, table: str):
    """TRUNCATE a table — HANA cursor is not a context manager so we handle it directly."""
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {table}")


def execute_sql(conn, sql: str, params=None):
    """Run a single SQL statement."""
    cur = conn.cursor()
    cur.execute(sql, params or [])
    return cur


def snap_week() -> date:
    """Today's date — used as snapshot_week key."""
    return date.today()
