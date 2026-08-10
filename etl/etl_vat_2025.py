"""
ETL: VAT 2025 CSVs → full_vat_2025 / dh_vat_2025 tables.
Reuses etl_vat._load_vat with different file keys and tables.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from utils import get_logger
from config import FILES
import etl_vat

log = get_logger("etl.vat_2025")


def run(only: str | None = None) -> dict:
    """Load VAT 2025 tables. `only` can be 'full_vat_2025' or 'dh_vat_2025'."""
    result = {}
    if only != "dh_vat_2025":
        path = FILES["full_vat_2025"]
        if not path.exists():
            raise FileNotFoundError(f"VAT Full 2025 file not found: {path}")
        log.info("Loading VAT Full 2025 …")
        result["full_vat_2025"] = etl_vat._load_vat(path, "full_vat_2025")

    if only != "full_vat_2025":
        path = FILES["dh_vat_2025"]
        if not path.exists():
            raise FileNotFoundError(f"VAT DH 2025 file not found: {path}")
        log.info("Loading VAT DH 2025 …")
        result["dh_vat_2025"] = etl_vat._load_vat(path, "dh_vat_2025")

    return result


if __name__ == "__main__":
    print(run())
