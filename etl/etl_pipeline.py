"""
ETL: Pipeline CSV → pipeline (or pipeline_last_week) table.

Handles:
- Multi-row per opportunity (one row per solution line)
- VAT_DH / VAT_Full aggregation from dh_vat / full_vat tables
- Engagement_Type classification
- VAT_Size calculation (LoBs excluded, SCM included)
- ACV_total_opp (sum per opportunity)
- ACV_per_VAT (for ERP areas + SCM partial — see calc_rules)
"""
import pandas as pd
from collections import defaultdict
from pathlib import Path
from utils import get_conn, get_logger, upsert, clean_str, clean_num, clean_int, clean_date, snap_week
from config import FILES

log = get_logger("etl.pipeline")

# Pipeline CSV column index → DB field name
# Format: 54 columns (verified 2026-07-31 from merged (1).csv)
# Cols 45-50: SDE Initial Engagement Date/Name, SDE Handover Name, SIC Code/Description, SAP Mastercode
# Col 51: Account BP (NEW)
# ACV kUSD shifted to col 52
_COL_MAP = {
    0:  "region_lvl1",
    1:  "region_lvl2",
    2:  "region_lvl3",
    3:  "region_lvl4",
    4:  "opportunity_id",
    5:  "account_name",
    6:  "opp_description",
    7:  "opp_owner_name",
    8:  "phase_since_days",
    9:  "fc_qualification",
    10: "opp_phase",
    11: "closing_qtr",
    12: "deal_size_label",       # text bucket, not stored
    13: "closing_date",
    # 14: formatted (skipped)
    15: "age_days",
    16: "phase_aging",
    17: "phase_since_date",
    # 18: formatted (skipped)
    19: "untouched_days",
    20: "distribution_channel",
    21: "channel_manager",
    22: "channel_partner",
    23: "opp_campaign_text",
    24: "solution_area_l1",
    25: "sub_solution_area_l2",
    26: "solution_area_l3",
    27: "solution_area_l4",
    28: "drm_category",
    29: "iss_group_text",
    30: "sde_current_territory_owner",
    31: "pipe_build_type_text",
    32: "deal_qualification_decision",
    33: "deal_qualification_score",
    34: "qualification_reviewer",
    35: "qualification_date",
    # 36: formatted (skipped)
    37: "sde_handover_date",
    38: "local_acct_owner",
    39: "account_planned_archetype_text",
    40: "account_sales_group_text",
    41: "iac",
    42: "opp_origin",
    43: "flsm",
    44: "mm_pipe_source",
    45: "sde_initial_engagement_date",
    46: "sde_primary_engagement_name",
    47: "sde_handover_name",
    48: "sic_code",
    49: "sic_description",
    50: "sap_mastercode",
    51: "account_bp",             # NEW — Account BP ID
    52: "acv_usd",               # col 52 = "ACV kUSD" — stored as USD (×1000 applied below)
    # 53: # of opps (skipped)
}


def _load_vat_maps(conn) -> tuple[dict, dict, dict]:
    """
    Returns three dicts keyed by opportunity_id:
      dh_names  → list of DH SA names
      dh_ids    → list of DH SA inumbers
      full_names → list of all SA names
    """
    dh_names  = defaultdict(list)
    dh_ids    = defaultdict(list)
    full_names = defaultdict(list)

    with conn.cursor() as cur:
        cur.execute("SELECT opportunity_id, sales_team_member_name, sales_team_member_id FROM dh_vat")
        for opp_id, name, member_id in cur.fetchall():
            if name:
                dh_names[opp_id].append(name)
            if member_id:
                dh_ids[opp_id].append(member_id)

        cur.execute("SELECT opportunity_id, sales_team_member_name FROM full_vat")
        for opp_id, name in cur.fetchall():
            if name:
                full_names[opp_id].append(name)

    return dh_names, dh_ids, full_names


def _load_lobs_info(conn) -> tuple[set, set]:
    """
    Returns:
      lobs_inumbers — all LoBs inumbers (excluding SCM)
      scm_inumbers  — SCM inumbers (counted in VAT_Size)
    """
    lobs_inumbers = set()
    scm_inumbers  = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT inumber, sub_team FROM targets WHERE area = 'LoBs Team'"
        )
        for inumber, sub_team in cur.fetchall():
            if sub_team == "SCM":
                scm_inumbers.add(inumber)
            else:
                lobs_inumbers.add(inumber)
    return lobs_inumbers, scm_inumbers


def _calc_engagement_type(opp_id: str, dh_names: dict, full_names: dict) -> str:
    has_dh   = bool(dh_names.get(opp_id))
    has_full = bool(full_names.get(opp_id))
    if not has_dh and not has_full:
        return "No Engagement"
    if has_dh:
        return "Digital"
    return "Field"


def _calc_vat_size(opp_id: str, dh_ids: dict,
                   lobs_inumbers: set, scm_inumbers: set) -> int:
    ids = dh_ids.get(opp_id, [])
    count = 0
    for i in ids:
        if i in scm_inumbers:
            count += 1          # SCM always counted
        elif i in lobs_inumbers:
            pass                # other LoBs excluded
        else:
            count += 1          # ERP / non-LoBs always counted
    return count


def _read_pipeline_csv(path) -> list[list]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i <= 1:
                continue        # Skip rows 0 (SAC metadata) and 1 (column headers)
            parts = line.rstrip("\n").split(";")
            # Extra guard: skip if opp_id column looks like a column header
            if len(parts) > 4 and parts[4].strip() in ('Opportunity ID', 'Opp ID', ''):
                continue
            rows.append(parts)
    return rows


def _build_rows(raw_rows: list[list], dh_names: dict, dh_ids: dict,
                full_names: dict, lobs_inumbers: set, scm_inumbers: set,
                snapshot: object, acv_col: int = 52, min_cols: int = 53) -> list[dict]:
    """
    acv_col / min_cols allow handling older 47-col exports (last week file)
    where ACV kUSD is at col 45 rather than col 52.
    """
    def _get(p, i):
        return p[i] if len(p) > i else ''

    # First pass: compute acv_total_opp per opportunity
    acv_by_opp: dict[str, float] = defaultdict(float)
    for parts in raw_rows:
        if len(parts) < min_cols:
            continue
        opp_id = clean_str(parts[4])
        acv_k  = clean_num(parts[acv_col])
        acv    = acv_k * 1000 if acv_k is not None else None
        if opp_id and acv:
            acv_by_opp[opp_id] += acv

    rows = []
    for parts in raw_rows:
        if len(parts) < min_cols:
            continue

        opp_id = clean_str(parts[4])
        if not opp_id:
            continue

        # VAT fields
        dh_list   = dh_names.get(opp_id, [])
        full_list = full_names.get(opp_id, [])
        vat_size  = _calc_vat_size(opp_id, dh_ids, lobs_inumbers, scm_inumbers)
        acv_total = acv_by_opp.get(opp_id, 0) or None
        acv_usd   = clean_num(parts[acv_col])
        acv_usd   = acv_usd * 1000 if acv_usd is not None else None  # kUSD → USD
        acv_per_vat = (acv_total / vat_size) if (acv_total and vat_size) else None

        rows.append({
            "region_lvl1":                   clean_str(parts[0]),
            "region_lvl2":                   clean_str(parts[1]),
            "region_lvl3":                   clean_str(parts[2]),
            "region_lvl4":                   clean_str(parts[3]),
            "opportunity_id":                opp_id,
            "account_name":                  clean_str(parts[5]),
            "opp_description":               clean_str(parts[6]),
            "opp_owner_name":                clean_str(parts[7]),
            "phase_since_days":              clean_int(parts[8]),
            "fc_qualification":              clean_str(parts[9]),
            "opp_phase":                     clean_str(parts[10]),
            "closing_qtr":                   clean_str(parts[11]),
            "closing_date":                  clean_date(parts[13]),
            "age_days":                      clean_int(parts[15]),
            "phase_aging":                   clean_str(parts[16]),
            "phase_since_date":              clean_date(parts[17]),
            "untouched_days":                clean_int(parts[19]),
            "distribution_channel":          clean_str(parts[20]),
            "channel_manager":               clean_str(parts[21]),
            "channel_partner":               clean_str(parts[22]),
            "opp_campaign_text":             clean_str(parts[23]),
            "solution_area_l1":              clean_str(parts[24]),
            "sub_solution_area_l2":          clean_str(parts[25]),
            "solution_area_l3":              clean_str(parts[26]),
            "solution_area_l4":              clean_str(parts[27]),
            "drm_category":                  clean_str(parts[28]),
            "iss_group_text":                clean_str(parts[29]),
            "sde_current_territory_owner":   clean_str(parts[30]),
            "pipe_build_type_text":          clean_str(parts[31]),
            "deal_qualification_decision":   clean_str(parts[32]),
            "deal_qualification_score":      clean_num(parts[33]),
            "qualification_reviewer":        clean_str(parts[34]),
            "qualification_date":            clean_date(parts[35]),
            "sde_handover_date":             clean_date(_get(parts, 37)),
            "local_acct_owner":              clean_str(_get(parts, 38)),
            "account_planned_archetype_text": clean_str(_get(parts, 39)),
            "account_sales_group_text":      clean_str(_get(parts, 40)),
            "iac":                           clean_str(_get(parts, 41)),
            "opp_origin":                    clean_str(_get(parts, 42)),
            "flsm":                          clean_str(_get(parts, 43)),
            "mm_pipe_source":                clean_str(_get(parts, 44)),
            # Extended cols — present in 54-col format only; NULL in compact (47-col) format
            "sde_initial_engagement_date":   clean_date(_get(parts, 45)) if acv_col == 52 else None,
            "sde_primary_engagement_name":   clean_str(_get(parts, 46)) if acv_col == 52 else None,
            "sde_handover_name":             clean_str(_get(parts, 47)) if acv_col == 52 else None,
            "sic_code":                      clean_str(_get(parts, 48)) if acv_col == 52 else None,
            "sic_description":               clean_str(_get(parts, 49)) if acv_col == 52 else None,
            "sap_mastercode":                clean_str(_get(parts, 50)) if acv_col == 52 else None,
            "account_bp":                    clean_str(_get(parts, 51)) if acv_col == 52 else None,
            "acv_usd":                       acv_usd,
            # Derived
            "vat_dh":           ", ".join(dh_list) or None,
            "vat_full":         ", ".join(full_list) or None,
            "engagement_type":  _calc_engagement_type(opp_id, dh_names, full_names),
            "vat_size":         vat_size,
            "acv_total_opp":    acv_total,
            "acv_per_vat":      acv_per_vat,
            "snapshot_week":    snapshot,
        })

    return rows


def _load_pipeline(path, table: str, snapshot,
                   dh_names, dh_ids, full_names,
                   lobs_inumbers, scm_inumbers) -> int:
    log.info("Reading %s …", path.name)
    raw_rows = _read_pipeline_csv(path)
    log.info("  %d raw rows", len(raw_rows))

    # Auto-detect column format: newer exports have 54 cols (ACV at 52),
    # older/compact exports have 47 cols (ACV at 45).
    sample_len = len(raw_rows[0]) if raw_rows else 0
    if sample_len < 53:
        log.info("  Compact format detected (%d cols) — ACV at col 45", sample_len)
        acv_col, min_cols = 45, 46
    else:
        acv_col, min_cols = 52, 53

    rows = _build_rows(raw_rows, dh_names, dh_ids, full_names,
                       lobs_inumbers, scm_inumbers, snapshot,
                       acv_col=acv_col, min_cols=min_cols)
    log.info("  %d data rows after parsing", len(rows))

    conflict_cols = ["opportunity_id", "solution_area_l1",
                     "sub_solution_area_l2", "solution_area_l3",
                     "solution_area_l4", "snapshot_week"]

    if table == "pipeline_last_week":
        conflict_cols = ["opportunity_id", "solution_area_l1",
                         "sub_solution_area_l2", "solution_area_l3",
                         "solution_area_l4"]

    with get_conn() as conn:
        if table == "pipeline_last_week":
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE pipeline_last_week")
        n = upsert(conn, table, rows, conflict_cols)

    log.info("%s: %d rows upserted", table, n)
    return n


def _update_weekly_snapshot(snapshot, pipeline_rows: int):
    """Upsert the weekly_snapshots row for this snapshot using HANA MERGE."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                MERGE INTO weekly_snapshots USING (SELECT ? AS wk, ? AS pr FROM DUMMY) src
                ON (weekly_snapshots.snapshot_week = src.wk)
                WHEN MATCHED THEN UPDATE SET
                    pipeline_rows = src.pr,
                    loaded_at     = NOW()
                WHEN NOT MATCHED THEN INSERT
                    (snapshot_week, pipeline_rows, loaded_at)
                    VALUES (src.wk, src.pr, NOW())
                """,
                [snapshot, pipeline_rows],
            )
    log.info("weekly_snapshots updated: %s  (%d rows)", snapshot, pipeline_rows)


def run_multi(paths: list, last_week: bool = False) -> dict:
    """Load pipeline from multiple CSV parts (avoids external merge step)."""
    snapshot = snap_week()

    with get_conn() as conn:
        _ensure_columns(conn)
        dh_names, dh_ids, full_names = _load_vat_maps(conn)
        lobs_inumbers, scm_inumbers  = _load_lobs_info(conn)

    all_raw: list[list] = []
    for path in paths:
        part_rows = _read_pipeline_csv(path)
        log.info("  Part %s: %d rows", Path(path).name, len(part_rows))
        all_raw.extend(part_rows)

    log.info("Merged %d raw rows from %d files", len(all_raw), len(paths))

    sample_len = len(all_raw[0]) if all_raw else 0
    if sample_len < 53:
        log.info("  Compact format (%d cols) — ACV at col 45", sample_len)
        acv_col, min_cols = 45, 46
    else:
        acv_col, min_cols = 52, 53

    table = "pipeline_last_week" if last_week else "pipeline"
    rows = _build_rows(all_raw, dh_names, dh_ids, full_names,
                       lobs_inumbers, scm_inumbers, snapshot,
                       acv_col=acv_col, min_cols=min_cols)
    log.info("  %d data rows after parsing", len(rows))

    conflict_cols = ["opportunity_id", "solution_area_l1",
                     "sub_solution_area_l2", "solution_area_l3",
                     "solution_area_l4", "snapshot_week"]

    if last_week:
        conflict_cols = ["opportunity_id", "solution_area_l1",
                         "sub_solution_area_l2", "solution_area_l3",
                         "solution_area_l4"]

    with get_conn() as conn:
        if last_week:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE pipeline_last_week")
        else:
            # Smart delete: only remove rows whose opportunity_id is in THIS upload.
            # Allows multiple partial-file uploads to accumulate for the same snapshot_week
            # instead of each one wiping all previous data.
            opp_ids = list({r["opportunity_id"] for r in rows if r.get("opportunity_id")})
            cur = conn.cursor()
            deleted = 0
            for i in range(0, len(opp_ids), 500):
                batch = opp_ids[i:i + 500]
                ph = ",".join("?" for _ in batch)
                cur.execute(
                    f"DELETE FROM pipeline WHERE snapshot_week = ? AND opportunity_id IN ({ph})",
                    [snapshot] + batch,
                )
                deleted += cur.rowcount if hasattr(cur, "rowcount") else 0
            log.info("Smart-cleared %d opp_ids (%d rows) for %s before reload",
                     len(opp_ids), deleted, snapshot)
        n = upsert(conn, table, rows, conflict_cols)

    log.info("%s: %d rows upserted", table, n)

    if not last_week:
        # Count total rows now in pipeline for this snapshot (may span multiple uploads)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM pipeline WHERE snapshot_week = ?", [snapshot])
            total_rows = cur.fetchone()[0]
        _update_weekly_snapshot(snapshot, total_rows)

    return {table: n}


def _ensure_columns(conn):
    """Add columns that were introduced after initial deployment."""

    def _add_missing(table_upper: str, pending: list):
        cur = conn.cursor()
        cur.execute("SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = ?", [table_upper])
        existing = {r[0].lower() for r in cur.fetchall()}
        for col, ddl in pending:
            if col not in existing:
                conn.cursor().execute(ddl)
                log.info("Migration: added %s to %s", col.upper(), table_upper)

    def _widen(table_upper: str, pending: list):
        """Widen columns whose current LENGTH is below the required minimum."""
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME, LENGTH FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME = ?",
            [table_upper],
        )
        lengths = {r[0].lower(): (r[1] or 0) for r in cur.fetchall()}
        for col, min_len, ddl in pending:
            if lengths.get(col, 0) < min_len:
                conn.cursor().execute(ddl)
                log.info("Migration: widened %s on %s to %d", col.upper(), table_upper, min_len)

    _widen("PIPELINE", [
        ("closing_qtr", 50, 'ALTER TABLE "PIPELINE" ALTER ("CLOSING_QTR" NVARCHAR(50))'),
    ])
    _widen("PIPELINE_LAST_WEEK", [
        ("closing_qtr", 50, 'ALTER TABLE "PIPELINE_LAST_WEEK" ALTER ("CLOSING_QTR" NVARCHAR(50))'),
    ])

    _add_missing("PIPELINE", [
        ("phase_since_days", 'ALTER TABLE "PIPELINE" ADD ("PHASE_SINCE_DAYS" INTEGER)'),
        ("region_lvl4",      'ALTER TABLE "PIPELINE" ADD ("REGION_LVL4" NVARCHAR(100))'),
    ])
    _add_missing("PIPELINE_LAST_WEEK", [
        ("region_lvl4",                    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("REGION_LVL4" NVARCHAR(100))'),
        ("opp_phase",                      'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("OPP_PHASE" NVARCHAR(100))'),
        ("engagement_type",                'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ENGAGEMENT_TYPE" NVARCHAR(50))'),
        ("iss_group_text",                 'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ISS_GROUP_TEXT" NVARCHAR(200))'),
        ("account_planned_archetype_text", 'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACCOUNT_PLANNED_ARCHETYPE_TEXT" NVARCHAR(200))'),
        ("deal_qualification_decision",    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("DEAL_QUALIFICATION_DECISION" NVARCHAR(100))'),
        ("flsm",                           'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("FLSM" NVARCHAR(100))'),
        ("vat_dh",                         'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_DH" NVARCHAR(2000))'),
        ("vat_full",                       'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_FULL" NVARCHAR(2000))'),
        ("vat_size",                       'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("VAT_SIZE" INTEGER)'),
        ("acv_total_opp",                  'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACV_TOTAL_OPP" DOUBLE)'),
        ("acv_per_vat",                    'ALTER TABLE "PIPELINE_LAST_WEEK" ADD ("ACV_PER_VAT" DOUBLE)'),
    ])


def run(last_week: bool = False) -> dict:
    snapshot = snap_week()

    # Load VAT & team lookups once using a read-only connection
    with get_conn() as conn:
        _ensure_columns(conn)
        dh_names, dh_ids, full_names = _load_vat_maps(conn)
        lobs_inumbers, scm_inumbers  = _load_lobs_info(conn)

    results = {}

    if not last_week:
        results["pipeline"] = _load_pipeline(
            FILES["pipeline"], "pipeline", snapshot,
            dh_names, dh_ids, full_names, lobs_inumbers, scm_inumbers
        )

    results["pipeline_last_week"] = _load_pipeline(
        FILES["pipeline_lw"], "pipeline_last_week", snapshot,
        dh_names, dh_ids, full_names, lobs_inumbers, scm_inumbers
    )

    if not last_week:
        _update_weekly_snapshot(snapshot, results.get("pipeline", 0))

    return results


if __name__ == "__main__":
    run()
