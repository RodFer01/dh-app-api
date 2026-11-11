"""Partner Analysis router — pipeline and demo activity breakdown by Channel Partner."""
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from database import query
from auth import verify_token
from routers.pipeline import _build_where, _multi_filter

router = APIRouter(prefix="/api/partner-analysis", tags=["partner-analysis"])


def _common_params():
    """Shared filter param declarations (used in multiple endpoints)."""
    pass  # params declared inline in each endpoint via fastapi Query


# ── /summary ─────────────────────────────────────────────────────────────────
@router.get("/summary")
def partner_summary(
    closing_qtrs:       List[str] = Query(default=[]),
    regions:            List[str] = Query(default=[]),
    region_lvl4s:       List[str] = Query(default=[]),
    sol_l1s:            List[str] = Query(default=[]),
    sol_l2s:            List[str] = Query(default=[]),
    drms:               List[str] = Query(default=[]),
    opp_phases:         List[str] = Query(default=[]),
    qual_decisions:     List[str] = Query(default=[]),
    iss_groups:         List[str] = Query(default=[]),
    iacs:               List[str] = Query(default=[]),
    flsms:              List[str] = Query(default=[]),
    opp_owners:         List[str] = Query(default=[]),
    local_owners:       List[str] = Query(default=[]),
    channel_partners:   List[str] = Query(default=[]),
    acc_sub_segments:   List[str] = Query(default=[]),
    engagement_types:   List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    where, params = _build_where(
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s, drms=drms, opp_phases=opp_phases,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs,
        flsms=flsms, opp_owners=opp_owners, local_owners=local_owners,
        channel_partners=channel_partners, acc_sub_segments=acc_sub_segments,
        engagement_types=engagement_types,
    )

    # Append partner-not-null guard (if no specific channel filter, still skip nulls)
    extra = "channel_partner IS NOT NULL AND channel_partner <> ''"
    if where:
        where = where + f" AND {extra}"
    else:
        where = f"WHERE {extra}"

    sql = f"""
        SELECT
            channel_partner                                                     AS partner,
            COUNT(DISTINCT opportunity_id)                                      AS n_opps,
            COALESCE(SUM(acv_usd), 0)                                           AS total_acv,
            COUNT(DISTINCT CASE WHEN drm_category = 'Committed'  THEN opportunity_id END)  AS n_committed,
            COALESCE(SUM(CASE WHEN drm_category = 'Committed'  THEN acv_usd ELSE 0 END), 0) AS acv_committed,
            COUNT(DISTINCT CASE WHEN drm_category = 'Probable'   THEN opportunity_id END)  AS n_probable,
            COALESCE(SUM(CASE WHEN drm_category = 'Probable'   THEN acv_usd ELSE 0 END), 0) AS acv_probable,
            COUNT(DISTINCT CASE WHEN drm_category = 'Upside'    THEN opportunity_id END)   AS n_upside,
            COALESCE(SUM(CASE WHEN drm_category = 'Upside'    THEN acv_usd ELSE 0 END), 0) AS acv_upside,
            COUNT(DISTINCT CASE WHEN drm_category = 'Pipeline'  THEN opportunity_id END)   AS n_pipeline,
            COALESCE(SUM(CASE WHEN drm_category = 'Pipeline'  THEN acv_usd ELSE 0 END), 0) AS acv_pipeline,
            COUNT(DISTINCT CASE WHEN drm_category = 'Booked/Won' THEN opportunity_id END)  AS n_booked,
            COALESCE(SUM(CASE WHEN drm_category = 'Booked/Won' THEN acv_usd ELSE 0 END), 0) AS acv_booked
        FROM pipeline
        {where}
        GROUP BY channel_partner
        ORDER BY total_acv DESC
    """
    return query(sql, params or None)


# ── /by-solution ─────────────────────────────────────────────────────────────
@router.get("/by-solution")
def partner_by_solution(
    closing_qtrs:     List[str] = Query(default=[]),
    regions:          List[str] = Query(default=[]),
    sol_l1s:          List[str] = Query(default=[]),
    drms:             List[str] = Query(default=[]),
    opp_phases:       List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    iss_groups:       List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    where, params = _build_where(
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s,
        drms=drms, opp_phases=opp_phases, channel_partners=channel_partners,
        iss_groups=iss_groups,
    )

    extra = "channel_partner IS NOT NULL AND channel_partner <> '' AND solution_area_l1 IS NOT NULL"
    if where:
        where = where + f" AND {extra}"
    else:
        where = f"WHERE {extra}"

    sql = f"""
        SELECT
            channel_partner                AS partner,
            solution_area_l1               AS sol_l1,
            COUNT(DISTINCT opportunity_id) AS n_opps,
            COALESCE(SUM(acv_usd), 0)      AS total_acv
        FROM pipeline
        {where}
        GROUP BY channel_partner, solution_area_l1
        ORDER BY total_acv DESC
    """
    return query(sql, params or None)


# ── /demo-activity ────────────────────────────────────────────────────────────
@router.get("/demo-activity")
def partner_demo_activity(
    date_from:        Optional[str] = None,
    date_to:          Optional[str] = None,
    channel_partners: List[str]     = Query(default=[]),
    sol_l1s:          List[str]     = Query(default=[]),
    sol_l2s:          List[str]     = Query(default=[]),
    closing_qtrs:     List[str]     = Query(default=[]),
    regions:          List[str]     = Query(default=[]),
    region_lvl4s:     List[str]     = Query(default=[]),
    drms:             List[str]     = Query(default=[]),
    opp_phases:       List[str]     = Query(default=[]),
    qual_decisions:   List[str]     = Query(default=[]),
    iss_groups:       List[str]     = Query(default=[]),
    iacs:             List[str]     = Query(default=[]),
    flsms:            List[str]     = Query(default=[]),
    opp_owners:       List[str]     = Query(default=[]),
    local_owners:     List[str]     = Query(default=[]),
    acc_sub_segments: List[str]     = Query(default=[]),
    engagement_types: List[str]     = Query(default=[]),
    engagement_type:  Optional[str] = None,
    opp_main_sol_l2:  Optional[str] = None,
    _user=Depends(verify_token),
):
    """
    Join melody activities with the latest pipeline snapshot to get the channel_partner
    for each opp, then join melody.activ_team_empl_id → targets.inumber → targets.sub_team
    to identify which LoB (CX, SCM, HCM, etc.) each SA belongs to.
    Returns: partner × sa_lob breakdown of demo/activity count.
    """
    mel_clauses, mel_params = [], []
    if date_from:
        mel_clauses.append("m.activ_start_date_utc >= %s"); mel_params.append(date_from)
    if date_to:
        mel_clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); mel_params.append(date_to)
    if opp_main_sol_l2:
        mel_clauses.append("m.opp_main_solution_l2 = %s"); mel_params.append(opp_main_sol_l2)

    # Build pipeline subquery WHERE using shared _build_where (includes snapshot filter)
    pip_where, pip_params = _build_where(
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s, drms=drms, opp_phases=opp_phases,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs,
        flsms=flsms, opp_owners=opp_owners, local_owners=local_owners,
        channel_partners=channel_partners, acc_sub_segments=acc_sub_segments,
        engagement_types=engagement_types or ([] if not engagement_type else [engagement_type]),
    )
    # Always require a channel partner in this view
    pip_where = pip_where + " AND channel_partner IS NOT NULL AND channel_partner <> ''"

    mel_where = ("WHERE " + " AND ".join(mel_clauses)) if mel_clauses else ""
    # pip params appear first in the SQL (inside the subquery), then mel params (outer WHERE)
    all_params = pip_params + mel_params

    sql = f"""
        SELECT
            p.channel_partner                                       AS partner,
            COALESCE(m.opp_main_solution_l2, 'Not Specified')      AS opp_main_sol_l2,
            COUNT(m.activ_id)                                       AS n_activities,
            COUNT(DISTINCT m.opp_id)                               AS n_distinct_opps,
            COUNT(DISTINCT m.activ_team_empl_id)                   AS n_distinct_sas
        FROM melody m
        JOIN (
            SELECT DISTINCT opportunity_id, channel_partner
            FROM pipeline
            {pip_where}
        ) p ON p.opportunity_id = m.opp_id
        LEFT JOIN targets t ON t.inumber = m.activ_team_empl_id
        {mel_where}
        GROUP BY p.channel_partner, COALESCE(m.opp_main_solution_l2, 'Not Specified')
        ORDER BY n_activities DESC
    """
    return query(sql, all_params or None)


# ── /demo-activity-l3 ─────────────────────────────────────────────────────────
@router.get("/demo-activity-l3")
def partner_demo_activity_l3(
    date_from:        Optional[str] = None,
    date_to:          Optional[str] = None,
    channel_partners: List[str]     = Query(default=[]),
    sol_l1s:          List[str]     = Query(default=[]),
    sol_l2s:          List[str]     = Query(default=[]),
    closing_qtrs:     List[str]     = Query(default=[]),
    regions:          List[str]     = Query(default=[]),
    region_lvl4s:     List[str]     = Query(default=[]),
    drms:             List[str]     = Query(default=[]),
    opp_phases:       List[str]     = Query(default=[]),
    qual_decisions:   List[str]     = Query(default=[]),
    iss_groups:       List[str]     = Query(default=[]),
    iacs:             List[str]     = Query(default=[]),
    flsms:            List[str]     = Query(default=[]),
    opp_owners:       List[str]     = Query(default=[]),
    local_owners:     List[str]     = Query(default=[]),
    acc_sub_segments: List[str]     = Query(default=[]),
    engagement_types: List[str]     = Query(default=[]),
    _user=Depends(verify_token),
):
    mel_clauses, mel_params = [], []
    if date_from:
        mel_clauses.append("m.activ_start_date_utc >= %s"); mel_params.append(date_from)
    if date_to:
        mel_clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); mel_params.append(date_to)

    pip_where, pip_params = _build_where(
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s, drms=drms, opp_phases=opp_phases,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs,
        flsms=flsms, opp_owners=opp_owners, local_owners=local_owners,
        channel_partners=channel_partners, acc_sub_segments=acc_sub_segments,
        engagement_types=engagement_types,
    )
    pip_where = pip_where + " AND channel_partner IS NOT NULL AND channel_partner <> ''"

    mel_where = ("WHERE " + " AND ".join(mel_clauses)) if mel_clauses else ""
    all_params = pip_params + mel_params

    sql = f"""
        SELECT
            p.channel_partner                                       AS partner,
            COALESCE(m.opp_main_solution_l3, 'Not Specified')      AS opp_main_sol_l3,
            COUNT(m.activ_id)                                       AS n_activities,
            COUNT(DISTINCT m.opp_id)                               AS n_distinct_opps,
            COUNT(DISTINCT m.activ_team_empl_id)                   AS n_distinct_sas
        FROM melody m
        JOIN (
            SELECT DISTINCT opportunity_id, channel_partner
            FROM pipeline
            {pip_where}
        ) p ON p.opportunity_id = m.opp_id
        {mel_where}
        GROUP BY p.channel_partner, COALESCE(m.opp_main_solution_l3, 'Not Specified')
        ORDER BY n_activities DESC
    """
    return query(sql, all_params or None)


# ── /demo-activity-l4 ─────────────────────────────────────────────────────────
@router.get("/demo-activity-l4")
def partner_demo_activity_l4(
    date_from:        Optional[str] = None,
    date_to:          Optional[str] = None,
    channel_partners: List[str]     = Query(default=[]),
    sol_l1s:          List[str]     = Query(default=[]),
    sol_l2s:          List[str]     = Query(default=[]),
    closing_qtrs:     List[str]     = Query(default=[]),
    regions:          List[str]     = Query(default=[]),
    region_lvl4s:     List[str]     = Query(default=[]),
    drms:             List[str]     = Query(default=[]),
    opp_phases:       List[str]     = Query(default=[]),
    qual_decisions:   List[str]     = Query(default=[]),
    iss_groups:       List[str]     = Query(default=[]),
    iacs:             List[str]     = Query(default=[]),
    flsms:            List[str]     = Query(default=[]),
    opp_owners:       List[str]     = Query(default=[]),
    local_owners:     List[str]     = Query(default=[]),
    acc_sub_segments: List[str]     = Query(default=[]),
    engagement_types: List[str]     = Query(default=[]),
    _user=Depends(verify_token),
):
    mel_clauses, mel_params = [], []
    if date_from:
        mel_clauses.append("m.activ_start_date_utc >= %s"); mel_params.append(date_from)
    if date_to:
        mel_clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); mel_params.append(date_to)

    pip_where, pip_params = _build_where(
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s, drms=drms, opp_phases=opp_phases,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs,
        flsms=flsms, opp_owners=opp_owners, local_owners=local_owners,
        channel_partners=channel_partners, acc_sub_segments=acc_sub_segments,
        engagement_types=engagement_types,
    )
    pip_where = pip_where + " AND channel_partner IS NOT NULL AND channel_partner <> ''"

    mel_where = ("WHERE " + " AND ".join(mel_clauses)) if mel_clauses else ""
    all_params = pip_params + mel_params

    sql = f"""
        SELECT
            p.channel_partner                                       AS partner,
            COALESCE(m.opp_main_solution_l4, 'Not Specified')      AS opp_main_sol_l4,
            COUNT(m.activ_id)                                       AS n_activities,
            COUNT(DISTINCT m.opp_id)                               AS n_distinct_opps,
            COUNT(DISTINCT m.activ_team_empl_id)                   AS n_distinct_sas
        FROM melody m
        JOIN (
            SELECT DISTINCT opportunity_id, channel_partner
            FROM pipeline
            {pip_where}
        ) p ON p.opportunity_id = m.opp_id
        {mel_where}
        GROUP BY p.channel_partner, COALESCE(m.opp_main_solution_l4, 'Not Specified')
        ORDER BY n_activities DESC
    """
    return query(sql, all_params or None)


# ── /demo-by-partner ─────────────────────────────────────────────────────────
@router.get("/demo-by-partner")
def demo_by_partner(
    partner:          Optional[str] = None,
    date_from:        Optional[str] = None,
    date_to:          Optional[str] = None,
    _user=Depends(verify_token),
):
    """Detail view: for a given partner, list all melody activities with SA LoB info."""
    clauses, params = [], []
    clauses.append("p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)")
    if partner:
        clauses.append("p.channel_partner = %s"); params.append(partner)
    else:
        clauses.append("p.channel_partner IS NOT NULL AND p.channel_partner <> ''")
    if date_from:
        clauses.append("m.activ_start_date_utc >= %s"); params.append(date_from)
    if date_to:
        clauses.append("m.activ_start_date_utc < ADD_DAYS(CAST(%s AS DATE), 1)"); params.append(date_to)

    where = "WHERE " + " AND ".join(clauses)

    sql = f"""
        SELECT TOP 500
            m.activ_id                                          AS activity_id,
            m.activ_type                                        AS activity_type,
            m.activ_status                                      AS status,
            m.acct_name                                         AS account_name,
            p.channel_partner                                   AS partner,
            m.activ_team_empl_name                             AS sa_name,
            COALESCE(m.opp_main_solution_l2, 'Not Specified')  AS opp_main_sol_l2,
            m.activ_solution_l2                                AS solution_l2,
            m.activ_solution_l3                                AS solution_l3,
            m.activ_start_date_utc                             AS activity_date,
            p.solution_area_l1,
            p.drm_category,
            p.closing_qtr,
            p.acv_total_opp
        FROM melody m
        JOIN (
            SELECT opportunity_id, channel_partner, solution_area_l1, drm_category,
                   closing_qtr, acv_total_opp, snapshot_week,
                   ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY snapshot_week DESC) AS _rn
            FROM pipeline
        ) p ON p.opportunity_id = m.opp_id AND p._rn = 1
        {where}
        ORDER BY m.activ_start_date_utc DESC
    """
    return query(sql, params or None)
