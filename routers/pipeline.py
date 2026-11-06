"""Pipeline router — all endpoints that query pipeline / pipeline_last_week."""
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from database import query, query_one, execute, IS_HANA
from auth import verify_token


def _ensure_shadow_comments_table():
    try:
        execute("""
            CREATE TABLE shadow_opp_comments (
                closed_opp_id NVARCHAR(50) NOT NULL,
                dh_opp_id     NVARCHAR(50) NOT NULL,
                comment       NCLOB,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by    NVARCHAR(200),
                PRIMARY KEY (closed_opp_id, dh_opp_id)
            )
        """)
    except Exception:
        pass
    try:
        execute("ALTER TABLE shadow_opp_comments ADD (corregir TINYINT DEFAULT 0)")
    except Exception:
        pass


class ShadowComment(BaseModel):
    comment: str = ""
    corregir: int = 0

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

_PH = "?" if IS_HANA else "%s"


@router.get("/account/{account_bp}")
def pipeline_by_account(account_bp: str, _user=Depends(verify_token)):
    """Return AE, FLSM, SDE and open opportunities for a specific account BP ID.

    Used by Intelligence domain-enrichment to resolve team info without fuzzy name search.
    """
    rows = query(f"""
        SELECT
            opportunity_id, opp_phase, opp_owner_name, opp_owner_email,
            flsm, sde_current_territory_owner, solution_area_l1,
            sub_solution_area_l2, acv_usd, closing_qtr, drm_category,
            region_lvl2, region_lvl3, region_lvl4
        FROM pipeline
        WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND account_bp = {_PH}
        ORDER BY acv_usd DESC NULLS LAST
        LIMIT 20
    """, [account_bp])

    open_opps  = [r for r in rows if (r.get("drm_category") or "").lower() not in
                  ("disc/lost", "booked/won", "excluded from pipeline")]
    ae_names   = list({r["opp_owner_name"] for r in open_opps if r.get("opp_owner_name")})
    ae_emails  = list({r["opp_owner_email"] for r in open_opps if r.get("opp_owner_email")})
    flsms      = list({r["flsm"] for r in open_opps if r.get("flsm")})
    sdes       = list({r["sde_current_territory_owner"] for r in open_opps
                       if r.get("sde_current_territory_owner")})
    regions    = list({r["region_lvl2"] or r["region_lvl3"] or r["region_lvl4"]
                       for r in open_opps
                       if r.get("region_lvl2") or r.get("region_lvl3") or r.get("region_lvl4")})

    return {
        "account_bp":   account_bp,
        "open_opps":    len(open_opps),
        "ae":           ae_names[0] if ae_names else None,
        "ae_email":     ae_emails[0] if ae_emails else None,
        "flsm":         flsms[0] if flsms else None,
        "sde":          sdes[0] if sdes else None,
        "region":       regions[0] if regions else None,
        "aes":          ae_names,
        "ae_emails":    ae_emails,
        "flsms":        flsms,
        "sdes":         sdes,
        "regions":      regions,
        "opportunities": rows,
    }


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
    closing_qtrs=None, regions=None, region_lvl4s=None, sol_l1s=None, sol_l2s=None,
    drms=None, opp_phases=None, fcs=None, opp_owners=None,
    qual_decisions=None, iss_groups=None, iacs=None, flsms=None,
    local_owners=None, acc_sub_segments=None,
    engagement_types=None,
    digital_sas=None,
    channel_partners=None,
    presales_statuses=None,  # multi-value: ['Red','Yellow','Green']
    # SPEC filters (join on account_specs via account_bp)
    iaes=None, spec_cxs=None, spec_hxms=None, spec_procurements=None,
    spec_dscs=None, spec_btps=None, spec_bdcs=None, spec_fins=None, spec_pros=None,
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
    _multi_filter("region_lvl4", region_lvl4s or ([] if not region_lvl4 else [region_lvl4]), clauses, params)
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

    # SPEC filters — subquery on account_specs via account_bp
    def _spec_filter(col, vals):
        if not vals:
            return
        v = [x for x in vals if x]
        if not v:
            return
        ph = ",".join(["%s"] * len(v))
        clauses.append(
            f"account_bp IN (SELECT account_bp FROM account_specs WHERE {col} IN ({ph}))"
        )
        params.extend(v)

    _spec_filter("iae",              iaes or [])
    _spec_filter("spec_cx",          spec_cxs or [])
    _spec_filter("spec_hxm",         spec_hxms or [])
    _spec_filter("spec_procurement",  spec_procurements or [])
    _spec_filter("spec_dsc",         spec_dscs or [])
    _spec_filter("spec_btp",         spec_btps or [])
    _spec_filter("spec_bdc",         spec_bdcs or [])
    _spec_filter("spec_fin",         spec_fins or [])
    _spec_filter("spec_pro",         spec_pros or [])

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


def _apply_fin_sub_type(where: str, fin_sub_type: Optional[str]) -> str:
    """Filter to Finance deals matching a specific sub-type (procurement_only / fieldglass_only / mixed_finance)."""
    if not fin_sub_type or fin_sub_type not in ('procurement_only', 'fieldglass_only', 'mixed_finance'):
        return where
    if fin_sub_type == 'procurement_only':
        condition = "proc_lines = total_lines"
    elif fin_sub_type == 'fieldglass_only':
        condition = "fg_lines = total_lines"
    else:
        condition = "proc_lines < total_lines AND fg_lines < total_lines"
    subq = f"""opportunity_id IN (
        SELECT opportunity_id FROM (
            SELECT opportunity_id,
                   SUM(CASE WHEN sub_solution_area_l2 = 'Procurement' THEN 1 ELSE 0 END) AS proc_lines,
                   SUM(CASE WHEN sub_solution_area_l2 = 'Fieldglass'  THEN 1 ELSE 0 END) AS fg_lines,
                   COUNT(*) AS total_lines
            FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND solution_area_l1 = 'Finance and Spend Management'
            GROUP BY opportunity_id
        ) _dt WHERE {condition}
    )"""
    return (where + " AND " + subq) if where else ("WHERE " + subq)


def _apply_opp_extra_filters(
    where: str, params: list,
    dda_statuses=None, range_agings=None,
    priorities=None, probability_min=None, probability_max=None,
) -> str:
    """Filters stored outside the pipeline table (DDA, opportunity_priorities).
    Same semantics as the table endpoint (GET /api/pipeline), so table, KPIs and
    charts always see the same set of opportunities. Mutates params in place."""
    def _add(clause: str):
        nonlocal where
        where = (where + " AND " if where else "WHERE ") + clause

    dda_statuses = [v for v in (dda_statuses or []) if v]
    range_agings = [v for v in (range_agings or []) if v]
    priorities   = [int(v) for v in (priorities or []) if v is not None]
    if dda_statuses:
        ph = ",".join(["%s"] * len(dda_statuses))
        _add(f"opportunity_id IN (SELECT opp_id FROM dda WHERE dda_status IN ({ph}))")
        params.extend(dda_statuses)
    if range_agings:
        ph = ",".join(["%s"] * len(range_agings))
        _add(f"opportunity_id IN (SELECT opp_id FROM dda WHERE range_aging IN ({ph}))")
        params.extend(range_agings)
    if priorities:
        ph = ",".join(["%s"] * len(priorities))
        _add(f"opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE priority IN ({ph}))")
        params.extend(priorities)
    if probability_min is not None:
        _add("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct >= %s)")
        params.append(float(probability_min))
    if probability_max is not None:
        _add("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct <= %s)")
        params.append(float(probability_max))
    return where


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
    iacs: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    iaes: List[str] = Query(default=[]),
    spec_cxs: List[str] = Query(default=[]),
    spec_hxms: List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs: List[str] = Query(default=[]),
    spec_btps: List[str] = Query(default=[]),
    spec_bdcs: List[str] = Query(default=[]),
    spec_fins: List[str] = Query(default=[]),
    spec_pros: List[str] = Query(default=[]),
    exclude_neg: bool = False,
    include_purchase: bool = False,
    deal_type: Optional[str] = None,
    fin_sub_type: Optional[str] = None,
    stall_days: int = 90,
    dda_statuses: List[str] = Query(default=[]),
    range_agings: List[str] = Query(default=[]),
    priorities: List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
    acv_min: Optional[float] = None,
    acv_max: Optional[float] = None,
    _user=Depends(verify_token),
):
    """Server-side KPI aggregation — handles all filtered rows, not capped by limit."""
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, sol_l2=sol_l2,
        drm=drm, opp_phase=opp_phase, fc=fc, engagement_type=engagement_type,
        opp_owner=opp_owner, digital_sa=digital_sa, search=search,
        snapshot_week=snapshot_week,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs,
        opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas,
        local_owners=local_owners, flsms=flsms,
        iss_groups=iss_groups, iacs=iacs, acc_sub_segments=acc_sub_segments,
        qual_decisions=qual_decisions,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
        iaes=iaes, spec_cxs=spec_cxs, spec_hxms=spec_hxms, spec_procurements=spec_procurements,
        spec_dscs=spec_dscs, spec_btps=spec_btps, spec_bdcs=spec_bdcs,
        spec_fins=spec_fins, spec_pros=spec_pros,
    )
    where = _apply_deal_type(where, deal_type)
    where = _apply_fin_sub_type(where, fin_sub_type)
    where = _apply_opp_extra_filters(
        where, params, dda_statuses=dda_statuses, range_agings=range_agings,
        priorities=priorities, probability_min=probability_min, probability_max=probability_max,
    )
    if acv_min is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp >= %s"
        params.append(float(acv_min))
    if acv_max is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp <= %s"
        params.append(float(acv_max))
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
                   fc_qualification, engagement_type, acv_usd, age_days, phase_since_days
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
                                                        'Excluded from Pipeline'))  AS n_untouched,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')), 0)  AS active_pipeline_acv,
            COALESCE(SUM(l.acv_usd) FILTER (WHERE l.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
                              AND l.phase_since_days > {stall_days}), 0)  AS stalled_pipeline_acv
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


class OppLinesRequest(BaseModel):
    opp_ids: list


@router.post("/bookings/opp-lines")
def get_opp_lines(payload: OppLinesRequest, _user=Depends(verify_token)):
    """Return line items (sol_l1, sol_l2, eng_name, acv) per opportunity from the latest snapshot."""
    if not payload.opp_ids:
        return {}
    ph = ",".join(["%s"] * len(payload.opp_ids))
    rows = query(
        f"""
        SELECT opportunity_id, solution_area_l1, sub_solution_area_l2,
               sde_primary_engagement_name,
               SUM(acv_usd) AS acv_usd
        FROM pipeline
        WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND opportunity_id IN ({ph})
          AND acv_usd > 0
        GROUP BY opportunity_id, solution_area_l1, sub_solution_area_l2,
                 sde_primary_engagement_name
        ORDER BY opportunity_id, solution_area_l1, acv_usd DESC
        """,
        payload.opp_ids,
    )
    result = {}
    for r in rows:
        oid = r["opportunity_id"]
        if oid not in result:
            result[oid] = []
        result[oid].append({
            "sol_l1": r["solution_area_l1"] or "",
            "sol_l2": r["sub_solution_area_l2"] or "",
            "eng":    r["sde_primary_engagement_name"] or "",
            "acv":    float(r["acv_usd"] or 0),
        })
    return result


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

    def _spec_col(col):
        rows = query(
            f"SELECT DISTINCT s.{col} AS v FROM account_specs s"
            f" INNER JOIN pipeline p ON p.account_bp = s.account_bp"
            f" WHERE p.snapshot_week = {SNAP} AND s.{col} IS NOT NULL"
            f" ORDER BY s.{col}"
        )
        return [r["v"] for r in rows if r["v"]]

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
        # SPEC filters — from account_specs table via pipeline account_bp
        "iaes":              _spec_col("iae"),
        "spec_cxs":          _spec_col("spec_cx"),
        "spec_hxms":         _spec_col("spec_hxm"),
        "spec_procurements": _spec_col("spec_procurement"),
        "spec_dscs":         _spec_col("spec_dsc"),
        "spec_btps":         _spec_col("spec_btp"),
        "spec_bdcs":         _spec_col("spec_bdc"),
        "spec_fins":         _spec_col("spec_fin"),
        "spec_pros":         _spec_col("spec_pro"),
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


@router.get("/filters/local-owners-by-flsm")
def local_owners_by_flsm(
    flsms: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Return distinct local_acct_owner values for the given FLSM(s)."""
    if not flsms:
        return {"local_owners": []}
    ph = ",".join(["%s"] * len(flsms))
    rows = query(
        f"""SELECT DISTINCT local_acct_owner
            FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND flsm IN ({ph})
              AND local_acct_owner IS NOT NULL
            ORDER BY local_acct_owner""",
        flsms,
    )
    return {"local_owners": [r["local_acct_owner"] for r in rows if r["local_acct_owner"]]}


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
    search: Optional[str] = None,
    closing_qtrs: List[str] = Query(default=[]),
    regions: List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    sol_l1s: List[str] = Query(default=[]),
    sol_l2s: List[str] = Query(default=[]),
    drms: List[str] = Query(default=[]),
    opp_phases: List[str] = Query(default=[]),
    fcs: List[str] = Query(default=[]),
    engagement_types: List[str] = Query(default=[]),
    opp_owners: List[str] = Query(default=[]),
    digital_sas: List[str] = Query(default=[]),
    flsms: List[str] = Query(default=[]),
    local_owners: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    iss_groups: List[str] = Query(default=[]),
    iacs: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    iaes: List[str] = Query(default=[]),
    spec_cxs: List[str] = Query(default=[]),
    spec_hxms: List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs: List[str] = Query(default=[]),
    spec_btps: List[str] = Query(default=[]),
    spec_bdcs: List[str] = Query(default=[]),
    spec_fins: List[str] = Query(default=[]),
    spec_pros: List[str] = Query(default=[]),
    deal_type: Optional[str] = None,
    exclude_neg: bool = False,
    priorities: List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
    acv_min: Optional[float] = None,
    acv_max: Optional[float] = None,
    dda_statuses: List[str] = Query(default=[]),
    range_agings: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Aggregations for DDA Status and Range Aging charts — uses the same
    pipeline filter set as /kpis, /charts and the table endpoint."""
    where, params = _build_where(
        snapshot_week=snapshot_week,
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, drm=drm,
        engagement_type=engagement_type, opp_owner=opp_owner, digital_sa=digital_sa,
        flsm=flsm, search=search,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs,
        engagement_types=engagement_types, opp_owners=opp_owners,
        digital_sas=digital_sas, flsms=flsms, local_owners=local_owners,
        channel_partners=channel_partners, qual_decisions=qual_decisions,
        iss_groups=iss_groups, iacs=iacs, acc_sub_segments=acc_sub_segments,
        presales_statuses=presales_statuses,
        iaes=iaes, spec_cxs=spec_cxs, spec_hxms=spec_hxms, spec_procurements=spec_procurements,
        spec_dscs=spec_dscs, spec_btps=spec_btps, spec_bdcs=spec_bdcs,
        spec_fins=spec_fins, spec_pros=spec_pros,
    )
    where = _apply_deal_type(where, deal_type)
    # DDA status / range aging are applied on the dda table below, not here.
    where = _apply_opp_extra_filters(
        where, params, priorities=priorities,
        probability_min=probability_min, probability_max=probability_max,
    )
    if acv_min is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp >= %s"
        params.append(float(acv_min))
    if acv_max is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp <= %s"
        params.append(float(acv_max))
    neg_filter = ("AND " if where else "WHERE ") + "acv_usd > 0" if exclude_neg else ""

    dda_clauses: list = []
    dda_params: list = []
    dda_statuses = [v for v in dda_statuses if v]
    range_agings = [v for v in range_agings if v]
    if dda_statuses:
        ph = ",".join(["%s"] * len(dda_statuses))
        dda_clauses.append(f"d.dda_status IN ({ph})"); dda_params.extend(dda_statuses)
    if range_agings:
        ph = ",".join(["%s"] * len(range_agings))
        dda_clauses.append(f"d.range_aging IN ({ph})"); dda_params.extend(range_agings)
    dda_extra = (" AND " + " AND ".join(dda_clauses)) if dda_clauses else ""
    all_params = params + dda_params

    # Filtered opportunity set computed once in a subquery, so _build_where's
    # clauses (LIKE, EXISTS, sub-selects) never need table-alias rewriting.
    opps_cte = f"WITH opps AS (SELECT DISTINCT opportunity_id FROM pipeline {where} {neg_filter})"

    by_dda_status = query(f"""
        {opps_cte}
        SELECT d.dda_status AS label,
               COUNT(DISTINCT o.opportunity_id) AS n_opps
        FROM opps o
        JOIN dda d ON d.opp_id = o.opportunity_id
        WHERE d.dda_status IS NOT NULL
          {dda_extra}
        GROUP BY d.dda_status ORDER BY n_opps DESC
    """, all_params or None)

    by_range_aging = query(f"""
        {opps_cte}
        SELECT d.range_aging AS label,
               COUNT(DISTINCT o.opportunity_id) AS n_opps
        FROM opps o
        JOIN dda d ON d.opp_id = o.opportunity_id
        WHERE d.range_aging IS NOT NULL
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
    region_lvl4s: List[str] = Query(default=[]),
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
    fin_sub_type: Optional[str] = None,
    priorities: List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
    acv_min: Optional[float] = None,
    acv_max: Optional[float] = None,
    deduplicate: bool = True,
    limit: int = Query(default=5000, le=20000),
    # SPEC filters
    iaes: List[str] = Query(default=[]),
    spec_cxs: List[str] = Query(default=[]),
    spec_hxms: List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs: List[str] = Query(default=[]),
    spec_btps: List[str] = Query(default=[]),
    spec_bdcs: List[str] = Query(default=[]),
    spec_fins: List[str] = Query(default=[]),
    spec_pros: List[str] = Query(default=[]),
    has_comment: bool = False,
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
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs, opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas,
        qual_decisions=qual_decisions, iss_groups=iss_groups, iacs=iacs, flsms=flsms,
        local_owners=local_owners, acc_sub_segments=acc_sub_segments,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
        iaes=iaes, spec_cxs=spec_cxs, spec_hxms=spec_hxms, spec_procurements=spec_procurements,
        spec_dscs=spec_dscs, spec_btps=spec_btps, spec_bdcs=spec_bdcs,
        spec_fins=spec_fins, spec_pros=spec_pros,
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
    if priorities:
        ph = ",".join(["%s"] * len(priorities))
        where = (where + " AND " if where else "WHERE ") + f"opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE priority IN ({ph}))"
        params.extend([int(p) for p in priorities])
    if probability_min is not None:
        where = (where + " AND " if where else "WHERE ") + "opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct >= %s)"
        params.append(float(probability_min))
    if probability_max is not None:
        where = (where + " AND " if where else "WHERE ") + "opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct <= %s)"
        params.append(float(probability_max))
    if acv_min is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp >= %s"
        params.append(float(acv_min))
    if acv_max is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp <= %s"
        params.append(float(acv_max))
    if has_comment:
        where = (where + " AND " if where else "WHERE ") + "opportunity_id IN (SELECT DISTINCT opportunity_id FROM comments)"
    where = _apply_deal_type(where, deal_type)
    where = _apply_fin_sub_type(where, fin_sub_type)
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
                   ps.presales_lead_status,
                   op.priority_t1, op.priority, op.probability_pct,
                   sp.iae, sp.spec_cx, sp.spec_hxm, sp.spec_procurement,
                   sp.spec_dsc, sp.spec_btp, sp.spec_bdc, sp.spec_fin, sp.spec_pro,
                   CASE WHEN cmt.opportunity_id IS NOT NULL THEN 1 ELSE 0 END AS has_comment
            FROM base b
            LEFT JOIN lob_pivot lp ON lp.opportunity_id = b.opportunity_id
            LEFT JOIN presales ps ON ps.opp_id = LPAD(b.opportunity_id, 10, '0')
            LEFT JOIN opportunity_priorities op ON op.opportunity_id = b.opportunity_id
            LEFT JOIN account_specs sp ON sp.account_bp = b.account_bp
            LEFT JOIN (SELECT DISTINCT opportunity_id FROM comments) cmt ON cmt.opportunity_id = b.opportunity_id
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
                TO_NVARCHAR(p.snapshot_week, 'YYYY-MM-DD'),
                sp.iae, sp.spec_cx, sp.spec_hxm, sp.spec_procurement,
                sp.spec_dsc, sp.spec_btp, sp.spec_bdc, sp.spec_fin, sp.spec_pro
            FROM pipeline p
            LEFT JOIN dda d ON d.opp_id = p.opportunity_id
            LEFT JOIN presales ps ON ps.opp_id = LPAD(p.opportunity_id, 10, '0')
            LEFT JOIN account_specs sp ON sp.account_bp = p.account_bp
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

    if IS_HANA:
        # _adapt_sql() can't handle nested DISTINCT ON (the rewriter finds the inner
        # subquery's ORDER BY before the outer one, producing malformed SQL).
        # Write ROW_NUMBER()-based SQL directly; _adapt_sql() still converts %s → ?.
        sql = f"""
            SELECT opportunity_id, account_name, opp_description, opp_owner_name,
                   region_lvl3, solution_area_l1, sub_solution_area_l2,
                   drm_category, opp_phase, closing_qtr, fc_qualification,
                   engagement_type, acv_usd, acv_total_opp,
                   vat_dh, vat_full, vat_size,
                   flsm, local_acct_owner, account_sales_group_text,
                   prev_drm_category, prev_opp_phase
            FROM (
                SELECT p.opportunity_id, p.account_name, p.opp_description, p.opp_owner_name,
                       p.region_lvl3, p.solution_area_l1, p.sub_solution_area_l2,
                       p.drm_category, p.opp_phase, p.closing_qtr, p.fc_qualification,
                       p.engagement_type, p.acv_usd, p.acv_total_opp,
                       p.vat_dh, p.vat_full, p.vat_size,
                       p.flsm, p.local_acct_owner, p.account_sales_group_text,
                       COALESCE(lw.drm_category, 'Not in previous snapshot') AS prev_drm_category,
                       COALESCE(lw.opp_phase, '-') AS prev_opp_phase,
                       ROW_NUMBER() OVER (PARTITION BY p.opportunity_id ORDER BY p.acv_total_opp DESC) AS _rn
                FROM pipeline p
                LEFT JOIN (
                    SELECT opportunity_id, drm_category, opp_phase
                    FROM (
                        SELECT opportunity_id, drm_category, opp_phase,
                               ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS _rn2
                        FROM pipeline_last_week
                    ) _lw_inner WHERE _rn2 = 1
                ) lw ON lw.opportunity_id = p.opportunity_id
                WHERE p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND (p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')
                  AND (
                    lw.opportunity_id IS NULL
                    OR NOT (lw.drm_category = 'Booked/Won' OR lw.opp_phase = 'A - Purchase')
                  )
                  {extra_where}
            ) _outer WHERE _rn = 1
        """
    else:
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


@router.get("/bookings/filters")
def get_bookings_filters(
    flsms: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Filter options for the Bookings congratulations dashboard — HANA-compatible separate queries."""
    SNAP = "(SELECT MAX(snapshot_week) FROM pipeline)"
    BASE = f"snapshot_week = {SNAP} AND (drm_category = 'Booked/Won' OR opp_phase = 'A - Purchase')"

    def _col(col):
        rows = query(
            f"SELECT DISTINCT {col} AS v FROM pipeline WHERE {BASE} AND {col} IS NOT NULL ORDER BY {col}"
        )
        return [r["v"] for r in rows if r["v"]]

    dsa_rows = query("SELECT DISTINCT nombre_empleado AS v FROM targets WHERE nombre_empleado IS NOT NULL ORDER BY nombre_empleado")
    team_rows = query("SELECT DISTINCT area AS v FROM targets WHERE area IS NOT NULL ORDER BY area")

    if flsms:
        ph = ",".join(["%s"] * len(flsms))
        opp_rows = query(
            f"SELECT DISTINCT opp_owner_name AS v FROM pipeline "
            f"WHERE {BASE} AND opp_owner_name IS NOT NULL AND flsm IN ({ph}) ORDER BY opp_owner_name",
            flsms,
        )
        opp_owners = [r["v"] for r in opp_rows if r["v"]]
    else:
        opp_owners = _col("opp_owner_name")

    return {
        "closing_qtrs": _col("closing_qtr"),
        "regions":      _col("region_lvl3"),
        "sol_l1s":      _col("solution_area_l1"),
        "sol_l2s":      _col("sub_solution_area_l2"),
        "dsas":         [r["v"] for r in dsa_rows if r["v"]],
        "dvas":         [],
        "teams":        [r["v"] for r in team_rows if r["v"]],
        "flsms":        _col("flsm"),
        "opp_owners":   opp_owners,
    }


@router.get("/bookings")
def get_bookings(
    closing_qtrs: List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    sol_l1s:      List[str] = Query(default=[]),
    sol_l2s:      List[str] = Query(default=[]),
    dsas:         List[str] = Query(default=[]),
    dvas:         List[str] = Query(default=[]),
    teams:        List[str] = Query(default=[]),
    flsms:        List[str] = Query(default=[]),
    opp_owners:   List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """Booked/Won opportunities with full VAT team info for the congratulations dashboard."""
    clauses = [
        "snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)",
        "(drm_category = 'Booked/Won' OR opp_phase = 'A - Purchase')",
    ]
    params: list = []
    if closing_qtrs:
        ph = ",".join(["?"] * len(closing_qtrs))
        clauses.append(f"closing_qtr IN ({ph})")
        params.extend(closing_qtrs)
    if regions:
        ph = ",".join(["?"] * len(regions))
        clauses.append(f"region_lvl3 IN ({ph})")
        params.extend(regions)
    if sol_l1s:
        ph = ",".join(["?"] * len(sol_l1s))
        clauses.append(f"solution_area_l1 IN ({ph})")
        params.extend(sol_l1s)
    if sol_l2s:
        ph = ",".join(["?"] * len(sol_l2s))
        clauses.append(f"sub_solution_area_l2 IN ({ph})")
        params.extend(sol_l2s)
    if dsas:
        or_c = " OR ".join(["UPPER(vat_dh) LIKE ?"] * len(dsas))
        clauses.append(f"({or_c})")
        params.extend([f"%{v.upper()}%" for v in dsas])
    if teams:
        ph = ",".join(["?"] * len(teams))
        clauses.append(f"""
            opportunity_id IN (
                SELECT DISTINCT pe.opportunity_id
                FROM pipeline_exploded pe
                JOIN targets t ON t.inumber = pe.sa_inumber
                WHERE pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  AND t.area IN ({ph})
            )
        """)
        params.extend(teams)
    if flsms:
        ph = ",".join(["?"] * len(flsms))
        clauses.append(f"flsm IN ({ph})")
        params.extend(flsms)
    if opp_owners:
        ph = ",".join(["?"] * len(opp_owners))
        clauses.append(f"opp_owner_name IN ({ph})")
        params.extend(opp_owners)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            opportunity_id, account_name, opp_description,
            region_lvl3, solution_area_l1, sub_solution_area_l2,
            closing_date, closing_qtr,
            acv_total_opp,
            opp_owner_name,
            vat_dh, vat_full,
            sde_primary_engagement_name,
            sde_current_territory_owner,
            flsm
        FROM (
            SELECT
                opportunity_id, account_name, opp_description,
                region_lvl3, solution_area_l1, sub_solution_area_l2,
                closing_date, closing_qtr,
                acv_total_opp,
                opp_owner_name,
                vat_dh, vat_full,
                sde_primary_engagement_name,
                sde_current_territory_owner,
                flsm,
                ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS rn
            FROM pipeline
            WHERE {where}
        ) t
        WHERE t.rn = 1
    """
    rows = query(sql, params or None)
    rows.sort(key=lambda r: r.get("closing_date") or "", reverse=True)
    return rows


@router.get("/bookings/missed-vat")
def get_missed_vat(
    closing_qtrs: List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    sol_l1s:      List[str] = Query(default=[]),
    teams:        List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """
    Closed deals (Booked/Won + A-Purchase) where DH was NOT in the VAT,
    matched against parallel DH-involved opportunities at the same account
    with the same Solution L1 — surfaces missed VAT coverage.
    """
    # Filters applied to the closed side
    closed_extra_clauses: list[str] = []
    closed_params: list = []
    if closing_qtrs:
        ph = ",".join(["?"] * len(closing_qtrs))
        closed_extra_clauses.append(f"closing_qtr IN ({ph})")
        closed_params.extend(closing_qtrs)
    if regions:
        ph = ",".join(["?"] * len(regions))
        closed_extra_clauses.append(f"region_lvl3 IN ({ph})")
        closed_params.extend(regions)
    if sol_l1s:
        ph = ",".join(["?"] * len(sol_l1s))
        closed_extra_clauses.append(f"solution_area_l1 IN ({ph})")
        closed_params.extend(sol_l1s)

    closed_filter = ""
    if closed_extra_clauses:
        closed_filter = "AND " + " AND ".join(closed_extra_clauses)

    # Filter DH parallel opps by SA team (via pipeline_exploded + targets)
    team_params: list = []
    dh_team_filter = ""
    if teams:
        team_ph = ",".join(["?"] * len(teams))
        dh_team_filter = f"""
            AND lo.opportunity_id IN (
                SELECT DISTINCT pe.opportunity_id
                FROM pipeline_exploded pe
                JOIN targets t ON t.inumber = pe.sa_inumber
                WHERE pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  AND t.area IN ({team_ph})
            )
        """
        team_params.extend(teams)

    all_params_combined = closed_params + team_params

    sql = f"""
        WITH latest_opps AS (
            SELECT opportunity_id, account_name, opp_description,
                   solution_area_l1, sub_solution_area_l2,
                   drm_category, opp_phase, closing_qtr,
                   acv_total_opp, vat_dh
            FROM (
                SELECT opportunity_id, account_name, opp_description,
                       solution_area_l1, sub_solution_area_l2,
                       drm_category, opp_phase, closing_qtr,
                       acv_total_opp, vat_dh,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY snapshot_week DESC) AS rn
                FROM pipeline
            ) t
            WHERE t.rn = 1
        ),
        closed_no_dh AS (
            SELECT opportunity_id, account_name, opp_description,
                   solution_area_l1, sub_solution_area_l2,
                   closing_date, closing_qtr, region_lvl3,
                   acv_total_opp, vat_full, opp_owner_name,
                   sde_primary_engagement_name, flsm, drm_category
            FROM (
                SELECT opportunity_id, account_name, opp_description,
                       solution_area_l1, sub_solution_area_l2,
                       closing_date, closing_qtr, region_lvl3,
                       acv_total_opp, vat_full, opp_owner_name,
                       sde_primary_engagement_name, flsm, drm_category,
                       ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS rn
                FROM pipeline
                WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND (drm_category = 'Booked/Won' OR opp_phase = 'A - Purchase')
                  AND (vat_dh IS NULL OR vat_dh = '')
                  {closed_filter}
            ) t
            WHERE t.rn = 1
        ),
        dh_involved AS (
            SELECT
                lo.opportunity_id      AS dh_opp_id,
                lo.account_name,
                lo.opp_description     AS dh_opp_description,
                lo.solution_area_l1,
                lo.sub_solution_area_l2 AS dh_sol_l2,
                lo.drm_category        AS dh_drm_category,
                lo.opp_phase           AS dh_opp_phase,
                lo.closing_qtr         AS dh_closing_qtr,
                lo.acv_total_opp       AS dh_acv,
                lo.vat_dh              AS dh_vat_dh,
                STRING_AGG(dv.sales_team_member_id, ', ') AS dh_vat_inumbers
            FROM latest_opps lo
            LEFT JOIN dh_vat dv ON dv.opportunity_id = lo.opportunity_id
            WHERE lo.vat_dh IS NOT NULL AND lo.vat_dh != ''
              {dh_team_filter}
            GROUP BY
                lo.opportunity_id, lo.account_name, lo.opp_description,
                lo.solution_area_l1, lo.sub_solution_area_l2,
                lo.drm_category, lo.opp_phase, lo.closing_qtr,
                lo.acv_total_opp, lo.vat_dh
        )
        SELECT
            c.opportunity_id       AS closed_opp_id,
            c.account_name,
            c.opp_description      AS closed_description,
            c.solution_area_l1     AS closed_sol_l1,
            c.sub_solution_area_l2 AS closed_sol_l2,
            c.closing_date,
            c.closing_qtr,
            c.region_lvl3,
            c.acv_total_opp        AS closed_acv,
            c.vat_full             AS closed_vat_full,
            c.opp_owner_name,
            c.sde_primary_engagement_name,
            c.flsm,
            c.drm_category         AS closed_drm_category,
            d.dh_opp_id,
            d.dh_opp_description,
            d.dh_sol_l2,
            d.dh_drm_category,
            d.dh_opp_phase,
            d.dh_closing_qtr,
            d.dh_acv,
            d.dh_vat_dh,
            d.dh_vat_inumbers,
            CASE WHEN c.sub_solution_area_l2 = d.dh_sol_l2 THEN 1 ELSE 0 END AS same_sol_l2,
            (SELECT COUNT(*) FROM melody WHERE opp_id = c.opportunity_id) AS closed_melody_count,
            (SELECT COUNT(*) FROM melody WHERE opp_id = d.dh_opp_id)     AS dh_melody_count
        FROM closed_no_dh c
        JOIN dh_involved d
          ON d.account_name     = c.account_name
         AND d.dh_opp_id       != c.opportunity_id
         AND d.solution_area_l1 = c.solution_area_l1
        ORDER BY c.closing_date DESC NULLS LAST, c.account_name, c.opportunity_id
    """
    all_params = all_params_combined or None
    rows = query(sql, all_params)
    return rows


@router.get("/bookings/shadow-comments")
def get_shadow_comments(_user=Depends(verify_token)):
    _ensure_shadow_comments_table()
    rows = query(
        "SELECT closed_opp_id, dh_opp_id, comment, updated_by, corregir, "
        "TO_NVARCHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at "
        "FROM shadow_opp_comments",
        None,
    )
    result = {}
    for r in rows:
        key = f"{r['closed_opp_id']}|{r['dh_opp_id']}"
        result[key] = {
            "comment":    r["comment"] or "",
            "updated_at": r["updated_at"] or "",
            "updated_by": r["updated_by"] or "",
            "corregir":   int(r["corregir"] or 0),
        }
    return result


@router.put("/bookings/shadow-comments/{closed_opp_id}/{dh_opp_id}")
def upsert_shadow_comment(
    closed_opp_id: str,
    dh_opp_id: str,
    payload: ShadowComment,
    _user=Depends(verify_token),
):
    _ensure_shadow_comments_table()
    author = _user.get("name", "")
    updated = execute(
        "UPDATE shadow_opp_comments SET comment=?, corregir=?, updated_at=CURRENT_TIMESTAMP, updated_by=? "
        "WHERE closed_opp_id=? AND dh_opp_id=?",
        [payload.comment, payload.corregir, author, closed_opp_id, dh_opp_id],
    )
    if updated == 0:
        execute(
            "INSERT INTO shadow_opp_comments (closed_opp_id, dh_opp_id, comment, corregir, updated_by) VALUES (?,?,?,?,?)",
            [closed_opp_id, dh_opp_id, payload.comment, payload.corregir, author],
        )
    return {"ok": True}


@router.get("/bookings/no-dh")
def get_no_dh(
    closing_qtrs: List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    sol_l1s:      List[str] = Query(default=[]),
    sol_l2s:      List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """All Booked/Won opps where DH had NO presence in the VAT team."""
    clauses = [
        "snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)",
        "(drm_category = 'Booked/Won' OR opp_phase = 'A - Purchase')",
        "(vat_dh IS NULL OR vat_dh = '')",
    ]
    params: list = []
    if closing_qtrs:
        ph = ",".join(["?"] * len(closing_qtrs))
        clauses.append(f"closing_qtr IN ({ph})")
        params.extend(closing_qtrs)
    if regions:
        ph = ",".join(["?"] * len(regions))
        clauses.append(f"region_lvl3 IN ({ph})")
        params.extend(regions)
    if sol_l1s:
        ph = ",".join(["?"] * len(sol_l1s))
        clauses.append(f"solution_area_l1 IN ({ph})")
        params.extend(sol_l1s)
    if sol_l2s:
        ph = ",".join(["?"] * len(sol_l2s))
        clauses.append(f"sub_solution_area_l2 IN ({ph})")
        params.extend(sol_l2s)
    where_str = " AND ".join(clauses)
    sql = f"""
        SELECT
            opportunity_id, account_name, opp_description,
            region_lvl3, solution_area_l1, sub_solution_area_l2,
            closing_date, closing_qtr, acv_total_opp,
            opp_owner_name, vat_full,
            sde_primary_engagement_name, flsm
        FROM (
            SELECT
                opportunity_id, account_name, opp_description,
                region_lvl3, solution_area_l1, sub_solution_area_l2,
                closing_date, closing_qtr, acv_total_opp,
                opp_owner_name, vat_full,
                sde_primary_engagement_name, flsm,
                ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY acv_total_opp DESC) AS rn
            FROM pipeline
            WHERE {where_str}
        ) t
        WHERE t.rn = 1
        ORDER BY closing_date DESC
    """
    rows = query(sql, params or None)
    return rows


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
    engagement_type:  Optional[str] = None,
    digital_sa:       Optional[str] = None,
    region_lvl4s:     List[str] = Query(default=[]),
    sol_l2s:          List[str] = Query(default=[]),
    opp_phases:       List[str] = Query(default=[]),
    qual_decisions:   List[str] = Query(default=[]),
    iss_groups:       List[str] = Query(default=[]),
    flsms:            List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region, sol_l1=sol_l1, drm=drm,
        search=search, engagement_type=engagement_type, digital_sa=digital_sa,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, engagement_types=engagement_types,
        opp_owners=opp_owners, fcs=fcs, digital_sas=digital_sas,
        qual_decisions=qual_decisions, iss_groups=iss_groups, flsms=flsms,
        acc_sub_segments=acc_sub_segments,
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
    iacs: List[str] = Query(default=[]),
    acc_sub_segments: List[str] = Query(default=[]),
    qual_decisions: List[str] = Query(default=[]),
    channel_partners: List[str] = Query(default=[]),
    presales_statuses: List[str] = Query(default=[]),
    iaes: List[str] = Query(default=[]),
    spec_cxs: List[str] = Query(default=[]),
    spec_hxms: List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs: List[str] = Query(default=[]),
    spec_btps: List[str] = Query(default=[]),
    spec_bdcs: List[str] = Query(default=[]),
    spec_fins: List[str] = Query(default=[]),
    spec_pros: List[str] = Query(default=[]),
    deal_type: Optional[str] = None,
    acv_min: Optional[float] = None,
    acv_max: Optional[float] = None,
    dda_statuses: List[str] = Query(default=[]),
    range_agings: List[str] = Query(default=[]),
    priorities: List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
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
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4s=region_lvl4s,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases, fcs=fcs,
        opp_owners=opp_owners, engagement_types=engagement_types, digital_sas=digital_sas,
        local_owners=local_owners, flsms=flsms,
        iss_groups=iss_groups, iacs=iacs, acc_sub_segments=acc_sub_segments,
        qual_decisions=qual_decisions,
        channel_partners=channel_partners,
        presales_statuses=presales_statuses,
        iaes=iaes, spec_cxs=spec_cxs, spec_hxms=spec_hxms, spec_procurements=spec_procurements,
        spec_dscs=spec_dscs, spec_btps=spec_btps, spec_bdcs=spec_bdcs,
        spec_fins=spec_fins, spec_pros=spec_pros,
    )
    where = _apply_deal_type(where, deal_type)
    where = _apply_opp_extra_filters(
        where, params, dda_statuses=dda_statuses, range_agings=range_agings,
        priorities=priorities, probability_min=probability_min, probability_max=probability_max,
    )
    if acv_min is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp >= %s"
        params.append(float(acv_min))
    if acv_max is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp <= %s"
        params.append(float(acv_max))
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


@router.get("/single-bag-breakdown")
def get_single_bag_breakdown(
    closing_qtr:       Optional[str]   = None,
    region:            Optional[str]   = None,
    closing_qtrs:      List[str]       = Query(default=[]),
    regions:           List[str]       = Query(default=[]),
    sol_l1s:           List[str]       = Query(default=[]),
    sol_l2s:           List[str]       = Query(default=[]),
    drms:              List[str]       = Query(default=[]),
    opp_phases:        List[str]       = Query(default=[]),
    opp_owners:        List[str]       = Query(default=[]),
    local_owners:      List[str]       = Query(default=[]),
    flsms:             List[str]       = Query(default=[]),
    iss_groups:        List[str]       = Query(default=[]),
    acc_sub_segments:  List[str]       = Query(default=[]),
    qual_decisions:    List[str]       = Query(default=[]),
    channel_partners:  List[str]       = Query(default=[]),
    presales_statuses: List[str]       = Query(default=[]),
    engagement_types:  List[str]       = Query(default=[]),
    has_comment:       bool            = False,
    acv_min:           Optional[float] = None,
    acv_max:           Optional[float] = None,
    _user=Depends(verify_token),
):
    """Single bag deal counts and ACV by Solution L1, with Finance & Spend sub-breakdown."""
    where, params = _build_where(
        closing_qtr=closing_qtr, region=region,
        closing_qtrs=closing_qtrs, regions=regions, region_lvl4=None,
        sol_l1s=sol_l1s, sol_l2s=sol_l2s,
        drms=drms, opp_phases=opp_phases,
        opp_owners=opp_owners, local_owners=local_owners, flsms=flsms,
        iss_groups=iss_groups, acc_sub_segments=acc_sub_segments,
        qual_decisions=qual_decisions, channel_partners=channel_partners,
        presales_statuses=presales_statuses,
        engagement_types=engagement_types,
    )
    if has_comment:
        cmt_clause = "opportunity_id IN (SELECT DISTINCT opportunity_id FROM comments)"
        where = (where + " AND " + cmt_clause) if where else ("WHERE " + cmt_clause)
    if acv_min is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp >= %s"
        params.append(float(acv_min))
    if acv_max is not None:
        where = (where + " AND " if where else "WHERE ") + "acv_total_opp <= %s"
        params.append(float(acv_max))

    where_single = _apply_deal_type(where, 'single')

    # ── by Solution L1 ──────────────────────────────────────────────────────
    l1_sql = f"""
        SELECT solution_area_l1                    AS label,
               COUNT(DISTINCT opportunity_id)      AS n_opps,
               COALESCE(SUM(acv_usd), 0)           AS acv
        FROM pipeline
        {where_single}
        GROUP BY solution_area_l1
        ORDER BY acv DESC
    """
    by_sol_l1 = [
        {'label': r['label'], 'n_opps': r['n_opps'], 'acv': float(r['acv'])}
        for r in query(l1_sql, params or None)
    ]

    # ── Finance & Spend sub-breakdown ────────────────────────────────────────
    fin_where = where_single + " AND solution_area_l1 = %s"
    fin_params = list(params) + ['Finance and Spend Management']

    fin_sql = f"""
        WITH fin_opps AS (
            SELECT opportunity_id,
                   SUM(acv_usd) AS acv,
                   SUM(CASE WHEN sub_solution_area_l2 = 'Procurement' THEN 1 ELSE 0 END) AS proc_lines,
                   SUM(CASE WHEN sub_solution_area_l2 = 'Fieldglass'  THEN 1 ELSE 0 END) AS fg_lines,
                   COUNT(*) AS total_lines
            FROM pipeline
            {fin_where}
            GROUP BY opportunity_id
        )
        SELECT
            COUNT(*)  AS total_n,
            COALESCE(SUM(acv), 0)  AS total_acv,
            SUM(CASE WHEN proc_lines = total_lines THEN 1 ELSE 0 END)  AS procurement_n,
            COALESCE(SUM(CASE WHEN proc_lines = total_lines THEN acv ELSE 0 END), 0)  AS procurement_acv,
            SUM(CASE WHEN fg_lines = total_lines THEN 1 ELSE 0 END)  AS fieldglass_n,
            COALESCE(SUM(CASE WHEN fg_lines = total_lines THEN acv ELSE 0 END), 0)  AS fieldglass_acv,
            SUM(CASE WHEN proc_lines < total_lines AND fg_lines < total_lines THEN 1 ELSE 0 END)  AS other_n,
            COALESCE(SUM(CASE WHEN proc_lines < total_lines AND fg_lines < total_lines THEN acv ELSE 0 END), 0)  AS other_acv
        FROM fin_opps
    """
    fr = query_one(fin_sql, fin_params) or {}

    return {
        'by_sol_l1': by_sol_l1,
        'finance_breakdown': {
            'total':            {'n_opps': int(fr.get('total_n', 0) or 0),       'acv': float(fr.get('total_acv', 0) or 0)},
            'procurement_only': {'n_opps': int(fr.get('procurement_n', 0) or 0), 'acv': float(fr.get('procurement_acv', 0) or 0)},
            'fieldglass_only':  {'n_opps': int(fr.get('fieldglass_n', 0) or 0),  'acv': float(fr.get('fieldglass_acv', 0) or 0)},
            'other':            {'n_opps': int(fr.get('other_n', 0) or 0),       'acv': float(fr.get('other_acv', 0) or 0)},
        },
    }


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
