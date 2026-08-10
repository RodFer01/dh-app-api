"""
ETL: Fetch USD→EUR rate from ECB public API → fx_rates table.

Falls back to the last stored rate if the API is unreachable.
Can also be called with a manual rate:
  python etl_fx.py --rate 0.9150
"""
import argparse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from utils import get_conn, get_logger, clean_num

log = get_logger("etl.fx")

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_NS  = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"


def _fetch_ecb_usd_eur() -> float | None:
    """
    ECB publishes EUR as base. EUR/USD rate → invert to get USD/EUR.
    Returns USD→EUR rate (e.g. 0.9234) or None on failure.
    """
    try:
        with urllib.request.urlopen(ECB_URL, timeout=10) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        # Walk: Envelope → Cube → Cube[time] → Cube[currency]
        for cube_time in root.iter(f"{{{ECB_NS}}}Cube"):
            if cube_time.get("time"):
                for cube_cur in cube_time:
                    if cube_cur.get("currency") == "USD":
                        eur_usd = float(cube_cur.get("rate"))
                        usd_eur = round(1.0 / eur_usd, 6)
                        log.info("ECB rate: 1 EUR = %.4f USD  →  1 USD = %.4f EUR",
                                 eur_usd, usd_eur)
                        return usd_eur
    except Exception as e:
        log.warning("ECB fetch failed: %s", e)
    return None


def run(manual_rate: float | None = None) -> float | None:
    rate = manual_rate or _fetch_ecb_usd_eur()

    if rate is None:
        log.warning("No rate available — keeping last stored rate")
        return None

    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        # MERGE INTO instead of ON CONFLICT (HANA-compatible)
        cur.execute(
            """
            MERGE INTO fx_rates AS t
            USING (SELECT ? AS from_currency, ? AS to_currency, ? AS rate,
                          ? AS rate_date, ? AS source FROM DUMMY) AS s
            ON (t.from_currency = s.from_currency AND t.to_currency = s.to_currency
                AND t.rate_date = s.rate_date)
            WHEN MATCHED THEN UPDATE SET t.rate = s.rate, t.source = s.source
            WHEN NOT MATCHED THEN INSERT (from_currency, to_currency, rate, rate_date, source)
                VALUES (s.from_currency, s.to_currency, s.rate, s.rate_date, s.source)
            """,
            ('USD', 'EUR', rate, today, "manual" if manual_rate else "ECB")
        )
    log.info("fx_rates: USD→EUR = %.6f stored for %s", rate, today)
    return rate


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=None,
                        help="Manual override rate (e.g. 0.9150)")
    args = parser.parse_args()
    run(manual_rate=args.rate)
