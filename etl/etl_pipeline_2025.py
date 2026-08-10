"""
ETL: Pipeline 2025 CSV → pipeline_2025 table.
Reuses etl_pipeline internal functions — same VAT enrichment, same column mapping.
The 2025 file is treated as a single static snapshot (snap_week = last Sunday of the file).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from utils import get_conn, get_logger, upsert, snap_week, truncate
from config import FILES
import etl_pipeline as _ep

log = get_logger("etl.pipeline_2025")


def run() -> dict:
    path = FILES["pipeline_2025"]
    if not path.exists():
        raise FileNotFoundError(f"Pipeline 2025 file not found: {path}\n"
                                f"Expected: {path}")

    log.info("Loading Pipeline 2025 from %s …", path.name)

    with get_conn() as conn:
        dh_names, dh_ids, full_names = _ep._load_vat_maps(conn)
        lobs_inumbers, scm_inumbers  = _ep._load_lobs_info(conn)

    raw_rows = _ep._read_pipeline_csv(path)
    log.info("  %d raw rows", len(raw_rows))

    # Use a fixed snapshot week for 2025 data (end-of-year marker)
    snapshot = snap_week()   # today's snap_week — just a tag, not used for filtering
    rows = _ep._build_rows(raw_rows, dh_names, dh_ids, full_names,
                           lobs_inumbers, scm_inumbers, snapshot)
    log.info("  %d data rows after parsing", len(rows))

    conflict_cols = ["opportunity_id", "solution_area_l1", "sub_solution_area_l2",
                     "solution_area_l3", "solution_area_l4", "snapshot_week"]

    with get_conn() as conn:
        truncate(conn, "pipeline_2025")
        n = upsert(conn, "pipeline_2025", rows, conflict_cols)

    log.info("pipeline_2025: %d rows upserted", n)
    return {"pipeline_2025": n}


if __name__ == "__main__":
    print(run())
