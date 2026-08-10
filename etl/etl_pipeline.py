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
    """Returns (raw_rows, header_col_map) where header_col_map maps col name → index."""
    rows = []
    header_map = {}
    meta_map = {}  # from row 0 (SAC metadata labels like "ACV kUSD")
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i == 0:
                # Parse metadata row — contains "ACV kUSD" and "# of Opps" labels
                parts = line.rstrip("\n").split(";")
                meta_map = {h.strip(): idx for idx, h in enumerate(parts) if h.strip()}
                continue
            if i == 1:
                # Parse header row to find dynamic column positions
                parts = line.rstrip("\n").split(";")
                header_map = {h.strip(): idx for idx, h in enumerate(parts) if h.strip()}
                # Merge meta_map into header_map so ACV kUSD position is accessible
                for k, v in meta_map.items():
                    if k not in header_map:
                        header_map[k] = v
                continue
            parts = line.rstrip("\n").split(";")
            # Extra guard: skip if opp_id column looks like a column header
            if len(parts) > 4 and parts[4].strip() in ('Opportunity ID', 'Opp ID', ''):
                continue
            rows.append(parts)
    return rows, header_map


def _build_rows(raw_rows: list[list], dh_names: dict, dh_ids: dict,
                full_names: dict, lobs_inumbers: set, scm_inumbers: set,
                snapshot: object, header_map: dict = None) -> list[dict]:

    # Detect positions of new SDE columns from header (may not exist in older files)
    _hm = header_map or {}
    idx_sde_init  = _hm.get("SDE Initial Engagement Date")
    idx_sde_prim  = _hm.get("SDE Initial Engagement Name") or _hm.get("SDE Primary Engagement Name")
    idx_sde_hname = _hm.get("SDE Handover Name")

    # ACV column — use header map if available, else fall back to legacy position 45
    idx_acv = _hm.get("ACV kUSD") if _hm else None
    if idx_acv is None:
        idx_acv = 45  # legacy position

    # Opp ID column
    idx_opp = _hm.get("Opportunity ID") if _hm else None
    if idx_opp is None:
        idx_opp = 4  # legacy position

    # Account Sales Group column — may differ between versions
    idx_acct_srg = _hm.get("IC_ACCOUNT_SRG_TEXT") or _hm.get("Account Sales Group Text")
    if idx_acct_srg is None:
        idx_acct_srg = 40  # legacy

    # SIC / Mastercode / Account BP columns (added 2026-07-31)
    idx_sic_code   = _hm.get("SIC Code")
    idx_sic_desc   = _hm.get("SIC Description")
    idx_mastercode = _hm.get("SAP Mastercode")
    idx_account_bp = _hm.get("Account BP")

    min_cols = max(idx_acv, idx_opp) + 1 if idx_acv and idx_opp else 46

    def _safe(parts, idx):
        return parts[idx] if idx is not None and idx < len(parts) else None

    # First pass: compute acv_total_opp per opportunity
    acv_by_opp: dict[str, float] = defaultdict(float)
    for parts in raw_rows:
        if len(parts) < min_cols:
            continue
        opp_id = clean_str(parts[idx_opp])
        acv_k  = clean_num(parts[idx_acv])
        acv    = acv_k * 1000 if acv_k is not None else None   # kUSD → USD
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
        acv_usd   = clean_num(parts[idx_acv])
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
            "sde_handover_date":             clean_date(parts[37]),
            "local_acct_owner":              clean_str(parts[38]),
            "account_planned_archetype_text": clean_str(parts[39]),
            "account_sales_group_text":      clean_str(_safe(parts, idx_acct_srg)),
            "iac":                           clean_str(parts[41]),
            "opp_origin":                    clean_str(parts[42]),
            "flsm":                          clean_str(parts[43]),
            "mm_pipe_source":                clean_str(parts[44]),
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
