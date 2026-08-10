import re
"""Targets and attainment router."""
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from database import query, query_one
from auth import verify_token

router = APIRouter(prefix="/api", tags=["targets"])


def _get_eur_to_usd() -> float:
    """Read EUR→USD rate from fx_rates (inverse of stored USD→EUR rate)."""
    try:
        r = query_one(
            "SELECT rate FROM fx_rates WHERE from_currency='USD' AND to_currency='EUR' "
            "ORDER BY rate_date DESC LIMIT 1"
        )
        if r and r.get("rate"):
            return round(1.0 / float(r["rate"]), 6)
    except Exception:
        pass
    return round(1.0 / 0.8618, 6)  # fallback ~1.1604


@router.get("/targets")
def get_targets(_user=Depends(verify_token)):
    return query("""
        SELECT inumber, nombre_empleado AS name, manager, area, sub_team,
               acv_booked_annual_target, deals_booked_annual_target,
               quota_q1, quota_q2, quota_q3, quota_q4,
               deals_q1, deals_q2, deals_q3, deals_q4
        FROM targets ORDER BY area, nombre_empleado
    """)


@router.get("/attainment/erp")
def erp_attainment(
    areas: List[str] = Query(default=["ERP Cloud BR", "ERP Cloud MCLA", "ERP MX MCLA IB"]),
    quarter: str = "Full Year",
    exclude_enterprise: bool = False,
    include_purchase: bool = False,
    _user=Depends(verify_token),
):
    """Booked ACV vs quota for ERP areas, from pipeline_exploded."""
    q_col = {"Q1": "quota_q1", "Q2": "quota_q2", "Q3": "quota_q3", "Q4": "quota_q4"}.get(quarter, "acv_booked_annual_target")
    d_col = {"Q1": "deals_q1", "Q2": "deals_q2", "Q3": "deals_q3", "Q4": "deals_q4"}.get(quarter, "deals_booked_annual_target")

    eur_to_usd = _get_eur_to_usd()   # quotas stored in EUR → convert to USD for display

    enterprise_filter = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    # A-Purchase: treat opp_phase='A - Purchase' as Booked/Won
    booked_cond = "(p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')" if include_purchase else "p.drm_category = 'Booked/Won'"

    qtr_filter = ""
    qtr_params: list = []
    if quarter != "Full Year":
        qtr_filter = "AND pe.closing_qtr LIKE %s"
        qtr_params = [f"%-{quarter}"]

    placeholders = ",".join(["%s"] * len(areas))
    sql = f"""
        SELECT
            t.inumber,
            t.nombre_empleado              AS name,
            t.area,
            t.{q_col} * 1000 * {eur_to_usd} AS quota_acv,
            t.{d_col}                      AS quota_deals,
            COALESCE(b.booked_acv, 0)      AS booked_acv,
            COALESCE(b.booked_deals, 0)    AS booked_deals,
            CASE WHEN t.{q_col} > 0
                 THEN ROUND(COALESCE(b.booked_acv, 0) / (t.{q_col} * 1000 * {eur_to_usd}) * 100, 1)
                 ELSE 0 END                AS attainment_pct,
            COALESCE(a.active_acv, 0)      AS active_acv,
            COALESCE(a.active_deals, 0)    AS active_deals
        FROM targets t
        LEFT JOIN (
            SELECT pe.sa_inumber,
                   SUM(pe.acv_attributed)            AS booked_acv,
                   COUNT(DISTINCT pe.opportunity_id) AS booked_deals
            FROM pipeline_exploded pe
            WHERE pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id = pe.opportunity_id
                    AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                    AND {booked_cond}
                    {enterprise_filter}
              )
              {qtr_filter}
            GROUP BY pe.sa_inumber
        ) b ON b.sa_inumber = t.inumber
        LEFT JOIN (
            SELECT pe.sa_inumber,
                   SUM(pe.acv_attributed)            AS active_acv,
                   COUNT(DISTINCT pe.opportunity_id) AS active_deals
            FROM pipeline_exploded pe
            WHERE pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id = pe.opportunity_id
                    AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                    AND p.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
                    AND p.fc_qualification IN ('Committed','Probable','Upside')
                    {enterprise_filter}
              )
              {qtr_filter}
            GROUP BY pe.sa_inumber
        ) a ON a.sa_inumber = t.inumber
        WHERE t.area IN ({placeholders})
        ORDER BY t.area, t.nombre_empleado
    """
    return query(sql, qtr_params + qtr_params + areas)


@router.get("/attainment/lobs")
def lobs_attainment(
    sub_teams: List[str] = Query(default=["BAIP", "HCM", "FSM", "CX", "SCM"]),
    quarter: str = "Full Year",
    exclude_enterprise: bool = False,
    include_purchase: bool = False,
    _user=Depends(verify_token),
):
    """Booked ACV vs quota for LoBs sub-teams."""

    q_col = {"Q1": "quota_q1", "Q2": "quota_q2", "Q3": "quota_q3", "Q4": "quota_q4"}.get(quarter, "acv_booked_annual_target")
    d_col = {"Q1": "deals_q1", "Q2": "deals_q2", "Q3": "deals_q3", "Q4": "deals_q4"}.get(quarter, "deals_booked_annual_target")

    eur_to_usd = _get_eur_to_usd()   # quotas stored in EUR → convert to USD

    enterprise_filter = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    booked_cond_p  = "(p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')"  if include_purchase else "p.drm_category = 'Booked/Won'"
    booked_cond_pe = "(p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')"  if include_purchase else "p.drm_category = 'Booked/Won'"

    qtr_filter = ""
    qtr_params: list = []
    if quarter != "Full Year":
        qtr_filter = "AND closing_qtr LIKE %s"
        qtr_params = [f"%-{quarter}"]

    placeholders = ",".join(["%s"] * len(sub_teams))

    # SCM uses pipeline_exploded; other LoBs use direct pipeline lines
    sql = f"""
        WITH scm_booked AS (
            SELECT pe.sa_inumber                     AS sa_inumber,
                   SUM(pe.acv_attributed)            AS booked_acv,
                   COUNT(DISTINCT pe.opportunity_id) AS booked_deals
            FROM pipeline_exploded pe
            WHERE pe.sub_team = 'SCM'
              AND pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id = pe.opportunity_id
                    AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                    AND {booked_cond_pe}
                    {enterprise_filter}
              )
              {qtr_filter.replace('closing_qtr', 'pe.closing_qtr')}
            GROUP BY pe.sa_inumber
        ),
        lobs_booked AS (
            SELECT v.sales_team_member_id            AS sa_inumber,
                   SUM(p.acv_usd)                    AS booked_acv,
                   COUNT(DISTINCT p.opportunity_id)  AS booked_deals
            FROM pipeline p
            JOIN dh_vat v ON v.opportunity_id = p.opportunity_id
            JOIN targets t ON t.inumber = v.sales_team_member_id
            WHERE {booked_cond_p}
              AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND t.area = 'LoBs Team' AND t.sub_team <> 'SCM'
              AND (
                  (t.sub_team = 'HCM'  AND p.solution_area_l1 = 'Human Capital Management')
                  OR (t.sub_team = 'FSM'  AND p.solution_area_l1 = 'Finance and Spend Management'
                                          AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement'))
                  OR (t.sub_team = 'CX'   AND p.solution_area_l1 = 'Customer Experience')
                  OR (t.sub_team = 'BAIP' AND p.solution_area_l1 IN ('BAIP - Data & AI','BAIP - Technology Foundation'))
              )
              {qtr_filter.replace('closing_qtr', 'p.closing_qtr')}
              {enterprise_filter}
            GROUP BY v.sales_team_member_id
        ),
        all_booked AS (
            SELECT sa_inumber, booked_acv, booked_deals FROM scm_booked
            UNION ALL
            SELECT sa_inumber, booked_acv, booked_deals FROM lobs_booked
        ),
        aggregated AS (
            SELECT sa_inumber,
                   SUM(booked_acv)   AS booked_acv,
                   SUM(booked_deals) AS booked_deals
            FROM all_booked GROUP BY sa_inumber
        ),
        scm_active AS (
            SELECT pe.sa_inumber                     AS sa_inumber,
                   SUM(pe.acv_attributed)            AS active_acv,
                   COUNT(DISTINCT pe.opportunity_id) AS active_deals
            FROM pipeline_exploded pe
            WHERE pe.sub_team = 'SCM'
              AND pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id = pe.opportunity_id
                    AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                    AND p.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
                    AND p.fc_qualification IN ('Committed','Probable','Upside')
                    {enterprise_filter}
              )
              {qtr_filter.replace('closing_qtr', 'pe.closing_qtr')}
            GROUP BY pe.sa_inumber
        ),
        lobs_active AS (
            SELECT v.sales_team_member_id            AS sa_inumber,
                   SUM(p.acv_usd)                    AS active_acv,
                   COUNT(DISTINCT p.opportunity_id)  AS active_deals
            FROM pipeline p
            JOIN dh_vat v ON v.opportunity_id = p.opportunity_id
            JOIN targets t ON t.inumber = v.sales_team_member_id
            WHERE p.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
              AND p.fc_qualification IN ('Committed','Probable','Upside')
              AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND t.area = 'LoBs Team' AND t.sub_team <> 'SCM'
              AND (
                  (t.sub_team = 'HCM'  AND p.solution_area_l1 = 'Human Capital Management')
                  OR (t.sub_team = 'FSM'  AND p.solution_area_l1 = 'Finance and Spend Management'
                                          AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement'))
                  OR (t.sub_team = 'CX'   AND p.solution_area_l1 = 'Customer Experience')
                  OR (t.sub_team = 'BAIP' AND p.solution_area_l1 IN ('BAIP - Data & AI','BAIP - Technology Foundation'))
              )
              {qtr_filter.replace('closing_qtr', 'p.closing_qtr')}
              {enterprise_filter}
            GROUP BY v.sales_team_member_id
        ),
        all_active AS (
            SELECT sa_inumber, active_acv, active_deals FROM scm_active
            UNION ALL
            SELECT sa_inumber, active_acv, active_deals FROM lobs_active
        ),
        aggregated_active AS (
            SELECT sa_inumber,
                   SUM(active_acv)   AS active_acv,
                   SUM(active_deals) AS active_deals
            FROM all_active GROUP BY sa_inumber
        )
        SELECT
            t.inumber,
            t.nombre_empleado              AS name,
            t.sub_team,
            t.{q_col} * 1000 * {eur_to_usd} AS quota_acv,
            t.{d_col}                      AS quota_deals,
            COALESCE(b.booked_acv, 0)      AS booked_acv,
            COALESCE(b.booked_deals, 0)    AS booked_deals,
            CASE WHEN t.{q_col} > 0
                 THEN ROUND(COALESCE(b.booked_acv, 0) / (t.{q_col} * 1000 * {eur_to_usd}) * 100, 1)
                 ELSE 0 END                AS attainment_pct,
            COALESCE(a.active_acv, 0)      AS active_acv,
            COALESCE(a.active_deals, 0)    AS active_deals
        FROM targets t
        LEFT JOIN aggregated b ON b.sa_inumber = t.inumber
        LEFT JOIN aggregated_active a ON a.sa_inumber = t.inumber
        WHERE t.area = 'LoBs Team' AND t.sub_team IN ({placeholders})
        ORDER BY t.sub_team, t.nombre_empleado
    """
    # qtr_params appears twice in the SQL (scm + lobs for booked, scm + lobs for active)
    return query(sql, qtr_params + qtr_params + qtr_params + qtr_params + sub_teams)


@router.get("/attainment/deals")
def attainment_deals(
    sa_inumber: str,
    quarter: str = "Full Year",
    exclude_enterprise: bool = False,
    team_type: str = "erp",   # "erp" | "lobs"
    _user=Depends(verify_token),
):
    """
    Return individual deals counting toward a SA's attainment.
    ERP/SCM: from pipeline_exploded (acv_attributed = acv / vat_size).
    LoBs (non-SCM): from pipeline JOIN dh_vat JOIN targets (full acv_usd per line).
    """
    qtr_filter_p  = ""   # uses p.closing_qtr
    qtr_filter_pe = ""   # uses pe.closing_qtr
    qtr_params: list = []
    if quarter != "Full Year":
        qtr_filter_p  = "AND p.closing_qtr LIKE %s"
        qtr_filter_pe = "AND pe.closing_qtr LIKE %s"
        qtr_params = [f"%-{quarter}"]

    ent_filter = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    # Determine if this SA is in pipeline_exploded (ERP or SCM) or LoBs-direct
    in_exploded = query_one(
        "SELECT 1 FROM pipeline_exploded WHERE sa_inumber = %s LIMIT 1",
        [sa_inumber]
    )

    if in_exploded:
        sql = f"""
            SELECT opportunity_id, account_name, opp_description, closing_qtr,
                   drm_category, segment, region, solution_area_l1, sub_solution_area_l2,
                   vat_dh, vat_full, vat_size, acv_line_usd, acv_attributed, acv_total_opp, sub_team, area
            FROM (
                SELECT pe.opportunity_id, p.account_name, p.opp_description, pe.closing_qtr,
                    p.drm_category, p.iss_group_text AS segment, p.region_lvl3 AS region,
                    pe.solution_area_l1, pe.sub_solution_area_l2,
                    p.vat_dh, p.vat_full, p.vat_size,
                    pe.acv_usd_line AS acv_line_usd, pe.acv_attributed AS acv_attributed,
                    p.acv_total_opp AS acv_total_opp, pe.sub_team, pe.area,
                    ROW_NUMBER() OVER (PARTITION BY pe.opportunity_id, pe.solution_area_l1, pe.sub_solution_area_l2 ORDER BY pe.acv_attributed DESC) AS _rn
                FROM pipeline_exploded pe
                JOIN pipeline p ON p.opportunity_id = pe.opportunity_id
                               AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                               AND p.solution_area_l1 = pe.solution_area_l1
                               AND p.sub_solution_area_l2 = pe.sub_solution_area_l2
                WHERE pe.sa_inumber = %s
                  AND p.drm_category = 'Booked/Won'
                  AND pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  {qtr_filter_pe}
                  {ent_filter}
            ) _d WHERE _rn = 1
        """
        params = [sa_inumber] + qtr_params
    else:
        sa_info = query_one("SELECT sub_team FROM targets WHERE inumber = %s", [sa_inumber])
        sub_team = sa_info["sub_team"] if sa_info else None

        lob_l1_filter = {
            "HCM":  "AND p.solution_area_l1 = 'Human Capital Management'",
            "CX":   "AND p.solution_area_l1 = 'Customer Experience'",
            "FSM":  "AND p.solution_area_l1 = 'Finance and Spend Management' AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement')",
            "BAIP": "AND p.solution_area_l1 IN ('BAIP - Data & AI','BAIP - Technology Foundation')",
            "SCM":  "AND p.solution_area_l1 = 'Supply Chain Management'",
        }.get(sub_team or "", "")

        sql = f"""
            SELECT opportunity_id, account_name, opp_description, closing_qtr,
                   drm_category, segment, region, solution_area_l1, sub_solution_area_l2,
                   vat_dh, vat_full, vat_size, acv_line_usd, acv_attributed, acv_total_opp, sub_team, area
            FROM (
                SELECT p.opportunity_id, p.account_name, p.opp_description, p.closing_qtr,
                    p.drm_category, p.iss_group_text AS segment, p.region_lvl3 AS region,
                    p.solution_area_l1, p.sub_solution_area_l2,
                    p.vat_dh, p.vat_full, p.vat_size,
                    p.acv_usd AS acv_line_usd, p.acv_usd AS acv_attributed,
                    p.acv_total_opp AS acv_total_opp,
                    %s AS sub_team, 'LoBs Team' AS area,
                    ROW_NUMBER() OVER (PARTITION BY p.opportunity_id, p.solution_area_l1, p.sub_solution_area_l2 ORDER BY p.acv_usd DESC) AS _rn
                FROM pipeline p
                JOIN dh_vat v ON v.opportunity_id = p.opportunity_id
                JOIN targets t ON t.inumber = v.sales_team_member_id
                WHERE v.sales_team_member_id = %s
                  AND p.drm_category = 'Booked/Won'
                  AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND t.area = 'LoBs Team'
                  {lob_l1_filter}
                  {qtr_filter_p}
                  {ent_filter}
            ) _d WHERE _rn = 1
        """
        params = [sub_team or "", sa_inumber] + qtr_params

    return query(sql, params)


@router.get("/attainment/deals-export")
def deals_export(
    team_type: str = "erp",          # "erp" | "lobs"
    areas: List[str] = Query(default=[]),
    sub_teams: List[str] = Query(default=[]),
    quarter: str = "Full Year",
    exclude_enterprise: bool = False,
    include_purchase: bool = False,
    _user=Depends(verify_token),
):
    """
    Full deals list for Excel export — all SAs, all quarters.
    Returns one row per (opportunity_id, solution_area_l1, sub_solution_area_l2, sa_inumber).
    """
    from fastapi.responses import StreamingResponse
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    ent_filter = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    booked_cond_p  = "(p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')" if include_purchase else "p.drm_category = 'Booked/Won'"
    booked_cond_pe = "(p.drm_category = 'Booked/Won' OR p.opp_phase = 'A - Purchase')" if include_purchase else "p.drm_category = 'Booked/Won'"

    qtr_filter_pe = ""
    qtr_filter_p  = ""
    qtr_params: list = []
    if quarter != "Full Year":
        qtr_filter_pe = "AND pe.closing_qtr LIKE %s"
        qtr_filter_p  = "AND p.closing_qtr LIKE %s"
        qtr_params = [f"%-{quarter}"]

    if team_type == "erp":
        if not areas:
            areas = ["ERP Cloud BR", "ERP Cloud MCLA", "ERP MX MCLA IB"]
        placeholders = ",".join(["%s"] * len(areas))
        sql = f"""
            SELECT
                pe.closing_qtr,
                pe.sa_name                  AS sa_nome,
                pe.area                     AS team_area,
                pe.sub_team,
                pe.sa_inumber,
                p.opportunity_id,
                p.account_name,
                p.opp_description,
                p.opp_owner_name,
                p.region_lvl3               AS region,
                p.solution_area_l1,
                p.sub_solution_area_l2,
                p.drm_category,
                p.opp_phase,
                p.iss_group_text            AS segment,
                p.acv_total_opp             AS acv_total_opp_usd,
                p.vat_size,
                p.vat_dh,
                p.account_sales_group_text  AS sales_group,
                pe.acv_usd_line             AS acv_linha_usd,
                pe.acv_attributed           AS acv_atribuido_usd
            FROM pipeline_exploded pe
            JOIN pipeline p
              ON p.opportunity_id     = pe.opportunity_id
             AND p.solution_area_l1   = pe.solution_area_l1
             AND p.sub_solution_area_l2 = pe.sub_solution_area_l2
             AND p.snapshot_week      = (SELECT MAX(snapshot_week) FROM pipeline)
             AND {booked_cond_pe}
             {ent_filter}
            WHERE pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.area IN ({placeholders})
              {qtr_filter_pe}
            ORDER BY pe.closing_qtr, pe.area, pe.sa_name, p.account_name
        """
        params = areas + qtr_params

    else:  # lobs
        if not sub_teams:
            sub_teams = ["BAIP", "HCM", "FSM", "CX", "SCM"]
        sub_placeholders = ",".join(["%s"] * len(sub_teams))

        # SCM via pipeline_exploded; other LoBs via pipeline + dh_vat
        sql = f"""
            WITH scm_deals AS (
                SELECT
                    pe.closing_qtr,
                    pe.sa_name                  AS sa_nome,
                    'LoBs Team'                 AS team_area,
                    pe.sub_team,
                    pe.sa_inumber,
                    p.opportunity_id,
                    p.account_name,
                    p.opp_description,
                    p.opp_owner_name,
                    p.region_lvl3               AS region,
                    p.solution_area_l1,
                    p.sub_solution_area_l2,
                    p.drm_category,
                    p.opp_phase,
                    p.iss_group_text            AS segment,
                    p.acv_total_opp             AS acv_total_opp_usd,
                    p.vat_size,
                    p.vat_dh,
                    p.account_sales_group_text  AS sales_group,
                    pe.acv_usd_line             AS acv_linha_usd,
                    pe.acv_attributed           AS acv_atribuido_usd
                FROM pipeline_exploded pe
                JOIN pipeline p
                  ON p.opportunity_id       = pe.opportunity_id
                 AND p.solution_area_l1     = pe.solution_area_l1
                 AND p.sub_solution_area_l2 = pe.sub_solution_area_l2
                 AND p.snapshot_week        = (SELECT MAX(snapshot_week) FROM pipeline)
                 AND {booked_cond_pe}
                 {ent_filter}
                WHERE pe.sub_team = 'SCM'
                  AND pe.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  {qtr_filter_pe}
            ),
            lobs_deals AS (
                SELECT
                    p.closing_qtr,
                    t.nombre_empleado           AS sa_nome,
                    'LoBs Team'                 AS team_area,
                    t.sub_team,
                    t.inumber                   AS sa_inumber,
                    p.opportunity_id,
                    p.account_name,
                    p.opp_description,
                    p.opp_owner_name,
                    p.region_lvl3               AS region,
                    p.solution_area_l1,
                    p.sub_solution_area_l2,
                    p.drm_category,
                    p.opp_phase,
                    p.iss_group_text            AS segment,
                    p.acv_total_opp             AS acv_total_opp_usd,
                    p.vat_size,
                    p.vat_dh,
                    p.account_sales_group_text  AS sales_group,
                    p.acv_usd                   AS acv_linha_usd,
                    p.acv_usd                   AS acv_atribuido_usd
                FROM pipeline p
                JOIN dh_vat v ON v.opportunity_id = p.opportunity_id
                JOIN targets t ON t.inumber = v.sales_team_member_id
                WHERE {booked_cond_p}
                  AND p.snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
                  AND t.area = 'LoBs Team' AND t.sub_team <> 'SCM'
                  AND (
                      (t.sub_team = 'HCM'  AND p.solution_area_l1 = 'Human Capital Management')
                      OR (t.sub_team = 'FSM' AND p.solution_area_l1 = 'Finance and Spend Management'
                                              AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement'))
                      OR (t.sub_team = 'CX'  AND p.solution_area_l1 = 'Customer Experience')
                      OR (t.sub_team = 'BAIP' AND p.solution_area_l1 IN ('BAIP - Data & AI','BAIP - Technology Foundation'))
                  )
                  {qtr_filter_p}
                  {ent_filter}
            )
            SELECT * FROM scm_deals  WHERE sub_team IN ({sub_placeholders})
            UNION ALL
            SELECT * FROM lobs_deals WHERE sub_team IN ({sub_placeholders})
            ORDER BY closing_qtr, sub_team, sa_nome, account_name
        """
        params = qtr_params + sub_teams + qtr_params + sub_teams

    rows = query(sql, params)

    # ── Build Excel ────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Deals"

    hdr_font  = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="0070F2")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin      = Side(style="thin", color="E1E2E6")
    bdr       = Border(bottom=thin, right=thin)
    dat_font  = Font(name="Calibri", size=10)
    num_fmt   = '#,##0.00'
    int_fmt   = '#,##0'

    headers = [
        ("Closing Qtr",        14),
        ("SA Nome",            22),
        ("Team / Area",        18),
        ("Sub-Team",           12),
        ("I-Number",           12),
        ("Opportunity ID",     18),
        ("Account",            30),
        ("Description",        40),
        ("Opp Owner",          22),
        ("Region",             16),
        ("Solution L1",        22),
        ("Sub-Solution L2",    24),
        ("DRM Category",       16),
        ("Opp Phase",          16),
        ("Segment",            14),
        ("Sales Group",        24),
        ("ACV Total Opp (USD)", 20),
        ("VAT Size",           10),
        ("VAT DH",             30),
        ("ACV Linha (USD)",    18),
        ("ACV Atribuído (USD)", 20),
    ]

    fields = [
        "closing_qtr","sa_nome","team_area","sub_team","sa_inumber",
        "opportunity_id","account_name","opp_description","opp_owner_name",
        "region","solution_area_l1","sub_solution_area_l2","drm_category","opp_phase","segment",
        "sales_group","acv_total_opp_usd","vat_size","vat_dh","acv_linha_usd","acv_atribuido_usd",
    ]
    numeric_fields = {"acv_total_opp_usd","acv_linha_usd","acv_atribuido_usd"}
    int_fields     = {"vat_size"}

    for col, (label, width) in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=label)
        c.font = hdr_font; c.fill = hdr_fill
        c.alignment = hdr_align; c.border = bdr
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    fill_even = PatternFill("solid", fgColor="F0F4FF")
    fill_odd  = PatternFill("solid", fgColor="FFFFFF")

    for ri, row in enumerate(rows, 2):
        fill = fill_even if ri % 2 == 0 else fill_odd
        for ci, field in enumerate(fields, 1):
            val = row.get(field)
            c = ws.cell(row=ri, column=ci, value=float(val) if val is not None and field in numeric_fields else
                                                  int(val)   if val is not None and field in int_fields    else val)
            c.font = dat_font; c.fill = fill; c.border = bdr
            if field in numeric_fields:
                c.number_format = num_fmt
                c.alignment = Alignment(horizontal="right")
            elif field in int_fields:
                c.number_format = int_fmt
                c.alignment = Alignment(horizontal="center")

    # Auto-filter
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    qtr_label = quarter.replace(" ", "_")
    filename = f"deals_booked_{team_type}_{qtr_label}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── DH Manager Report ────────────────────────────────────────────────────────



@router.get("/attainment/manager")
def manager_attainment(
    quarter: str = "Full Year",
    include_purchase: bool = False,
    exclude_enterprise: bool = False,
    _user=Depends(verify_token),
):
    """
    DH Manager Report — single-pass aggregation per team.
    Uses one CTE query per team to get all quarter breakdowns at once.
    """
    eur_to_usd = _get_eur_to_usd()   # quotas stored in EUR → convert to USD
    booked_p  = "(p.drm_category='Booked/Won' OR p.opp_phase='A - Purchase')" if include_purchase else "p.drm_category='Booked/Won'"
    ent_f     = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""
    qtr_cond  = f"AND p.closing_qtr = ANY(%s)" if quarter != "Full Year" else ""
    qtr_vals  = [[f"%-{quarter}"]] if quarter != "Full Year" else None  # unused when Full Year

    LOB_MAP = {
        "BAIP": ["BAIP - Data & AI", "BAIP - Technology Foundation"],
        "HCM":  ["Human Capital Management"],
        "FSM":  ["Finance and Spend Management"],
        "CX":   ["Customer Experience"],
    }
    # FSM only counts Fieldglass + Procurement (Attribution Rules Req 4.3/4.5)
    LOB_SUB_FILTER = {
        "FSM": "AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement')",
    }

    # ── Single-pass ERP booked: all quarters in one CTE query ─────────────
    def erp_all_qtrs(area: str):
        """Returns {quarter: {acv, deals}, 'Full Year': {...}}"""
        rows = query(f"""
            SELECT pe.closing_qtr AS closing_qtr,
                   SUM(pe.acv_attributed)            AS acv,
                   COUNT(DISTINCT pe.opportunity_id) AS deals
            FROM pipeline_exploded pe
            WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.area=%s
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id=pe.opportunity_id
                    AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                    AND {booked_p} {ent_f}
              )
            GROUP BY closing_qtr
        """, [area])
        result = {}
        fy_acv = 0; fy_deals = 0
        for r in rows:
            _m = re.search(r"Q[1-4]", r["closing_qtr"] or ""); q = _m.group(0) if _m else ""
            if q in ("Q1","Q2","Q3","Q4"):
                result[q] = {"acv": float(r["acv"] or 0), "deals": int(r["deals"] or 0)}
                fy_acv   += float(r["acv"] or 0)
                fy_deals += int(r["deals"] or 0)
        for q in ["Q1","Q2","Q3","Q4"]:
            result.setdefault(q, {"acv": 0, "deals": 0})
        result["Full Year"] = {"acv": fy_acv, "deals": fy_deals}
        return result

    def erp_active(area: str):
        r = query_one(f"""
            SELECT COALESCE(SUM(pe.acv_attributed),0) AS acv,
                   COUNT(DISTINCT pe.opportunity_id)  AS deals
            FROM pipeline_exploded pe
            WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.area=%s
              AND EXISTS (
                  SELECT 1 FROM pipeline p
                  WHERE p.opportunity_id=pe.opportunity_id
                    AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                    AND p.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
              )
        """, [area])
        return float(r["acv"] or 0), int(r["deals"] or 0)

    # ── Single-pass LoB booked: all sub-teams + quarters at once ──────────
    def lob_all_qtrs():
        """Returns {sub_team: {quarter: {acv, deals}}}"""
        # One query per sub-team (Full Value, DISTINCT per opp+sol per sub-team)
        combined = {}
        for st, sols in LOB_MAP.items():
            sol_ph = ",".join(["%s"]*len(sols))
            sub_filter = LOB_SUB_FILTER.get(st, "")
            rows = query(f"""
                SELECT closing_qtr AS q,
                       SUM(acv) AS acv,
                       COUNT(DISTINCT opportunity_id) AS deals
                FROM (
                    SELECT p.opportunity_id,
                        MAX(p.closing_qtr) AS closing_qtr,
                        SUM(p.acv_usd) AS acv
                    FROM pipeline p
                    WHERE p.solution_area_l1 IN ({sol_ph})
                      AND {booked_p} {ent_f}
                      AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                      AND EXISTS (
                          SELECT 1 FROM dh_vat v
                          JOIN targets t ON t.inumber=v.sales_team_member_id
                          WHERE v.opportunity_id=p.opportunity_id
                            AND t.area='LoBs Team' AND t.sub_team=%s
                      )
                      {sub_filter}
                    GROUP BY p.opportunity_id
                ) x
                GROUP BY closing_qtr
            """, sols + [st])
            by_q = {}
            fy_acv = 0; fy_deals = 0
            for r in rows:
                _m = re.search(r"Q[1-4]", r["q"] or ""); q = _m.group(0) if _m else ""
                if q in ("Q1","Q2","Q3","Q4"):
                    by_q[q] = {"acv": float(r["acv"] or 0), "deals": int(r["deals"] or 0)}
                    fy_acv   += float(r["acv"] or 0)
                    fy_deals += int(r["deals"] or 0)
            for q in ["Q1","Q2","Q3","Q4"]:
                by_q.setdefault(q, {"acv": 0, "deals": 0})
            by_q["Full Year"] = {"acv": fy_acv, "deals": fy_deals}
            combined[st] = by_q

        # SCM via pipeline_exploded
        scm_rows = query(f"""
            SELECT closing_qtr AS q,
                   SUM(attributed) AS acv, COUNT(DISTINCT opportunity_id) AS deals
            FROM (
                SELECT DISTINCT pe.opportunity_id, pe.acv_usd_line,
                       pe.acv_attributed AS attributed,
                       pe.closing_qtr AS closing_qtr
                FROM pipeline_exploded pe
                JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
                  AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  AND pe.sub_team='SCM' AND {booked_p} {ent_f}
            ) x GROUP BY closing_qtr
        """)
        scm_by_q = {}; fy_acv = 0; fy_deals = 0
        for r in scm_rows:
            _m = re.search(r"Q[1-4]", r["q"] or ""); q = _m.group(0) if _m else ""
            if q in ("Q1","Q2","Q3","Q4"):
                scm_by_q[q] = {"acv": float(r["acv"] or 0), "deals": int(r["deals"] or 0)}
                fy_acv += float(r["acv"] or 0); fy_deals += int(r["deals"] or 0)
        for q in ["Q1","Q2","Q3","Q4"]: scm_by_q.setdefault(q, {"acv": 0, "deals": 0})
        scm_by_q["Full Year"] = {"acv": fy_acv, "deals": fy_deals}
        combined["SCM"] = scm_by_q
        return combined

    def lob_active():
        r = query_one(f"""
            SELECT COALESCE(SUM(DISTINCT p.acv_usd),0) AS acv,
                   COUNT(DISTINCT p.opportunity_id) AS deals
            FROM pipeline p JOIN dh_vat v ON v.opportunity_id=p.opportunity_id
            JOIN targets t ON t.inumber=v.sales_team_member_id
            WHERE t.area='LoBs Team'
              AND p.drm_category NOT IN ('Booked/Won','Disc/Lost','Excluded from Pipeline')
              AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
        """)
        return float(r["acv"] or 0), int(r["deals"] or 0)

    # ── Fetch targets once ─────────────────────────────────────────────────
    q_col = {"Q1":"quota_q1","Q2":"quota_q2","Q3":"quota_q3","Q4":"quota_q4"}.get(quarter,"acv_booked_annual_target")
    d_col = {"Q1":"deals_q1","Q2":"deals_q2","Q3":"deals_q3","Q4":"deals_q4"}.get(quarter,"deals_booked_annual_target")

    # Manager individual quotas — stored in EUR, convert to USD
    tgts  = {r["inumber"]: r for r in query(f"""
        SELECT inumber, nombre_empleado,
               {q_col}*1000*{eur_to_usd} AS quota_acv, {d_col} AS quota_deals,
               quota_q1*1000*{eur_to_usd} AS q1, quota_q2*1000*{eur_to_usd} AS q2,
               quota_q3*1000*{eur_to_usd} AS q3, quota_q4*1000*{eur_to_usd} AS q4,
               acv_booked_annual_target*1000*{eur_to_usd} AS annual_target
        FROM targets WHERE inumber IN ('I816087','I813885','I564477','I856235')
    """)}

    # Area-level quotas from Manager Targets sheet — stored in EUR, convert to USD
    mgr_quota_col = {"Q1":"quota_q1","Q2":"quota_q2","Q3":"quota_q3","Q4":"quota_q4"}.get(quarter, "quota_eur")
    area_quota_rows = query(f"""
        SELECT area_inumber, area_label,
               {mgr_quota_col}*1000*{eur_to_usd}  AS quota_acv,
               quota_deals,
               quota_q1*1000*{eur_to_usd} AS q1, quota_q2*1000*{eur_to_usd} AS q2,
               quota_q3*1000*{eur_to_usd} AS q3, quota_q4*1000*{eur_to_usd} AS q4
        FROM manager_targets
    """)
    # Map by area_label for easy lookup
    area_quotas = {r["area_label"]: r for r in area_quota_rows}
    # LoBs manager row also in manager_targets (I816087 → LoBs Team)
    lobs_quota = area_quotas.get("LoBs Team", {})

    managers = [
        {"inum": "I816087", "name": "Rodrigo Ribeiro",  "label": "LoBs Team",       "area": "LOBS"},
        {"inum": "I813885", "name": "Rafael Morcelli",  "label": "ERP Cloud BR",    "area": "ERP Cloud BR"},
        {"inum": "I564477", "name": "Marmileth Olmos",  "label": "ERP Cloud MCLA",  "area": "ERP Cloud MCLA"},
        {"inum": "I856235", "name": "Carlos Flores",     "label": "ERP MX MCLA IB", "area": "ERP MX MCLA IB"},
    ]

    # ── Compute all in parallel order (LoBs = 5 queries, ERP = 1 each) ────
    lob_data = lob_all_qtrs()
    erp_data = {m["area"]: erp_all_qtrs(m["area"]) for m in managers if m["area"] != "LOBS"}
    lob_act  = lob_active()
    erp_acts = {m["area"]: erp_active(m["area"]) for m in managers if m["area"] != "LOBS"}

    results = []
    grand_acv = 0; grand_quota = 0; grand_deals = 0

    for m in managers:
        t = tgts.get(m["inum"], {})

        if m["area"] == "LOBS":
            # LoBs: quota from Manager Targets sheet
            lq = lobs_quota
            quota_acv   = float(lq.get("quota_acv") or 0)
            quota_deals = int(lq.get("quota_deals") or 0)
            fy_acv = sum(lob_data[st]["Full Year"]["acv"] for st in lob_data)
            fy_deals = sum(lob_data[st]["Full Year"]["deals"] for st in lob_data)
            act_acv, act_deals = lob_act
            bd = []
            for q in ["Q1","Q2","Q3","Q4"]:
                q_acv   = sum(lob_data[st][q]["acv"]   for st in lob_data)
                q_deals = sum(lob_data[st][q]["deals"] for st in lob_data)
                q_quota = float(lq.get(q.lower()) or 0)
                bd.append({"quarter":q, "quota_acv":q_quota, "booked_acv":q_acv,
                           "attainment_pct": round(q_acv/q_quota*100,1) if q_quota>0 else 0})
        else:
            # ERP: use area-level quota sum (all SAs), not manager individual quota
            aq = area_quotas.get(m["area"], {})
            quota_acv   = float(aq.get("quota_acv") or 0)
            quota_deals = int(aq.get("quota_deals") or 0)
            d = erp_data[m["area"]]
            fy_acv   = d["Full Year"]["acv"];  fy_deals = d["Full Year"]["deals"]
            act_acv, act_deals = erp_acts[m["area"]]
            bd = []
            for q in ["Q1","Q2","Q3","Q4"]:
                q_acv   = d[q]["acv"]
                q_quota = float(aq.get(q.lower()) or 0)
                bd.append({"quarter":q, "quota_acv":q_quota, "booked_acv":q_acv,
                           "attainment_pct": round(q_acv/q_quota*100,1) if q_quota>0 else 0})

        att = round(fy_acv / quota_acv * 100, 1) if quota_acv > 0 else 0
        results.append({
            "inumber": m["inum"], "name": m["name"], "label": m["label"],
            "quota_acv": quota_acv, "quota_deals": quota_deals,
            "booked_acv": fy_acv, "booked_deals": fy_deals,
            "attainment_pct": att,
            "active_acv": act_acv, "active_deals": act_deals,
            "breakdown": bd,
        })
        grand_acv   += fy_acv
        grand_quota += quota_acv
        grand_deals += fy_deals

    return {
        "title":   "DH Manager Report — LAC",
        "manager": "Rodrigo Ribeiro Ferreira",
        "rows":    results,
        "grand": {
            "quota_acv":      grand_quota,
            "booked_acv":     grand_acv,
            "booked_deals":   grand_deals,
            "attainment_pct": round(grand_acv/grand_quota*100,1) if grand_quota>0 else 0,
        },
    }



@router.get("/attainment/manager/detail")
def manager_attainment_detail(
    team_area: str,          # 'ERP Cloud BR' | 'ERP Cloud MCLA' | 'ERP MX MCLA IB' | 'LoBs Team'
    sub_team: str = "",      # for LoBs: 'BAIP' | 'HCM' | 'FSM' | 'CX' | 'SCM'
    quarter: str = "Full Year",
    include_purchase: bool = False,
    exclude_enterprise: bool = False,
    _user=Depends(verify_token),
):
    """Drill-down: list of booked opportunities for a given team/sub-team."""
    booked_p  = "(p.drm_category='Booked/Won' OR p.opp_phase='A - Purchase')" if include_purchase else "p.drm_category='Booked/Won'"
    ent_f     = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""
    qtr_f     = f"AND p.closing_qtr LIKE '%-{quarter}'" if quarter != "Full Year" else ""
    pe_qtr_f  = f"AND pe.closing_qtr LIKE '%-{quarter}'" if quarter != "Full Year" else ""

    LOB_MAP = {
        "BAIP": ["BAIP - Data & AI", "BAIP - Technology Foundation"],
        "HCM":  ["Human Capital Management"],
        "FSM":  ["Finance and Spend Management"],
        "CX":   ["Customer Experience"],
    }
    # FSM only counts Fieldglass + Procurement (Attribution Rules Req 4.3/4.5)
    LOB_SUB_FILTER = {
        "FSM": "AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement')",
    }

    if team_area == "LoBs Team":
        if sub_team == "SCM":
            # SCM: ÷ VAT_Size via pipeline_exploded, DISTINCT per (opp, line)
            rows = query(f"""
                SELECT DISTINCT pe.opportunity_id, p.account_name, p.opp_owner_name,
                       p.solution_area_l1, p.sub_solution_area_l2,
                       p.closing_qtr, p.iss_group_text, p.region_lvl3,
                       pe.acv_attributed AS acv_recognized,
                       pe.acv_usd_line   AS acv_line,
                       pe.vat_size,
                       string_agg(DISTINCT pe.sa_name, ', ') OVER (PARTITION BY pe.opportunity_id, pe.acv_usd_line) AS team_members
                FROM pipeline_exploded pe
                JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
                  AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
                  AND pe.sub_team='SCM' AND {booked_p} {ent_f} {pe_qtr_f}
                ORDER BY pe.opportunity_id, pe.acv_usd_line DESC
            """)
        elif sub_team in LOB_MAP:
            sol_list = LOB_MAP[sub_team]
            sol_ph = ",".join(["%s"]*len(sol_list))
            params = [sub_team] + sol_list
            # Full Value, DISTINCT per opp
            rows = query(f"""
                SELECT DISTINCT ON (p.opportunity_id, p.solution_area_l1)
                       p.opportunity_id, p.account_name, p.opp_owner_name,
                       p.solution_area_l1, p.sub_solution_area_l2,
                       p.closing_qtr, p.iss_group_text, p.region_lvl3,
                       p.acv_usd          AS acv_recognized,
                       p.acv_usd          AS acv_line,
                       p.vat_size,
                       (SELECT string_agg(DISTINCT t2.nombre_empleado, ', ')
                        FROM dh_vat v2 JOIN targets t2 ON t2.inumber=v2.sales_team_member_id
                        WHERE v2.opportunity_id=p.opportunity_id AND t2.sub_team=%s
                       ) AS team_members
                FROM pipeline p
                JOIN dh_vat v  ON v.opportunity_id=p.opportunity_id
                JOIN targets t ON t.inumber=v.sales_team_member_id
                WHERE t.area='LoBs Team' AND t.sub_team=%s
                  AND p.solution_area_l1 IN ({sol_ph})
                  AND {booked_p} {ent_f} {qtr_f}
                  AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                ORDER BY p.opportunity_id, p.solution_area_l1, p.acv_usd DESC NULLS LAST
            """, params)
        else:
            rows = []
    else:
        # ERP teams: one row per (opp, SA, solution) — matches SUM logic in erp_all_qtrs
        rows = query(f"""
            SELECT DISTINCT ON (pe.opportunity_id, pe.sa_inumber, pe.solution_area_l1)
                   pe.opportunity_id, p.account_name, p.opp_owner_name,
                   pe.solution_area_l1, p.sub_solution_area_l2,
                   p.closing_qtr, p.iss_group_text, p.region_lvl3,
                   pe.acv_attributed AS acv_recognized,
                   pe.acv_usd_line   AS acv_line,
                   pe.vat_size,
                   pe.sa_name        AS team_members
            FROM pipeline_exploded pe
            JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
              AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
            WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.area=%s AND {booked_p} {ent_f} {pe_qtr_f}
            ORDER BY pe.opportunity_id, pe.sa_inumber, pe.solution_area_l1
        """, [team_area, team_area])

    result = []
    for r in rows:
        rec = dict(r)
        for k in ['acv_recognized','acv_line']:
            rec[k] = float(rec.get(k) or 0)
        result.append(rec)
    return result


@router.get("/attainment/manager/subtotals")
def manager_lob_subtotals(
    include_purchase: bool = False,
    exclude_enterprise: bool = False,
    _user=Depends(verify_token),
):
    """Per-sub-team per-quarter — single grouped query per sub-team (no sequential quarter loops)."""
    booked_p = "(p.drm_category='Booked/Won' OR p.opp_phase='A - Purchase')" if include_purchase else "p.drm_category='Booked/Won'"
    ent_f    = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    LOB_MAP = {
        "BAIP": ["BAIP - Data & AI", "BAIP - Technology Foundation"],
        "HCM":  ["Human Capital Management"],
        "FSM":  ["Finance and Spend Management"],
        "CX":   ["Customer Experience"],
    }
    # FSM only counts Fieldglass + Procurement (Attribution Rules Req 4.3/4.5)
    LOB_SUB_FILTER = {
        "FSM": "AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement')",
    }

    result = {}
    for st, sols in LOB_MAP.items():
        sol_ph = ",".join(["%s"] * len(sols))
        sub_filter = LOB_SUB_FILTER.get(st, "")
        rows = query(f"""
            SELECT closing_qtr AS q,
                   SUM(acv) AS acv, COUNT(DISTINCT opportunity_id) AS deals
            FROM (
                SELECT p.opportunity_id,
                       MAX(p.closing_qtr) AS closing_qtr,
                       SUM(p.acv_usd) AS acv
                FROM pipeline p
                WHERE p.solution_area_l1 IN ({sol_ph})
                  AND {booked_p} {ent_f}
                  AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                  AND EXISTS (
                      SELECT 1 FROM dh_vat v
                      JOIN targets t ON t.inumber=v.sales_team_member_id
                      WHERE v.opportunity_id=p.opportunity_id
                        AND t.area='LoBs Team' AND t.sub_team=%s
                  )
                  {sub_filter}
                GROUP BY p.opportunity_id
            ) x
            GROUP BY closing_qtr
        """, sols + [st])

        by_q = {}; fy_acv = 0; fy_deals = 0
        for r in rows:
            _m = re.search(r"Q[1-4]", r["q"] or ""); q = _m.group(0) if _m else ""
            if q in ("Q1","Q2","Q3","Q4"):
                by_q[q] = {"acv": float(r["acv"] or 0), "deals": int(r["deals"] or 0)}
                fy_acv += float(r["acv"] or 0); fy_deals += int(r["deals"] or 0)
        for q in ["Q1","Q2","Q3","Q4"]: by_q.setdefault(q, {"acv": 0, "deals": 0})
        by_q["Full Year"] = {"acv": fy_acv, "deals": fy_deals}
        result[st] = by_q

    # SCM — single grouped query
    scm_rows = query(f"""
        SELECT closing_qtr AS q,
               SUM(attributed) AS acv, COUNT(DISTINCT opportunity_id) AS deals
        FROM (
            SELECT DISTINCT pe.opportunity_id, pe.acv_usd_line,
                   pe.acv_attributed AS attributed,
                   pe.closing_qtr AS closing_qtr
            FROM pipeline_exploded pe
            JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
              AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
            WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.sub_team='SCM' AND {booked_p} {ent_f}
        ) x GROUP BY closing_qtr
    """)
    scm_q = {}; fy_acv = 0; fy_deals = 0
    for r in scm_rows:
        _m = re.search(r"Q[1-4]", r["q"] or ""); q = _m.group(0) if _m else ""
        if q in ("Q1","Q2","Q3","Q4"):
            scm_q[q] = {"acv": float(r["acv"] or 0), "deals": int(r["deals"] or 0)}
            fy_acv += float(r["acv"] or 0); fy_deals += int(r["deals"] or 0)
    for q in ["Q1","Q2","Q3","Q4"]: scm_q.setdefault(q, {"acv": 0, "deals": 0})
    scm_q["Full Year"] = {"acv": fy_acv, "deals": fy_deals}
    result["SCM"] = scm_q
    return result


@router.get("/attainment/manager/all-opps")
def manager_all_opps(
    include_purchase: bool = False,
    exclude_enterprise: bool = False,
    _user=Depends(verify_token),
):
    """All booked opportunities across all teams for the unified table."""
    booked_p = "(p.drm_category='Booked/Won' OR p.opp_phase='A - Purchase')" if include_purchase else "p.drm_category='Booked/Won'"
    ent_f    = "AND (p.iss_group_text IS NULL OR p.iss_group_text <> 'Enterprise')" if exclude_enterprise else ""

    # ERP from pipeline_exploded
    erp_rows = query(f"""
        SELECT opportunity_id, account_name, opp_owner_name,
               solution_area_l1, sub_solution_area_l2,
               closing_qtr, iss_group_text, region_lvl3,
               team, acv_recognized, acv_line, vat_size, team_members
        FROM (
            SELECT pe.opportunity_id, p.account_name, p.opp_owner_name,
                   pe.solution_area_l1, p.sub_solution_area_l2,
                   p.closing_qtr, p.iss_group_text, p.region_lvl3,
                   pe.area AS team,
                   pe.acv_attributed AS acv_recognized,
                   pe.acv_usd_line   AS acv_line,
                   pe.vat_size,
                   pe.sa_name AS team_members,
                   ROW_NUMBER() OVER (PARTITION BY pe.opportunity_id, pe.area, pe.sa_inumber, pe.solution_area_l1 ORDER BY pe.acv_attributed DESC) AS _rn
            FROM pipeline_exploded pe
            JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
              AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
            WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
              AND pe.area IN ('ERP Cloud BR','ERP Cloud MCLA','ERP MX MCLA IB')
              AND {booked_p} {ent_f}
        ) _erp WHERE _rn = 1
    """)

    # SCM
    scm_rows = query(f"""
        SELECT DISTINCT pe.opportunity_id, p.account_name, p.opp_owner_name,
               p.solution_area_l1, p.sub_solution_area_l2,
               p.closing_qtr, p.iss_group_text, p.region_lvl3,
               'SCM (LoBs)' AS team,
               pe.acv_attributed AS acv_recognized,
               pe.acv_usd_line   AS acv_line,
               pe.vat_size,
               pe.sa_name AS team_members
        FROM pipeline_exploded pe
        JOIN pipeline p ON p.opportunity_id=pe.opportunity_id
          AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
        WHERE pe.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline_exploded)
          AND pe.sub_team='SCM' AND {booked_p} {ent_f}
        ORDER BY pe.opportunity_id, pe.acv_usd_line DESC
    """)

    # LoBs (non-SCM) via dh_vat
    LOB_MAP = {
        "BAIP": ["BAIP - Data & AI", "BAIP - Technology Foundation"],
        "HCM":  ["Human Capital Management"],
        "FSM":  ["Finance and Spend Management"],
        "CX":   ["Customer Experience"],
    }
    # FSM only counts Fieldglass + Procurement (Attribution Rules Req 4.3/4.5)
    LOB_SUB_FILTER = {
        "FSM": "AND p.sub_solution_area_l2 IN ('Fieldglass','Procurement')",
    }
    lob_rows = []
    for st, sols in LOB_MAP.items():
        sol_ph = ",".join(["%s"] * len(sols))
        sub_filter = LOB_SUB_FILTER.get(st, "")
        rows = query(f"""
            SELECT opportunity_id, account_name, opp_owner_name,
                   solution_area_l1, sub_solution_area_l2,
                   closing_qtr, iss_group_text, region_lvl3,
                   team, acv_recognized, acv_line, vat_size, team_members
            FROM (
                SELECT p.opportunity_id, p.account_name, p.opp_owner_name,
                       p.solution_area_l1, p.sub_solution_area_l2,
                       p.closing_qtr, p.iss_group_text, p.region_lvl3,
                       %s AS team,
                       p.acv_usd AS acv_recognized,
                       p.acv_usd AS acv_line,
                       p.vat_size,
                       NULL AS team_members,
                       ROW_NUMBER() OVER (PARTITION BY p.opportunity_id, p.solution_area_l1 ORDER BY p.acv_usd DESC) AS _rn
                FROM pipeline p
                JOIN dh_vat v ON v.opportunity_id=p.opportunity_id
                JOIN targets t ON t.inumber=v.sales_team_member_id
                WHERE t.area='LoBs Team' AND t.sub_team=%s
                  AND p.solution_area_l1 IN ({sol_ph})
                  AND {booked_p} {ent_f}
                  AND p.snapshot_week=(SELECT MAX(snapshot_week) FROM pipeline)
                  {sub_filter}
            ) _lob WHERE _rn = 1
        """, [f"{st} (LoBs)", st] + sols)
        lob_rows.extend(rows)

    all_rows = list(erp_rows) + list(scm_rows) + lob_rows
    result = []
    for r in all_rows:
        rec = dict(r)
        rec["acv_recognized"] = float(rec.get("acv_recognized") or 0)
        rec["acv_line"]       = float(rec.get("acv_line") or 0)
        result.append(rec)

    return sorted(result, key=lambda x: x.get("closing_qtr","") or "")
