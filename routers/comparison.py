"""Year-over-Year comparison router — 2025 vs 2026."""
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from database import query, query_one
from auth import verify_token

router = APIRouter(prefix="/api/comparison", tags=["comparison"])

ENT_FILTER = "AND (iss_group_text IS NULL OR iss_group_text <> 'Enterprise')"


def _yoy_data(table: str, exclude_enterprise: bool,
              quarters: list, solutions: list, sub_solutions: list = [],
              teams: list = [], sub_teams: list = []) -> dict:
    ent = ENT_FILTER if exclude_enterprise else ""

    # Only count opps with Digital SA participation
    digital_filter = "AND engagement_type = 'Digital'"

    extra_clauses = digital_filter
    if quarters:
        qtr_or = " OR ".join([f"closing_qtr LIKE '%%-{q}'" for q in quarters])
        extra_clauses += f" AND ({qtr_or})"
    if solutions:
        sol_ph = ",".join(["%s"] * len(solutions))
        extra_clauses += f" AND solution_area_l1 IN ({sol_ph})"
    if sub_solutions:
        sub_ph = ",".join(["%s"] * len(sub_solutions))
        extra_clauses += f" AND sub_solution_area_l2 IN ({sub_ph})"

    # Team filter: restrict to opps where the SA belongs to the selected team(s)
    # Use dh_vat / dh_vat_2025 depending on the table
    vat_table = "dh_vat_2025" if "2025" in table else "dh_vat"
    team_clause = ""
    team_params: list = []
    if teams or sub_teams:
        team_conditions = []
        if teams:
            t_ph = ",".join(["%s"] * len(teams))
            team_conditions.append(f"t.area IN ({t_ph})")
            team_params.extend(teams)
        if sub_teams:
            st_ph = ",".join(["%s"] * len(sub_teams))
            team_conditions.append(f"t.sub_team IN ({st_ph})")
            team_params.extend(sub_teams)
        team_where = " AND ".join(team_conditions)
        extra_clauses += f"""
          AND opportunity_id IN (
            SELECT DISTINCT v.opportunity_id
            FROM {vat_table} v
            JOIN targets t ON t.inumber = v.sales_team_member_id
            WHERE {team_where}
          )"""

    all_params = list(solutions) + list(sub_solutions) + team_params

    snap_sql = f"SELECT MAX(snapshot_week) FROM {table}"

    summary = query_one(f"""
        SELECT
            COUNT(DISTINCT CASE WHEN drm_category = 'Booked/Won' THEN opportunity_id END)   AS n_booked,
            COALESCE(SUM(CASE WHEN drm_category = 'Booked/Won' THEN acv_usd END), 0)        AS acv_booked,
            COUNT(DISTINCT CASE WHEN drm_category NOT IN
                ('Booked/Won','Disc/Lost','Excluded from Pipeline')
                AND fc_qualification IN ('Committed','Probable','Upside')
                THEN opportunity_id END)                                                     AS n_active,
            COALESCE(SUM(CASE WHEN drm_category NOT IN
                ('Booked/Won','Disc/Lost','Excluded from Pipeline')
                AND fc_qualification IN ('Committed','Probable','Upside')
                THEN acv_usd END), 0)                                                        AS acv_active,
            COUNT(DISTINCT CASE WHEN drm_category = 'Disc/Lost' THEN opportunity_id END)    AS n_lost,
            COALESCE(SUM(CASE WHEN drm_category = 'Disc/Lost' THEN acv_usd END), 0)         AS acv_lost
        FROM {table}
        WHERE snapshot_week = ({snap_sql}) {ent} {extra_clauses}
    """, all_params or None)

    by_quarter = query(f"""
        SELECT closing_qtr,
               COUNT(DISTINCT opportunity_id) AS n_booked,
               COALESCE(SUM(acv_usd), 0)      AS acv_booked
        FROM {table}
        WHERE snapshot_week = ({snap_sql})
          AND drm_category = 'Booked/Won'
          {ent} {extra_clauses}
        GROUP BY closing_qtr ORDER BY closing_qtr
    """, all_params or None)

    by_solution = query(f"""
        SELECT solution_area_l1,
               COUNT(DISTINCT opportunity_id) AS n_booked,
               COALESCE(SUM(acv_usd), 0)      AS acv_booked
        FROM {table}
        WHERE snapshot_week = ({snap_sql})
          AND drm_category = 'Booked/Won'
          {ent} {extra_clauses}
        GROUP BY solution_area_l1 ORDER BY acv_booked DESC
    """, all_params or None)

    by_region = query(f"""
        SELECT region_lvl3,
               COUNT(DISTINCT opportunity_id) AS n_booked,
               COALESCE(SUM(acv_usd), 0)      AS acv_booked
        FROM {table}
        WHERE snapshot_week = ({snap_sql})
          AND drm_category = 'Booked/Won'
          {ent} {extra_clauses}
        GROUP BY region_lvl3 ORDER BY acv_booked DESC
    """, all_params or None)

    by_segment = query(f"""
        SELECT iss_group_text,
               COUNT(DISTINCT opportunity_id) AS n_booked,
               COALESCE(SUM(acv_usd), 0)      AS acv_booked
        FROM {table}
        WHERE snapshot_week = ({snap_sql})
          AND drm_category = 'Booked/Won'
          {ent} {extra_clauses}
        GROUP BY iss_group_text ORDER BY acv_booked DESC
    """, all_params or None)

    by_drm = query(f"""
        SELECT drm_category,
               COUNT(DISTINCT opportunity_id) AS n_opps,
               COALESCE(SUM(acv_usd), 0)      AS acv
        FROM {table}
        WHERE snapshot_week = ({snap_sql}) {ent} {extra_clauses}
        GROUP BY drm_category ORDER BY acv DESC
    """, all_params or None)

    return {
        "summary":     summary,
        "by_quarter":  by_quarter,
        "by_solution": by_solution,
        "by_region":   by_region,
        "by_segment":  by_segment,
        "by_drm":      by_drm,
    }


def _melody_summary(melody_table: str, pipeline_table: str,
                    quarters: list, solutions: list) -> dict:
    """
    Melody support metrics — only opps with Digital SA engagement.
    """
    extra = " AND p.engagement_type = 'Digital'"
    sol_params = []
    if quarters:
        qtr_or = " OR ".join([
            f"TO_NVARCHAR(EXTRACT(YEAR FROM m.activ_start_date_utc)) || '-Q' || TO_NVARCHAR(CEIL(EXTRACT(MONTH FROM m.activ_start_date_utc)/3.0)) LIKE '%%-{q}'"
            for q in quarters
        ])
        extra += f" AND ({qtr_or})"
    if solutions:
        ph = ",".join(["%s"] * len(solutions))
        extra += f" AND p.solution_area_l1 IN ({ph})"
        sol_params = solutions

    snap_p = f"SELECT MAX(snapshot_week) FROM {pipeline_table}"

    # Quarter filter for melody table (uses m. alias)
    melody_qtr_filter = ""
    if quarters:
        qtr_or = " OR ".join([
            f"TO_NVARCHAR(EXTRACT(YEAR FROM m.activ_start_date_utc)) || '-Q' || TO_NVARCHAR(CEIL(EXTRACT(MONTH FROM m.activ_start_date_utc)/3.0)) LIKE '%%-{q}'"
            for q in quarters
        ])
        melody_qtr_filter = f" AND ({qtr_or})"

    # Pipeline-side filters (uses p. alias)
    pipeline_extra = "AND p.engagement_type = 'Digital'"
    if solutions:
        ph = ",".join(["%s"] * len(solutions))
        pipeline_extra += f" AND p.solution_area_l1 IN ({ph})"

    # Activities — count from melody joined to pipeline for Digital filter
    acts = query_one(f"""
        SELECT
            COUNT(*)                             AS n_activities,
            COUNT(DISTINCT m.activ_id)           AS n_unique_activities,
            COUNT(DISTINCT m.opp_id)             AS n_opps_supported,
            COUNT(DISTINCT m.activ_team_empl_id) AS n_advisors
        FROM {melody_table} m
        WHERE m.opp_id IS NOT NULL
          {melody_qtr_filter}
          AND EXISTS (
              SELECT 1 FROM {pipeline_table} p2
              WHERE p2.opportunity_id = m.opp_id
                AND p2.snapshot_week = ({snap_p})
                AND p2.engagement_type = 'Digital'
                {("AND p2.solution_area_l1 IN (" + ",".join(["%s"]*len(solutions)) + ")") if solutions else ""}
          )
    """, sol_params or None) or {}

    # ACV of supported opps from pipeline
    acv = query_one(f"""
        SELECT
            COUNT(DISTINCT p.opportunity_id)  AS n_opps_in_pipeline,
            COALESCE(SUM(p.acv_usd), 0)       AS acv_supported,
            COUNT(DISTINCT CASE WHEN p.drm_category = 'Booked/Won' THEN p.opportunity_id END) AS n_booked,
            COALESCE(SUM(CASE WHEN p.drm_category = 'Booked/Won' THEN p.acv_usd END), 0)     AS acv_booked_supported
        FROM {pipeline_table} p
        WHERE p.snapshot_week = ({snap_p})
          {pipeline_extra}
          AND EXISTS (
              SELECT 1 FROM {melody_table} m
              WHERE m.opp_id = p.opportunity_id
              {melody_qtr_filter}
          )
    """, sol_params or None) or {}

    has_data = (query_one(f"SELECT COUNT(*) AS n FROM {melody_table}") or {}).get("n", 0) > 0

    return {**acts, **acv, "has_data": has_data}


@router.get("/filters")
def get_filters(_user=Depends(verify_token)):
    """Return available filter options from pipeline 2026."""
    solutions = [r["solution_area_l1"] for r in query("""
        SELECT DISTINCT solution_area_l1
        FROM pipeline
        WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND solution_area_l1 IS NOT NULL
          AND solution_area_l1 NOT IN ('Solution Area (L1)')
        ORDER BY solution_area_l1
    """)]
    sub_solutions = [r["sub_solution_area_l2"] for r in query("""
        SELECT DISTINCT sub_solution_area_l2
        FROM pipeline
        WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND sub_solution_area_l2 IS NOT NULL
          AND sub_solution_area_l2 NOT IN ('Sub-Solution Area (L2)')
        ORDER BY sub_solution_area_l2
    """)]
    teams_row = query("""
        SELECT DISTINCT area, sub_team FROM targets
        WHERE area IS NOT NULL
        ORDER BY area, sub_team
    """)
    areas = sorted({r['area'] for r in teams_row if r['area']})
    sub_teams = sorted({r['sub_team'] for r in teams_row if r['sub_team']})
    return {
        "solutions":     [v for v in solutions if v],
        "sub_solutions": [v for v in sub_solutions if v],
        "quarters":      ["Q1", "Q2", "Q3", "Q4"],
        "teams":         areas,
        "sub_teams":     sub_teams,
    }


@router.get("")
def get_comparison(
    exclude_enterprise: bool = False,
    quarters: List[str] = Query(default=[]),
    solutions: List[str] = Query(default=[]),
    sub_solutions: List[str] = Query(default=[]),
    teams: List[str] = Query(default=[]),
    sub_teams: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    data_2025 = _yoy_data("pipeline_2025", exclude_enterprise, quarters, solutions, sub_solutions, teams, sub_teams)
    data_2026 = _yoy_data("pipeline",      exclude_enterprise, quarters, solutions, sub_solutions, teams, sub_teams)

    melody_2025 = _melody_summary("melody_2025", "pipeline_2025", quarters, solutions)
    melody_2026 = _melody_summary("melody",      "pipeline",      quarters, solutions)

    has_2025 = (query_one("SELECT COUNT(*) AS n FROM pipeline_2025") or {}).get("n", 0) > 0

    return {
        "y2025":        data_2025,
        "y2026":        data_2026,
        "melody_2025":  melody_2025,
        "melody_2026":  melody_2026,
        "has_2025":     has_2025,
    }
