"""
ETL: Databricks (Outreach) → HANA — tabela outreach_contacts.
Busca apenas os contatos cujos account_bpid estão no pipeline SAP LAC.
"""
import os
from utils import get_conn, get_logger

log = get_logger("etl.outreach_contacts")

DATABRICKS_CFG = {
    "server_hostname": os.getenv("DATABRICKS_HOST",  "adb-4313574291768790.10.azuredatabricks.net"),
    "http_path":       os.getenv("DATABRICKS_PATH",  "/sql/1.0/warehouses/3679dfdc0cbd20ed"),
    "access_token":    os.getenv("DATABRICKS_TOKEN", "dapia4e992f97cd9165c7a944cbe688ef4fd-2"),
    "_tls_no_verify":  True,
}

PROSPECT_QUERY = """
SELECT
    p.ACCOUNT_BPID                      AS account_bpid,
    CAST(p.PROSPECT_ID AS VARCHAR(50))  AS prospect_id,
    p.FIRST_NAME                        AS first_name,
    p.LAST_NAME                         AS last_name,
    p.TITLE                             AS title,
    p.FUNCTION                          AS function,
    p.DEPARTMENT_LOB                    AS department_lob,
    p.WORK_PHONE                        AS work_phone,
    e.EMAIL                             AS email,
    p.SALES_CLOUD_PROSPECT_ID           AS sales_cloud_prospect_id,
    p.SALES_CLOUD_PROSPECT_LINK         AS sales_cloud_prospect_link,
    p.CONTACT_CRM_BPID                  AS contact_crm_bpid,
    p.ENGAGED_SCORE                     AS engaged_score,
    p.OPEN_COUNT                        AS open_count,
    p.REPLY_COUNT                       AS reply_count,
    p.CLICK_COUNT                       AS click_count,
    p.TOUCHED_AT_TST_GMT                AS touched_at,
    p.ENGAGED_AT_TST_GMT                AS engaged_at,
    p.KEY_EXECUTIVE_CONTACT             AS key_executive_contact,
    p.CONTACT_DQ_STATUS                 AS contact_dq_status,
    p.REGIONAL_BUYING_CLASSIFICATION    AS regional_buying_classification,
    p.BUYING_COMMITTEE_ROLE_ERP         AS role_erp,
    p.BUYING_COMMITTEE_ROLE_HCM         AS role_hcm,
    p.BUYING_COMMITTEE_ROLE_CX          AS role_cx,
    p.BUYING_COMMITTEE_ROLE_SCM         AS role_scm,
    p.BUYING_COMMITTEE_ROLE_BTP         AS role_btp,
    p.BUYING_COMMITTEE_ROLE_F_S         AS role_fs,
    p.ERP_LEAD_SCORE_RANK               AS score_erp,
    p.HCM_LEAD_SCORE_RANK               AS score_hcm,
    p.CX_LEAD_SCORE_RANK                AS score_cx,
    p.SCM_LEAD_SCORE_RANK               AS score_scm,
    p.BTP_LEAD_SCORE_RANK               AS score_btp,
    p.F_S_LEAD_SCORE_RANK               AS score_fs,
    p.ERP_DIRECT_MARKETING_ELIGIBLE     AS eligible_erp,
    p.HCM_DIRECT_MARKETING_ELIGIBLE     AS eligible_hcm,
    p.CX_DIRECT_MARKETING_ELIGIBLE      AS eligible_cx,
    p.SCM_DIRECT_MARKETING_ELIGIBLE     AS eligible_scm,
    p.BTP_DIRECT_MARKETING_ELIGIBLE     AS eligible_btp,
    p.F_S_DIRECT_MARKETING_ELIGIBLE     AS eligible_fs,
    p.LAST_ACTIONS_ERP                  AS last_actions_erp,
    p.LAST_ACTIONS_HCM                  AS last_actions_hcm,
    p.LAST_ACTIONS_CX                   AS last_actions_cx,
    p.LAST_ACTIONS_SCM                  AS last_actions_scm,
    p.LAST_ACTIONS_BTP                  AS last_actions_btp,
    p.LAST_ACTIONS_F_S                  AS last_actions_fs,
    p.LAST_5_ACTIONS                    AS last_5_actions,
    p.MOBILE_PHONE                      AS mobile_phone
FROM ots2_prod.silver.prospects p
LEFT JOIN (
    SELECT PROSPECT_ID, EMAIL
    FROM ots2_prod.silver.prospect_email_addresses
    WHERE IS_DELETED_IN_APP = false AND ORDER = 1
) e ON e.PROSPECT_ID = p.PROSPECT_ID
WHERE p.IS_DELETED_IN_APP = false
  AND p.ACCOUNT_BPID IN ({placeholders})
ORDER BY p.ACCOUNT_BPID, p.ENGAGED_SCORE DESC NULLS LAST
"""

def _clean(v, max_len=None):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ('none', 'null', 'nan'):
        return None
    if max_len and len(s) > max_len:
        return s[:max_len]
    return s

# Field-level max lengths matching HANA column sizes
_FIELD_LIMITS = {
    "last_actions_erp": 5000, "last_actions_hcm": 5000, "last_actions_cx":  5000,
    "last_actions_scm": 5000, "last_actions_btp": 5000, "last_actions_fs":  5000,
    "last_5_actions":   5000, "title": 500, "sales_cloud_prospect_link": 1000,
    "email": 300, "mobile_phone": 100,
    "first_name": 200, "last_name": 200, "function": 200, "department_lob": 200,
    "role_erp": 200, "role_hcm": 200, "role_cx": 200, "role_scm": 200,
    "role_btp": 200, "role_fs":  200,
}


def _get_bpids(conn) -> list[str]:
    """Return distinct non-null account_bp values from the pipeline table."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT account_bp FROM pipeline
            WHERE account_bp IS NOT NULL AND account_bp <> ''
              AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
        """)
        return [r[0] for r in cur.fetchall() if r[0]]


def _fetch_from_databricks(bpids: list[str]) -> list[dict]:
    try:
        from databricks import sql as dbsql
        import urllib3; urllib3.disable_warnings()
    except ImportError:
        log.error("databricks-sql-connector not installed. Run: pip install databricks-sql-connector")
        return []

    if not bpids:
        log.warning("No account_bp IDs found in pipeline — skipping Databricks query")
        return []

    log.info("Connecting to Databricks…")
    conn = dbsql.connect(**DATABRICKS_CFG)
    cur  = conn.cursor()

    # Batch into chunks of 500 to avoid oversized IN clauses
    BATCH_SIZE = 500
    all_rows   = []
    batches    = [bpids[i:i+BATCH_SIZE] for i in range(0, len(bpids), BATCH_SIZE)]
    log.info("Fetching contacts for %d BPIDs in %d batches from Databricks…", len(bpids), len(batches))

    for idx, batch in enumerate(batches, 1):
        ph  = ",".join(["?" for _ in batch])
        sql = PROSPECT_QUERY.format(placeholders=ph)
        try:
            cur.execute(sql, batch)
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            all_rows.extend(rows)
            log.info("  Batch %d/%d: %d rows fetched (total so far: %d)", idx, len(batches), len(rows), len(all_rows))
        except Exception as e:
            log.error("  Batch %d/%d failed: %s", idx, len(batches), str(e)[:200])

    conn.close()
    log.info("Databricks returned %d prospect rows total", len(all_rows))
    return all_rows


def _upsert_contacts(conn, rows: list[dict]) -> int:
    """Load contacts into outreach_contacts — TRUNCATE + batch INSERT (fast for full reload)."""
    if not rows:
        return 0

    driver = os.getenv("DH_DB_DRIVER", "postgres")

    cols = [
        "account_bpid","prospect_id","first_name","last_name","title",
        "function","department_lob","work_phone","mobile_phone","email",
        "sales_cloud_prospect_id","sales_cloud_prospect_link","contact_crm_bpid",
        "engaged_score","open_count","reply_count","click_count",
        "touched_at","engaged_at","key_executive_contact","contact_dq_status",
        "regional_buying_classification",
        "role_erp","role_hcm","role_cx","role_scm","role_btp","role_fs",
        "score_erp","score_hcm","score_cx","score_scm","score_btp","score_fs",
        "eligible_erp","eligible_hcm","eligible_cx","eligible_scm","eligible_btp","eligible_fs",
        "last_actions_erp","last_actions_hcm","last_actions_cx",
        "last_actions_scm","last_actions_btp","last_actions_fs",
        "last_5_actions",
    ]

    table = "DBADMIN.OUTREACH_CONTACTS" if driver == "hana" else "outreach_contacts"
    ph_char = "?" if driver == "hana" else "%s"
    ts_expr = "CURRENT_TIMESTAMP" if driver == "hana" else "NOW()"

    col_list   = ",".join(cols)
    ph_list    = ",".join([ph_char] * len(cols))
    insert_sql = f"INSERT INTO {table} ({col_list}, synced_at) VALUES ({ph_list}, {ts_expr})"

    BATCH = 200
    total = 0

    # Step 1: TRUNCATE in its own transaction
    with conn.cursor() as cur:
        log.info("Truncating %s…", table)
        cur.execute(f"TRUNCATE TABLE {table}")
    conn.commit()
    log.info("Truncate committed.")

    # Step 2: INSERT in batches, each batch its own transaction
    with conn.cursor() as cur:
        log.info("Inserting %d rows in batches of %d…", len(rows), BATCH)
        for i in range(0, len(rows), BATCH):
            batch  = rows[i:i+BATCH]
            params = [[_clean(r.get(c), _FIELD_LIMITS.get(c)) for c in cols] for r in batch]
            try:
                cur.executemany(insert_sql, params)
                conn.commit()
                total += len(batch)
                if total % 10000 == 0:
                    log.info("  Progress: %d / %d (%.0f%%)", total, len(rows), total/len(rows)*100)
            except Exception as e:
                conn.rollback()
                log.warning("  Batch %d-%d failed (%s) — skipping", i, i+BATCH, str(e)[:60])

        log.info("Insert complete: %d rows", total)

    return total


def run() -> int:
    log.info("=== ETL: outreach_contacts ===")

    with get_conn() as hana_conn:
        bpids = _get_bpids(hana_conn)
        log.info("Found %d distinct account_bp IDs in pipeline", len(bpids))

        rows = _fetch_from_databricks(bpids)
        if not rows:
            log.warning("No contacts fetched — nothing to upsert")
            return 0

        # Clean rows with per-field limits + deduplicate by (account_bpid, prospect_id)
        clean_rows_raw = [{k: _clean(v, _FIELD_LIMITS.get(k)) for k, v in r.items()} for r in rows]
        seen = set()
        clean_rows = []
        for r in clean_rows_raw:
            key = (r.get("account_bpid"), r.get("prospect_id"))
            if key not in seen and key[0] and key[1]:
                seen.add(key)
                clean_rows.append(r)
        log.info("After dedup: %d rows (removed %d duplicates)", len(clean_rows), len(clean_rows_raw) - len(clean_rows))

        n = _upsert_contacts(hana_conn, clean_rows)
        log.info("outreach_contacts: %d rows upserted", n)

    return n


if __name__ == "__main__":
    run()
