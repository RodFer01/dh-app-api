"""
ETL: pipeline_exploded — expands pipeline rows per SA for attainment calculation.

For each (opportunity, solution line) row in pipeline:
  - joins dh_vat to get individual SA inumbers
  - applies calc_rules from DB to determine acv_attributed per SA
  - only processes rows where the SA belongs to ERP areas or SCM sub-team

Runs AFTER etl_pipeline.py.
"""
from collections import defaultdict
from utils import get_conn, get_logger, snap_week, truncate

log = get_logger("etl.pipeline_exploded")


def _load_calc_rules(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT area, solution_l1, solution_l2, divide_by_vat FROM calc_rules ORDER BY id")
    return [
        {"area": r[0], "solution_l1": r[1], "solution_l2": r[2], "divide_by_vat": r[3]}
        for r in cur.fetchall()
    ]


def _match_rule(rules: list[dict], area: str, l1: str, l2: str) -> bool | None:
    """Returns True (divide), False (full), None (no rule = skip this row)."""
    for rule in rules:
        if rule["area"] != area:
            continue
        if rule["solution_l1"] and rule["solution_l1"] != l1:
            continue
        if rule["solution_l2"] and rule["solution_l2"] != l2:
            continue
        return rule["divide_by_vat"]
    return None


def _load_sa_per_opp(conn) -> dict[str, list[dict]]:
    """Returns opp_id → list of {inumber, name, area, sub_team}."""
    result = defaultdict(list)
    cur = conn.cursor()
    cur.execute("""
        SELECT v.opportunity_id,
               v.sales_team_member_id,
               v.sales_team_member_name,
               t.area,
               t.sub_team
        FROM   dh_vat v
        JOIN   targets t ON t.inumber = v.sales_team_member_id
        WHERE  t.area IN ('ERP Cloud BR','ERP Cloud MCLA','ERP MX MCLA IB')
           OR  t.sub_team = 'SCM'
    """)
    for opp_id, inumber, name, area, sub_team in cur.fetchall():
        result[opp_id].append({
            "inumber":  inumber,
            "name":     name,
            "area":     area,
            "sub_team": sub_team,
        })
    return result


def run() -> int:
    snapshot = snap_week()
    log.info("Building pipeline_exploded for snapshot %s …", snapshot)

    with get_conn() as conn:
        rules    = _load_calc_rules(conn)
        sa_map   = _load_sa_per_opp(conn)

        # Load pipeline rows for current snapshot
        cur = conn.cursor()
        cur.execute(
            """
            SELECT opportunity_id, solution_area_l1, sub_solution_area_l2,
                   closing_qtr, drm_category, acv_usd, vat_size
            FROM   pipeline
            WHERE  snapshot_week = ?
            """,
            (snapshot,)
        )
        pipeline_rows = cur.fetchall()

        log.info("  %d pipeline rows to expand", len(pipeline_rows))

        exploded = []
        for (opp_id, l1, l2, closing_qtr, drm_cat, acv_usd, vat_size) in pipeline_rows:
            sa_list = sa_map.get(opp_id, [])
            if not sa_list:
                continue

            for sa in sa_list:
                area     = sa["area"] or ""
                sub_team = sa["sub_team"] or ""
                # For SCM use "SCM" as rule area key
                rule_area = "SCM" if sub_team == "SCM" else area
                divide = _match_rule(rules, rule_area, l1 or "", l2 or "")

                if divide is None:
                    continue   # no matching rule → this SA doesn't count this line

                if divide and vat_size and vat_size > 0:
                    acv_attributed = (acv_usd or 0) / vat_size
                else:
                    acv_attributed = acv_usd or 0

                exploded.append({
                    "opportunity_id":       opp_id,
                    "solution_area_l1":     l1,
                    "sub_solution_area_l2": l2,
                    "closing_qtr":          closing_qtr,
                    "drm_category":         drm_cat,
                    "sa_inumber":           sa["inumber"],
                    "sa_name":              sa["name"],
                    "area":                 area,
                    "sub_team":             sub_team if sub_team else None,
                    "acv_usd_line":         acv_usd,
                    "vat_size":             vat_size,
                    "acv_attributed":       acv_attributed,
                    "snapshot_week":        snapshot,
                })

        log.info("  %d exploded rows generated", len(exploded))

        # Delete existing rows for this snapshot, then bulk insert
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM pipeline_exploded WHERE snapshot_week = ?",
            (snapshot,)
        )

        if exploded:
            cols    = list(exploded[0].keys())
            col_str = ", ".join(c.upper() for c in cols)
            ph      = ", ".join("?" for _ in cols)
            sql     = f"INSERT INTO pipeline_exploded ({col_str}) VALUES ({ph})"
            cur2 = conn.cursor()
            for i in range(0, len(exploded), 500):
                batch = [[row[c] for c in cols] for row in exploded[i:i + 500]]
                cur2.executemany(sql, batch)

    log.info("pipeline_exploded: %d rows inserted", len(exploded))
    return len(exploded)


if __name__ == "__main__":
    run()
