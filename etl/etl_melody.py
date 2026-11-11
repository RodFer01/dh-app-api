"""
ETL: Melody CSV → melody table
"""
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, clean_int, truncate
from config import FILES
from datetime import datetime

log = get_logger("etl.melody")

_DATE_FMTS = ["%b %d, %Y", "%Y-%m-%d", "%d/%m/%Y"]


def _parse_ts(val) -> datetime | None:
    if not val or str(val).strip() in ("", "nan"):
        return None
    s = str(val).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_melody_csv(path) -> list:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i == 0:
                continue    # SAC header
            parts = [p.strip() for p in line.rstrip("\n").split(";")]
            if len(parts) < 11:
                continue
            activ_id = clean_str(parts[0])
            empl_id  = clean_str(parts[8])
            if not activ_id or not empl_id:
                continue
            # Skip SAC header rows (first data row contains column names)
            if activ_id.lower().startswith('activ') or empl_id.lower().startswith('activ'):
                continue
            rows.append({
                "activ_id":               activ_id,
                "activ_type":             clean_str(parts[1]),
                "activ_status":           clean_str(parts[2]),
                "acct_name":              clean_str(parts[3]),
                "activ_solution_l2":      clean_str(parts[4]),
                "activ_solution_l3":      clean_str(parts[5]),
                "activ_team_member_role": clean_str(parts[6]),
                "opp_id":                 clean_str(parts[7]),
                "activ_team_empl_id":     empl_id,
                "activ_team_empl_name":   clean_str(parts[9]),
                "activ_start_date_utc":   _parse_ts(parts[10]),
                "activ_end_date_utc":     _parse_ts(parts[11]) if len(parts) > 11 else None,
                "activ_notes":            clean_str(parts[12]) if len(parts) > 12 else None,
                "opp_main_solution_l2":   clean_str(parts[13]) if len(parts) > 13 else None,
                "opp_main_solution_l3":   clean_str(parts[14]) if len(parts) > 14 else None,
                "opp_main_solution_l4":   clean_str(parts[15]) if len(parts) > 15 else None,
                "activity_count":         clean_int(parts[16]) if len(parts) > 16 else None,
                "activity_team_count":    clean_int(parts[17]) if len(parts) > 17 else None,
            })
    return rows


def run() -> int:
    log.info("Loading Melody CSV …")
    rows = _parse_melody_csv(FILES["melody"])

    with get_conn() as conn:
        truncate(conn, "melody")
        n = upsert(conn, "melody", rows, ["activ_id", "activ_team_empl_id"])

    log.info("melody: %d rows loaded", n)
    return n


if __name__ == "__main__":
    run()
