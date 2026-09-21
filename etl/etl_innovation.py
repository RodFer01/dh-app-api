"""
ETL: InnovationDash_*.xlsx → account_installed_base table
Sheet: myListBP, header at row index 3 (0-based = 4th row in the file).
BP is column A (index 0). Product cols 38–69.
Full-replace (TRUNCATE + upsert) on every run.
"""
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, truncate
from config import FILES

log = get_logger("etl.innovation")

# Cols 39, 41, 68, 69 are version strings / cross-sell — skipped intentionally.
INNO_COL_MAP = {
    38: {"label": "SAP ERP (On-Premise)",                "l1": "S/4HANA Cloud"},
    40: {"label": "S/4HANA On-Premise",                  "l1": "S/4HANA Cloud"},
    42: {"label": "RISE with SAP",                       "l1": "S/4HANA Cloud"},
    43: {"label": "S/4HANA Private Cloud Edition",       "l1": "S/4HANA Cloud"},
    44: {"label": "S/4HANA Public Cloud Edition",        "l1": "S/4HANA Public Cloud"},
    45: {"label": "Finance & Q2C (GRC / Finance / Tax)", "l1": "S/4HANA Finance"},
    46: {"label": "Procurement (SAP Ariba)",             "l1": "SAP Ariba"},
    47: {"label": "Travel & Expense (SAP Concur)",       "l1": "SAP Concur"},
    48: {"label": "Finance & Spend (Public Cloud)",      "l1": "SAP Risk & Compliance"},
    49: {"label": "Business Network (Supply/Buy Side)",  "l1": "SAP Ariba"},
    50: {"label": "Digital Supply Chain (SAP IBP)",      "l1": "SAP IBP"},
    51: {"label": "SCM (Public Cloud)",                  "l1": "SAP IBP"},
    52: {"label": "Core HR & Payroll (SuccessFactors)",  "l1": "SAP SuccessFactors"},
    53: {"label": "Learning & Talent (SuccessFactors)",  "l1": "SAP SuccessFactors"},
    54: {"label": "HCM (Public Cloud)",                  "l1": "SAP SuccessFactors"},
    55: {"label": "SAP Commerce Cloud",                  "l1": "SAP Commerce Cloud"},
    56: {"label": "SAP Marketing (Emarsys)",             "l1": "SAP Emarsys"},
    57: {"label": "Sales & Service (CX)",                "l1": "SAP Sales Cloud"},
    58: {"label": "CX (Public Cloud)",                   "l1": "SAP Commerce Cloud"},
    59: {"label": "SAP BTP (Cross)",                     "l1": "SAP BTP"},
    60: {"label": "Business Data Cloud",                 "l1": "SAP Analytics Cloud"},
    61: {"label": "SAP Build",                           "l1": "SAP BTP"},
    62: {"label": "SAP Integration Suite",               "l1": "SAP BTP"},
    63: {"label": "SAP BTP (Public Cloud)",              "l1": "SAP BTP"},
    64: {"label": "SAP LeanIX",                          "l1": "SAP LeanIX"},
    65: {"label": "SAP Signavio",                        "l1": "SAP Signavio"},
    66: {"label": "Business Risk & Threat Management",   "l1": "SAP Risk & Compliance"},
    67: {"label": "Artificial Intelligence (SAP Joule)", "l1": "SAP Joule"},
}

VALID_STATUSES = {"Live", "Project", "Unused/Shelfware"}

# Cols 38-44 carry string statuses; all others carry numeric counts (0=absent, >0=Live)
STRING_STATUS_COLS = {38, 40, 42, 43, 44}


def _parse_status(raw, col_idx) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if col_idx in STRING_STATUS_COLS:
        s = str(raw).strip()
        return s if s in VALID_STATUSES else None
    # Numeric encoding: 0 = not installed, >0 = Live
    try:
        return "Live" if float(raw) > 0 else None
    except (ValueError, TypeError):
        return None


def run() -> int:
    log.info("Loading InnovationDash xlsx …")
    df = pd.read_excel(FILES["innovation_dash"], sheet_name="myListBP", header=3)

    rows = []
    for _, r in df.iterrows():
        bp = clean_str(r.iloc[0])
        if not bp:
            continue
        for col_idx, mapping in INNO_COL_MAP.items():
            if col_idx >= len(r):
                continue
            status = _parse_status(r.iloc[col_idx], col_idx)
            if not status:
                continue
            rows.append({
                "account_bp":    bp,
                "product_label": mapping["label"],
                "solution_l1":   mapping["l1"],
                "status":        status,
            })

    with get_conn() as conn:
        truncate(conn, "account_installed_base")
        n = upsert(conn, "account_installed_base", rows, ["account_bp", "product_label"])

    log.info("account_installed_base: %d rows loaded", n)
    return n


if __name__ == "__main__":
    run()
