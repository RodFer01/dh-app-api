"""Pipeline router — all endpoints that query pipeline / pipeline_last_week."""
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from database import query, query_one
from auth import verify_token

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _multi_filter(field: str, values: list, clauses: list, params: list):
    """Add an IN clause for multi-value filter, or = for single value."""
    if not values:
        return
    vals = [v for v in values if v]
    if not vals:
        return
    if len(vals) == 1:
        clauses.append(f"{field} = %s"); params.append(vals[0])
    else:
        ph = ",".join(["%s"] * len(vals))
        clauses.append(f"{field} IN ({ph})"); params.extend(vals)


def _build_where(
    closing_qtr=None, region=None, region_lvl4=None,
    sol_l1=None, sol_l2=None,
    drm=None, opp_phase=None, fc=None, engagement_type=None,
    opp_owner=None, digital_sa=None, search=None,
    qual_decision=None, iss_group=None, iac=None, flsm=None,
    local_owner=None, acc_sub_segment=None, sde_owner=None,
    snapshot_week=None, table="pipeline",
    # Multi-value versions (lists)
    closing_qtrs=None, regions=None, sol_l1s=None, sol_l2s=None,
    drms=None, opp_phases=None, fcs=None, opp_owners=None,
    qual_decisions=None, iss_groups=None, iacs=None, flsms=None,
    local_owners=None, acc_sub_segments=None,
    engagement_types=None,
    digital_sas=None,
    channel_partners=None,
    presales_statuses=None,  # multi-value: ['Red','Yellow','Green']
) -> tuple[str, list]:
    clauses, params = [], []

    if table == "pipeline":
        if snapshot_week:
            clauses.append("snapshot_week = %s"); params.append(snapshot_week)
        else:
            clauses.append("snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)")
    elif table == "pipeline_2025":
        clauses.append("snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_2025)")

    # Multi-value filters take priority over single-value
    _multi_filter("closing_qtr",                    closing_qtrs or ([closing_qtr] if closing_qtr else []), clauses, params)
    _multi_filter("region_lvl3",                    regions or ([region] if region else []), clauses, params)
    _multi_filter("region_lvl4",                    [] if not region_lvl4 else [region_lvl4], clauses, params)
    _multi_filter("solution_area_l1",               sol_l1s or ([sol_l1] if sol_l1 else []), clauses, params)
    _multi_filter("sub_solution_area_l2",           sol_l2s or ([sol_l2] if sol_l2 else []), clauses, params)
    _multi_filter("drm_category",                   drms or ([drm] if drm else []), clauses, params)
    _multi_filter("opp_phase",                      opp_phases or ([opp_phase] if opp_phase else []), clauses, params)
    _multi_filter("fc_qualification",               fcs or ([fc] if fc else []), clauses, params)
    _multi_filter("engagement_type", engagement_types or ([] if not engagement_type else [engagement_type]), clauses, params)
    _multi_filter("opp_owner_name",                 opp_owners or ([opp_owner] if opp_owner else []), clauses, params)
    _multi_filter("deal_qualification_decision",    qual_decisions or ([qual_decision] if qual_decision else []), clauses, params)
    _multi_filter("iss_group_text",                 iss_groups or ([iss_group] if iss_group else []), clauses, params)
    _multi_filter("iac",                            iacs or ([iac] if iac else []), clauses, params)
    _multi_filter("flsm",                           flsms or ([flsm] if flsm else []), clauses, params)
    _multi_filter("local_acct_owner",               local_owners or ([local_owner] if local_owner else []), clauses, params)
    _multi_filter("account_planned_archetype_text", acc_sub_segments or ([acc_sub_segment] if acc_sub_segment else []), clauses, params)
    _multi_filter("sde_current_territory_owner",    [] if not sde_owner else [sde_owner], clauses, params)
    _multi_filter("channel_partner",                channel_partners or [], clauses, params)

    if presales_statuses:
        vals = [v for v in presales_statuses if v]
        if vals:
            has_na = 'N/A' in vals
            real_vals = [v for v in vals if v != 'N/A']
            if has_na and real_vals:
                ph = ",".join(["%s"] * len(real_vals))
                clauses.append(
                    f"EXISTS (SELECT 1 FROM presales ps WHERE ps.opp_id = LPAD(opportunity_id, 10, '0') "
                    f"AND ps.presales_lead_status IN ({ph})) "
                    f"OR NOT EXISTS (SELECT 1 FROM presales ps WHERE ps.opp_id = LPAD(opportunity_id, 10, '0') "
                    f"AND ps.presales_lead_status IS NOT NULL)"
                )
                params.extend(real_vals)
            elif has_na:
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM presales ps WHERE ps.opp_id = LPAD(opportunity_id, 10, '0') "
                    "AND ps.presales_lead_status IS NOT NULL)"
                )
            else:
                ph = ",".join(["%s"] * len(real_vals))
                clauses.append(
                    f"EXISTS (SELECT 1 FROM presales ps WHERE ps.opp_id = LPAD(opportunity_id, 10, '0') "
                    f"AND ps.presales_lead_status IN ({ph}))"
                )
                params.extend(real_vals)

    if digital_sa:
        clauses.append("LOWER(vat_dh) LIKE %s"); params.append(f"%{digital_sa.lower()}%")
    if digital_sas:
        vals = [v for v in digital_sas if v]
        if len(vals) == 1:
            clauses.append("LOWER(vat_dh) LIKE %s"); params.append(f"%{vals[0].lower()}%")
        elif len(vals) > 1:
            or_clauses = " OR ".join(["LOWER(vat_dh) LIKE %s"] * len(vals))
            clauses.append(f"({or_clauses})"); params.extend([f"%{v.lower()}%" for v in vals])
    if search:
        clauses.append("(LOWER(opportunity_id) LIKE %s OR LOWER(account_name) LIKE %s OR LOWER(opp_owner_name) LIKE %s OR LOWER(opp_description) LIKE %s)")
        like = f"%{search.lower()}%"
        params.extend([like, like, like, like])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _apply_deal_type(where: str, deal_type: Optional[str]) -> str:
    """Add basket/single bag subquery filter to an existing WHERE clause."""
    if not deal_type or deal_type not in ('basket', 'single'):
        return where
    op = "> 1" if deal_type == 'basket' else "<= 1"
    subq = f"""opportunity_id IN (
        SELECT opportunity_id FROM (
            SELECT opportunity_id, COUNT(DISTINCT solution_area_l1) AS n_l1
            FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND solution_area_l1 NOT IN ('BAIP - Technology Foundation')
              AND solution_area_l1 IS NOT NULL
            GROUP BY opportunity_id
        ) _dt WHERE n_l1 {op}
    )"""
    return (where + " AND " + subq) if where else ("WHERE " + subq)


@router.get("/kpis")
def pipeline_kpis(
    snapshot_week: Optional[str] = None,
    closing_qtr: Optional[str] = None,
    region: Optional[str] = None,
    sol_l1: Optional[str] = None,
    sol_l2: Optional[str] = None,
    drm: Optional[str] = None,
    opp_phase: Optional[str] = None,
    fc: Optional[str] = None,
    engagement_type: Optional[str] = None,
    opp_owner: Optional[str] = None,
    digital_sa: Optional[str] = None,
    search: Optional[str] = None,
    # Multi-select — full set matching usePipelineFilters
    closing_qtrs: List[str] = Query(default=[]),
    regions: List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    sol_l1s: List[str] = Query(default=[]),
    sol_l2s: List[str] = Query(default=[]),
    drms: List[str] = Query(default=[]),
    opp_phases: List[str] = Query(default=[]),
    fcs: List[str] = Query(default=[]),
    opp_owners: List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    local_owners: List[str] = Query(default=[]),
    flsms: List[str] = Query(default=[]),
    iss_groups: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    exclude_neg: bool = False,
    include_purchase: bool = False,
    deal_type: Optional[str] = None,
    _user=Depends(verify_token),
):
    """Server-side KPI aggregation — handles all filtered rows, not capped by limit."""
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, sol_l2=sol_l2,
        drm=drm, opp_phase=opp_phase, fc=fc, engagement_type=engagement_type,
        opp_owner=opp_owner, digital_sa=digital_sa, search=search,
        snapshot_week=snapshot_week,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4=None,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs,
        opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas, local_owners=local_owners, flsms=flsms,
        iss_groups=iss_groups, acc_sub_segments=acc_sub_segments,
        qual_decisions=qual_decisions,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
    )
    where = _apply_deal_type(where, deal_type)
    neg_filter = "AND acv_usd > 0" if exclude_neg else ""
    # When include_purchase=True, treat opp_phase='A - Purchase' as Booked/Won
    purchase_expr = (
        "CASE WHEN opp_phase = 'A - Purchase' THEN 'Booked/Won' ELSE drm_category END"
        if include_purchase else "drm_category"
    )
    sql = f"""
        WITH lines AS (
            SELECT opportunity_id,
                   {purchase_expr}  AS drm_category,
                   fc_qualification, engagement_type, acv_usd, age_days
            FROM pipeline
            {where}
            {neg_filter}
        ),
        deduped_opps AS (
            SELECT DISTINCT ON (opportunity_id)
                opportunity_id, drm_category, fc_qualification, engagement_type, age_days
            FROM lines
            ORDER BY opportunity_id, acv_usd DESC
        )
        SELECT
            COUNT(DISTINCT d.opportunity_id)                                       AS n_total,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Booked/Won')  AS n_booked,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Committed'
                              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost',
                                                        'Excluded from Pipeline'))  AS n_committed,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Probable'
                              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost',
                                                        'Excluded from Pipeline'))  AS n_probable,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Upside'
                              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost',
                                                        'Excluded from Pipeline'))  AS n_upside,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Excluded from Pipeline')  AS n_excluded,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Disc/Lost')               AS n_lost,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Booked/Won'), 0)  AS acv_booked,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Committed'
                              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0)  AS acv_committed,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Probable'
                              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0)  AS acv_probable,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Upside'
                              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0)  AS acv_upside,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Excluded from Pipeline'), 0)  AS acv_excluded,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Disc/Lost'), 0)               AS acv_lost,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.engagement_type = 'Digital')          AS n_digital,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.age_days > 90
                              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost',
                                                        'Excluded from Pipeline'))  AS n_untouched
        FROM deduped_opps d
        JOIN lines l ON l.opportunity_id = d.opportunity_id
    """
    return query_one(sql, params or None)


@router.get("/summary")
def pipeline_summary(_user=Depends(verify_token)):
    sql = """
        WITH latest AS (
            SELECT * FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
        ),
        deduped AS (
            SELECT opportunity_id, acv_total_opp, drm_category, engagement_type, snapshot_week
            FROM (
                SELECT opportunity_id, acv_total_opp, drm_category, engagement_type, snapshot_week,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS _rn
                FROM latest
            ) _d WHERE _rn = 1
        )
        SELECT
            COUNT(*)                                                         AS n_opps,
            COALESCE(SUM(CASE WHEN drm_category NOT IN ('Booked/Won','Disc/Lost')
                         THEN acv_total_opp END), 0)                        AS acv_active,
            COALESCE(SUM(CASE WHEN drm_category = 'Booked/Won'
                         THEN acv_total_opp END), 0)                        AS acv_booked,
            COUNT(CASE WHEN engagement_type = 'Digital' THEN 1 END)         AS n_digital,
            TO_NVARCHAR(MAX(snapshot_week), 'YYYY-MM-DD')                   AS snapshot_week
        FROM deduped
    """
    return query_one(sql)


class KpisByIdsPayload(BaseModel):
    opp_ids: list
    exclude_neg: bool = False
    include_purchase: bool = False


@router.post("/kpis-by-ids")
def pipeline_kpis_by_ids(payload: KpisByIdsPayload, _user=Depends(verify_token)):
    """KPI aggregation for a specific set of opportunity IDs (used by Melody page)."""
    if not payload.opp_ids:
        return {k: 0 for k in ('n_total','n_booked','n_committed','n_probable','n_upside',
                                'n_excluded','n_lost','acv_booked','acv_committed',
                                'acv_probable','acv_upside','acv_excluded','acv_lost',
                                'n_digital','n_untouched')}
    ph = ",".join(["%s"] * len(payload.opp_ids))
    where = f"WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline) AND opportunity_id IN ({ph})"
    neg_filter = "AND acv_usd > 0" if payload.exclude_neg else ""
    purchase_expr = (
        "CASE WHEN opp_phase = 'A - Purchase' THEN 'Booked/Won' ELSE drm_category END"
        if payload.include_purchase else "drm_category"
    )
    sql = f"""
        WITH lines AS (
            SELECT opportunity_id,
                   {purchase_expr}  AS drm_category,
                   fc_qualification, engagement_type, acv_usd, age_days
            FROM pipeline {where} {neg_filter}
        ),
        deduped_opps AS (
            SELECT DISTINCT ON (opportunity_id)
                opportunity_id, drm_category, fc_qualification, engagement_type, age_days
            FROM lines ORDER BY opportunity_id, acv_usd DESC
        )
        SELECT
            COUNT(DISTINCT d.opportunity_id) AS n_total,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Booked/Won') AS n_booked,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Committed'
              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')) AS n_committed,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Probable'
              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')) AS n_probable,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.fc_qualification = 'Upside'
              AND d.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')) AS n_upside,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Excluded from Pipeline') AS n_excluded,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.drm_category = 'Disc/Lost') AS n_lost,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Booked/Won'), 0) AS acv_booked,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Committed'
              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0) AS acv_committed,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Probable'
              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0) AS acv_probable,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.fc_qualification = 'Upside'
              AND l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0) AS acv_upside,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Excluded from Pipeline'), 0) AS acv_excluded,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category = 'Disc/Lost'), 0) AS acv_lost,
            COUNT(DISTINCT d.opportunity_id) FILTER (WHERE d.engagement_type = 'Digital') AS n_digital,
            0 AS n_untouched
        FROM deduped_opps d JOIN lines l ON l.opportunity_id = d.opportunity_id
    """
    return query_one(sql, list(payload.opp_ids))



@router.get("/snapshots")
def pipeline_snapshots(_user=Depends(verify_token)):
    return query("SELECT TO_NVARCHAR(snapshot_week, 'YYYY-MM-DD') AS snapshot_week, pipeline_rows FROM weekly_snapshots ORDER BY snapshot_week DESC")


@router.get("/filters")
def pipeline_filter_options(_user=Depends(verify_token)):
    """Return distinct values for all filter dropdowns — HANA-compatible separate queries."""
    SNAP = "(SELECT MAX(snapshot_week) FROM pipeline)"
    HEADER_ARTIFACTS = {
        'Closing Qtr', 'Region Lvl 3', 'Region Lvl 4', 'Solution Area (L1)',
        'Sub-Solution Area (L2)', 'DRM Category', 'Opp Phase', 'FC Qualification',
        'Opp Owner Name', 'IAC (Engagement Model)', 'Channel Partner / VAR',
        'FLSM', 'Local Acct Owner', 'Deal Qualification Decision',
        'Account Planned Archetype', 'SDE Current Territory Owner',
        'ISS Group Text', 'Engagement Type',
    }

    def _col(col, alias=None):
        a = alias or col
        rows = query(f"SELECT DISTINCT {col} AS v FROM pipeline WHERE snapshot_week = {SNAP} AND {col} IS NOT NULL ORDER BY {col}")
        return [r["v"] for r in rows if r["v"] is not None and r["v"] not in HEADER_ARTIFACTS]

    return {
        "closing_qtrs":      _col("closing_qtr"),
        "regions":           _col("region_lvl3"),
        "region_lvl4s":      _col("region_lvl4"),
        "sol_l1s":           _col("solution_area_l1"),
        "sol_l2s":           _col("sub_solution_area_l2"),
        "drm_cats":          _col("drm_category"),
        "opp_phases":        _col("opp_phase"),
        "fcs":               _col("fc_qualification"),
        "engagement_types":  _col("engagement_type"),
        "opp_owners":        _col("opp_owner_name"),
        "iss_groups":        _col("iss_group_text"),
        "iacs":              _col("iac"),
        "channel_partners":  _col("channel_partner"),
        "flsms":             _col("flsm"),
        "local_owners":      _col("local_acct_owner"),
        "qual_decisions":    _col("deal_qualification_decision"),
        "acc_sub_segments":  _col("account_planned_archetype_text"),
        "sde_owners":        _col("sde_current_territory_owner"),
        "digital_sas":       _col("vat_dh"),
    }


@router.get("/filters/l2-by-l1")
def l2_by_l1(
    sol_l1s: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Return distinct sub_solution_area_l2 values filtered by the given L1 values."""
    if not sol_l1s:
        return {"sol_l2s": []}
    ph = ",".join(["%s"] * len(sol_l1s))
    rows = query(
        f"""SELECT DISTINCT sub_solution_area_l2
            FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND solution_area_l1 IN ({ph})
              AND sub_solution_area_l2 IS NOT NULL
            ORDER BY sub_solution_area_l2""",
        sol_l1s,
    )
    return {"sol_l2s": [r["sub_solution_area_l2"] for r in rows if r["sub_solution_area_l2"]]}


@router.get("/dda-filters")
def dda_filter_options(_user=Depends(verify_token)):
    """Distinct DDA Status and Range Aging values — values may be CSV per row."""
    dda_rows = query("SELECT dda_status FROM dda WHERE dda_status IS NOT NULL")
    range_rows = query("SELECT range_aging FROM dda WHERE range_aging IS NOT NULL")

    def _distinct_vals(rows, key):
        seen = set()
        for r in rows:
            raw = r.get(key) or ""
            for v in str(raw).split(","):
                v = v.strip()
                if v:
                    seen.add(v)
        return sorted(seen)

    return {
        "dda_statuses": _distinct_vals(dda_rows, "dda_status"),
        "range_agings":  _distinct_vals(range_rows, "range_aging"),
    }


@router.get("/dda-charts")
def dda_charts(
    snapshot_week: Optional[str] = None,
    closing_qtr: Optional[str] = None,
    region: Optional[str] = None,
    sol_l1: Optional[str] = None,
    drm: Optional[str] = None,
    engagement_type: Optional[str] = None,
    opp_owner: Optional[str] = None,
    digital_sa: Optional[str] = None,
    flsm: Optional[str] = None,
    closing_qtrs: List[str] = Query(default=[]),
    regions: List[str] = Query(default=[]),
    sol_l1s: List[str] = Query(default=[]),
    sol_l2s: List[str] = Query(default=[]),
    drms: List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    opp_owners: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    flsms: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    iss_groups: List[str] = Query(default=[]),
    dda_statuses: List[str] = Query(default=[]),
    range_agings: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Aggregations for DDA Status and Range Aging charts — uses full pipeline filter set."""
    where, params = _build_where(
        snapshot_week=snapshot_week,
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, drm=drm,
        engagement_type=engagement_type, opp_owner=opp_owner, digital_sa=digital_sa, flsm=flsm,
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, engagement_types=engagement_types, opp_owners=opp_owners,
        digital_sas=digital_sas, flsms=flsms, channel_partners=channel_partners,
        qual_decisions=qual_decisions, iss_groups=iss_groups,
    )
    # Replace "WHERE" prefix — we need "p." aliased version
    p_where = where.replace("WHERE ", "WHERE p.").replace(" AND ", " AND p.") if where else ""
    # Fix presales subquery alias that _build_where may add
    p_where = p_where.replace("p.EXISTS", "EXISTS").replace("p.NOT EXISTS", "NOT EXISTS")

    dda_clauses: list = []
    dda_params: list = []
    if dda_statuses:
        ph = ",".join(["%s"] * len(dda_statuses))
        dda_clauses.append(f"d.dda_status IN ({ph})"); dda_params.extend(dda_statuses)
    if range_agings:
        ph = ",".join(["%s"] * len(range_agings))
        dda_clauses.append(f"d.range_aging IN ({ph})"); dda_params.extend(range_agings)

    dda_extra = (" AND " + " AND ".join(dda_clauses)) if dda_clauses else ""
    all_params = params + dda_params

    # If no pipeline filters at all, add the snapshot clause
    if not p_where:
        p_where = "WHERE p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)"
    elif "snapshot_week" not in p_where:
        p_where += " AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)"

    by_dda_status = query(f"""
        SELECT d.dda_status AS label,
               COUNT(DISTINCT p.opportunity_id) AS n_opps
        FROM pipeline p
        JOIN dda d ON d.opp_id = p.opportunity_id
        {p_where}
          AND d.dda_status IS NOT NULL
          {dda_extra}
        GROUP BY d.dda_status ORDER BY n_opps DESC
    """, all_params or None)

    by_range_aging = query(f"""
        SELECT d.range_aging AS label,
               COUNT(DISTINCT p.opportunity_id) AS n_opps
        FROM pipeline p
        JOIN dda d ON d.opp_id = p.opportunity_id
        {p_where}
          AND d.range_aging IS NOT NULL
          {dda_extra}
        GROUP BY d.range_aging ORDER BY n_opps DESC
    """, all_params or None)

    return {
        "by_dda_status":  by_dda_status,
        "by_range_aging": by_range_aging,
    }


@router.get("")
def get_pipeline(
    snapshot_week: Optional[str] = None,
    # Single-value filters (backward compat)
    closing_qtr: Optional[str] = None,
    region: Optional[str] = None,
    sol_l1: Optional[str] = None,
    sol_l2: Optional[str] = None,
    drm: Optional[str] = None,
    opp_phase: Optional[str] = None,
    fc: Optional[str] = None,
    engagement_type: Optional[str] = None,
    opp_owner: Optional[str] = None,
    digital_sa: Optional[str] = None,
    search: Optional[str] = None,
    qual_decision: Optional[str] = None,
    iss_group: Optional[str] = None,
    iac: Optional[str] = None,
    flsm: Optional[str] = None,
    local_owner: Optional[str] = None,
    acc_sub_segment: Optional[str] = None,
    sde_owner: Optional[str] = None,
    region_lvl4: Optional[str] = None,
    # Multi-value filters
    closing_qtrs: List[str] = Query(default=[]),
    regions: List[str] = Query(default=[]),
    sol_l1s: List[str] = Query(default=[]),
    sol_l2s: List[str] = Query(default=[]),
    drms: List[str] = Query(default=[]),
    opp_phases: List[str] = Query(default=[]),
    fcs: List[str] = Query(default=[]),
    opp_owners: List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    iss_groups: List[str] = Query(default=[]),
    iacs: List[str] = Query(default=[]),
    flsms: List[str] = Query(default=[]),
    local_owners: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    dda_statuses: List[str] = Query(default=[]),
    range_agings: List[str] = Query(default=[]),
    deal_type: Optional[str] = None,   # 'single' | 'basket' | None
    deduplicate: bool = True,
    limit: int = Query(default=5000, le=20000),
    _user=Depends(verify_token),
):
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, region_lvl4=region_lvl4,
        sol_l1=sol_l1, sol_l2=sol_l2,
        drm=drm, opp_phase=opp_phase, fc=fc, engagement_type=engagement_type,
        opp_owner=opp_owner, digital_sa=digital_sa, search=search,
        qual_decision=qual_decision, iss_group=iss_group, iac=iac, flsm=flsm,
        local_owner=local_owner, acc_sub_segment=acc_sub_segment, sde_owner=sde_owner,
        snapshot_week=snapshot_week,
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs, opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs, flsms=flsms,
        local_owners=local_owners, acc_sub_segments=acc_sub_segments,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
    )
    # Add DDA filters via subquery
    if dda_statuses:
        ph = ",".join(["%s"] * len(dda_statuses))
        where = (where + " AND " if where else "WHERE ") + f"opportunity_id IN (SELECT opp_id FROM dda WHERE dda_status IN ({ph}))"
        params.extend(dda_statuses)
    if range_agings:
        ph = ",".join(["%s"] * len(range_agings))
        where = (where + " AND " if where else "WHERE ") + f"opportunity_id IN (SELECT opp_id FROM dda WHERE range_aging IN ({ph}))"
        params.extend(range_agings)
    where = _apply_deal_type(where, deal_type)
    if deduplicate:
        # Pivot ACV by Solution L1 in a single pass alongside the deduplicated row
        sql = f"""
            WITH base AS (
                SELECT DISTINCT ON (opportunity_id)
                    opportunity_id, account_name, opp_description, opp_owner_name,
                    region_lvl3, region_lvl4, solution_area_l1, sub_solution_area_l2,
                    opp_phase, fc_qualification, closing_qtr, closing_date,
                    drm_category, iss_group_text, deal_qualification_decision,
                    deal_qualification_score, local_acct_owner, flsm, iac,
                    engagement_type, vat_dh, vat_full, vat_size,
                    acv_total_opp, acv_per_vat, acv_total_opp AS acv_usd,
                    channel_partner, channel_manager, opp_origin, age_days, untouched_days,
                    account_planned_archetype_text,
                    sde_current_territory_owner,
                    sde_initial_engagement_date,
                    sde_primary_engagement_name,
                    sde_handover_name,
                    sic_code,
                    sic_description,
                    sap_mastercode,
                    account_bp,
                    TO_NVARCHAR(snapshot_week, 'YYYY-MM-DD') AS snapshot_week
                FROM pipeline {where}
                ORDER BY opportunity_id, acv_total_opp DESC
            ),
            lob_pivot AS (
                SELECT
                    opportunity_id,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'BAIP - Data & AI'), 0)                 AS acv_l1_baip_dai,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'BAIP - Technology Foundation'), 0)     AS acv_l1_baip_tf,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Business Transformation Management'), 0) AS acv_l1_btm,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Cloud ERP Private'), 0)                AS acv_l1_cloud_erp,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Customer Experience'), 0)              AS acv_l1_cx,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Finance and Spend Management'), 0)     AS acv_l1_finance,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Human Capital Management'), 0)         AS acv_l1_hcm,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Supply Chain Management'), 0)          AS acv_l1_scm,
                    COALESCE(SUM(acv_usd) FILTER (WHERE solution_area_l1 = 'Other'), 0)                            AS acv_l1_other
                FROM pipeline
                WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND opportunity_id IN (SELECT opportunity_id FROM base)
                GROUP BY opportunity_id
            )
            SELECT b.*, lp.acv_l1_baip_dai, lp.acv_l1_baip_tf, lp.acv_l1_btm,
                   lp.acv_l1_cloud_erp, lp.acv_l1_cx, lp.acv_l1_finance,
                   lp.acv_l1_hcm, lp.acv_l1_scm, lp.acv_l1_other,
                   ps.presales_lead_status
            FROM base b
            LEFT JOIN lob_pivot lp ON lp.opportunity_id = b.opportunity_id
            LEFT JOIN presales ps ON ps.opp_id = LPAD(b.opportunity_id, 10, '0')
            ORDER BY b.acv_total_opp DESC
            LIMIT {int(limit)}
        """
    else:
        sql = f"""
            SELECT p.opportunity_id, p.account_name, p.opp_description, p.opp_owner_name,
                p.region_lvl3, p.solution_area_l1, p.sub_solution_area_l2,
                p.solution_area_l3, p.solution_area_l4, p.opp_phase, p.fc_qualification,
                p.closing_qtr, TO_NVARCHAR(p.closing_date, 'YYYY-MM-DD'), p.drm_category, p.iss_group_text,
                p.deal_qualification_decision, p.deal_qualification_score,
                p.engagement_type, p.vat_dh, p.vat_full, p.vat_size,
                p.acv_usd, p.acv_total_opp, p.acv_per_vat,
                p.account_sales_group_text, p.flsm, p.local_acct_owner,
                d.dda_status, d.range_aging, d.aging_up_today,
                ps.presales_lead_status,
                p.sde_initial_engagement_date,
                p.sde_primary_engagement_name,
                p.sde_handover_name,
                p.sic_code,
                p.sic_description,
                p.sap_mastercode,
                p.account_bp,
                TO_NVARCHAR(p.snapshot_week, 'YYYY-MM-DD')
            FROM pipeline p
            LEFT JOIN dda d ON d.opp_id = p.opportunity_id
            LEFT JOIN presales ps ON ps.opp_id = LPAD(p.opportunity_id, 10, '0')
            {where}
            ORDER BY p.acv_total_opp DESC
            LIMIT {int(limit)}
        """
    return query(sql, params)


@router.get("/new-bookings")
def get_new_bookings(
    closing_qtrs:     List[str] = Query(default=[]),
    regions:          List[str] = Query(default=[]),
    sol_l1s:          List[str] = Query(default=[]),
    drms:             List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    digital_sas:      List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """
    Returns opportunities that are Booked/Won or A-Purchase in the CURRENT snapshot
    but were NOT Booked/Won or A-Purchase in the previous snapshot (pipeline_last_week).
    These are the 'new wins' since the last data refresh.
    """
    extra_clauses = []
    extra_params: list = []
    if closing_qtrs:
        ph = ",".join(["%s"]*len(closing_qtrs)); extra_clauses.append(f"p.closing_qtr IN ({ph})"); extra_params.extend(closing_qtrs)
    if regions:
        ph = ",".join(["%s"]*len(regions)); extra_clauses.append(f"p.region_lvl3 IN ({ph})"); extra_params.extend(regions)
    if sol_l1s:
        ph = ",".join(["%s"]*len(sol_l1s)); extra_clauses.append(f"p.solution_area_l1 IN ({ph})"); extra_params.extend(sol_l1s)
    if engagement_types:
        ph = ",".join(["%s"]*len(engagement_types)); extra_clauses.append(f"p.engagement_type IN ({ph})"); extra_params.extend(engagement_types)
    if digital_sas:
        or_c = " OR ".join(["p.vat_dh ILIKE %s"]*len(digital_sas)); extra_clauses.append(f"({or_c})"); extra_params.extend([f"%{v}%" for v in digital_sas])

    extra_where = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""

    sql = f"""
        SELECT DISTINCT ON (p.opportunity_id)
            p.opportunity_id, p.account_name, p.opp_description, p.opp_owner_name,
            p.region_lvl3, p.solution_area_l1, p.sub_solution_area_l2,
            p.drm_category, p.opp_phase, p.closing_qtr, p.fc_qualification,
            p.engagement_type, p.acv_usd, p.acv_total_opp,
            p.vat_dh, p.vat_full, p.vat_size,
            p.flsm, p.local_acct_owner, p.account_sales_group_text,
            -- What was the previous status?
            COALESCE(lw.drm_category, 'Not in previous snapshot') AS prev_drm_category,
            COALESCE(lw.opp_phase, '—')                           AS prev_opp_phase
        FROM pipeline p
        LEFT JOIN (
            SELECT DISTINCT ON (opportunity_id)
                opportunity_id, drm_category, opp_phase
            FROM pipeline_last_week
            ORDER BY opportunity_id, acv_total_opp DESC
        ) lw ON lw.opportunity_id = p.opportunity_id
        WHERE p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND (p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')
          AND (
            lw.opportunity_id IS NULL
            OR NOT (lw.drm_category = 'Booked/Won' OR lw.opp_phase = 'A - Purchase')
          )
          {extra_where}
        ORDER BY p.opportunity_id, p.acv_total_opp DESC
    """
    return query(sql, extra_params or None)


@router.get("/prev")
def get_pipeline_prev(
    closing_qtr:  Optional[str] = None,
    region:       Optional[str] = None,
    sol_l1:       Optional[str] = None,
    drm:          Optional[str] = None,
    # Multi-value
    closing_qtrs:     List[str] = Query(default=[]),
    regions:          List[str] = Query(default=[]),
    sol_l1s:          List[str] = Query(default=[]),
    drms:             List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    opp_owners:       List[str] = Query(default=[]),
    fcs:              List[str] = Query(default=[]),
    digital_sas:      List[str] = Query(default=[]),
    search:           Optional[str] = None,
    _user=Depends(verify_token),
):
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, drm=drm,
        search=search,
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s,
        drms=drms, engagement_types=engagement_types,
        opp_owners=opp_owners, fcs=fcs, digital_sas=digital_sas,
        table="pipeline_last_week",
    )
    sql = f"""
        SELECT DISTINCT ON (opportunity_id)
            opportunity_id, account_name, opp_owner_name,
            region_lvl3, region_lvl4, solution_area_l1, sub_solution_area_l2,
            opp_phase, fc_qualification, closing_qtr, drm_category,
            engagement_type, iss_group_text, account_planned_archetype_text,
            deal_qualification_decision, flsm, vat_dh,
            acv_total_opp, acv_usd
        FROM pipeline_last_week {where}
        ORDER BY opportunity_id, acv_total_opp DESC
    """
    return query(sql, params or None)


@router.get("/charts")
def pipeline_charts(
    snapshot_week: Optional[str] = None,
    closing_qtr: Optional[str] = None,
    region: Optional[str] = None,
    sol_l1: Optional[str] = None,
    sol_l2: Optional[str] = None,
    drm: Optional[str] = None,
    opp_phase: Optional[str] = None,
    fc: Optional[str] = None,
    engagement_type: Optional[str] = None,
    opp_owner: Optional[str] = None,
    digital_sa: Optional[str] = None,
    search: Optional[str] = None,
    exclude_neg: bool = False,
    include_purchase: bool = False,
    closing_qtrs: List[str] = Query(default=[]),
    regions: List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    sol_l1s: List[str] = Query(default=[]),
    sol_l2s: List[str] = Query(default=[]),
    drms: List[str] = Query(default=[]),
    opp_phases: List[str] = Query(default=[]),
    fcs: List[str] = Query(default=[]),
    opp_owners: List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    local_owners: List[str] = Query(default=[]),
    flsms: List[str] = Query(default=[]),
    iss_groups: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    deal_type: Optional[str] = None,
    _user=Depends(verify_token),
):
    """
    All chart aggregations in a single SQL pass using a CTE + UNION ALL.
    Cuts round-trips from 13 to 1, reducing latency ~8x.
    """
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, sol_l2=sol_l2,
        drm=drm, opp_phase=opp_phase, fc=fc, engagement_type=engagement_type,
        opp_owner=opp_owner, digital_sa=digital_sa, search=search,
        snapshot_week=snapshot_week,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4=None,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs,
        opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas, local_owners=local_owners, flsms=flsms,
        iss_groups=iss_groups, acc_sub_segments=acc_sub_segments,
        qual_decisions=qual_decisions,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
    )
    where = _apply_deal_type(where, deal_type)
    neg_filter = "AND acv_usd > 0" if exclude_neg else ""
    purchase_expr = (
        "CASE WHEN opp_phase = 'A - Purchase' THEN 'Booked/Won' ELSE drm_category END"
        if include_purchase else "drm_category"
    )
    is_active = "drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')"

    sql = f"""
        WITH base AS (
            SELECT opportunity_id,
                   {purchase_expr}               AS drm_category,
                   fc_qualification, opp_phase,
                   iss_group_text, region_lvl3, solution_area_l1, sub_solution_area_l2,
                   deal_qualification_decision, channel_partner, engagement_type,
                   closing_qtr, acv_usd, age_days
            FROM pipeline
            {where}
            {neg_filter}
        ),
        agg AS (
            SELECT 'segment'     AS dim, iss_group_text         AS lbl, NULL AS cat,
                   COUNT(DISTINCT opportunity_id) AS n, COALESCE(SUM(acv_usd),0) AS acv, NULL::int AS ord
            FROM base GROUP BY iss_group_text
        UNION ALL
            SELECT 'region',     region_lvl3,           NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY region_lvl3
        UNION ALL
            SELECT 'sol_l1',     solution_area_l1,      NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY solution_area_l1
        UNION ALL
            SELECT 'sub_sol',    sub_solution_area_l2,  NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY sub_solution_area_l2
        UNION ALL
            SELECT 'drm',        drm_category,          NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY drm_category
        UNION ALL
            SELECT 'phase',      opp_phase,             NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY opp_phase
        UNION ALL
            SELECT 'fc',         fc_qualification,      NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL
            FROM base WHERE {is_active} GROUP BY fc_qualification
        UNION ALL
            SELECT 'closing_qtr',closing_qtr,           NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY closing_qtr
        UNION ALL
            SELECT 'qual',       deal_qualification_decision, NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY deal_qualification_decision
        UNION ALL
            SELECT 'channel',    channel_partner,       NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY channel_partner
        UNION ALL
            SELECT 'engagement', engagement_type,       NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL FROM base GROUP BY engagement_type
        UNION ALL
            SELECT 'age',
                CASE
                    WHEN age_days <=  30 THEN '0-30d'
                    WHEN age_days <=  60 THEN '31-60d'
                    WHEN age_days <=  90 THEN '61-90d'
                    WHEN age_days <= 180 THEN '91-180d'
                    WHEN age_days <= 365 THEN '181-365d'
                    ELSE '>365d'
                END,
                NULL, COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0),
                MIN(age_days)
            FROM base GROUP BY
                CASE
                    WHEN age_days <=  30 THEN '0-30d'
                    WHEN age_days <=  60 THEN '31-60d'
                    WHEN age_days <=  90 THEN '61-90d'
                    WHEN age_days <= 180 THEN '91-180d'
                    WHEN age_days <= 365 THEN '181-365d'
                    ELSE '>365d'
                END
        UNION ALL
            SELECT 'qtr_fc',     closing_qtr,           fc_qualification,
                   COUNT(DISTINCT opportunity_id), COALESCE(SUM(acv_usd),0), NULL
            FROM base WHERE {is_active}
            GROUP BY closing_qtr, fc_qualification
        )
        SELECT dim, lbl, cat, n, acv, ord FROM agg
        ORDER BY dim, acv DESC, ord
    """

    rows = query(sql, params or None)

    result: dict = {k: [] for k in (
        'by_segment','by_region','by_sol_l1','by_sub_sol','by_drm',
        'by_phase','by_fc','by_closing_qtr','by_qual_decision',
        'by_channel','by_engagement','by_age_range','by_qtr_fc',
    )}
    dim_map = {
        'segment':'by_segment','region':'by_region','sol_l1':'by_sol_l1',
        'sub_sol':'by_sub_sol','drm':'by_drm','phase':'by_phase','fc':'by_fc',
        'closing_qtr':'by_closing_qtr','qual':'by_qual_decision',
        'channel':'by_channel','engagement':'by_engagement',
        'age':'by_age_range','qtr_fc':'by_qtr_fc',
    }
    age_order = {'0-30d':0,'31-60d':1,'61-90d':2,'91-180d':3,'181-365d':4,'>365d':5}

    for r in rows:
        key = dim_map.get(r['dim'])
        if not key:
            continue
        if r['dim'] == 'qtr_fc':
            result[key].append({'label': r['lbl'], 'category': r['cat'], 'acv': float(r['acv'])})
        elif r['dim'] == 'age':
            result[key].append({'label': r['lbl'], 'n_opps': r['n'], 'acv': float(r['acv']),
                                 '_ord': age_order.get(r['lbl'], 99)})
        else:
            result[key].append({'label': r['lbl'], 'n_opps': r['n'], 'acv': float(r['acv'])})

    # Sort age range in day order, then remove helper key
    result['by_age_range'].sort(key=lambda x: x.pop('_ord', 99))

    return result


@router.get("/{opp_id}")
def get_opportunity(opp_id: str, _user=Depends(verify_token)):
    """Full detail for a single opportunity (latest snapshot, all solution lines)."""
    sql = """
        SELECT p.*
        FROM pipeline p
        WHERE p.opportunity_id = %s
          AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
        ORDER BY p.acv_usd DESC
    """
    rows = query(sql, [opp_id])
    if not rows:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return rows


@router.get("/lac/lob-breakdown")
def lac_lob_breakdown(
    closing_qtr:      Optional[str] = None,
    region:           Optional[str] = None,
    engagement_type:  Optional[str] = None,
    sol_l1:           Optional[str] = None,
    opp_owner:        Optional[str] = None,
    fc:               Optional[str] = None,
    closing_qtrs:     List[str] = Query(default=[]),
    regions:          List[str] = Query(default=[]),
    drms:             List[str] = Query(default=[]),
    sol_l1s:          List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    opp_owners:       List[str] = Query(default=[]),
    fcs:              List[str] = Query(default=[]),
    search:           Optional[str] = None,
    _user=Depends(verify_token),
):
    """
    Returns ACV and opp counts broken down by the LoB taxonomy defined for LAC Results:
      - Each top-level LoB (BAIP D&AI, BAIP TF, BTM, CX, Finance, HCM)
      - Finance splits into 3 sub-groups based on sub_solution_area_l2
    For each LoB: current period + pipeline_2025 comparison (Booked/Won + closed opps).
    """
    # ── build filter clauses (current) ───────────────────────────────────────
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region,
        engagement_type=engagement_type, sol_l1=sol_l1,
        opp_owner=opp_owner, fc=fc, search=search,
        closing_qtrs=closing_qtrs, regions=regions, drms=drms,
        sol_l1s=sol_l1s, opp_owners=opp_owners, fcs=fcs,
        engagement_types=engagement_types, digital_sas=digital_sas,
    )

    # ── 2025 WHERE — same filters, quarters mapped 2026→2025 (e.g. 2026-Q2 → 2025-Q2)
    qtrs_2025 = [q.replace("2026-", "2025-").replace("2027-", "2026-") for q in closing_qtrs] if closing_qtrs else []
    where_2025, params_2025 = _build_where(
        region=region,
        engagement_type=engagement_type, sol_l1=sol_l1,
        opp_owner=opp_owner, fc=fc, search=search,
        regions=regions, drms=drms,
        closing_qtrs=qtrs_2025,
        sol_l1s=sol_l1s, opp_owners=opp_owners, fcs=fcs,
        engagement_types=engagement_types, digital_sas=digital_sas,
        table="pipeline_2025",
    )

    # ── Finance sub-group mapping ─────────────────────────────────────────────
    FINANCE_GROUPS = {
        "Procurement / Business Network / Fieldglass": [
            "Procurement", "Fieldglass",
        ],
        "Cloud ERP Finance / Cloud ERP SCM": [
            "Cloud ERP Finance", "Treasury & Working Capital",
            "Accounting & Financial Close", "Financial Planning & Analysis",
            "Quote to Cash",
        ],
        "Demais soluções Finance": [
            "Finance Private", "GRC, Tax and Trade",
        ],
    }

    LOB_FILTERS = {
        "BAIP - Data & AI":               ("solution_area_l1", "BAIP - Data & AI",                   None),
        "BAIP - Technology Foundation":   ("solution_area_l1", "BAIP - Technology Foundation",       None),
        "Business Transformation Mgmt":   ("solution_area_l1", "Business Transformation Management", None),
        "Customer Experience":            ("solution_area_l1", "Customer Experience",                None),
        "Human Capital Management":       ("solution_area_l1", "Human Capital Management",           None),
        "Cloud ERP Private":              ("solution_area_l1", "Cloud ERP Private",                  None),
        # Finance top-level
        "Finance & Spend Management":     ("solution_area_l1", "Finance and Spend Management",       None),
        # Finance sub-groups
        **{k: ("sub_solution_area_l2_in", v, "Finance and Spend Management") for k, v in FINANCE_GROUPS.items()},
    }

    def _kpis_for_filter(lob_field, lob_val, parent_l1, table="pipeline"):
        """Compute ACV + counts for a given LoB slice."""
        if table == "pipeline_2025":
            bw, bc = where_2025, list(params_2025)
        else:
            bw, bc = where, list(params)

        extra = []
        ep2 = []
        if parent_l1:
            extra.append("solution_area_l1 = %s"); ep2.append(parent_l1)
        if lob_field == "solution_area_l1":
            extra.append("solution_area_l1 = %s"); ep2.append(lob_val)
        elif lob_field == "sub_solution_area_l2_in":
            ph = ",".join(["%s"] * len(lob_val))
            extra.append(f"sub_solution_area_l2 IN ({ph})"); ep2.extend(lob_val)

        full_where = bw
        if extra:
            join = " AND ".join(extra)
            full_where = f"{bw} AND {join}" if bw else f"WHERE {join}"

        all_params = bc + ep2

        row = query_one(f"""
            SELECT
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Booked/Won')  AS n_booked,
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Committed')   AS n_committed,
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Probable')    AS n_probable,
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Upside')      AS n_upside,
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Excluded from Pipeline') AS n_excluded,
                COUNT(DISTINCT opportunity_id) FILTER (WHERE drm_category = 'Disc/Lost')   AS n_lost,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Booked/Won'),  0)      AS acv_booked,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Committed'),   0)      AS acv_committed,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Probable'),    0)      AS acv_probable,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Upside'),      0)      AS acv_upside,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Excluded from Pipeline'), 0) AS acv_excluded,
                COALESCE(SUM(acv_usd) FILTER (WHERE drm_category = 'Disc/Lost'),   0)      AS acv_lost
            FROM {table}
            {full_where}
        """, all_params or None)
        return {k: float(v or 0) for k, v in (row or {}).items()}

    result = []
    for lob_name, (field, val, parent) in LOB_FILTERS.items():
        curr = _kpis_for_filter(field, val, parent, table="pipeline")
        prev = _kpis_for_filter(field, val, parent, table="pipeline_2025")

        n_closed = curr["n_booked"] + curr["n_lost"]
        conv_rate = (curr["n_booked"] / n_closed * 100) if n_closed > 0 else None

        result.append({
            "lob":          lob_name,
            "is_finance_sub": parent == "Finance and Spend Management",
            # current
            "n_booked":     int(curr["n_booked"]),
            "n_committed":  int(curr["n_committed"]),
            "n_probable":   int(curr["n_probable"]),
            "n_upside":     int(curr["n_upside"]),
            "n_excluded":   int(curr["n_excluded"]),
            "n_lost":       int(curr["n_lost"]),
            "acv_booked":   curr["acv_booked"],
            "acv_committed":curr["acv_committed"],
            "acv_probable": curr["acv_probable"],
            "acv_upside":   curr["acv_upside"],
            "acv_excluded": curr["acv_excluded"],
            "acv_lost":     curr["acv_lost"],
            "conversion_rate": round(conv_rate, 1) if conv_rate is not None else None,
            # 2025 comparison
            "prev_n_booked":  int(prev["n_booked"]),
            "prev_n_lost":    int(prev["n_lost"]),
            "prev_acv_booked":prev["acv_booked"],
            "prev_acv_lost":  prev["acv_lost"],
            "prev_conv_rate": round(prev["n_booked"] / (prev["n_booked"] + prev["n_lost"]) * 100, 1)
                              if (prev["n_booked"] + prev["n_lost"]) > 0 else None,
        })

    return {"lobs": result}
