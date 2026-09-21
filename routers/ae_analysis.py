"""
AE Analysis router — territory view by industry + opportunity suggestions.
Endpoints:
  GET /api/ae-analysis/filters   — distinct AEs, quarters, regions, FLSMs
  GET /api/ae-analysis/territory — pipeline grouped by AE + industry (mastercode)
  GET /api/ae-analysis/opps      — opportunity list for a given AE, filterable
"""
from fastapi import APIRouter, Depends, Query
from typing import List, Optional
import httpx
import os
import time
from auth import verify_token
from database import query

_SPARK_URL = "https://spark-meeting.cfapps.us10-002.hana.ondemand.com"
_spark_cache: dict = {"token": None, "exp": 0.0, "data": None, "data_at": 0.0}

router = APIRouter(prefix="/api/ae-analysis", tags=["ae-analysis"])

# ── Solution equivalence map ───────────────────────────────────────────────────
# Maps suggestion label → pipeline L1/L2 values it should be considered "covered" by.
# Key: suggestion name (as used in INDUSTRY_META / catalog sap_product)
# Value: list of substrings — if ANY of them appears in a pipeline solution, the suggestion is considered present.
SOLUTION_EQUIVALENCES: dict[str, list[str]] = {
    "S/4HANA Cloud":         ["cloud erp", "cloud erp private", "cloud erp finance", "cloud erp scm", "s/4hana"],
    "S/4HANA Cloud Private": ["cloud erp private", "cloud erp", "s/4hana"],
    "S/4HANA Cloud ERP":     ["cloud erp", "cloud erp private", "cloud erp finance", "s/4hana"],
    "S/4HANA Finance":       ["cloud erp finance", "finance private", "finance and spend", "s/4hana"],
    "S/4HANA Retail":        ["cloud erp", "s/4hana", "cloud erp private"],
    "S/4HANA Utilities":     ["cloud erp", "s/4hana", "cloud erp private"],
    "S/4HANA Public Cloud":  ["cloud erp", "s/4hana", "erp for sme"],
    "SAP IBP":               ["ibp", "supply chain", "plan", "planning"],
    "SAP TM":                ["tm", "transportation", "supply chain"],
    "SAP EWM":               ["ewm", "warehouse", "supply chain"],
    "SAP Analytics Cloud":   ["sac", "analytics", "data & analytics"],
    "SAP Joule":             ["joule", "artificial intelligence", "joule studio"],
    "SAP SuccessFactors":    ["successfactors", "hcm", "human capital", "core hr", "learning", "recruiting"],
    "SAP Ariba":             ["ariba", "procurement", "business network"],
    "SAP Concur":            ["concur", "travel", "expense"],
    "SAP Sustainability":    ["sustainability", "esg", "carbon"],
    "SAP PM/EAM":            ["pm", "eam", "asset", "maintenance"],
    "SAP Commerce Cloud":    ["commerce", "cx", "customer experience"],
    "SAP Emarsys":           ["emarsys", "marketing", "cx"],
    "SAP Sales Cloud":       ["sales", "cx", "crm", "customer experience"],
    "SAP Service Cloud":     ["service", "cx", "customer experience"],
    "SAP Customer Data Platform": ["cdp", "customer data", "cx"],
    "SAP Signavio":          ["signavio", "business transformation"],
    "SAP LeanIX":            ["leanix", "business transformation"],
    "SAP BTP":               ["btp", "technology foundation", "integration suite"],
    "SAP PLM":               ["plm", "product lifecycle"],
    "SAP QM":                ["qm", "quality"],
    "SAP EHS":               ["ehs", "environment", "health", "safety"],
    "SAP PS":                ["ps", "project system", "engineering"],
    "SAP Risk & Compliance": ["risk", "compliance", "grc"],
    "SAP LO-SER":            ["serialization", "lo-ser", "track"],
    "SAP TPM":               ["tpm", "trade promotion"],
}


def _is_covered(suggestion: str, pipeline_solutions: list[str]) -> bool:
    """Returns True if the suggestion is already covered by any pipeline solution."""
    sug_lower = suggestion.lower()
    equivalents = SOLUTION_EQUIVALENCES.get(suggestion, [])
    # Build all match tokens: first word of suggestion + all equivalences
    tokens = [sug_lower.split()[0]] + equivalents

    for sol in pipeline_solutions:
        sol_lower = sol.lower()
        if any(tok in sol_lower for tok in tokens):
            return True
    return False

# ── Industry metadata (mastercode → display info + SAP suggestions) ────────────
INDUSTRY_META = {
    "55 - Mill Products and Mining": {
        "icon": "⛏️", "color": "#78350f",
        "suggestions": ["S/4HANA Cloud ERP","SAP PM/EAM","SAP IBP","SAP Sustainability","SAP Joule"],
    },
    "31 - Professional Services": {
        "icon": "💼", "color": "#1e3a5f",
        "suggestions": ["S/4HANA Cloud","SAP SuccessFactors","SAP PS/PPM","SAP Concur","SAP Sales Cloud"],
    },
    "05 - Consumer Products": {
        "icon": "🛒", "color": "#7c3aed",
        "suggestions": ["S/4HANA Cloud","SAP IBP","SAP Emarsys","SAP TPM","SAP Commerce Cloud"],
    },
    "13 - Retail": {
        "icon": "🏪", "color": "#b91c1c",
        "suggestions": ["S/4HANA Retail","SAP Emarsys","SAP Commerce Cloud","SAP IBP","SAP Customer Data Platform"],
    },
    "63 - Agribusiness": {
        "icon": "🌱", "color": "#166534",
        "suggestions": ["S/4HANA Cloud","SAP Sustainability","SAP IBP","SAP TM","SAP Analytics Cloud"],
    },
    "35 - Wholesale Distribution": {
        "icon": "📦", "color": "#0369a1",
        "suggestions": ["S/4HANA Cloud","SAP EWM","SAP IBP","SAP TM","SAP Ariba"],
    },
    "03 - Banking": {
        "icon": "🏦", "color": "#1e40af",
        "suggestions": ["S/4HANA Finance","SAP SuccessFactors","SAP Joule","SAP Sales Cloud","SAP Risk & Compliance"],
    },
    "15 - Utilities": {
        "icon": "⚡", "color": "#a16207",
        "suggestions": ["S/4HANA Utilities","SAP PM/EAM","SAP Sustainability","SAP Analytics Cloud","SAP Joule"],
    },
    "66 - Travel and Transportation": {
        "icon": "✈️", "color": "#0f766e",
        "suggestions": ["S/4HANA Cloud","SAP TM","SAP PM/EAM","SAP SuccessFactors","SAP Joule"],
    },
    "02 - Automotive": {
        "icon": "🚗", "color": "#374151",
        "suggestions": ["S/4HANA Cloud","SAP IBP","SAP Ariba","SAP Sustainability","SAP PLM"],
    },
    "10 - Oil, Gas and Energy": {
        "icon": "🛢️", "color": "#92400e",
        "suggestions": ["S/4HANA Cloud","SAP PM/EAM","SAP Sustainability","SAP Ariba","SAP Analytics Cloud"],
    },
    "37 - Engineering, Construction and Operation": {
        "icon": "🏗️", "color": "#b45309",
        "suggestions": ["S/4HANA Cloud","SAP PS","SAP Ariba","SAP SuccessFactors","SAP Analytics Cloud"],
    },
    "11 - Life Sciences": {
        "icon": "🧬", "color": "#0f766e",
        "suggestions": ["S/4HANA Cloud","SAP QM","SAP LO-SER","SAP IBP","SAP TM"],
    },
    "04 - Chemicals": {
        "icon": "⚗️", "color": "#6d28d9",
        "suggestions": ["S/4HANA Cloud","SAP PM/EAM","SAP EHS","SAP PLM","SAP Sustainability"],
    },
    "07 - Healthcare": {
        "icon": "🏥", "color": "#065f46",
        "suggestions": ["S/4HANA Cloud","SAP SuccessFactors","SAP Ariba","SAP Analytics Cloud","SAP Joule"],
    },
    "12 - Public Sector": {
        "icon": "🏛️", "color": "#1e3a5f",
        "suggestions": ["S/4HANA Public Cloud","SAP SuccessFactors","SAP Ariba","SAP Analytics Cloud","SAP BTP"],
    },
    "14 - Telecommunications": {
        "icon": "📡", "color": "#4338ca",
        "suggestions": ["S/4HANA Cloud","SAP SuccessFactors","SAP Analytics Cloud","SAP Service Cloud","SAP Joule"],
    },
    "06 - Industrial Manufacturing": {
        "icon": "🏭", "color": "#374151",
        "suggestions": ["S/4HANA Cloud","SAP IBP","SAP PM/EAM","SAP Ariba","SAP Quality Mgmt"],
    },
    "30 - Higher Education and Research": {
        "icon": "🎓", "color": "#1e40af",
        "suggestions": ["S/4HANA Public Cloud","SAP SuccessFactors","SAP Ariba","SAP Analytics Cloud","SAP BTP"],
    },
    "09 - Insurance": {
        "icon": "🛡️", "color": "#1e3a5f",
        "suggestions": ["S/4HANA Finance","SAP SuccessFactors","SAP Analytics Cloud","SAP Joule","SAP Risk & Compliance"],
    },
}

_SNAP = "(SELECT MAX(snapshot_week) FROM pipeline)"


@router.get("/filters")
def ae_filters(_user=Depends(verify_token)):
    """Return filter options for the AE Analysis page."""
    def col(c):
        rows = query(f"""
            SELECT DISTINCT {c} AS v FROM pipeline
            WHERE snapshot_week = {_SNAP} AND {c} IS NOT NULL AND {c} <> ''
            ORDER BY {c}
        """)
        return [r["v"] for r in rows if r["v"]]

    def spec_col(c):
        rows = query(
            f"SELECT DISTINCT s.{c} AS v FROM account_specs s"
            f" INNER JOIN pipeline p ON p.account_bp = s.account_bp"
            f" WHERE p.snapshot_week = {_SNAP} AND s.{c} IS NOT NULL AND s.{c} <> ''"
            f" ORDER BY s.{c}"
        )
        return [r["v"] for r in rows if r["v"]]

    return {
        "opp_owners":    col("opp_owner_name"),
        "flsms":         col("flsm"),
        "regions":       col("region_lvl3"),
        "region_lvl4s":  col("region_lvl4"),
        "closing_qtrs":  col("closing_qtr"),
        "iss_groups":    col("iss_group_text"),
        "drm_cats":      col("drm_category"),
        # SPEC filters
        "iaes":              spec_col("iae"),
        "spec_cxs":          spec_col("spec_cx"),
        "spec_hxms":         spec_col("spec_hxm"),
        "spec_procurements": spec_col("spec_procurement"),
        "spec_dscs":         spec_col("spec_dsc"),
        "spec_btps":         spec_col("spec_btp"),
        "spec_bdcs":         spec_col("spec_bdc"),
        "spec_fins":         spec_col("spec_fin"),
        "spec_pros":         spec_col("spec_pro"),
        "solutions_l1":      col("solution_area_l1"),
        "solutions_l2":      col("sub_solution_area_l2"),
    }


@router.get("/territory")
def ae_territory(
    opp_owners:   List[str] = Query(default=[]),
    flsms:        List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    closing_qtrs: List[str] = Query(default=[]),
    iss_groups:   List[str] = Query(default=[]),
    drm_cats:     List[str] = Query(default=[]),
    priorities:   List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
    # SPEC filters
    iaes:              List[str] = Query(default=[]),
    spec_cxs:          List[str] = Query(default=[]),
    spec_hxms:         List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs:         List[str] = Query(default=[]),
    spec_btps:         List[str] = Query(default=[]),
    spec_bdcs:         List[str] = Query(default=[]),
    spec_fins:         List[str] = Query(default=[]),
    spec_pros:         List[str] = Query(default=[]),
    # Solution filters
    sol_l1s:           List[str] = Query(default=[]),
    sol_l2s:           List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """
    Returns: for each AE → list of industries → {acv, opps, solutions_present}.
    Used to render the territory map and gap analysis.
    """
    clauses, params = [f"snapshot_week = {_SNAP}"], []

    def mf(col, vals):
        if vals:
            ph = ",".join(["%s"] * len(vals))
            clauses.append(f"{col} IN ({ph})")
            params.extend(vals)

    mf("opp_owner_name",    opp_owners)
    mf("flsm",              flsms)
    mf("region_lvl3",       regions)
    mf("region_lvl4",       region_lvl4s)
    mf("closing_qtr",       closing_qtrs)
    mf("iss_group_text",    iss_groups)
    mf("drm_category",      drm_cats)

    if priorities:
        ph = ",".join(["%s"] * len(priorities))
        clauses.append(f"opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE priority IN ({ph}))")
        params.extend([int(p) for p in priorities])
    if probability_min is not None:
        clauses.append("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct >= %s)")
        params.append(float(probability_min))
    if probability_max is not None:
        clauses.append("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct <= %s)")
        params.append(float(probability_max))

    def sf(col, vals):
        if vals:
            ph = ",".join(["%s"] * len(vals))
            clauses.append(f"account_bp IN (SELECT account_bp FROM account_specs WHERE {col} IN ({ph}))")
            params.extend(vals)

    sf("iae",              iaes)
    sf("spec_cx",          spec_cxs)
    sf("spec_hxm",         spec_hxms)
    sf("spec_procurement", spec_procurements)
    sf("spec_dsc",         spec_dscs)
    sf("spec_btp",         spec_btps)
    sf("spec_bdc",         spec_bdcs)
    sf("spec_fin",         spec_fins)
    sf("spec_pro",         spec_pros)

    if sol_l1s:
        ph = ",".join(["%s"] * len(sol_l1s))
        clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND solution_area_l1 IN ({ph}))")
        params.extend(sol_l1s)
    if sol_l2s:
        ph = ",".join(["%s"] * len(sol_l2s))
        clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND sub_solution_area_l2 IN ({ph}))")
        params.extend(sol_l2s)

    where = "WHERE " + " AND ".join(clauses)

    rows = query(f"""
        SELECT
            ae,
            industry,
            COUNT(DISTINCT opportunity_id)                      AS opps,
            SUM(CASE WHEN rn = 1 THEN acv_total_opp ELSE 0 END) AS acv_total,
            STRING_AGG(DISTINCT solution_area_l1,    '|')       AS solutions_l1,
            STRING_AGG(DISTINCT sub_solution_area_l2,'|')       AS solutions_l2
        FROM (
            SELECT
                opp_owner_name                                                    AS ae,
                COALESCE(sap_mastercode, '—')                                    AS industry,
                opportunity_id,
                solution_area_l1,
                sub_solution_area_l2,
                acv_total_opp,
                ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY solution_area_l1) AS rn
            FROM pipeline
            {where}
        ) t
        GROUP BY ae, industry
        ORDER BY ae, acv_total DESC
    """, params)

    catalog = _get_catalog_by_industry()
    result = {}
    for r in rows:
        ae   = r["ae"] or "Unassigned"
        ind  = r["industry"]
        code = ind.split(" - ")[0].strip().lstrip("0") if " - " in ind else ""
        meta = INDUSTRY_META.get(ind, {"icon": "🏢", "color": "#6b7280", "suggestions": []})
        sols_l1 = set((r["solutions_l1"] or "").split("|")) - {""}
        sols_l2 = set((r["solutions_l2"] or "").split("|")) - {""}

        # catalog entries: [{l1, l2, priority, sap_product}, ...] sorted by priority
        cat_entries = catalog.get(code, [])
        # Build deduplicated suggestion list with priority, checking coverage against L1+L2
        seen = set()
        gaps = []
        for entry in cat_entries:
            label = entry["l1"]
            if label in seen:
                continue
            seen.add(label)
            if not _is_covered(label, list(sols_l1)) and not _is_covered_l2(label, list(sols_l2)):
                gaps.append({
                    "label":    label,
                    "l2":       entry.get("l2", ""),
                    "priority": entry["priority"],
                    "product":  entry.get("product", label),
                })
        # Also add hardcoded meta suggestions not already covered (priority=2 default)
        for s in meta["suggestions"]:
            if s not in seen and not _is_covered(s, list(sols_l1)) and not _is_covered_l2(s, list(sols_l2)):
                seen.add(s)
                gaps.append({"label": s, "l2": "", "priority": 2, "product": s})
        gaps.sort(key=lambda x: x["priority"])

        entry = {
            "industry":       ind,
            "icon":           meta["icon"],
            "color":          meta["color"],
            "opps":           r["opps"],
            "acv_total":      float(r["acv_total"] or 0),
            "solutions_l1":   sorted(sols_l1),
            "solutions_l2":   sorted(sols_l2),
            "solutions_present": sorted(sols_l1),  # kept for backward compat
            "suggestions":    [g["label"] for g in gaps],
            "gaps":           gaps,  # structured, ordered by priority
        }
        result.setdefault(ae, []).append(entry)

    ae_acv = {ae: sum(i["acv_total"] for i in inds) for ae, inds in result.items()}
    sorted_result = dict(sorted(result.items(), key=lambda x: -ae_acv[x[0]]))
    return {"data": sorted_result, "ae_acv": ae_acv}


@router.get("/opps")
def ae_opps(
    ae:           Optional[str]  = None,
    industry:     Optional[str]  = None,
    opp_owners:   List[str] = Query(default=[]),
    closing_qtrs: List[str] = Query(default=[]),
    flsms:        List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    iss_groups:   List[str] = Query(default=[]),
    drm_cats:     List[str] = Query(default=[]),
    priorities:   List[int] = Query(default=[]),
    probability_min: Optional[float] = None,
    probability_max: Optional[float] = None,
    # SPEC filters
    iaes:              List[str] = Query(default=[]),
    spec_cxs:          List[str] = Query(default=[]),
    spec_hxms:         List[str] = Query(default=[]),
    spec_procurements: List[str] = Query(default=[]),
    spec_dscs:         List[str] = Query(default=[]),
    spec_btps:         List[str] = Query(default=[]),
    spec_bdcs:         List[str] = Query(default=[]),
    spec_fins:         List[str] = Query(default=[]),
    spec_pros:         List[str] = Query(default=[]),
    # Solution filters
    sol_l1s:           List[str] = Query(default=[]),
    sol_l2s:           List[str] = Query(default=[]),
    _user=Depends(verify_token),
):
    """
    Opportunity list — respects all page-level filters.
    ae/industry are optional drill-down overrides (click on territory card).
    """
    clauses, params = [f"snapshot_week = {_SNAP}"], []

    def mf(col, vals):
        if vals:
            ph = ",".join(["%s"] * len(vals))
            clauses.append(f"{col} IN ({ph})")
            params.extend(vals)

    # Single-AE drill-down takes precedence; otherwise use multi-select list
    if ae:
        clauses.append("opp_owner_name = %s")
        params.append(ae)
    else:
        mf("opp_owner_name", opp_owners)

    if industry:
        clauses.append("sap_mastercode = %s")
        params.append(industry)

    mf("closing_qtr",  closing_qtrs)
    mf("flsm",         flsms)
    mf("region_lvl3",  regions)
    mf("region_lvl4",  region_lvl4s)
    mf("iss_group_text", iss_groups)
    mf("drm_category", drm_cats)

    if priorities:
        ph = ",".join(["%s"] * len(priorities))
        clauses.append(f"opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE priority IN ({ph}))")
        params.extend([int(p) for p in priorities])
    if probability_min is not None:
        clauses.append("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct >= %s)")
        params.append(float(probability_min))
    if probability_max is not None:
        clauses.append("opportunity_id IN (SELECT opportunity_id FROM opportunity_priorities WHERE probability_pct <= %s)")
        params.append(float(probability_max))

    def sf(col, vals):
        if vals:
            ph = ",".join(["%s"] * len(vals))
            clauses.append(f"account_bp IN (SELECT account_bp FROM account_specs WHERE {col} IN ({ph}))")
            params.extend(vals)

    sf("iae",              iaes)
    sf("spec_cx",          spec_cxs)
    sf("spec_hxm",         spec_hxms)
    sf("spec_procurement", spec_procurements)
    sf("spec_dsc",         spec_dscs)
    sf("spec_btp",         spec_btps)
    sf("spec_bdc",         spec_bdcs)
    sf("spec_fin",         spec_fins)
    sf("spec_pro",         spec_pros)

    if sol_l1s:
        ph = ",".join(["%s"] * len(sol_l1s))
        clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND solution_area_l1 IN ({ph}))")
        params.extend(sol_l1s)
    if sol_l2s:
        ph = ",".join(["%s"] * len(sol_l2s))
        clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND sub_solution_area_l2 IN ({ph}))")
        params.extend(sol_l2s)

    where = "WHERE " + " AND ".join(clauses)

    # Aggregate one row per opportunity — solutions collapsed into a pipe-separated string
    rows = query(f"""
        SELECT
            p.opportunity_id,
            MAX(p.account_name)                                   AS account_name,
            MAX(p.opp_description)                                AS opp_description,
            MAX(p.opp_owner_name)                                 AS opp_owner_name,
            MAX(p.opp_phase)                                      AS opp_phase,
            MAX(p.closing_qtr)                                    AS closing_qtr,
            MAX(p.drm_category)                                   AS drm_category,
            MAX(p.iss_group_text)                                 AS iss_group_text,
            MAX(p.region_lvl3)                                    AS region_lvl3,
            MAX(p.region_lvl4)                                    AS region_lvl4,
            MAX(p.flsm)                                           AS flsm,
            STRING_AGG(DISTINCT p.solution_area_l1,    ' | ')    AS solutions_l1,
            STRING_AGG(DISTINCT p.sub_solution_area_l2,' | ')    AS solutions_l2,
            MAX(p.sap_mastercode)                                 AS sap_mastercode,
            MAX(p.sic_description)                                AS sic_description,
            MAX(p.account_bp)                                     AS account_bp,
            MAX(p.acv_total_opp)                                  AS acv_total_opp,
            MAX(p.engagement_type)                                AS engagement_type,
            MAX(p.vat_dh)                                         AS vat_dh,
            MAX(p.deal_qualification_score)                       AS deal_qualification_score,
            COUNT(*)                                              AS solution_lines,
            MAX(sp.iae)                                           AS iae,
            MAX(sp.spec_cx)                                       AS spec_cx,
            MAX(sp.spec_hxm)                                      AS spec_hxm,
            MAX(sp.spec_procurement)                              AS spec_procurement,
            MAX(sp.spec_dsc)                                      AS spec_dsc,
            MAX(sp.spec_btp)                                      AS spec_btp,
            MAX(sp.spec_bdc)                                      AS spec_bdc,
            MAX(sp.spec_fin)                                      AS spec_fin,
            MAX(sp.spec_pro)                                      AS spec_pro
        FROM pipeline p
        LEFT JOIN account_specs sp ON sp.account_bp = p.account_bp
        {where}
        GROUP BY p.opportunity_id
        ORDER BY acv_total_opp DESC
        LIMIT 5000
    """, params)

    # Mark baskets (more than one solution line) and clean up empty aggregates
    result = []
    for r in rows:
        r["is_basket"]   = (r.get("solution_lines") or 1) > 1
        r["solutions_l1"] = [s.strip() for s in (r.get("solutions_l1") or "").split("|") if s.strip()]
        r["solutions_l2"] = [s.strip() for s in (r.get("solutions_l2") or "").split("|") if s.strip()]
        result.append(r)

    catalog = _get_catalog_by_industry()
    for r in result:
        mc   = r.get("sap_mastercode") or ""
        code = mc.split(" - ")[0].strip().lstrip("0") if " - " in mc else ""
        present_l1 = r.get("solutions_l1", [])
        present_l2 = r.get("solutions_l2", [])
        entries    = catalog.get(code, [])
        seen = set()
        sugs = []
        for entry in entries:
            label = entry["l1"]
            if label in seen:
                continue
            seen.add(label)
            if not _is_covered(label, present_l1) and not _is_covered_l2(label, present_l2):
                sugs.append({"label": label, "priority": entry["priority"], "product": entry.get("product", label)})
        sugs.sort(key=lambda x: x["priority"])
        r["suggestions"] = sugs

    return {"opps": result}


def _get_catalog_by_industry() -> dict:
    """Returns {mastercode_number: [{l1, l2, priority, product}, ...]} sorted by priority."""
    rows = query("""
        SELECT industry_codes, solution_l1, solution_l2, priority, sap_product
        FROM DBADMIN.SOLUTION_CATALOG
        WHERE active = 1
        ORDER BY priority ASC, solution_l1, solution_l2
    """)
    result: dict = {}
    for r in rows:
        codes   = (r.get("industry_codes") or "").split(",")
        l1      = r.get("solution_l1") or ""
        l2      = r.get("solution_l2") or ""
        pri     = int(r.get("priority") or 2)
        product = r.get("sap_product") or l1
        for code in codes:
            code = code.strip().lstrip("0")
            if not code:
                continue
            result.setdefault(code, [])
            result[code].append({"l1": l1, "l2": l2, "priority": pri, "product": product})
    return result


def _is_covered_l2(suggestion: str, pipeline_l2: list[str]) -> bool:
    """Returns True if the suggestion is covered by any L2 pipeline value."""
    sug_lower  = suggestion.lower()
    equivalents = SOLUTION_EQUIVALENCES.get(suggestion, [])
    tokens = [sug_lower.split()[0]] + equivalents
    for sol in pipeline_l2:
        sol_lower = sol.lower()
        if any(tok in sol_lower for tok in tokens):
            return True
    return False


@router.get("/solution-catalog")
def get_solution_catalog(
    industry_code: Optional[str] = None,
    l1: Optional[str] = None,
    _user=Depends(verify_token),
):
    """Return solution catalog entries, optionally filtered by industry code or L1."""
    clauses = ["active = 1"]
    params  = []
    if industry_code:
        clauses.append("industry_codes LIKE ?")
        params.append(f"%{industry_code}%")
    if l1:
        clauses.append("solution_l1 = ?")
        params.append(l1)
    where = "WHERE " + " AND ".join(clauses)
    rows = query(f"""
        SELECT id, solution_l1, solution_l2, solution_l3,
               industry_codes, priority, sap_product, notes
        FROM DBADMIN.SOLUTION_CATALOG
        {where}
        ORDER BY priority, solution_l1, solution_l2, solution_l3
    """, params)
    return {"catalog": rows}


@router.put("/solution-catalog/{item_id}")
def update_catalog_item(
    item_id: int,
    body: dict,
    _user=Depends(verify_token),
):
    """Update industry_codes, priority, sap_product or notes for a catalog entry."""
    allowed = {"industry_codes", "priority", "sap_product", "notes", "active"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "No valid fields to update"}
    sets   = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [item_id]
    query(f"UPDATE DBADMIN.SOLUTION_CATALOG SET {sets} WHERE id = ?", params)
    return {"ok": True}


LOB_ROLE_COLS = {
    "ERP": "role_erp",
    "HCM": "role_hcm",
    "CX":  "role_cx",
    "SCM": "role_scm",
    "BTP": "role_btp",
    "F&S": "role_fs",
}


@router.get("/audience-filters")
def audience_filters(_user=Depends(verify_token)):
    """Filter options for the Audiência tab — titles, functions, solutions from installed base."""
    def col(table, c):
        rows = query(f"""
            SELECT DISTINCT {c} AS v FROM {table}
            WHERE {c} IS NOT NULL AND {c} <> ''
            ORDER BY {c}
        """)
        return [r["v"] for r in rows if r["v"]]

    def func_col():
        rows = query("""
            SELECT DISTINCT oc.function AS v FROM outreach_contacts oc
            WHERE oc.function IS NOT NULL AND oc.function <> ''
            ORDER BY oc.function
        """)
        return [r["v"] for r in rows if r["v"]]

    return {
        "titles":       col("outreach_contacts", "title"),
        "functions":    func_col(),
        "lob_roles":    list(LOB_ROLE_COLS.keys()),
        "solutions":    col("account_installed_base", "solution_l1"),
        "solutions_l1": col("pipeline", "solution_area_l1"),
        "solutions_l2": col("pipeline", "sub_solution_area_l2"),
    }


@router.get("/audience")
def audience(
    opp_owners:   List[str] = Query(default=[]),
    flsms:        List[str] = Query(default=[]),
    regions:      List[str] = Query(default=[]),
    region_lvl4s: List[str] = Query(default=[]),
    closing_qtrs: List[str] = Query(default=[]),
    iss_groups:   List[str] = Query(default=[]),
    drm_cats:     List[str] = Query(default=[]),
    titles:       List[str] = Query(default=[]),
    functions:    List[str] = Query(default=[]),
    lob_roles:    List[str] = Query(default=[]),
    solutions:    List[str] = Query(default=[]),
    sol_l1s:      List[str] = Query(default=[]),
    sol_l2s:      List[str] = Query(default=[]),
    has_opps:     bool = False,
    _user=Depends(verify_token),
):
    """
    Campaign audience builder: accounts + matching contacts + open opportunities.
    Territory filters scope to a pipeline subset; contact/solution filters narrow further.
    """
    # ── Territory subquery ────────────────────────────────────────────────────
    t_clauses, t_params = [f"snapshot_week = {_SNAP}"], []

    def mf(col, vals):
        if vals:
            ph = ",".join(["%s"] * len(vals))
            t_clauses.append(f"{col} IN ({ph})")
            t_params.extend(vals)

    mf("opp_owner_name", opp_owners)
    mf("flsm",           flsms)
    mf("region_lvl3",    regions)
    mf("region_lvl4",    region_lvl4s)
    mf("closing_qtr",    closing_qtrs)
    mf("iss_group_text", iss_groups)
    mf("drm_category",   drm_cats)

    if sol_l1s:
        ph = ",".join(["%s"] * len(sol_l1s))
        t_clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND solution_area_l1 IN ({ph}))")
        t_params.extend(sol_l1s)
    if sol_l2s:
        ph = ",".join(["%s"] * len(sol_l2s))
        t_clauses.append(f"opportunity_id IN (SELECT DISTINCT opportunity_id FROM pipeline WHERE snapshot_week = {_SNAP} AND sub_solution_area_l2 IN ({ph}))")
        t_params.extend(sol_l2s)

    territory_where = " AND ".join(t_clauses)

    # ── Contact filter clauses ────────────────────────────────────────────────
    c_clauses, c_params = [], []

    if titles:
        ph = ",".join(["%s"] * len(titles))
        c_clauses.append(f"oc.title IN ({ph})")
        c_params.extend(titles)

    if functions:
        ph = ",".join(["%s"] * len(functions))
        c_clauses.append(f"oc.function IN ({ph})")
        c_params.extend(functions)

    if lob_roles:
        role_parts = []
        for role in lob_roles:
            col_name = LOB_ROLE_COLS.get(role)
            if col_name:
                role_parts.append(f"(oc.{col_name} IS NOT NULL AND oc.{col_name} <> '')")
        if role_parts:
            c_clauses.append("(" + " OR ".join(role_parts) + ")")

    if solutions:
        ph = ",".join(["%s"] * len(solutions))
        c_clauses.append(
            f"EXISTS (SELECT 1 FROM account_installed_base aib WHERE aib.account_bp = oc.account_bpid AND aib.solution_l1 IN ({ph}))"
        )
        c_params.extend(solutions)

    contact_where = ("AND " + " AND ".join(c_clauses)) if c_clauses else ""

    # ── Query 1: contacts × territory ────────────────────────────────────────
    all_params = t_params + c_params
    contact_rows = query(f"""
        SELECT
            oc.account_bpid          AS account_bp,
            p.account_name,
            p.opp_owner_name,
            p.region_lvl3,
            p.iss_group_text,
            oc.prospect_id,
            oc.first_name,
            oc.last_name,
            oc.title,
            oc.function,
            oc.email,
            oc.work_phone,
            oc.mobile_phone,
            oc.engaged_score,
            oc.key_executive_contact,
            oc.role_erp,
            oc.role_hcm,
            oc.role_cx,
            oc.role_scm,
            oc.role_btp,
            oc.role_fs
        FROM outreach_contacts oc
        JOIN (
            SELECT account_bp,
                   MAX(account_name)   AS account_name,
                   MAX(opp_owner_name) AS opp_owner_name,
                   MAX(region_lvl3)    AS region_lvl3,
                   MAX(iss_group_text) AS iss_group_text
            FROM pipeline
            WHERE {territory_where}
            GROUP BY account_bp
        ) p ON p.account_bp = oc.account_bpid
        WHERE oc.account_bpid IS NOT NULL
        {contact_where}
        ORDER BY oc.account_bpid, oc.engaged_score DESC NULLS LAST
        LIMIT 5000
    """, all_params)

    if not contact_rows:
        return {"accounts": [], "total_accounts": 0, "total_contacts": 0}

    # ── Group contacts by account ─────────────────────────────────────────────
    accounts: dict = {}
    for r in contact_rows:
        bp = r["account_bp"]
        if bp not in accounts:
            accounts[bp] = {
                "account_bp":      bp,
                "account_name":    r["account_name"],
                "opp_owner_name":  r["opp_owner_name"],
                "region_lvl3":     r["region_lvl3"],
                "iss_group_text":  r["iss_group_text"],
                "contacts":        [],
                "opps":            [],
                "installed_solutions": [],
            }
        accounts[bp]["contacts"].append({
            "prospect_id":           r["prospect_id"],
            "first_name":            r["first_name"],
            "last_name":             r["last_name"],
            "title":                 r["title"],
            "function":              r["function"],
            "email":                 r["email"],
            "work_phone":            r["work_phone"],
            "mobile_phone":          r["mobile_phone"],
            "engaged_score":         r["engaged_score"],
            "key_executive_contact": r["key_executive_contact"],
            "role_erp":              r["role_erp"],
            "role_hcm":              r["role_hcm"],
            "role_cx":               r["role_cx"],
            "role_scm":              r["role_scm"],
            "role_btp":              r["role_btp"],
            "role_fs":               r["role_fs"],
        })

    bps = list(accounts.keys())
    ph  = ",".join(["%s"] * len(bps))

    # ── Query 2: opportunities for found accounts ─────────────────────────────
    opp_rows = query(f"""
        SELECT
            account_bp,
            opportunity_id,
            MAX(opp_description)                                AS opp_description,
            MAX(closing_qtr)                                    AS closing_qtr,
            MAX(acv_total_opp)                                  AS acv_total_opp,
            MAX(drm_category)                                   AS drm_category,
            MAX(opp_owner_name)                                 AS opp_owner_name,
            STRING_AGG(DISTINCT solution_area_l1, ' | ')       AS solutions_l1
        FROM pipeline
        WHERE snapshot_week = {_SNAP} AND account_bp IN ({ph})
        GROUP BY account_bp, opportunity_id
        ORDER BY account_bp, acv_total_opp DESC
    """, bps)

    for r in opp_rows:
        bp = r["account_bp"]
        if bp in accounts:
            accounts[bp]["opps"].append({
                "opportunity_id":  r["opportunity_id"],
                "opp_description": r["opp_description"],
                "closing_qtr":     r["closing_qtr"],
                "acv_total_opp":   float(r["acv_total_opp"] or 0),
                "drm_category":    r["drm_category"],
                "opp_owner_name":  r["opp_owner_name"],
                "solutions_l1":    [s.strip() for s in (r["solutions_l1"] or "").split("|") if s.strip()],
            })

    # ── Query 3: installed solutions for found accounts ───────────────────────
    isb_rows = query(f"""
        SELECT DISTINCT account_bp, solution_l1
        FROM account_installed_base
        WHERE account_bp IN ({ph})
        ORDER BY account_bp, solution_l1
    """, bps)

    for r in isb_rows:
        bp = r["account_bp"]
        if bp in accounts:
            accounts[bp]["installed_solutions"].append(r["solution_l1"])

    # ── Apply has_opps filter ─────────────────────────────────────────────────
    result = list(accounts.values())
    if has_opps:
        result = [a for a in result if a["opps"]]

    # Sort by account name
    result.sort(key=lambda a: (a["account_name"] or "").lower())

    return {
        "accounts":       result,
        "total_accounts": len(result),
        "total_contacts": sum(len(a["contacts"]) for a in result),
    }


@router.get("/spark-registrations")
def get_spark_registrations(_user=Depends(verify_token)):
    """Proxy to spark-meeting: returns registrations keyed by lowercase email. Cached 5 min."""
    now = time.time()
    if _spark_cache["data"] is not None and _spark_cache["data_at"] > now - 300:
        return _spark_cache["data"]

    # Get or refresh JWT token
    if not _spark_cache["token"] or _spark_cache["exp"] < now + 300:
        email = os.environ.get("SPARK_MEETING_EMAIL", "")
        pwd   = os.environ.get("SPARK_MEETING_PASSWORD", "")
        if not email or not pwd:
            return {}
        r = httpx.post(f"{_SPARK_URL}/api/auth/login",
                       json={"email": email, "password": pwd}, timeout=10)
        r.raise_for_status()
        data = r.json()
        _spark_cache["token"] = data["token"]
        _spark_cache["exp"]   = now + data.get("expires_in", 43200)

    r = httpx.get(f"{_SPARK_URL}/api/registrations",
                  headers={"Authorization": f"Bearer {_spark_cache['token']}"},
                  timeout=15)
    r.raise_for_status()

    result = {}
    for reg in r.json():
        em = (reg.get("email") or "").strip().lower()
        if em:
            result[em] = {
                "session":      reg.get("session"),
                "date":         reg.get("date"),
                "registeredAt": reg.get("registeredAt"),
                "status":       reg.get("status"),
            }
    _spark_cache["data"]    = result
    _spark_cache["data_at"] = now
    return result


@router.get("/installed-base/{account_bp}")
def get_installed_base(account_bp: str, _user=Depends(verify_token)):
    """Return InnovationDash installed base for an account, grouped by solution L1."""
    rows = query(
        """SELECT product_label, solution_l1, status
           FROM account_installed_base
           WHERE account_bp = ?
           ORDER BY solution_l1, product_label""",
        [account_bp]
    )
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["solution_l1"], []).append({
            "product_label": r["product_label"],
            "status":        r["status"],
        })
    return {"account_bp": account_bp, "solutions": grouped}
