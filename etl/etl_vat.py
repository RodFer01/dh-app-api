"""
ETL: VAT CSVs → full_vat / dh_vat tables.
Runs before pipeline ETL so VAT-derived fields (VAT_DH, VAT_Full, etc.) are ready.
"""
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, clean_int, truncate
from config import FILES

log = get_logger("etl.vat")

_COLS = {
    "Opportunity ID":            "opportunity_id",
    "Opportunity Description":   "opportunity_description",
    "Sales Team Member ID":      "sales_team_member_id",
    "Sales Team Member Name":    "sales_team_member_name",
    "Sales Team Role":           "sales_team_role",
    "Company Name":              "company_name",
    "Region Level 1":            "region_level_1",
    "Region Level 2":            "region_level_2",
    "Region Level 3":            "region_level_3",
    "DRM Category":              "drm_category",
    "Deal Phase":                "deal_phase",
    "Opportunity Close Quarter": "opportunity_close_quarter",
    "Opp Count":                 "opp_count",
}


def _load_vat(path, table: str) -> int:
    log.info("Loading %s → %s …", path.name, table)
    df = pd.read_csv(path, sep=";", skiprows=1, encoding="utf-8",
                     on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    rows = []
    for _, r in df.iterrows():
        opp_id    = clean_str(r.get("Opportunity ID"))
        member_id = clean_str(r.get("Sales Team Member ID"))
        if not opp_id or not member_id:
            continue

        rows.append({
            "opportunity_id":            opp_id,
            "opportunity_description":   clean_str(r.get("Opportunity Description")),
            "sales_team_member_id":      member_id,
            "sales_team_member_name":    clean_str(r.get("Sales Team Member Name")),
            "sales_team_role":           clean_str(r.get("Sales Team Role")),
            "company_name":              clean_str(r.get("Company Name")),
            "region_level_1":            clean_str(r.get("Region Level 1")),
            "region_level_2":            clean_str(r.get("Region Level 2")),
            "region_level_3":            clean_str(r.get("Region Level 3")),
            "drm_category":              clean_str(r.get("DRM Category")),
            "deal_phase":                clean_str(r.get("Deal Phase")),
            "opportunity_close_quarter": clean_str(r.get("Opportunity Close Quarter")),
            "opp_count":                 clean_int(r.get("Opp Count")),
        })

    with get_conn() as conn:
        truncate(conn, table)
        n = upsert(conn, table, rows, ["opportunity_id", "sales_team_member_id"])

    log.info("%s: %d rows loaded", table, n)
    return n


def run(only: str | None = None) -> dict:
    """Load VAT tables. `only` can be 'full_vat' or 'dh_vat' to load just one."""
    result = {}
    if only != "dh_vat":
        result["full_vat"] = _load_vat(FILES["full_vat"], "full_vat")
    if only != "full_vat":
        result["dh_vat"] = _load_vat(FILES["dh_vat"], "dh_vat")
    return result


if __name__ == "__main__":
    run()
