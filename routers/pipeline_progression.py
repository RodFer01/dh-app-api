"""Pipeline Progression router — weekly snapshot evolution analysis."""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, Depends
from database import query
from auth import verify_token

router = APIRouter(prefix="/api/pipeline-progression", tags=["pipeline-progression"])

# Cached flag — once True, never re-checks until restart
_TABLE_EXISTS: Optional[bool] = None


def _check_table() -> bool:
    global _TABLE_EXISTS
    if _TABLE_EXISTS:
        return True
    try:
        query("SELECT TOP 1 1 AS ok FROM past_pipeline", [])
        _TABLE_EXISTS = True
        return True
    except Exception:
        return False


def _latest_snapshot() -> Optional[str]:
    if not _check_table():
        return None
    rows = query("SELECT MAX(snapshot_week) AS max_week FROM past_pipeline", [])
    if not rows or rows[0]["max_week"] is None:
        return None
    return str(rows[0]["max_week"])[:10]


def _multi_filter(field: str, values: list, clauses: list, params: list):
    vals = [v for v in values if v]
    if not vals:
        return
    if len(vals) == 1:
        clauses.append(f"{field} = %s"); params.append(vals[0])
    else:
        ph = ",".join(["%s"] * len(vals))
        clauses.append(f"{field} IN ({ph})"); params.extend(vals)


def _pp_build_where(
    snapshot_week: Optional[str] = None,
    closing_qtrs: List[str] = [],
    regions: List[str] = [],
    sol_l1s: List[str] = [],
    drms: List[str] = [],
    opp_phases: List[str] = [],
) -> tuple:
    clauses, params = [], []

    if snapshot_week:
        clauses.append("snapshot_week = %s"); params.append(snapshot_week)

    _multi_filter("closing_qtr",     closing_qtrs, clauses, params)
    _multi_filter("region_lvl3",     regions,      clauses, params)
    _multi_filter("solution_area_l1", sol_l1s,     clauses, params)
    _multi_filter("drm_category",    drms,         clauses, params)
    _multi_filter("opp_phase",       opp_phases,   clauses, params)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ── /snapshots ────────────────────────────────────────────────────────────────
@router.get("/snapshots")
def get_snapshots(token: str = Depends(verify_token)) -> List[Dict[str, Any]]:
    if not _check_table():
        return []
    rows = query(
        "SELECT DISTINCT snapshot_week FROM past_pipeline ORDER BY snapshot_week DESC", []
    )
    return [{"snapshot_week": str(r["snapshot_week"])[:10]} for r in rows]


# ── /summary ──────────────────────────────────────────────────────────────────
@router.get("/summary")
def get_summary(
    snapshot_week: Optional[str] = Query(None),
    closing_qtrs:  List[str]     = Query(default=[]),
    regions:       List[str]     = Query(default=[]),
    sol_l1s:       List[str]     = Query(default=[]),
    drms:          List[str]     = Query(default=[]),
    token: str = Depends(verify_token),
) -> Dict[str, Any]:
    effective_week = snapshot_week or _latest_snapshot()
    if effective_week is None:
        return {"snapshot_week": None, "total_opps": 0, "total_acv": 0.0, "stalled_opps": 0, "stalled_acv": 0.0}

    where, params = _pp_build_where(
        snapshot_week=effective_week,
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s, drms=drms,
    )

    rows = query(
        f"""
        SELECT
            COUNT(*)                                           AS total_opps,
            SUM(acv_usd)                                      AS total_acv,
            SUM(CASE WHEN age_days > 21 THEN 1 ELSE 0 END)    AS stalled_opps,
            SUM(CASE WHEN age_days > 21 THEN acv_usd ELSE 0 END) AS stalled_acv
        FROM past_pipeline
        {where}
        """,
        params or None,
    )

    r = rows[0] if rows else {}
    return {
        "snapshot_week": effective_week,
        "total_opps":   int(r.get("total_opps") or 0),
        "total_acv":    float(r.get("total_acv") or 0.0),
        "stalled_opps": int(r.get("stalled_opps") or 0),
        "stalled_acv":  float(r.get("stalled_acv") or 0.0),
    }


# ── /evolution (DRM Category × week) ─────────────────────────────────────────
@router.get("/evolution")
def get_evolution(
    closing_qtrs:  List[str] = Query(default=[]),
    regions:       List[str] = Query(default=[]),
    sol_l1s:       List[str] = Query(default=[]),
    token: str = Depends(verify_token),
) -> List[Dict[str, Any]]:
    if not _check_table():
        return []

    where, params = _pp_build_where(
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s,
    )

    rows = query(
        f"""
        SELECT
            snapshot_week,
            drm_category,
            COUNT(*)     AS n_opps,
            SUM(acv_usd) AS total_acv
        FROM past_pipeline
        {where}
        GROUP BY snapshot_week, drm_category
        ORDER BY snapshot_week ASC, drm_category ASC
        """,
        params or None,
    )
    return [
        {
            "snapshot_week": str(r["snapshot_week"])[:10],
            "drm_category":  r["drm_category"],
            "n_opps":        int(r["n_opps"] or 0),
            "total_acv":     float(r["total_acv"] or 0.0),
        }
        for r in rows
    ]


# ── /evolution-phase (Opp Phase × week) ───────────────────────────────────────
@router.get("/evolution-phase")
def get_evolution_phase(
    closing_qtrs: List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    sol_l1s:      List[str] = Query(default=[]),
    drms:         List[str] = Query(default=[]),
    token: str = Depends(verify_token),
) -> List[Dict[str, Any]]:
    if not _check_table():
        return []

    where, params = _pp_build_where(
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s, drms=drms,
    )

    rows = query(
        f"""
        SELECT
            snapshot_week,
            opp_phase,
            COUNT(*)     AS n_opps,
            SUM(acv_usd) AS total_acv
        FROM past_pipeline
        {where}
        GROUP BY snapshot_week, opp_phase
        ORDER BY snapshot_week ASC, opp_phase ASC
        """,
        params or None,
    )
    return [
        {
            "snapshot_week": str(r["snapshot_week"])[:10],
            "opp_phase":     r["opp_phase"],
            "n_opps":        int(r["n_opps"] or 0),
            "total_acv":     float(r["total_acv"] or 0.0),
        }
        for r in rows
    ]


# ── /changes ──────────────────────────────────────────────────────────────────
@router.get("/changes")
def get_changes(
    from_week: str = Query(..., description="YYYY-MM-DD"),
    to_week: str = Query(..., description="YYYY-MM-DD"),
    token: str = Depends(verify_token),
) -> List[Dict[str, Any]]:
    if not _check_table():
        return []
    rows = query(
        """
        SELECT
            t2.opportunity_id,
            t2.account_name,
            t2.opp_owner_name,
            t2.region_lvl3,
            t1.drm_category          AS drm_from,
            t2.drm_category          AS drm_to,
            t1.acv_usd               AS acv_from,
            t2.acv_usd               AS acv_to,
            t1.opp_phase             AS phase_from,
            t2.opp_phase             AS phase_to,
            t1.deal_qualification_decision AS qual_from,
            t2.deal_qualification_decision AS qual_to
        FROM past_pipeline t1
        INNER JOIN past_pipeline t2 ON t1.opportunity_id = t2.opportunity_id
        WHERE t1.snapshot_week = %s
          AND t2.snapshot_week = %s
          AND (
              t1.drm_category <> t2.drm_category
              OR ABS(COALESCE(t2.acv_usd, 0) - COALESCE(t1.acv_usd, 0)) > 0
          )
        ORDER BY t2.opportunity_id ASC
        """,
        [from_week, to_week],
    )
    return [
        {
            "opportunity_id": r["opportunity_id"],
            "account_name":   r["account_name"],
            "opp_owner_name": r["opp_owner_name"],
            "region_lvl3":    r["region_lvl3"],
            "drm_from":       r["drm_from"],
            "drm_to":         r["drm_to"],
            "acv_from":       float(r["acv_from"] or 0.0),
            "acv_to":         float(r["acv_to"] or 0.0),
            "phase_from":     r["phase_from"],
            "phase_to":       r["phase_to"],
            "qual_from":      r["qual_from"],
            "qual_to":        r["qual_to"],
        }
        for r in rows
    ]


# ── /stalled ──────────────────────────────────────────────────────────────────
@router.get("/stalled")
def get_stalled(
    snapshot_week: Optional[str] = Query(None),
    closing_qtrs:  List[str]     = Query(default=[]),
    regions:       List[str]     = Query(default=[]),
    sol_l1s:       List[str]     = Query(default=[]),
    token: str = Depends(verify_token),
) -> List[Dict[str, Any]]:
    effective_week = snapshot_week or _latest_snapshot()
    if effective_week is None:
        return []

    where, params = _pp_build_where(
        snapshot_week=effective_week,
        closing_qtrs=closing_qtrs, regions=regions, sol_l1s=sol_l1s,
    )

    rows = query(
        f"""
        SELECT
            opportunity_id, account_name, opp_owner_name, region_lvl3,
            drm_category, opp_phase, acv_usd, age_days, dda_status, snapshot_week
        FROM past_pipeline
        {where}
        {"AND" if where else "WHERE"} age_days > 21
        ORDER BY age_days DESC
        """,
        params or None,
    )
    return [
        {
            "opportunity_id": r["opportunity_id"],
            "account_name":   r["account_name"],
            "opp_owner_name": r["opp_owner_name"],
            "region_lvl3":    r["region_lvl3"],
            "drm_category":   r["drm_category"],
            "opp_phase":      r["opp_phase"],
            "acv_usd":        float(r["acv_usd"] or 0.0),
            "age_days":       int(r["age_days"] or 0),
            "dda_status":     r["dda_status"],
            "snapshot_week":  str(r["snapshot_week"])[:10],
        }
        for r in rows
    ]
