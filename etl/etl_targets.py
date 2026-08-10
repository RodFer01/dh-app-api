"""
ETL: Targets.xlsx → targets table
Values in file are in EUR (€); stored AS-IS in EUR.
The frontend applies fxRate (USD→EUR) for display, so backend must return USD.
Conversion EUR→USD happens at query time using fx_rates table.
"""
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, clean_num, clean_int
from config import FILES, LOBS_SUB_TEAMS

log = get_logger("etl.targets")

_NAME_TO_SUBTEAM = {
    name.lower(): sub
    for sub, names in LOBS_SUB_TEAMS.items()
    for name in names
}

def _infer_subteam(manager: str, name: str) -> str | None:
    if manager != "LoBs Team":
        return None
    key = (name or "").lower()
    if key in _NAME_TO_SUBTEAM:
        return _NAME_TO_SUBTEAM[key]
    for k, sub in _NAME_TO_SUBTEAM.items():
        if k in key or key in k:
            return sub
    return None


def run() -> int:
    log.info("Loading Targets.xlsx …")
    import openpyxl
    wb = openpyxl.load_workbook(str(FILES["targets"]), read_only=True, data_only=True)
    # Use 'Team Targets' sheet if present, otherwise fall back to active sheet
    if 'Team Targets' in wb.sheetnames:
        ws = wb['Team Targets']
    else:
        ws = wb.active

    # Read header row and stop after 10 consecutive empty rows
    # (file may have 1M+ blank rows which would take minutes to iterate)
    headers = None
    raw_rows = []
    consecutive_empty = 0

    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = row
            continue
        if row[1]:   # Inumber is column index 1
            raw_rows.append(row)
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty > 10:
                break

    wb.close()
    log.info("targets: %d data rows found in file", len(raw_rows))

    col_map = {str(h).strip(): i for i, h in enumerate(headers) if h}

    def get(row, col_name):
        i = col_map.get(col_name)
        return row[i] if i is not None and i < len(row) else None

    with get_conn() as conn:
        rows = []
        for r in raw_rows:
            inumber = clean_str(get(r, "Inumber"))
            if not inumber:
                continue
            manager = clean_str(get(r, "Manager"))
            name    = clean_str(get(r, "Nombre de empleado"))
            rows.append({
                "inumber":                    inumber,
                "nombre_empleado":            name,
                "manager":                    manager,
                "area":                       manager,
                "sub_team":                   _infer_subteam(manager or "", name or ""),
                "acv_booked_annual_target":   clean_num(get(r, "€ of Booked ACV supported")),
                "deals_booked_annual_target": clean_int(get(r, "# of Booked Deals supported")),
                "quota_q1":  clean_num(get(r, "Q1")),
                "quota_q2":  clean_num(get(r, "Q2")),
                "quota_q3":  clean_num(get(r, "Q3")),
                "quota_q4":  clean_num(get(r, "Q4")),
                "deals_q1":  clean_int(get(r, "Op1")),
                "deals_q2":  clean_int(get(r, "Op2")),
                "deals_q3":  clean_int(get(r, "Op3")),
                "deals_q4":  clean_int(get(r, "Op4")),
            })

        # HANA upsert = delete matching inumbers first, then insert
        cur = conn.cursor()
        inumbers = [r["inumber"] for r in rows if r.get("inumber")]
        if inumbers:
            ph = ",".join("?" for _ in inumbers)
            cur.execute(f"DELETE FROM targets WHERE inumber IN ({ph})", inumbers)
            log.info("targets: deleted %d existing rows before re-insert", cur.rowcount)

        n = upsert(conn, "targets", rows, ["inumber"])

    log.info("targets: %d rows upserted (EUR values stored as-is)", n)
    return n


if __name__ == "__main__":
    run()

