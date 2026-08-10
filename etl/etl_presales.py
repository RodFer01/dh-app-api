"""
ETL: Deal Management CSV → presales table
"""
from utils import get_conn, get_logger, upsert, clean_str, truncate
from config import FILES
import re

log = get_logger("etl.presales")

FIORI_BASE = "https://sapit-home-prod-004.launchpad.cfapps.eu10.hana.ondemand.com/site#harmonyquote-Display?sap-ui-app-id-hint=1ff99794-b150-4e6d-ad32-d960c79f9a7a&/Opportunity/"


def _extract_opp_id(raw: str) -> str | None:
    """Extract plain opportunity ID from either a bare ID or a SAP dynamic redirect URL."""
    if not raw:
        return None
    # URL pattern: .../opportunities/OPP/0303920459  or  .../0303920459
    m = re.search(r'/([A-Z0-9]{10,})\s*$', raw.strip())
    if m:
        return m.group(1)
    # Already a plain ID (no slash)
    if '/' not in raw:
        return raw.strip()
    return None


def _opportunity_url(opp_id: str) -> str:
    return f"{FIORI_BASE}{opp_id}"


def run() -> int:
    log.info("Loading Presales CSV …")
    rows = []
    with open(FILES["presales"], encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i == 0:
                continue
            parts = [p.strip() for p in line.rstrip("\n").split(";")]
            if len(parts) < 4:
                continue
            raw_opp = clean_str(parts[3])
            if not raw_opp or raw_opp in ("Opp ID", "opp_id"):
                continue
            opp_id = _extract_opp_id(raw_opp)
            if not opp_id:
                continue
            rows.append({
                "region_l1":              clean_str(parts[0]),
                "region_l2":              clean_str(parts[1]),
                "region_l3":              clean_str(parts[2]),
                "opp_id":                 opp_id,
                "presales_lead_status":   clean_str(parts[4]) if len(parts) > 4 else None,
                "presales_lead_comments": clean_str(parts[5]) if len(parts) > 5 else None,
                "opportunity_link":       _opportunity_url(opp_id),
            })

    with get_conn() as conn:
        truncate(conn, "presales")
        n = upsert(conn, "presales", rows, ["opp_id"])

    log.info("presales: %d rows loaded", n)
    return n


if __name__ == "__main__":
    run()
