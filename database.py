"""
dh-app-api — shared DB connection and helpers.
Supports both:
  - SAP HANA Cloud (hdbcli) — when DH_DB_DRIVER=hana
  - PostgreSQL (psycopg2)   — default (DH_DB_DRIVER=postgres or unset)
"""
import os
import re
from contextlib import contextmanager
from typing import Any

DB_DRIVER = os.getenv("DH_DB_DRIVER", "hana").lower()

# ── HANA Cloud config ─────────────────────────────────────────────────────────
HANA_CONFIG = {
    "address":  os.getenv("DH_DB_HOST",     "localhost"),
    "port":     int(os.getenv("DH_DB_PORT", "443")),
    "user":     os.getenv("DH_DB_USER",     "DBADMIN"),
    "password": os.getenv("DH_DB_PASSWORD", ""),
    "encrypt":  True,
    "sslValidateCertificate": True,
}

# ── PostgreSQL config ─────────────────────────────────────────────────────────
PG_CONFIG = {
    "host":     os.getenv("DH_DB_HOST",     "localhost"),
    "port":     int(os.getenv("DH_DB_PORT", "5432")),
    "dbname":   os.getenv("DH_DB_NAME",     "dh_main_app"),
    "user":     os.getenv("DH_DB_USER",     "postgres"),
    "password": os.getenv("DH_DB_PASSWORD", ""),
}

IS_HANA = DB_DRIVER == "hana"


# ── SQL translation helpers ───────────────────────────────────────────────────

def _adapt_sql(sql: str) -> str:
    """Translate PostgreSQL SQL to HANA-compatible SQL when using hdbcli."""
    if not IS_HANA:
        return sql

    # %s → ?  (placeholder)
    sql = sql.replace("%s", "?")

    # array_agg(DISTINCT col ORDER BY col) → STRING_AGG(col, ',') ORDER BY not supported in HANA STRING_AGG
    # DISTINCT also not supported — use plain STRING_AGG (duplicates are rare in filter lists)
    sql = re.sub(
        r'array_agg\s*\(\s*DISTINCT\s+(\w+(?:\.\w+)?)\s+ORDER\s+BY\s+\w+(?:\.\w+)?\s*\)',
        r"STRING_AGG(\1, ',')",
        sql, flags=re.IGNORECASE
    )
    # array_agg(col ORDER BY col) → STRING_AGG(col, ',')
    sql = re.sub(
        r'array_agg\s*\(\s*(\w+(?:\.\w+)?)\s+ORDER\s+BY\s+\w+(?:\.\w+)?\s*\)',
        r"STRING_AGG(\1, ',')",
        sql, flags=re.IGNORECASE
    )
    # array_agg(col) → STRING_AGG(col, ',')
    sql = re.sub(
        r'array_agg\s*\(\s*(\w+(?:\.\w+)?)\s*\)',
        r"STRING_AGG(\1, ',')",
        sql, flags=re.IGNORECASE
    )

    # JOIN ... USING (col) → JOIN ... ON t1.col = t2.col
    # HANA doesn't support USING — we need to find the table aliases
    # Simple approach: convert USING (col) to ON col = col (works when table.col is unambiguous in HANA)
    # Full fix requires knowing the table aliases; for now remove USING and let HANA infer
    # Pattern: [LEFT|RIGHT|INNER|...] JOIN alias USING (col) → JOIN alias ON alias.col = prev_alias.col
    # Simplified: replace USING (col) with a known-working equivalent
    sql = re.sub(
        r'\bUSING\s*\((\w+)\)',
        lambda m: f"ON {m.group(1)} = {m.group(1)}",  # placeholder — callers must use explicit ON
        sql, flags=re.IGNORECASE
    )
    sql = re.sub(
        r"STRING_AGG\s*\(\s*DISTINCT\s+",
        "STRING_AGG(",
        sql, flags=re.IGNORECASE
    )

    # ── FILTER clause rewrites ────────────────────────────────────────────────
    # Problem: the WHERE condition may contain nested parens e.g. NOT IN ('a','b')
    # We use a helper to extract balanced content after FILTER (WHERE
    def _rewrite_filter(sql_frag: str) -> str:
        """
        Replace all AGG(...) FILTER (WHERE cond) with CASE WHEN equivalents.
        Uses character-by-character parsing to handle nested parens in conditions.
        """
        result = []
        i = 0
        n = len(sql_frag)

        while i < n:
            # Find next occurrence of FILTER (WHERE — this is what we're replacing
            filter_match = re.search(r'\)\s*FILTER\s*\(\s*WHERE\s+', sql_frag[i:], re.IGNORECASE)
            if not filter_match:
                result.append(sql_frag[i:])
                break

            # The FILTER starts at i + filter_match.start() + 1 (after the closing ')')
            filter_close_paren_pos = i + filter_match.start()  # position of ')' before FILTER

            # Walk backwards from filter_close_paren_pos to find the matching open paren
            # and the function name (COUNT, SUM, etc.)
            depth = 1
            j = filter_close_paren_pos - 1
            while j >= i and depth > 0:
                if sql_frag[j] == ')':
                    depth += 1
                elif sql_frag[j] == '(':
                    depth -= 1
                elif sql_frag[j] in ("'", '"'):
                    q = sql_frag[j]
                    j -= 1
                    while j >= i and sql_frag[j] != q:
                        j -= 1
                j -= 1

            open_paren_pos = j + 1  # position of '(' matching the closing paren

            # Find function name before '('
            k = open_paren_pos - 1
            while k >= i and sql_frag[k] in ' \t\n':
                k -= 1
            func_end = k + 1
            while k >= i and (sql_frag[k].isalnum() or sql_frag[k] == '_'):
                k -= 1
            func_name = sql_frag[k+1:func_end].upper()

            # Check for DISTINCT keyword inside the parens
            inner_content = sql_frag[open_paren_pos+1:filter_close_paren_pos]
            has_distinct = inner_content.lstrip().upper().startswith('DISTINCT')
            if has_distinct:
                actual_inner = re.sub(r'^\s*DISTINCT\s+', '', inner_content, flags=re.IGNORECASE)
            else:
                actual_inner = inner_content

            # Emit everything before this aggregation call
            result.append(sql_frag[i:k+1])

            # Now extract the balanced WHERE condition after FILTER (WHERE
            pos = i + filter_match.end()
            depth = 1
            while pos < n and depth > 0:
                if sql_frag[pos] == '(':
                    depth += 1
                elif sql_frag[pos] == ')':
                    depth -= 1
                elif sql_frag[pos] in ("'", '"'):
                    q = sql_frag[pos]; pos += 1
                    while pos < n and sql_frag[pos] != q:
                        pos += 1
                pos += 1
            cond = sql_frag[i + filter_match.end(): pos - 1].strip()

            # Build replacement based on function
            inner = actual_inner.strip()
            if func_name == 'COUNT':
                if inner == '*':
                    replacement = f"COUNT(CASE WHEN {cond} THEN 1 END)"
                elif has_distinct:
                    replacement = f"COUNT(DISTINCT CASE WHEN {cond} THEN {inner} END)"
                else:
                    replacement = f"COUNT(CASE WHEN {cond} THEN {inner} END)"
            elif func_name == 'SUM':
                replacement = f"SUM(CASE WHEN {cond} THEN {inner} ELSE 0 END)"
            elif func_name == 'STRING_AGG':
                # STRING_AGG(col, ',') FILTER (WHERE cond) → STRING_AGG(CASE WHEN cond THEN col END, ',')
                # inner = "col, ','" — split off the separator
                sep_match = re.search(r",\s*'[^']*'\s*$", inner)
                if sep_match:
                    col_part = inner[:sep_match.start()].strip()
                    sep_part = inner[sep_match.start():]
                    replacement = f"STRING_AGG(CASE WHEN {cond} THEN {col_part} END{sep_part})"
                else:
                    replacement = f"STRING_AGG(CASE WHEN {cond} THEN {inner} END, ',')"
            else:
                # Generic fallback
                replacement = f"{func_name}(CASE WHEN {cond} THEN {inner} END)"

            result.append(replacement)
            i = pos

        return ''.join(result)

    # Apply FILTER rewrite for COUNT (handles nested parens correctly)
    if IS_HANA:
        sql = _rewrite_filter(sql)



    # SUM FILTER handled by _rewrite_sum_filter below — remove old regex

    # COALESCE(SUM(...) FILTER ...) — handled by _rewrite_sum_filter

    # DISTINCT ON (col) → ROW_NUMBER() OVER (PARTITION BY col ORDER BY ...)
    # Pattern: SELECT DISTINCT ON (col) col_list FROM source ORDER BY col, sort_col DIR NULLS ...
    # Rewritten as: SELECT col_list FROM (
    #     SELECT col_list, ROW_NUMBER() OVER (PARTITION BY col ORDER BY sort_col DIR) AS _rn
    #     FROM source) t WHERE _rn = 1
    def _rewrite_distinct_on(m):
        part_col     = m.group(1).strip()
        col_list     = m.group(2).strip()
        source       = m.group(3).strip()
        order_clause = m.group(4).strip()
        parts = [p.strip() for p in order_clause.split(',')]
        sort_cols = ', '.join(parts[1:]) if len(parts) > 1 else parts[0]
        sort_cols = re.sub(r'\s+NULLS\s+(LAST|FIRST)', '', sort_cols, flags=re.IGNORECASE)

        if col_list.strip() == '*':
            # DISTINCT ON (col) * → need explicit columns; use ROW_NUMBER trick
            return (
                f"* FROM ("
                f"SELECT *, ROW_NUMBER() OVER "
                f"(PARTITION BY {part_col} ORDER BY {sort_cols}) AS _rn "
                f"FROM {source}) _dist WHERE _rn = 1"
            )
        return (
            f"{col_list} FROM ("
            f"SELECT {col_list}, ROW_NUMBER() OVER "
            f"(PARTITION BY {part_col} ORDER BY {sort_cols}) AS _rn "
            f"FROM {source}) _distinct_subq WHERE _rn = 1"
        )

    # DISTINCT ON → ROW_NUMBER: find each occurrence and rewrite
    # Pattern: DISTINCT ON (col) <col_list> FROM <table> [WHERE...] ORDER BY col, sort [NULLS ...]
    def _rewrite_all_distinct_on(sql_frag: str) -> str:
        # Find all DISTINCT ON occurrences
        pattern = re.compile(r'DISTINCT\s+ON\s*\(([^)]+)\)', re.IGNORECASE)
        out = []
        pos = 0
        for m in pattern.finditer(sql_frag):
            out.append(sql_frag[pos:m.start()])
            part_cols = m.group(1).strip()
            # Everything after DISTINCT ON (...) up to matching ORDER BY ... NULLS... end
            rest_start = m.end()
            rest = sql_frag[rest_start:]
            # Find ORDER BY (last one before the next closing paren at depth 0)
            ob_match = re.search(r'\s+ORDER\s+BY\s+', rest, re.IGNORECASE)
            if not ob_match:
                out.append(sql_frag[m.start():])
                pos = len(sql_frag)
                break
            col_list_raw = rest[:ob_match.start()].strip()
            # Extract ORDER BY clause — everything until NULLS LAST/FIRST or end of clause
            after_ob = rest[ob_match.end():]
            # Find end of ORDER BY (next clause keyword or closing paren at depth 0)
            end_match = re.search(
                r'(?=\s*(?:LIMIT\b|OFFSET\b|WHERE\b|GROUP\s+BY\b|HAVING\b|\)))',
                after_ob, re.IGNORECASE
            )
            if end_match:
                order_clause = after_ob[:end_match.start()].strip()
                rest_after = after_ob[end_match.start():]
            else:
                order_clause = after_ob.strip()
                rest_after = ''
            # Remove NULLS LAST/FIRST
            order_clause = re.sub(r'\s+NULLS\s+(LAST|FIRST)', '', order_clause, flags=re.IGNORECASE).strip()
            # Build sort cols: skip partition col (first) if duplicated
            sort_parts = [p.strip() for p in order_clause.split(',')]
            sort_col = ', '.join(sort_parts[1:]) if len(sort_parts) > 1 else sort_parts[0]
            # Determine if col_list is *
            cols = col_list_raw.strip()
            if cols == '*':
                repl = (
                    f"* FROM (SELECT *, ROW_NUMBER() OVER "
                    f"(PARTITION BY {part_cols} ORDER BY {sort_col}) AS _rn "
                    # FROM + rest was included in col_list — we need to extract it
                )
                # Actually col_list_raw includes FROM table [WHERE ...]
                # Let's split on FROM
                from_split = re.split(r'\bFROM\b', col_list_raw, maxsplit=1, flags=re.IGNORECASE)
                cols_only = from_split[0].strip()
                from_part = 'FROM' + from_split[1] if len(from_split) > 1 else ''
                repl = (
                    f"{cols_only} FROM (SELECT {cols_only}, ROW_NUMBER() OVER "
                    f"(PARTITION BY {part_cols} ORDER BY {sort_col}) AS _rn "
                    f"{from_part}) _dist_subq WHERE _rn = 1"
                )
            else:
                # col_list_raw = "col1, col2, ... FROM table [WHERE ...]"
                from_split = re.split(r'\bFROM\b', col_list_raw, maxsplit=1, flags=re.IGNORECASE)
                cols_only = from_split[0].strip()
                from_part = 'FROM' + from_split[1] if len(from_split) > 1 else ''
                repl = (
                    f"{cols_only} FROM (SELECT {cols_only}, ROW_NUMBER() OVER "
                    f"(PARTITION BY {part_cols} ORDER BY {sort_col}) AS _rn "
                    f"{from_part}) _dist_subq WHERE _rn = 1"
                )
            out.append(repl)
            pos = rest_start + ob_match.end() + (end_match.start() if end_match else len(after_ob))

        out.append(sql_frag[pos:])
        return ''.join(out)

    if IS_HANA:
        sql = _rewrite_all_distinct_on(sql)
    # We do a best-effort inline rewrite for simple cases:
    # SELECT DISTINCT ON (col) ... FROM t ORDER BY col, ...
    # → SELECT ... FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY col ORDER BY col) AS _rn FROM t) WHERE _rn = 1
    # (complex rewrites must stay in callers — flag them)

    # SUBSTRING(col, 'regex') → SUBSTRING_REGEXPR('regex' IN col)  [HANA syntax]
    sql = re.sub(
        r'SUBSTRING\s*\(\s*(\w+(?:\.\w+)?)\s*,\s*\'([^\']+)\'\s*\)',
        lambda m: f"SUBSTRING_REGEXPR('{m.group(2)}' IN {m.group(1)})",
        sql, flags=re.IGNORECASE
    )

    # col::type and table.col::type → HANA equivalents
    sql = re.sub(r'(\w+(?:\.\w+)?)::text\b',    r'TO_NVARCHAR(\1)', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::varchar\b', r'TO_NVARCHAR(\1)', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::date\b',    r'TO_DATE(\1)',      sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::int\b',     r'TO_INT(\1)',       sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::numeric\b', r'\1',               sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::float\b',   r'\1',               sql, flags=re.IGNORECASE)
    sql = re.sub(r'(\w+(?:\.\w+)?)::boolean\b', r'\1',               sql, flags=re.IGNORECASE)

    # ILIKE → LOWER() LIKE LOWER()
    sql = re.sub(
        r'(\w[\w.]*)\s+ILIKE\s+(\?|\'[^\']*\')',
        lambda m: f"LOWER({m.group(1)}) LIKE LOWER({m.group(2)})",
        sql, flags=re.IGNORECASE
    )

    # NOW() → CURRENT_TIMESTAMP
    sql = re.sub(r'\bNOW\(\)', 'CURRENT_TIMESTAMP', sql, flags=re.IGNORECASE)

    return sql


def _adapt_params(params):
    """Convert tuple/list params — no-op for now, but centralised for future use."""
    if params is None:
        return []
    return list(params)


# ── Connection context manager ────────────────────────────────────────────────

@contextmanager
def get_conn():
    if IS_HANA:
        from hdbcli import dbapi
        conn = dbapi.connect(**HANA_CONFIG)
    else:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(**PG_CONFIG)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Query helpers ─────────────────────────────────────────────────────────────

def _post_process_hana_row(row: dict) -> dict:
    """
    Post-process a HANA result row:
    - STRING_AGG results come back as 'val1,val2,val3' strings.
      The /filters endpoint and others expect Python lists.
      We detect columns that look like CSV aggregates by checking if the
      original SQL contained STRING_AGG for that alias — too complex, so instead
      we check if the value is a string containing commas AND the column name
      is a known plural (ends in 's') or is in a known set.
      Simpler: the caller passes convert_arrays=True when needed.
    """
    return row


def _csv_to_list(val):
    """Convert a comma-separated STRING_AGG result to a sorted unique list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return sorted({v.strip() for v in str(val).split(',') if v.strip()})


def query(sql: str, params=None, array_cols: list[str] | None = None) -> list[dict]:
    """
    Run a SELECT and return list of dicts.
    array_cols: when using HANA, convert these column names from CSV strings to Python lists.
    """
    sql = _adapt_sql(sql)
    params = _adapt_params(params)
    with get_conn() as conn:
        if IS_HANA:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
            except Exception as e:
                # Log the failing SQL for debugging
                import logging
                logging.getLogger("database").error("HANA SQL ERROR: %s\nSQL: %s", e, sql[:2000])
                raise
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            if array_cols:
                for r in rows:
                    for c in array_cols:
                        if c in r:
                            r[c] = _csv_to_list(r[c])
            return rows
        else:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params=None, array_cols: list[str] | None = None) -> dict | None:
    rows = query(sql, params, array_cols=array_cols)
    return rows[0] if rows else None


def execute(sql: str, params=None) -> int:
    """Run INSERT/UPDATE/DELETE. Returns rowcount."""
    sql = _adapt_sql(sql)
    params = _adapt_params(params)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params=None) -> Any:
    """Run INSERT and return the auto-generated PK."""
    sql_adapted = _adapt_sql(sql)
    params = _adapt_params(params)

    with get_conn() as conn:
        cur = conn.cursor()
        if IS_HANA:
            # HANA: execute INSERT then fetch IDENTITY
            # Remove RETURNING clause if present (PostgreSQL syntax)
            sql_hana = re.sub(r'\s+RETURNING\s+\w+', '', sql_adapted, flags=re.IGNORECASE)
            cur.execute(sql_hana, params)
            cur.execute("SELECT CURRENT_IDENTITY_VALUE() FROM DUMMY")
            row = cur.fetchone()
            return row[0] if row else None
        else:
            # PostgreSQL: use RETURNING id
            if "RETURNING" not in sql.upper():
                sql_adapted = sql_adapted + " RETURNING id"
            cur.execute(sql_adapted, params)
            row = cur.fetchone()
            return row[0] if row else None
