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
import csv
from collections import defaultdict
from pathlib import Path
from utils import get_conn, get_logger, upsert, clean_str, clean_num, clean_int, clean_date, snap_week, truncate
from config import FILES

log = get_logger("etl.pipeline")

# Pipeline CSV column index → DB field name
# Indices verified from actual file inspection
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
    12: "deal_size_label",       # "Deal Size (ACV EUR)" — text bucket, not stored
    13: "closing_date",
    # 14: closing_date formatted  (skipped)
    15: "age_days",
    16: "phase_aging",
    17: "phase_since_date",
    # 18: phase_since_date formatted (skipped)
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
    # 36: qualification_date formatted (skipped)
    37: "sde_handover_date",
    38: "local_acct_owner",
    39: "account_planned_archetype_text",
    40: "account_sales_group_text",
    41: "iac",
    42: "opp_origin",
    43: "flsm",
    44: "mm_pipe_source",
    45: "acv_usd",               # col 45 = "ACV kUSD" — stored as USD (×1000 applied below)
    # 46: # of opps (skipped — not stored at row level)
    # New SDE columns — added by user; positions may vary, read by header name below
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

    cur = conn.cursor()
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
    cur = conn.cursor()
    cur.execute("SELECT inumber, sub_team FROM targets WHERE area = 'LoBs Team'")
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


def _read_pipeline_csv(path) -> tuple[list[list], dict]:
    """Returns (raw_rows, header_col_map) where header_col_map maps col name → index.

    Handles two file formats:
    - SAC export: row 0 = metadata labels (ACV kUSD etc), row 1 = column headers
    - Merged/custom CSV: row 0 = column headers (detected by presence of 'Opportunity ID')
    Auto-detects delimiter (semicolon or comma).
    """
    rows = []
    header_map = {}
    meta_map = {}

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        first_line = f.readline().lstrip("﻿")

    # Auto-detect delimiter from first line (count outside quotes)
    semi = first_line.count(";")
    comma = first_line.count(",")
    sep = ";" if semi >= comma else ","

    # Detect format: SAC export has a metadata row 0 (no "Opportunity ID");
    # merged/custom files have column headers on row 0.
    row0_parts = [h.strip() for h in next(csv.reader([first_line.rstrip("\n")], delimiter=sep))]
    single_header = "Opportunity ID" in row0_parts or "opportunity_id" in row0_parts

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter=sep)
        for i, parts in enumerate(reader):
            # Strip BOM from first field of first row
            if i == 0:
                parts[0] = parts[0].lstrip("﻿")

            if single_header:
                if i == 0:
                    header_map = {h.strip(): idx for idx, h in enumerate(parts) if h.strip()}
                    continue
            else:
                if i == 0:
                    # SAC metadata row — contains "ACV kUSD" and "# of Opps" labels
                    meta_map = {h.strip(): idx for idx, h in enumerate(parts) if h.strip()}
                    continue
                if i == 1:
                    header_map = {h.strip(): idx for idx, h in enumerate(parts) if h.strip()}
                    for k, v in meta_map.items():
                        if k not in header_map:
                            header_map[k] = v
                    continue

            # Skip accidental repeated header rows
            if len(parts) > 4 and parts[4].strip() in ('Opportunity ID', 'Opp ID', ''):
                continue
            rows.append(parts)

    return rows, header_map


def _build_rows(raw_rows: list[list], dh_names: dict, dh_ids: dict,
                full_names: dict, lobs_inumbers: set, scm_inumbers: set,
                snapshot: object, header_map: dict = None) -> list[dict]:

    _hm = header_map or {}

    def _col(*names, fallback=None):
        """Return column index by trying header names in order, then fallback position."""
        for n in names:
            if n in _hm:
                return _hm[n]
        return fallback

    # Resolve all column positions — header_map wins, legacy positions are fallback
    idx_opp        = _col("Opportunity ID",                                 fallback=4)
    idx_acv        = _col("ACV kUSD",                                       fallback=None)   # may not exist
    idx_acct_srg   = _col("IC_ACCOUNT_SRG_TEXT", "Account Sales Group Text", fallback=40)
    idx_sde_init   = _col("SDE Initial Engagement Date")
    idx_sde_prim   = _col("SDE Initial Engagement Name", "SDE Primary Engagement Name")
    idx_sde_hname  = _col("SDE Handover Name")
    idx_sic_code   = _col("SIC Code")
    idx_sic_desc   = _col("SIC Description")
    idx_mastercode = _col("SAP Mastercode")
    idx_account_bp = _col("Account BP")

    # Positional fields — resolve via header when available (handles col-shift between formats)
    p_account_name    = _col("Account Name",                    fallback=5)
    p_opp_desc        = _col("Opp Description",                 fallback=6)
    p_opp_owner       = _col("Opp Owner Name",                  fallback=7)
    p_phase_since_d   = _col("Phase Since Days",                fallback=8)
    p_fc_qual         = _col("FC Qualification",                fallback=9)
    p_opp_phase       = _col("Opp Phase",                       fallback=10)
    p_closing_qtr     = _col("Closing Qtr",                     fallback=11)
    p_closing_date    = _col("Closing Date",                    fallback=13)
    p_age_days        = _col("Age Days",                        fallback=15)
    p_phase_aging     = _col("Phase Aging",                     fallback=16)
    p_phase_since_dt  = _col("Phase Since Date",                fallback=17)
    p_untouched       = _col("Untouched Days",                  fallback=19)
    p_dist_channel    = _col("Distribution Channel",            fallback=20)
    p_channel_mgr     = _col("Channel Manager",                 fallback=21)
    p_channel_partner = _col("Channel Partner",                 fallback=22)
    p_opp_campaign    = _col("Opp Campaign Text",               fallback=23)
    p_sol_l1          = _col("Solution Area (L1)", "Solution Area L1", fallback=24)
    p_sol_l2          = _col("Sub-Solution Area (L2)", "Sub-Solution Area L2", fallback=25)
    p_sol_l3          = _col("Solution Area L3",                fallback=26)
    p_sol_l4          = _col("Solution Area L4",                fallback=27)
    p_drm_cat         = _col("DRM Category",                    fallback=28)
    p_iss_group       = _col("ISS Group Text",                  fallback=29)
    p_sde_terr        = _col("SDE Current Territory Owner Name", "SDE Current Territory Owner", fallback=30)
    p_pipe_build      = _col("Pipe Build Type Text",            fallback=31)
    p_dq_decision     = _col("Deal Qualification Decision",     fallback=32)
    p_dq_score        = _col("Deal Qualification Score",        fallback=33)
    p_qual_reviewer   = _col("Qualification Reviewer",          fallback=34)
    p_qual_date       = _col("Qualification Date",              fallback=35)
    p_sde_hov_date    = _col("SDE Handover Date",               fallback=37)
    p_local_acct      = _col("Local Acct Owner",                fallback=38)
    p_archetype       = _col("Account Planned Archetype Text",  fallback=39)
    p_iac             = _col("IAC (Engagement Model)", "IAC",   fallback=41)
    p_opp_origin      = _col("Opp Origin",                      fallback=42)
    p_flsm            = _col("FLSM of Local Account Owner Name", "FLSM", fallback=43)
    p_mm_pipe         = _col("MM Pipe Source",                  fallback=44)

    # Minimum column count — use the highest resolved index, floor at 13 (closing_date)
    all_idx = [i for i in [idx_opp, p_closing_date, p_opp_phase, p_sol_l1] if i is not None]
    min_cols = max(all_idx) + 1 if all_idx else 46

    def _safe(parts, idx):
        return parts[idx] if idx is not None and idx < len(parts) else None

    # First pass: compute acv_total_opp per opportunity
    acv_by_opp: dict[str, float] = defaultdict(float)
    for parts in raw_rows:
        if len(parts) < min_cols:
            continue
        opp_id = clean_str(parts[idx_opp])
        if idx_acv is not None:
            acv_k = clean_num(parts[idx_acv])
            acv   = acv_k * 1000 if acv_k is not None else None   # kUSD → USD
            if opp_id and acv:
                acv_by_opp[opp_id] += acv

    rows = []
    for parts in raw_rows:
        if len(parts) < min_cols:
            continue

        opp_id = clean_str(parts[idx_opp])
        if not opp_id:
            continue

        # VAT fields
        dh_list   = dh_names.get(opp_id, [])
        full_list = full_names.get(opp_id, [])
        vat_size  = _calc_vat_size(opp_id, dh_ids, lobs_inumbers, scm_inumbers)
        acv_total = acv_by_opp.get(opp_id, 0) or None
        if idx_acv is not None:
            acv_usd = clean_num(parts[idx_acv])
            acv_usd = acv_usd * 1000 if acv_usd is not None else None  # kUSD → USD
        else:
            acv_usd = None
        acv_per_vat = (acv_total / vat_size) if (acv_total and vat_size) else None

        rows.append({
            "region_lvl1":                   clean_str(parts[0]),
            "region_lvl2":                   clean_str(parts[1]),
            "region_lvl3":                   clean_str(parts[2]),
            "region_lvl4":                   clean_str(parts[3]),
            "opportunity_id":                opp_id,
            "account_name":                  clean_str(_safe(parts, p_account_name)),
            "opp_description":               clean_str(_safe(parts, p_opp_desc)),
            "opp_owner_name":                clean_str(_safe(parts, p_opp_owner)),
            "phase_since_days":              clean_int(_safe(parts, p_phase_since_d)),
            "fc_qualification":              clean_str(_safe(parts, p_fc_qual)),
            "opp_phase":                     clean_str(_safe(parts, p_opp_phase)),
            "closing_qtr":                   clean_str(_safe(parts, p_closing_qtr)),
            "closing_date":                  clean_date(_safe(parts, p_closing_date)),
            "age_days":                      clean_int(_safe(parts, p_age_days)),
            "phase_aging":                   clean_str(_safe(parts, p_phase_aging)),
            "phase_since_date":              clean_date(_safe(parts, p_phase_since_dt)),
            "untouched_days":                clean_int(_safe(parts, p_untouched)),
            "distribution_channel":          clean_str(_safe(parts, p_dist_channel)),
            "channel_manager":               clean_str(_safe(parts, p_channel_mgr)),
            "channel_partner":               clean_str(_safe(parts, p_channel_partner)),
            "opp_campaign_text":             clean_str(_safe(parts, p_opp_campaign)),
            "solution_area_l1":              clean_str(_safe(parts, p_sol_l1)),
            "sub_solution_area_l2":          clean_str(_safe(parts, p_sol_l2)),
            "solution_area_l3":              clean_str(_safe(parts, p_sol_l3)),
            "solution_area_l4":              clean_str(_safe(parts, p_sol_l4)),
            "drm_category":                  clean_str(_safe(parts, p_drm_cat)),
            "iss_group_text":                clean_str(_safe(parts, p_iss_group)),
            "sde_current_territory_owner":   clean_str(_safe(parts, p_sde_terr)),
            "pipe_build_type_text":          clean_str(_safe(parts, p_pipe_build)),
            "deal_qualification_decision":   clean_str(_safe(parts, p_dq_decision)),
            "deal_qualification_score":      clean_num(_safe(parts, p_dq_score)),
            "qualification_reviewer":        clean_str(_safe(parts, p_qual_reviewer)),
            "qualification_date":            clean_date(_safe(parts, p_qual_date)),
            "sde_handover_date":             clean_date(_safe(parts, p_sde_hov_date)),
            "local_acct_owner":              clean_str(_safe(parts, p_local_acct)),
            "account_planned_archetype_text": clean_str(_safe(parts, p_archetype)),
            "account_sales_group_text":      clean_str(_safe(parts, idx_acct_srg)),
            "iac":                           clean_str(_safe(parts, p_iac)),
            "opp_origin":                    clean_str(_safe(parts, p_opp_origin)),
            "flsm":                          clean_str(_safe(parts, p_flsm)),
            "mm_pipe_source":                clean_str(_safe(parts, p_mm_pipe)),
            "acv_usd":                       acv_usd,
            # New SDE columns (may be None if not present in source file)
            "sde_initial_engagement_date":   clean_date(_safe(parts, idx_sde_init)),
            "sde_primary_engagement_name":   clean_str(_safe(parts, idx_sde_prim)),
            "sde_handover_name":             clean_str(_safe(parts, idx_sde_hname)),
            # SIC / Mastercode / Account BP columns
            "sic_code":                      clean_str(_safe(parts, idx_sic_code)),
            "sic_description":               clean_str(_safe(parts, idx_sic_desc)),
            "sap_mastercode":                clean_str(_safe(parts, idx_mastercode)),
            "account_bp":                    clean_str(_safe(parts, idx_account_bp)),
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
    log.info("Reading ? …", path.name)
    raw_rows, header_map = _read_pipeline_csv(path)
    log.info("  %d raw rows", len(raw_rows))

    rows = _build_rows(raw_rows, dh_names, dh_ids, full_names,
                       lobs_inumbers, scm_inumbers, snapshot, header_map)
    log.info("  %d data rows after parsing", len(rows))

    conflict_cols = ["opportunity_id", "solution_area_l1",
                     "sub_solution_area_l2", "solution_area_l3",
                     "solution_area_l4", "snapshot_week"]

    if table == "pipeline_last_week":
        conflict_cols = ["opportunity_id", "solution_area_l1",
                         "sub_solution_area_l2", "solution_area_l3",
                         "solution_area_l4"]
        # Remove snapshot_week — column does not exist in pipeline_last_week
        rows = [{k: v for k, v in r.items() if k != "snapshot_week"} for r in rows]

    with get_conn() as conn:
        if table == "pipeline_last_week":
            truncate(conn, "pipeline_last_week")
        else:
            # Delete existing rows for this snapshot before re-inserting
            cur = conn.cursor()
            cur.execute("DELETE FROM pipeline WHERE snapshot_week = ?", (snapshot,))
        n = upsert(conn, table, rows, conflict_cols)

    log.info("%s: %d rows upserted", table, n)
    return n


def run_multi(paths: list, last_week: bool = False) -> dict:
    """Load multiple pipeline CSV files incrementally into the same snapshot week.

    Deletes the current snapshot once before the first file, then upserts each
    file in sequence — so the caller can split a large export into parts without
    any external merge step.
    """
    snapshot = snap_week()
    table    = "pipeline_last_week" if last_week else "pipeline"

    with get_conn() as conn:
        dh_names, dh_ids, full_names = _load_vat_maps(conn)
        lobs_inumbers, scm_inumbers  = _load_lobs_info(conn)

        # Delete snapshot once up-front; each subsequent file only upserts
        if not last_week:
            cur = conn.cursor()
            cur.execute("DELETE FROM pipeline WHERE snapshot_week = ?", (snapshot,))
            log.info("Snapshot %s cleared before multi-file load", snapshot)
        else:
            truncate(conn, "pipeline_last_week")

    total = 0
    for i, path in enumerate(paths):
        log.info("File %d/%d: %s", i + 1, len(paths), Path(path).name)
        raw_rows, header_map = _read_pipeline_csv(path)
        log.info("  %d raw rows", len(raw_rows))

        rows = _build_rows(raw_rows, dh_names, dh_ids, full_names,
                           lobs_inumbers, scm_inumbers, snapshot, header_map)
        log.info("  %d data rows after parsing", len(rows))

        conflict_cols = ["opportunity_id", "solution_area_l1",
                         "sub_solution_area_l2", "solution_area_l3",
                         "solution_area_l4", "snapshot_week"]

        if last_week:
            conflict_cols = ["opportunity_id", "solution_area_l1",
                             "sub_solution_area_l2", "solution_area_l3",
                             "solution_area_l4"]
            rows = [{k: v for k, v in r.items() if k != "snapshot_week"} for r in rows]

        with get_conn() as conn:
            n = upsert(conn, table, rows, conflict_cols)
        log.info("  %d rows upserted", n)
        total += n

    # Register snapshot
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DO BEGIN
                IF NOT EXISTS (SELECT 1 FROM SYS.TABLES WHERE TABLE_NAME = 'WEEKLY_SNAPSHOTS' AND SCHEMA_NAME = CURRENT_SCHEMA) THEN
                    EXEC 'CREATE TABLE weekly_snapshots (
                        snapshot_week DATE PRIMARY KEY,
                        pipeline_rows INTEGER,
                        loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )';
                END IF;
            END
        """)
        cur.execute(
            """
            MERGE INTO weekly_snapshots AS t
            USING (SELECT ? AS snapshot_week, ? AS pipeline_rows FROM DUMMY) AS s
            ON (t.snapshot_week = s.snapshot_week)
            WHEN MATCHED THEN UPDATE SET t.pipeline_rows = s.pipeline_rows
            WHEN NOT MATCHED THEN INSERT (snapshot_week, pipeline_rows)
                VALUES (s.snapshot_week, s.pipeline_rows)
            """,
            (snapshot, total)
        )

    return {"pipeline" if not last_week else "pipeline_last_week": total}


def run(last_week: bool = False) -> dict:
    snapshot = snap_week()

    # Load VAT & team lookups once using a read-only connection
    with get_conn() as conn:
        dh_names, dh_ids, full_names = _load_vat_maps(conn)
        lobs_inumbers, scm_inumbers  = _load_lobs_info(conn)

    results = {}

    if not last_week:
        results["pipeline"] = _load_pipeline(
            FILES["pipeline"], "pipeline", snapshot,
            dh_names, dh_ids, full_names, lobs_inumbers, scm_inumbers
        )

    # Last-week file is optional — skip if not present
    lw_path = FILES["pipeline_lw"]
    if last_week or (not last_week and Path(str(lw_path)).exists()):
        results["pipeline_last_week"] = _load_pipeline(
            lw_path, "pipeline_last_week", snapshot,
            dh_names, dh_ids, full_names, lobs_inumbers, scm_inumbers
        )
    else:
        log.info("pipeline_last_week: skipped (file not present)")

    # Register snapshot — create table if needed, then upsert
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DO BEGIN
                IF NOT EXISTS (SELECT 1 FROM SYS.TABLES WHERE TABLE_NAME = 'WEEKLY_SNAPSHOTS' AND SCHEMA_NAME = CURRENT_SCHEMA) THEN
                    EXEC 'CREATE TABLE weekly_snapshots (
                        snapshot_week DATE PRIMARY KEY,
                        pipeline_rows INTEGER,
                        loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )';
                END IF;
            END
        """)
        cur.execute(
            """
            MERGE INTO weekly_snapshots AS t
            USING (SELECT ? AS snapshot_week, ? AS pipeline_rows FROM DUMMY) AS s
            ON (t.snapshot_week = s.snapshot_week)
            WHEN MATCHED THEN UPDATE SET t.pipeline_rows = s.pipeline_rows
            WHEN NOT MATCHED THEN INSERT (snapshot_week, pipeline_rows)
                VALUES (s.snapshot_week, s.pipeline_rows)
            """,
            (snapshot, results.get("pipeline", 0))
        )

    return results


if __name__ == "__main__":
    run()
