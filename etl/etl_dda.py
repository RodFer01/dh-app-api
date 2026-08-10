"""
ETL: DET.xlsx or LAC Cloud ERP.xlsx → dda table

Supports two formats:
  - DET.xlsx: original format, first sheet
  - LAC Cloud ERP.xlsx: tab named "DET dd.mm.aa" (detected automatically)

Column mapping handles both formats via aliases.
"""
import re
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, clean_num, clean_int, clean_date, truncate
from config import FILES

log = get_logger("etl.dda")

# Column aliases: canonical name → list of possible header strings
COL_ALIASES = {
    "opp_id":                   ["Opp ID", "OPP ID"],
    "region_l3":                ["Region L3"],
    "region_l4":                ["Region L4"],
    "region_l5":                ["Region L5"],
    "solution_area_l2":         ["Solution Area L2"],
    "solution_area_l3":         ["Solution Area L3"],
    "solution_area_l4":         ["Solution Area L4"],
    "company_name":             ["Company Name"],
    "iss":                      ["Engagement Model (ISS)", "ISS"],
    "amount":                   ["Amount (USD)", "Amount"],
    "quarter":                  ["Closing Quarter", "Quarter"],
    "opp_description":          ["Opp Description"],
    "opp_creation_date":        ["Created Date", "Opp Creation Date"],
    "opp_close_date":           ["Closing Date", "Opp Close Date"],
    "fc_category":              ["DRM Category", "FC Category"],
    "market_basket":            ["Market Basket"],
    "channel_partner":          ["Channel Partner"],
    "business_model":           ["Business Model", "Busines Model"],
    "opp_status":               ["Opp Status"],
    "opp_phase":                ["Opp Phase"],
    "opportunity_owner":        ["Opp Owner Name", "Opportunity Owner"],
    "local_account_owner_name": ["Local Account Owner Name"],
    "architect_advisory":       ["Presales CSM Name", "Architect Advisory"],
    "presales_lead_status":     ["Presales Lead Status"],
    "presales_lead_comments":   ["Presales Lead Comments"],
    "qualification_decision":   ["Decision", "Qualification Decision"],
    "qualification_reviewer":   ["Reviewer Name", "Qualification Reviewer"],
    "qualification_date":       ["Review Date", "Qualification Date"],
    "qualification_score":      ["Qualification Score"],
    "distribution_channel":     ["Distribution Channel"],
    "business_type":            ["Business Type"],
    "mm_pipe_source":           ["MM Pipe Source"],
    "adrm_matching":            ["ADRM Matching"],
    "dda_guidance":             ["DDA Guidance"],
    "solution":                 ["Solution"],
    "dda_status":               ["DDA Status"],
    "aging_up_today":           ["Aging (up today)"],
    "range_aging":              ["Range Aging"],
    "channel_manager_name":     ["FLSM of Local Account Owner", "Channel Manager Name"],
    "account_sales_group_text": ["Account Sales Group Text"],
    "total_at_fc":              ["Total @ FC"],
    "btp_l2":                   ["BTP (L2)"],
    "cx_l2":                    ["CX (L2)"],
    "hcm_l2":                   ["HCM (L2)"],
    "btm_l2":                   ["BTM (L2)"],
    "fsm_l2":                   ["FSM (L2)"],
    "scm_l2":                   ["SCM (L2)"],
    "data_ai_l2":               ["Data & AI (L2)"],
    "cloud_erp_private_l2":     ["Cloud ERP Private (L2)"],
    "opp_creation_date_en":     ["Opp Creation Date (EN)"],
    "motive":                   ["Motive"],
}

DATE_FIELDS  = {"opp_creation_date", "opp_close_date", "qualification_date"}
INT_FIELDS   = {"aging_up_today"}
NUM_FIELDS   = {"amount", "qualification_score", "total_at_fc", "btp_l2", "cx_l2",
                "hcm_l2", "btm_l2", "fsm_l2", "scm_l2", "data_ai_l2",
                "cloud_erp_private_l2"}


def _find_det_sheet(path) -> str | None:
    """Find the DET dd.mm.aa tab in a multi-sheet Excel."""
    xl = pd.ExcelFile(path)
    for name in xl.sheet_names:
        if re.match(r"^DET\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}$", name.strip(), re.IGNORECASE):
            return name
    return None


def _build_col_map(df_cols: list) -> dict:
    """Build mapping: dataframe column → canonical db field."""
    col_map = {}
    cols_lower = {c.strip().lower(): c.strip() for c in df_cols}
    for db_field, aliases in COL_ALIASES.items():
        for alias in aliases:
            if alias.lower() in cols_lower:
                col_map[cols_lower[alias.lower()]] = db_field
                break
    return col_map


def _get_value(row, col_map, db_field):
    src_cols = [k for k, v in col_map.items() if v == db_field]
    for col in src_cols:
        val = row.get(col)
        if val is not None and str(val).strip() not in ("", "nan", "NaT", "None"):
            return val
    return None


def _load_dataframe(path) -> pd.DataFrame:
    """Load the correct sheet from the file."""
    sheet = _find_det_sheet(path)
    if sheet:
        log.info("  Found DET sheet: '%s'", sheet)
        return pd.read_excel(path, sheet_name=sheet, dtype=str)
    # Fallback: first sheet
    log.info("  No DET tab found, using first sheet")
    return pd.read_excel(path, dtype=str)


def run(path=None) -> int:
    if path is None:
        # Prefer LAC Cloud ERP if it exists, fallback to DET.xlsx
        lac_path = FILES.get("lac_cloud_erp")
        if lac_path and lac_path.exists():
            path = lac_path
            log.info("Loading LAC Cloud ERP: %s …", path.name)
        else:
            path = FILES["dda"]
            log.info("Loading DET.xlsx: %s …", path.name)

    df = _load_dataframe(path)
    df.columns = [str(c).strip() for c in df.columns]
    col_map = _build_col_map(df.columns.tolist())
    log.info("  Columns mapped: %d / %d", len(col_map), len(COL_ALIASES))

    rows = []
    for _, r in df.iterrows():
        opp_id = clean_str(_get_value(r, col_map, "opp_id"))
        if not opp_id or opp_id.lower() in ("opp id", "nan"):
            continue

        row = {"opp_id": opp_id}
        for db_field in COL_ALIASES:
            if db_field == "opp_id":
                continue
            val = _get_value(r, col_map, db_field)
            if db_field in DATE_FIELDS:
                row[db_field] = clean_date(val)
            elif db_field in INT_FIELDS:
                row[db_field] = clean_int(val)
            elif db_field in NUM_FIELDS:
                row[db_field] = clean_num(val)
            else:
                row[db_field] = clean_str(val)
        rows.append(row)

    with get_conn() as conn:
        truncate(conn, "dda")
        n = upsert(conn, "dda", rows, ["opp_id"])

    log.info("dda: %d rows loaded", n)
    return n


if __name__ == "__main__":
    print(run())
