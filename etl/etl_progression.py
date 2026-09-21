"""
ETL: Progression Framework (Details tab) → opportunity_priorities table.
Columns loaded: opportunity_id, priority_t1, priority, priority_adj, probability_pct.
"""
import pandas as pd
from utils import get_conn, get_logger, clean_str, clean_num, truncate, upsert
from config import FILES

log = get_logger("etl.progression")


def run() -> int:
    log.info("Loading Progression Framework xlsx …")
    df = pd.read_excel(FILES["progression"], sheet_name="Details", header=0)
    df.columns = [str(c).strip() for c in df.columns]

    rows = []
    for _, r in df.iterrows():
        opp_id = clean_str(r.get("Opportunity ID") or r.get("Opp ID (clean)"))
        if not opp_id:
            continue
        # Normalise priority_t1: may be "No prior value" or a number
        raw_t1 = r.get("Prioritie T-1")
        if raw_t1 is None or str(raw_t1).strip().lower() in ("no prior value", "nan", ""):
            priority_t1 = None
        else:
            try:
                priority_t1 = int(float(str(raw_t1).strip()))
            except (ValueError, TypeError):
                priority_t1 = None

        raw_p = r.get("Prioritie")
        try:
            priority = int(float(str(raw_p).strip())) if raw_p is not None else None
        except (ValueError, TypeError):
            priority = None

        raw_adj = r.get("Priority Adj")
        try:
            priority_adj = int(float(str(raw_adj).strip())) if raw_adj is not None else None
        except (ValueError, TypeError):
            priority_adj = None

        probability_pct = clean_num(r.get("Probability %"))

        rows.append({
            "opportunity_id": opp_id,
            "priority_t1":    priority_t1,
            "priority":       priority,
            "priority_adj":   priority_adj,
            "probability_pct": probability_pct,
        })

    with get_conn() as conn:
        truncate(conn, "opportunity_priorities")
        n = upsert(conn, "opportunity_priorities", rows, ["opportunity_id"])

    log.info("opportunity_priorities: %d rows loaded", n)
    return n


if __name__ == "__main__":
    run()
