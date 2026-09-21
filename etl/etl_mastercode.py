"""
ETL: Mastercode Excel → account_specs table in HANA.

Maps Business Partner ID → IAE + SPEC assignments per LoB.
File: ML*.xlsx (any version with the column layout described below)

Column index → DB field:
  B (1)  = Business Partner ID
  T (19) = IAE / MSE / TSM / ERP SPEC
  U (20) = SPEC CX
  V (21) = SPEC HXM
  W (22) = SPEC PROCUREMENT
  X (23) = SPEC DSC
  Y (24) = SPEC BTP
  AC (28) = SPEC BDC
  AF (31) = SPEC FIN
  AG (32) = SPEC PRO
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from pathlib import Path
from utils import get_conn, get_logger, clean_str
from config import FILES

log = get_logger("etl.mastercode")

_CREATE_SQL = (
    'CREATE TABLE account_specs ('
    '  account_bp          NVARCHAR(50)  NOT NULL,'
    '  iae                 NVARCHAR(200),'
    '  spec_cx             NVARCHAR(200),'
    '  spec_hxm            NVARCHAR(200),'
    '  spec_procurement    NVARCHAR(200),'
    '  spec_dsc            NVARCHAR(200),'
    '  spec_btp            NVARCHAR(200),'
    '  spec_bdc            NVARCHAR(200),'
    '  spec_fin            NVARCHAR(200),'
    '  spec_pro            NVARCHAR(200),'
    '  PRIMARY KEY (account_bp)'
    ')'
)


def _clean_bp(val) -> str | None:
    """Clean BP ID: strip whitespace, remove .0 suffix from numeric-formatted Excel cells."""
    s = clean_str(val)
    if s is None:
        return None
    if s.endswith('.0') and s[:-2].lstrip('-').isdigit():
        s = s[:-2]
    return s


def run(file_path: str | Path | None = None) -> int:
    path = Path(file_path) if file_path else FILES["mastercode"]
    if not path.exists():
        raise FileNotFoundError(f"Mastercode file not found: {path}")

    log.info("Reading %s …", path.name)
    df = pd.read_excel(path, sheet_name=0, header=0, dtype=str)
    log.info("  %d rows, %d cols", len(df), len(df.columns))

    rows = []
    for _, r in df.iterrows():
        bp_raw = r.iloc[1] if len(r) > 1 else None
        bp = _clean_bp(bp_raw)
        if bp is None:
            continue

        def _get(idx):
            if idx >= len(r):
                return None
            return clean_str(r.iloc[idx])

        rows.append({
            "account_bp":       bp,
            "iae":              _get(19),
            "spec_cx":          _get(20),
            "spec_hxm":         _get(21),
            "spec_procurement": _get(22),
            "spec_dsc":         _get(23),
            "spec_btp":         _get(24),
            "spec_bdc":         _get(28),
            "spec_fin":         _get(31),
            "spec_pro":         _get(32),
        })

    log.info("  %d valid BP rows", len(rows))
    if not rows:
        log.warning("No rows to load — aborting")
        return 0

    with get_conn() as conn:
        cur = conn.cursor()

        try:
            cur.execute('DROP TABLE account_specs')
            log.info("Dropped existing account_specs table")
        except Exception:
            pass  # table may not exist on first run

        cur.execute(_CREATE_SQL)
        log.info("Created table account_specs")

        sql = (
            'INSERT INTO account_specs '
            '(account_bp, iae, spec_cx, spec_hxm, spec_procurement,'
            ' spec_dsc, spec_btp, spec_bdc, spec_fin, spec_pro) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)'
        )
        batch = [
            (
                r["account_bp"], r["iae"], r["spec_cx"], r["spec_hxm"],
                r["spec_procurement"], r["spec_dsc"], r["spec_btp"],
                r["spec_bdc"], r["spec_fin"], r["spec_pro"],
            )
            for r in rows
        ]
        for i in range(0, len(batch), 500):
            cur.executemany(sql, batch[i:i + 500])

        log.info("Inserted %d rows into account_specs", len(batch))
        return len(batch)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Load Mastercode Excel into account_specs")
    ap.add_argument("file", nargs="?", help="Path to the Mastercode .xlsx file")
    args = ap.parse_args()
    n = run(args.file)
    print(f"Loaded {n} rows into account_specs")
