"""
Master runner — executes all ETLs in dependency order.

Load order:
  1. fx            (USD→EUR rate from ECB — independent)
  2. targets       (team definitions — needed by all VAT logic)
  3. full_vat / dh_vat
  4. melody        (independent)
  5. presales      (independent)
  6. dda           (independent)
  7. rvp           (independent)
  8. pipeline      (depends on vat + targets)
  9. pipeline_exploded (depends on pipeline + vat + targets + calc_rules)

Usage:
  python run_all.py                        # full load (standard weekly run)
  python run_all.py --skip-pipeline        # reference tables only (fast)
  python run_all.py --only-pipeline        # pipeline + exploded only
  python run_all.py --last-week-only       # reload pipeline_last_week only
  python run_all.py --fx-rate 0.9150       # manual EUR rate override + full load
  python run_all.py --skip-fx              # skip ECB fetch (keep last stored rate)
"""
import argparse
import sys
import time
from utils import get_logger

log = get_logger("etl.run_all")


def _run(name: str, fn):
    log.info("━━━ START %s ━━━", name)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        log.info("━━━ DONE  %s  (%.1fs) result=%s ━━━", name, elapsed, result)
        return True
    except Exception as e:
        log.error("━━━ FAIL  %s: %s ━━━", name, e, exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="DH Main App ETL runner")
    parser.add_argument("--skip-pipeline",  action="store_true",
                        help="Skip pipeline and pipeline_exploded loads")
    parser.add_argument("--only-pipeline",  action="store_true",
                        help="Load pipeline and pipeline_exploded only")
    parser.add_argument("--last-week-only", action="store_true",
                        help="Reload only pipeline_last_week (skip current-week pipeline)")
    parser.add_argument("--skip-fx",        action="store_true",
                        help="Skip ECB rate fetch (keep last stored rate)")
    parser.add_argument("--fx-rate",        type=float, default=None,
                        help="Manual USD→EUR rate override (e.g. 0.9150)")
    args = parser.parse_args()

    import etl_fx
    import etl_targets
    import etl_vat
    import etl_melody
    import etl_presales
    import etl_dda
    import etl_rvp
    import etl_pipeline
    import etl_pipeline_exploded

    steps_ref = [
        ("targets",  etl_targets.run),
        ("vat",      etl_vat.run),
        ("melody",   etl_melody.run),
        ("presales", etl_presales.run),
        ("dda",      etl_dda.run),
        ("rvp",      etl_rvp.run),
    ]

    steps_pipeline = [
        ("pipeline",          lambda: etl_pipeline.run(last_week=args.last_week_only)),
        ("pipeline_exploded", etl_pipeline_exploded.run),
    ]

    failures = []
    t_start = time.time()

    # FX rate — always first unless explicitly skipped
    if not args.skip_fx and not args.only_pipeline:
        _run("fx", lambda: etl_fx.run(manual_rate=args.fx_rate))

    if not args.only_pipeline:
        for name, fn in steps_ref:
            if not _run(name, fn):
                failures.append(name)

    if not args.skip_pipeline:
        for name, fn in steps_pipeline:
            if not _run(name, fn):
                failures.append(name)

    elapsed = time.time() - t_start
    if failures:
        log.error("ETL completed with failures: %s  (total %.1fs)", failures, elapsed)
        sys.exit(1)
    else:
        log.info("All ETLs completed successfully in %.1fs", elapsed)


if __name__ == "__main__":
    main()
