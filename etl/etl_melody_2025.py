"""
ETL: Melody 2025 CSV → melody_2025 table.
Reuses etl_melody._parse_melody_csv — same format, different table.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from utils import get_conn, get_logger, upsert, truncate
from config import FILES
import etl_melody

log = get_logger("etl.melody_2025")


def run() -> int:
    path = FILES["melody_2025"]
    if not path.exists():
        raise FileNotFoundError(f"Melody 2025 file not found: {path}")

    log.info("Loading Melody 2025 from %s …", path.name)
    rows = etl_melody._parse_melody_csv(path)
    log.info("  %d rows parsed", len(rows))

    with get_conn() as conn:
        truncate(conn, "melody_2025")
        n = upsert(conn, "melody_2025", rows, ["activ_id", "activ_team_empl_id"])

    log.info("melody_2025: %d rows loaded", n)
    return n


if __name__ == "__main__":
    print(run())
