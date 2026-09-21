"""Melody / activities router."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from database import query, query_one
from auth import verify_token

router = APIRouter(prefix="/api/melody", tags=["melody"])


@router.get("")
def get_activities(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    region: Optional[str] = None,
    sol_l1: Optional[str] = None,
    activity_type: Optional[str] = None,
    status: Optional[str] = None,
    team_area: Optional[str] = None,
    sub_team: Optional[str] = None,
    employee_name: Optional[str] = None,
    drm: Optional[str] = None,
    closing_qtr: Optional[str] = None,
    limit: int = Query(default=5000, le=20000),
    _user=Depends(verify_token),
):
    clauses, params = [], []
    join_cond_extra = ""     # extra conditions pushed into the JOIN ON clause
    join_params = []

    if date_from:     clauses.append("m.activ_start_date_utc >= %s"); params.append(date_from)
    if date_to:       clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); params.append(date_to)
    if activity_type: clauses.append("m.activ_type = %s"); params.append(activity_type)
    if status:        clauses.append("m.activ_status = %s"); params.append(status)
    if employee_name: clauses.append("m.activ_team_empl_name = %s"); params.append(employee_name)
    if region:        clauses.append("p.region_lvl3 = %s"); params.append(region)
    if sol_l1:        clauses.append("p.solution_area_l1 = %s"); params.append(sol_l1)
    if drm:           clauses.append("p.drm_category = %s"); params.append(drm)
    if closing_qtr:   clauses.append("p.closing_qtr = %s"); params.append(closing_qtr)
    # team_area / sub_team filter: must use EXISTS so rows without targets match still appear
    # (but are excluded when filter is active)
    if team_area:
        clauses.append("EXISTS (SELECT 1 FROM targets t2 WHERE t2.inumber = m.activ_team_empl_id AND t2.area = %s)")
        params.append(team_area)
    if sub_team:
        clauses.append("EXISTS (SELECT 1 FROM targets t2 WHERE t2.inumber = m.activ_team_empl_id AND t2.sub_team = %s)")
        params.append(sub_team)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT
            m.activ_id             AS activity_id,
            m.activ_type           AS activity_type,
            m.activ_status         AS activity_status,
            m.acct_name            AS account_name,
            m.activ_solution_l2    AS solution_l2,
            m.activ_solution_l3    AS solution_l3,
            m.opp_id               AS opportunity_id,
            m.activ_team_empl_id   AS employee_id,
            m.activ_team_empl_name AS employee_name,
            m.activ_start_date_utc AS start_date,
            m.activ_end_date_utc   AS end_date,
            m.activ_notes          AS notes,
            t.area                 AS team_area,
            t.sub_team,
            p.region_lvl3          AS region,
            p.solution_area_l1,
            p.closing_qtr,
            p.drm_category,
            p.acv_total_opp
        FROM melody m
        LEFT JOIN (
            SELECT opportunity_id, region_lvl3, solution_area_l1, closing_qtr, drm_category, acv_total_opp
            FROM (
                SELECT opportunity_id, region_lvl3, solution_area_l1, closing_qtr, drm_category, acv_total_opp,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY snapshot_week DESC) AS _rn
                FROM pipeline
                WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
            ) _p WHERE _rn = 1
        ) p ON p.opportunity_id = m.opp_id
        LEFT JOIN targets t ON t.inumber = m.activ_team_empl_id
        {where}
        ORDER BY m.activ_start_date_utc DESC
        LIMIT {int(limit)}
    """
    return query(sql, params)


@router.get("/weekly-trend")
def weekly_trend(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    team_area: Optional[str] = None,
    sub_team: Optional[str] = None,
    employee_name: Optional[str] = None,
    _user=Depends(verify_token),
):
    clauses, params = [], []
    if date_from:     clauses.append("m.activ_start_date_utc >= %s"); params.append(date_from)
    if date_to:       clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); params.append(date_to)
    if employee_name: clauses.append("m.activ_team_empl_name = %s"); params.append(employee_name)
    if team_area:
        clauses.append("EXISTS (SELECT 1 FROM targets t2 WHERE t2.inumber = m.activ_team_empl_id AND t2.area = %s)")
        params.append(team_area)
    if sub_team:
        clauses.append("EXISTS (SELECT 1 FROM targets t2 WHERE t2.inumber = m.activ_team_empl_id AND t2.sub_team = %s)")
        params.append(sub_team)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT
            TO_NVARCHAR(m.activ_start_date_utc, 'YYYY-MM-DD') AS week,
            COUNT(*)                                               AS activity_count
        FROM melody m
        LEFT JOIN targets t ON t.inumber = m.activ_team_empl_id
        {where}
        GROUP BY TO_NVARCHAR(m.activ_start_date_utc, 'YYYY-MM-DD')
        ORDER BY 1
    """
    return query(sql, params or None)


@router.get("/filters")
def melody_filter_options(_user=Depends(verify_token)):
    def _distinct(sql):
        return [r[list(r.keys())[0]] for r in query(sql) if r[list(r.keys())[0]] is not None]

    activity_types = _distinct("SELECT DISTINCT activ_type FROM melody WHERE activ_type IS NOT NULL ORDER BY activ_type")
    statuses       = _distinct("SELECT DISTINCT activ_status FROM melody WHERE activ_status IS NOT NULL ORDER BY activ_status")
    employees      = _distinct("SELECT DISTINCT activ_team_empl_name FROM melody WHERE activ_team_empl_name IS NOT NULL ORDER BY activ_team_empl_name")
    team_areas     = _distinct("SELECT DISTINCT area FROM targets WHERE area IS NOT NULL ORDER BY area")
    sub_teams      = _distinct("SELECT DISTINCT sub_team FROM targets WHERE sub_team IS NOT NULL ORDER BY sub_team")

    def clean(arr):
        skip = {'Activ Status', 'Activ Team Empl Name *', 'Activ Type', 'Sub Team'}
        return [v for v in arr if v not in skip]

    return {
        "activity_types": clean(activity_types),
        "statuses":        clean(statuses),
        "employees":       clean(employees),
        "team_areas":      clean(team_areas),
        "sub_teams":       clean(sub_teams),
    }


@router.get("/vat-gaps")
def vat_gaps(
    drm_categories: list = Query(default=[]),
    digital_sa: str = Query(default=""),
    vat_size_min: int = Query(default=0),
    team: str = Query(default=""),
    _user=Depends(verify_token),
):
    """
    Opportunities where a Digital SA is in the VAT (vat_dh)
    but has NO Melody activity recorded for that opportunity.
    Includes Booked/Won; excludes only Disc/Lost.
    Filterable by DRM Category, SA name, and SA team (targets.area).
    """
    clauses = [
        "p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)",
        "p.vat_dh IS NOT NULL",
        "p.vat_size > 0",
        "p.drm_category NOT IN ('Disc/Lost')",
        "NOT EXISTS (SELECT 1 FROM melody m WHERE m.opp_id = p.opportunity_id)",
    ]
    params: list = []

    if drm_categories:
        ph = ",".join(["%s"] * len(drm_categories))
        clauses.append(f"p.drm_category IN ({ph})")
        params.extend(drm_categories)

    if digital_sa:
        clauses.append("LOWER(p.vat_dh) LIKE %s")
        params.append(f"%{digital_sa.lower()}%")

    if vat_size_min > 0:
        clauses.append("p.vat_size >= %s")
        params.append(vat_size_min)

    if team:
        # Filter to opps where at least one VAT SA belongs to the requested team
        clauses.append("""
            EXISTS (
                SELECT 1 FROM dh_vat v
                JOIN targets t ON t.inumber = v.sales_team_member_id
                WHERE v.opportunity_id = p.opportunity_id
                  AND t.area = %s
            )
        """)
        params.append(team)

    where = "WHERE " + " AND ".join(clauses)

    rows = query(f"""
        SELECT opportunity_id, account_name, opp_description, opp_owner_name,
               region_lvl3, solution_area_l1, sub_solution_area_l2, drm_category,
               opp_phase, fc_qualification, closing_qtr, engagement_type,
               deal_qualification_score, deal_qualification_decision,
               vat_dh, vat_full, vat_size, acv_total_opp, age_days, flsm
        FROM (
            SELECT p.opportunity_id, p.account_name, p.opp_description, p.opp_owner_name,
                   p.region_lvl3, p.solution_area_l1, p.sub_solution_area_l2, p.drm_category,
                   p.opp_phase, p.fc_qualification, p.closing_qtr, p.engagement_type,
                   p.deal_qualification_score, p.deal_qualification_decision,
                   p.vat_dh, p.vat_full, p.vat_size, p.acv_total_opp, p.age_days, p.flsm,
                   ROW_NUMBER() OVER (PARTITION BY p.opportunity_id ORDER BY p.acv_total_opp DESC) AS _rn
            FROM pipeline p
            {where}
        ) _vg WHERE _rn = 1
        ORDER BY acv_total_opp DESC
        LIMIT 5000
    """, params or None)

    return [dict(r) for r in rows]


@router.get("/vat-gaps/summary")
def vat_gaps_summary(
    drm_categories: list = Query(default=[]),
    digital_sa: str = Query(default=""),
    team: str = Query(default=""),
    _user=Depends(verify_token),
):
    """Summary stats for VAT gaps — respects the same filters as /vat-gaps."""
    clauses = [
        "p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)",
        "p.vat_dh IS NOT NULL",
        "p.vat_size > 0",
        "p.drm_category NOT IN ('Disc/Lost')",
    ]
    params: list = []

    if drm_categories:
        ph = ",".join(["%s"] * len(drm_categories))
        clauses.append(f"p.drm_category IN ({ph})")
        params.extend(drm_categories)

    if digital_sa:
        clauses.append("p.vat_dh ILIKE %s")
        params.append(f"%{digital_sa}%")

    if team:
        clauses.append("""
            EXISTS (
                SELECT 1 FROM dh_vat v
                JOIN targets t ON t.inumber = v.sales_team_member_id
                WHERE v.opportunity_id = p.opportunity_id
                  AND t.area = %s
            )
        """)
        params.append(team)

    where = "WHERE " + " AND ".join(clauses)

    r = query_one(f"""
        SELECT
            COUNT(DISTINCT p.opportunity_id) AS total_active_vat,
            COUNT(DISTINCT CASE WHEN NOT EXISTS (
                SELECT 1 FROM melody m WHERE m.opp_id = p.opportunity_id
            ) THEN p.opportunity_id END) AS no_melody,
            COUNT(DISTINCT CASE WHEN EXISTS (
                SELECT 1 FROM melody m WHERE m.opp_id = p.opportunity_id
            ) THEN p.opportunity_id END) AS has_melody,
            COALESCE(SUM(CASE WHEN NOT EXISTS (
                SELECT 1 FROM melody m WHERE m.opp_id = p.opportunity_id
            ) THEN p.acv_total_opp END), 0) AS no_melody_acv
        FROM pipeline p
        {where}
    """, params or None)
    return dict(r) if r else {}


@router.get("/vat-role-mismatch")
def vat_role_mismatch(
    drm_categories: list = Query(default=[]),
    digital_sa: str = Query(default=""),
    team: str = Query(default=""),
    _user=Depends(verify_token),
):
    """DH SAs in dh_vat whose sales_team_role is not 'Digital Solution Advisor'."""
    clauses = [
        "(v.sales_team_role IS NULL OR LOWER(v.sales_team_role) != 'digital solution advisor')",
    ]
    params: list = []

    if drm_categories:
        ph = ",".join(["%s"] * len(drm_categories))
        clauses.append(f"p.drm_category IN ({ph})")
        params.extend(drm_categories)

    if digital_sa:
        clauses.append("v.sales_team_member_name ILIKE %s")
        params.append(f"%{digital_sa}%")

    if team:
        clauses.append("t.area = %s")
        params.append(team)

    where = "WHERE " + " AND ".join(clauses)

    rows = query(f"""
        SELECT
            v.sales_team_member_id,
            v.sales_team_member_name,
            v.sales_team_role,
            t.area          AS sa_team,
            t.sub_team,
            v.opportunity_id,
            p.account_name,
            p.opp_owner_name,
            p.solution_area_l1,
            p.closing_qtr,
            p.opp_phase,
            p.drm_category,
            p.engagement_type,
            p.acv_total_opp,
            p.region_lvl3,
            p.flsm
        FROM full_vat v
        JOIN targets t ON t.inumber = v.sales_team_member_id
        JOIN (
            SELECT opportunity_id, account_name, opp_owner_name, solution_area_l1,
                   closing_qtr, opp_phase, drm_category, engagement_type,
                   acv_total_opp, region_lvl3, flsm
            FROM (
                SELECT opportunity_id, account_name, opp_owner_name, solution_area_l1,
                       closing_qtr, opp_phase, drm_category, engagement_type,
                       acv_total_opp, region_lvl3, flsm,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS _rn
                FROM pipeline
                WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND drm_category NOT IN ('Disc/Lost')
            ) _pp WHERE _rn = 1
        ) p ON p.opportunity_id = v.opportunity_id
        {where}
        ORDER BY p.acv_total_opp DESC
        LIMIT 5000
    """, params or None)

    return [dict(r) for r in rows]


@router.get("/vat-role-mismatch/summary")
def vat_role_mismatch_summary(
    drm_categories: list = Query(default=[]),
    digital_sa: str = Query(default=""),
    team: str = Query(default=""),
    _user=Depends(verify_token),
):
    """Counts for role mismatch — total SA-opp pairs, distinct opps, distinct SAs."""
    clauses = [
        "(v.sales_team_role IS NULL OR LOWER(v.sales_team_role) != 'digital solution advisor')",
    ]
    params: list = []

    if drm_categories:
        ph = ",".join(["%s"] * len(drm_categories))
        clauses.append(f"p.drm_category IN ({ph})")
        params.extend(drm_categories)

    if digital_sa:
        clauses.append("v.sales_team_member_name ILIKE %s")
        params.append(f"%{digital_sa}%")

    if team:
        clauses.append("t.area = %s")
        params.append(team)

    where = "WHERE " + " AND ".join(clauses)

    r = query_one(f"""
        SELECT
            COUNT(*)                                AS total_mismatches,
            COUNT(DISTINCT v.opportunity_id)        AS distinct_opps,
            COUNT(DISTINCT v.sales_team_member_id)  AS distinct_sas
        FROM full_vat v
        JOIN targets t ON t.inumber = v.sales_team_member_id
        JOIN (
            SELECT opportunity_id, drm_category
            FROM (
                SELECT opportunity_id, drm_category,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS _rn
                FROM pipeline
                WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND drm_category NOT IN ('Disc/Lost')
            ) _pp WHERE _rn = 1
        ) p ON p.opportunity_id = v.opportunity_id
        {where}
    """, params or None)
    return dict(r) if r else {}


@router.get("/by-opportunity/{opp_id}")
def melody_by_opportunity(opp_id: str, _user=Depends(verify_token)):
    """Timeline of Melody activities for a specific opportunity."""
    rows = query("""
        SELECT
            m.activ_id,
            m.activ_type,
            m.activ_status,
            m.activ_team_empl_name  AS sa_name,
            m.activ_team_member_role AS sa_role,
            m.activ_start_date_utc  AS activity_date,
            m.activ_end_date_utc    AS activity_end,
            m.activ_notes           AS notes,
            m.acct_name             AS account_name
        FROM melody m
        WHERE m.opp_id = %s
        ORDER BY m.activ_start_date_utc DESC
    """, [opp_id])
    result = []
    for r in rows:
        rec = dict(r)
        for f in ("activity_date", "activity_end"):
            if rec.get(f):
                rec[f] = str(rec[f])[:10]  # YYYY-MM-DD
        result.append(rec)
    return result
