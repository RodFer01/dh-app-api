"""
Outreach integration router.
  GET  /api/outreach/contacts/{account_bp}   — contacts from outreach_contacts cache
  GET  /api/outreach/assignments/{opp_id}    — saved suggestion assignments
  POST /api/outreach/assignments             — upsert assignment
  DELETE /api/outreach/assignments/{opp_id}/{suggestion} — remove
  GET  /api/outreach/account-solution-map/{account_bp}   — account-level solution+competitor map
  POST /api/outreach/account-solution-map                — upsert account solution competitor entry
"""
import json as _json
import os
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from auth import verify_token
from database import query

router = APIRouter(prefix="/api/outreach", tags=["outreach"])

_DRIVER = os.getenv("DH_DB_DRIVER", "postgres")
_PH     = "?" if _DRIVER == "hana" else "%s"

# ── LAC Competitor Reference Map ──────────────────────────────────────────────
# Keys are normalized solution labels (lowercase match used in lookup)
LAC_COMPETITOR_MAP = {
    "s/4hana cloud": {
        "tier1": ["Oracle Fusion Cloud ERP", "Microsoft Dynamics 365 F&O"],
        "tier2": ["Infor CloudSuite", "Sage X3", "Epicor Kinetic", "Acumatica"],
        "lac":   ["TOTVS ERP (BR)", "Senior Sistemas (BR)", "Softland (CL/CO)", "Cigam (BR)"],
    },
    "cloud erp": {
        "tier1": ["Oracle Fusion Cloud ERP", "Microsoft Dynamics 365 F&O"],
        "tier2": ["Infor CloudSuite", "Sage X3", "Epicor Kinetic", "Acumatica"],
        "lac":   ["TOTVS ERP (BR)", "Senior Sistemas (BR)", "Softland (CL/CO)", "Linx (BR Retail)"],
    },
    "erp for sme": {
        "tier1": ["Oracle NetSuite", "Microsoft Dynamics 365 BC"],
        "tier2": ["Acumatica", "Sage Intacct", "Odoo"],
        "lac":   ["TOTVS (BR)", "Linx (BR)", "Alegra (CO/MX)", "Contifico (EC)"],
    },
    "cloud erp finance": {
        "tier1": ["Oracle Fusion Cloud Financials", "Workday Financials"],
        "tier2": ["BlackLine", "Sage Intacct", "Microsoft D365 Finance"],
        "lac":   ["TOTVS Gestão Financeira (BR)", "Senior Financeiro (BR)", "Sankhya (BR)"],
    },
    "finance and spend": {
        "tier1": ["Oracle Fusion Cloud Financials", "Workday Financials"],
        "tier2": ["BlackLine", "Sage Intacct", "Coupa"],
        "lac":   ["TOTVS Gestão Financeira (BR)", "Senior Financeiro (BR)", "Sankhya (BR)"],
    },
    "supply chain": {
        "tier1": ["Kinaxis RapidResponse", "o9 Solutions", "Blue Yonder"],
        "tier2": ["Oracle Fusion SCM Planning", "Anaplan", "RELEX Solutions"],
        "lac":   ["TOTVS Logística (BR)", "invasWMS (LATAM)", "Mecalux (AR/MX)"],
    },
    "supply chain management": {
        "tier1": ["Kinaxis RapidResponse", "o9 Solutions", "Blue Yonder"],
        "tier2": ["Oracle Fusion SCM Planning", "Anaplan", "RELEX Solutions"],
        "lac":   ["TOTVS Logística (BR)", "invasWMS (LATAM)", "Mecalux (AR/MX)"],
    },
    "ibp": {
        "tier1": ["Kinaxis RapidResponse", "o9 Solutions", "Blue Yonder"],
        "tier2": ["Oracle Fusion SCM Planning", "Anaplan", "RELEX Solutions"],
        "lac":   ["TOTVS Supply Chain (BR)", "Logility (BR clients)"],
    },
    "ewm": {
        "tier1": ["Manhattan Active WM", "Blue Yonder WMS"],
        "tier2": ["Oracle WMS Cloud", "Infor WMS", "Körber WMS"],
        "lac":   ["TOTVS Logística (BR)", "invasWMS (BR/MX/CL/CO/AR)", "Mecalux (AR/MX)"],
    },
    "warehouse": {
        "tier1": ["Manhattan Active WM", "Blue Yonder WMS"],
        "tier2": ["Oracle WMS Cloud", "Infor WMS", "Körber WMS"],
        "lac":   ["TOTVS Logística (BR)", "invasWMS (BR/MX/CL/CO/AR)", "Mecalux (AR/MX)"],
    },
    "transportation": {
        "tier1": ["Blue Yonder TMS", "Oracle Transportation Mgmt"],
        "tier2": ["Descartes Systems", "WiseTech / CargoWise"],
        "lac":   ["Sispro TMS (BR)", "Opentech TMS (BR)", "TOTVS Freight (BR)"],
    },
    "procurement": {
        "tier1": ["Coupa", "Jaggaer", "Oracle Procurement Cloud"],
        "tier2": ["GEP SMART", "Ivalua", "Zycus"],
        "lac":   ["EGIXIA (LAC-native)", "GoSourced (LAC-native)", "BHub (BR)"],
    },
    "ariba": {
        "tier1": ["Coupa", "Jaggaer", "Oracle Procurement Cloud"],
        "tier2": ["GEP SMART", "Ivalua", "Zycus"],
        "lac":   ["EGIXIA (LAC-native)", "GoSourced (LAC-native)", "BHub (BR)"],
    },
    "concur": {
        "tier1": ["Navan (TripActions)", "Expensify", "TravelPerk"],
        "tier2": ["Coupa Expense", "Ramp", "Brex"],
        "lac":   ["Rexpense (BR)", "Fyle (BR/MX clients)", "Webexpenses"],
    },
    "travel": {
        "tier1": ["Navan (TripActions)", "Expensify", "TravelPerk"],
        "tier2": ["Coupa Expense", "Ramp", "Brex"],
        "lac":   ["Rexpense (BR)", "Fyle (BR/MX clients)", "Webexpenses"],
    },
    "human capital": {
        "tier1": ["Workday HCM", "Oracle Fusion HCM"],
        "tier2": ["ADP Workforce Now", "UKG Pro", "Dayforce (Ceridian)"],
        "lac":   ["TOTVS RH (BR)", "Senior RH (BR)", "Sankhya RH (BR)", "ADP Celergo (LAC)"],
    },
    "successfactors": {
        "tier1": ["Workday HCM", "Oracle Fusion HCM"],
        "tier2": ["ADP Workforce Now", "UKG Pro", "Dayforce (Ceridian)"],
        "lac":   ["TOTVS RH (BR)", "Senior RH (BR)", "Gupy (BR)", "Sólides (BR)"],
    },
    "core hr": {
        "tier1": ["Workday HCM", "Oracle Fusion HCM"],
        "tier2": ["ADP Workforce Now", "UKG Pro", "Dayforce (Ceridian)"],
        "lac":   ["TOTVS RH (BR)", "Senior RH (BR)", "Sankhya RH (BR)"],
    },
    "recruiting": {
        "tier1": ["Workday Recruiting", "Oracle Taleo"],
        "tier2": ["Greenhouse", "iCIMS", "Lever"],
        "lac":   ["Gupy (BR — 79% BR recruiting)", "Sólides (BR)", "Kenoby/Gupy (BR)"],
    },
    "learning": {
        "tier1": ["Cornerstone OnDemand", "Workday Learning"],
        "tier2": ["Docebo", "LMS365", "TalentLMS"],
        "lac":   ["Twygo (BR)", "Niduu (BR)", "LMS EAD (BR)"],
    },
    "commerce": {
        "tier1": ["Salesforce Commerce Cloud", "Adobe Commerce (Magento)"],
        "tier2": ["Shopify Plus", "VTEX", "Shopware"],
        "lac":   ["VTEX (BR — NYSE listed)", "Linx Commerce (BR)", "Tray Commerce (BR)"],
    },
    "customer experience": {
        "tier1": ["Salesforce Sales Cloud", "Microsoft Dynamics 365 Sales"],
        "tier2": ["HubSpot CRM", "Pipedrive", "Zendesk"],
        "lac":   ["Ploomes (BR)", "Agendor (BR)", "Nectar CRM (BR)", "Movidesk (BR)"],
    },
    "emarsys": {
        "tier1": ["Salesforce Marketing Cloud", "Adobe Experience Cloud"],
        "tier2": ["Klaviyo", "Braze", "Bloomreach"],
        "lac":   ["RD Station (BR — TOTVS)", "Dinamize (BR)", "E-goi (BR/PT)"],
    },
    "marketing": {
        "tier1": ["Salesforce Marketing Cloud", "Adobe Experience Cloud"],
        "tier2": ["Klaviyo", "Braze", "HubSpot"],
        "lac":   ["RD Station (BR — TOTVS)", "Dinamize (BR)", "E-goi (BR/PT)"],
    },
    "analytics": {
        "tier1": ["Microsoft Power BI", "Tableau (Salesforce)"],
        "tier2": ["Qlik Sense", "Looker (Google)", "MicroStrategy"],
        "lac":   ["Qlik (strong BR presence)", "Metabase (BR startups)", "Senior Analytics (BR)"],
    },
    "data": {
        "tier1": ["Snowflake", "Microsoft Azure Synapse"],
        "tier2": ["Databricks", "Google BigQuery", "AWS Redshift"],
        "lac":   ["Neoway (BR data intelligence)", "BigDataCorp (BR)"],
    },
    "integration": {
        "tier1": ["MuleSoft Anypoint Platform", "Boomi"],
        "tier2": ["Microsoft Azure Integration", "Workato", "SnapLogic"],
        "lac":   ["Stefanini (BR/LAC SI)", "TIVIT (BR SI)", "Sonda (LAC SI)", "Softtek (MX)"],
    },
    "btp": {
        "tier1": ["MuleSoft Anypoint Platform", "Microsoft Power Platform"],
        "tier2": ["Boomi", "ServiceNow App Engine", "Workato"],
        "lac":   ["Stefanini (BR/LAC)", "TIVIT (BR)", "UiPath (BR/MX presence)"],
    },
    "joule": {
        "tier1": ["Microsoft Copilot / Azure OpenAI", "Google Vertex AI / Gemini"],
        "tier2": ["Salesforce Einstein AI", "AWS Bedrock", "Workday AI"],
        "lac":   ["Neoway AI (BR)", "Semantix (BR)", "Zup Innovation (BR)"],
    },
    "artificial intelligence": {
        "tier1": ["Microsoft Copilot / Azure OpenAI", "Google Vertex AI / Gemini"],
        "tier2": ["Salesforce Einstein AI", "AWS Bedrock", "Databricks"],
        "lac":   ["Neoway AI (BR)", "Semantix (BR)", "Zup Innovation (BR)"],
    },
    "signavio": {
        "tier1": ["Celonis", "Software AG ARIS"],
        "tier2": ["IBM Process Mining", "UiPath Process Mining", "Microsoft Process Mining"],
        "lac":   ["ProcessMind (BR)", "Apromore (BR clients)"],
    },
    "business transformation": {
        "tier1": ["Celonis", "Software AG ARIS"],
        "tier2": ["IBM Process Mining", "UiPath Process Mining", "Camunda"],
        "lac":   ["ProcessMind (BR)", "Apromore (BR clients)"],
    },
    "leanix": {
        "tier1": ["Orbus Software iServer", "MEGA HOPEX"],
        "tier2": ["Avolution ABACUS", "Ardoq", "Bizzdesign"],
        "lac":   ["Local EA consultancies (BR/MX)"],
    },
    "sustainability": {
        "tier1": ["Microsoft Cloud for Sustainability", "IBM Envizi"],
        "tier2": ["Salesforce Net Zero Cloud", "Sphera", "Persefoni"],
        "lac":   ["Ecobio (BR)", "Plan A (LAC clients)"],
    },
}

def _get_lac_competitors(solution: str) -> dict:
    """Match a solution label against the LAC competitor map (fuzzy lowercase)."""
    sol_lower = solution.lower()
    # Direct key match first
    for key, val in LAC_COMPETITOR_MAP.items():
        if key in sol_lower or sol_lower in key:
            return val
    # Keyword fallback
    keyword_map = [
        (["erp", "s/4hana", "grow", "cloud erp"], "cloud erp"),
        (["supply chain", "ibp", "planning"], "ibp"),
        (["ewm", "warehouse"], "ewm"),
        (["tm", "transport"], "transportation"),
        (["ariba", "procurement"], "procurement"),
        (["concur", "travel", "expense"], "concur"),
        (["fieldglass", "workforce", "vms"], "integration"),
        (["successfactors", "core hr", "payroll", "hcm"], "successfactors"),
        (["recruiting", "talent"], "recruiting"),
        (["learning", "lms"], "learning"),
        (["performance", "goals"], "successfactors"),
        (["commerce"], "commerce"),
        (["emarsys", "marketing"], "emarsys"),
        (["sales cloud", "crm", "sfa"], "customer experience"),
        (["service cloud", "service"], "customer experience"),
        (["customer data", "cdp"], "customer experience"),
        (["analytics", "bi", "sac"], "analytics"),
        (["fp&a", "planning / fp"], "analytics"),
        (["datasphere", "hana cloud", "data platform"], "data"),
        (["integration suite", "btp"], "btp"),
        (["build", "low-code", "automation"], "btp"),
        (["joule", "ai", "artificial"], "joule"),
        (["signavio", "process mining", "bpm"], "signavio"),
        (["leanix", "enterprise arch"], "leanix"),
        (["sustainability", "esg"], "sustainability"),
    ]
    for keywords, map_key in keyword_map:
        if any(kw in sol_lower for kw in keywords):
            return LAC_COMPETITOR_MAP.get(map_key, {"tier1": [], "tier2": [], "lac": []})
    return {"tier1": [], "tier2": [], "lac": []}


@router.get("/contacts/{account_bp}")
def get_contacts(account_bp: str, _user=Depends(verify_token)):
    """Return cached Outreach contacts for a given Account BP ID, ordered by engagement."""
    rows = query(f"""
        SELECT
            account_bpid, prospect_id,
            first_name, last_name, title,
            function, department_lob, work_phone, mobile_phone, email,
            sales_cloud_prospect_id, sales_cloud_prospect_link,
            contact_crm_bpid,
            engaged_score, open_count, reply_count, click_count,
            touched_at, engaged_at,
            key_executive_contact, contact_dq_status,
            regional_buying_classification,
            role_erp, role_hcm, role_cx, role_scm, role_btp, role_fs,
            score_erp, score_hcm, score_cx, score_scm, score_btp, score_fs,
            eligible_erp, eligible_hcm, eligible_cx, eligible_scm, eligible_btp, eligible_fs,
            last_actions_erp, last_actions_hcm, last_actions_cx,
            last_actions_scm, last_actions_btp, last_actions_fs,
            last_5_actions, synced_at
        FROM outreach_contacts
        WHERE account_bpid = {_PH}
        ORDER BY
            CASE WHEN key_executive_contact IS NOT NULL AND key_executive_contact <> '' THEN 0 ELSE 1 END,
            COALESCE(engaged_score, 0) DESC
    """, [account_bp])

    return {
        "account_bp": account_bp,
        "contacts":   rows,
        "total":      len(rows),
    }


@router.get("/contacts/search/{account_name}")
def search_contacts_by_name(account_name: str, _user=Depends(verify_token)):
    """Search contacts by account name (partial, case-insensitive) — for testing/lookup."""
    like = f"%{account_name}%"
    rows = query(f"""
        SELECT oc.account_bpid, oc.prospect_id,
               oc.first_name, oc.last_name, oc.title,
               oc.function, oc.engaged_score, oc.key_executive_contact,
               oc.role_erp, oc.score_erp
        FROM outreach_contacts oc
        JOIN (
            SELECT DISTINCT account_bp, account_name
            FROM pipeline
            WHERE snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
              AND LOWER(account_name) LIKE LOWER({_PH})
              AND account_bp IS NOT NULL
        ) p ON p.account_bp = oc.account_bpid
        ORDER BY oc.engaged_score DESC NULLS LAST
        LIMIT 50
    """, [like])
    return {"contacts": rows}


@router.get("/assignments/{opportunity_id}")
def get_assignments(opportunity_id: str, _user=Depends(verify_token)):
    """Return all suggestion assignments for an opportunity."""
    rows = query(f"""
        SELECT id, opportunity_id, suggestion, accepted,
               prospect_id, contact_name, notes, updated_by, updated_at
        FROM opp_suggestion_assignments
        WHERE opportunity_id = {_PH}
        ORDER BY suggestion
    """, [opportunity_id])
    return {"opportunity_id": opportunity_id, "assignments": rows}


@router.post("/assignments")
def upsert_assignment(body: dict, user=Depends(verify_token)):
    """Create or update a suggestion assignment."""
    opp_id      = body.get("opportunity_id")
    suggestion  = body.get("suggestion")
    if not opp_id or not suggestion:
        raise HTTPException(status_code=400, detail="opportunity_id and suggestion are required")

    accepted     = body.get("accepted", 0)       # 0=pending 1=yes 2=no
    prospect_id  = body.get("prospect_id")
    contact_name = body.get("contact_name")
    notes        = body.get("notes")
    updated_by   = getattr(user, "name", None) or getattr(user, "email", "unknown")

    if _DRIVER == "hana":
        query(f"""
            MERGE INTO DBADMIN.OPP_SUGGESTION_ASSIGNMENTS AS t
            USING (SELECT ? AS opportunity_id, ? AS suggestion FROM DUMMY) AS s
            ON (t.opportunity_id = s.opportunity_id AND t.suggestion = s.suggestion)
            WHEN MATCHED THEN UPDATE SET
                accepted = ?, prospect_id = ?, contact_name = ?,
                notes = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (opportunity_id, suggestion, accepted, prospect_id, contact_name, notes, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [opp_id, suggestion,
              accepted, prospect_id, contact_name, notes, updated_by,
              opp_id, suggestion, accepted, prospect_id, contact_name, notes, updated_by])
    else:
        query(f"""
            INSERT INTO opp_suggestion_assignments
                (opportunity_id, suggestion, accepted, prospect_id, contact_name, notes, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (opportunity_id, suggestion)
            DO UPDATE SET
                accepted = EXCLUDED.accepted,
                prospect_id = EXCLUDED.prospect_id,
                contact_name = EXCLUDED.contact_name,
                notes = EXCLUDED.notes,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
        """, [opp_id, suggestion, accepted, prospect_id, contact_name, notes, updated_by])

    return {"ok": True, "opportunity_id": opp_id, "suggestion": suggestion}


@router.delete("/assignments/{opportunity_id}/{suggestion}")
def delete_assignment(opportunity_id: str, suggestion: str, _user=Depends(verify_token)):
    """Remove a suggestion assignment."""
    query(f"""
        DELETE FROM opp_suggestion_assignments
        WHERE opportunity_id = {_PH} AND suggestion = {_PH}
    """, [opportunity_id, suggestion])
    return {"ok": True}


@router.get("/sync-status")
def sync_status(_user=Depends(verify_token)):
    """Return stats about the outreach_contacts cache."""
    rows = query(f"""
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT account_bpid) AS accounts,
               MAX(synced_at) AS last_sync
        FROM outreach_contacts
    """)
    return rows[0] if rows else {"total": 0, "accounts": 0, "last_sync": None}


# ── Competitors ───────────────────────────────────────────────────────────────

@router.get("/competitors/{opportunity_id}")
def get_competitors(opportunity_id: str, _user=Depends(verify_token)):
    """Return competitor assignments for an opportunity, grouped by solution_l2."""
    rows = query(f"""
        SELECT id, opportunity_id, solution_l2, competitors, notes, updated_by, updated_at
        FROM opp_competitors
        WHERE opportunity_id = {_PH}
        ORDER BY solution_l2
    """, [opportunity_id])
    return {"opportunity_id": opportunity_id, "competitors": rows}


@router.post("/competitors")
def upsert_competitor(body: dict, user=Depends(verify_token)):
    """Upsert competitor info for a solution_l2 within an opportunity."""
    import json as _json
    opp_id     = body.get("opportunity_id")
    sol_l2     = body.get("solution_l2")
    if not opp_id or not sol_l2:
        raise HTTPException(status_code=400, detail="opportunity_id and solution_l2 are required")

    competitors = _json.dumps(body.get("competitors") or [])
    notes       = body.get("notes") or ""
    updated_by  = getattr(user, "name", None) or getattr(user, "email", "unknown")

    if _DRIVER == "hana":
        query(f"""
            MERGE INTO DBADMIN.OPP_COMPETITORS AS t
            USING (SELECT ? AS opportunity_id, ? AS solution_l2 FROM DUMMY) AS s
            ON (t.opportunity_id = s.opportunity_id AND t.solution_l2 = s.solution_l2)
            WHEN MATCHED THEN UPDATE SET
                competitors = ?, notes = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (opportunity_id, solution_l2, competitors, notes, updated_by)
            VALUES (?, ?, ?, ?, ?)
        """, [opp_id, sol_l2, competitors, notes, updated_by,
              opp_id, sol_l2, competitors, notes, updated_by])
    else:
        query(f"""
            INSERT INTO opp_competitors
                (opportunity_id, solution_l2, competitors, notes, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (opportunity_id, solution_l2)
            DO UPDATE SET
                competitors = EXCLUDED.competitors,
                notes = EXCLUDED.notes,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
        """, [opp_id, sol_l2, competitors, notes, updated_by])

    return {"ok": True}


# ── Review Email ──────────────────────────────────────────────────────────────

@router.post("/send-review-email")
def send_review_email(body: dict, user=Depends(verify_token)):
    """Send a pipeline review summary email via Microsoft Graph (Outlook)."""
    import requests as _req, json as _json
    from datetime import date

    to          = body.get("to", "")
    cc          = body.get("cc", "")
    opp_ids     = body.get("opportunity_ids", [])
    cal_token   = body.get("cal_token", "")

    if not to or not opp_ids:
        raise HTTPException(status_code=400, detail="to and opportunity_ids are required")
    if not cal_token:
        raise HTTPException(status_code=400, detail="cal_token (Outlook token) is required")

    # Fetch opps data
    if opp_ids:
        ph = ",".join([_PH] * len(opp_ids))
        opps = query(f"""
            SELECT DISTINCT opportunity_id, account_name, opp_description,
                   opp_owner_name, closing_qtr, drm_category, acv_total_opp,
                   sap_mastercode
            FROM pipeline
            WHERE opportunity_id IN ({ph})
              AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
        """, opp_ids)
    else:
        opps = []

    # Fetch assignments
    asgn_map = {}
    for opp in opps:
        oid = opp["opportunity_id"]
        rows = query(f"SELECT suggestion, accepted, contact_name FROM opp_suggestion_assignments WHERE opportunity_id = {_PH}", [oid])
        asgn_map[oid] = rows

    # Fetch competitors
    comp_map = {}
    for opp in opps:
        oid = opp["opportunity_id"]
        rows = query(f"SELECT solution_l2, competitors FROM opp_competitors WHERE opportunity_id = {_PH}", [oid])
        comp_map[oid] = rows

    accepted_labels = {0: "Pendente", 1: "✓ Sim", 2: "✗ Não"}

    def fmt_acv(v):
        try: return f"${float(v)/1000:,.1f}k"
        except: return "—"

    # Build HTML
    rows_html = ""
    for o in opps:
        oid    = o["opportunity_id"]
        asgns  = asgn_map.get(oid, [])
        comps  = comp_map.get(oid, [])
        sugs_html = "".join(
            f"<li><b>{a['suggestion']}</b>: {accepted_labels.get(a.get('accepted',0),'—')}"
            f"{' — ' + a['contact_name'] if a.get('contact_name') else ''}</li>"
            for a in asgns
        ) or "<li>Nenhuma</li>"
        comps_html = "".join(
            f"<li><b>{c['solution_l2']}</b>: {c['competitors']}</li>"
            for c in comps if c.get("competitors")
        ) or "<li>Nenhum</li>"
        rows_html += f"""
        <tr>
          <td style="padding:8px;border:1px solid #e5e7eb">{o.get('account_name','—')}</td>
          <td style="padding:8px;border:1px solid #e5e7eb">{o.get('opp_description','—')}</td>
          <td style="padding:8px;border:1px solid #e5e7eb">{o.get('opp_owner_name','—')}</td>
          <td style="padding:8px;border:1px solid #e5e7eb">{o.get('closing_qtr','—')}</td>
          <td style="padding:8px;border:1px solid #e5e7eb">{fmt_acv(o.get('acv_total_opp',0))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb"><ul style="margin:0;padding-left:16px">{sugs_html}</ul></td>
          <td style="padding:8px;border:1px solid #e5e7eb"><ul style="margin:0;padding-left:16px">{comps_html}</ul></td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Inter,Arial,sans-serif;color:#1f2937;max-width:900px">
      <div style="background:#0070f2;padding:20px 28px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;color:#fff;font-size:18px">Resumo da Revisão de Pipeline</h2>
        <p style="margin:4px 0 0;color:#bfdbfe;font-size:13px">{date.today().strftime('%d/%m/%Y')} · {len(opps)} oportunidades revisadas</p>
      </div>
      <div style="padding:20px 0">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#f1f5f9">
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">Conta</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">Oportunidade</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">AE</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">Quarter</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">ACV</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">Sugestões SAP</th>
              <th style="padding:10px 8px;border:1px solid #e5e7eb;text-align:left">Concorrentes</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
      <p style="font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:12px">
        Enviado via DH SA Portal · {date.today().year}
      </p>
    </div>"""

    # Send via Microsoft Graph
    headers = {"Authorization": f"Bearer {cal_token}", "Content-Type": "application/json"}
    payload = {
        "message": {
            "subject": f"Revisão de Pipeline SAP — {date.today().strftime('%d/%m/%Y')}",
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to.split(",") if a.strip()],
            "ccRecipients": [{"emailAddress": {"address": a.strip()}} for a in cc.split(",") if a.strip()],
        },
        "saveToSentItems": True,
    }
    resp = _req.post("https://graph.microsoft.com/v1.0/me/sendMail", json=payload, headers=headers, timeout=15)
    if resp.status_code not in (200, 202):
        raise HTTPException(status_code=502, detail=f"Graph API error: {resp.text[:200]}")

    return {"ok": True, "sent_to": to, "opps": len(opps)}


# ── Account Solution Map ───────────────────────────────────────────────────────

@router.get("/account-solution-map/{account_bp}")
def get_account_solution_map(account_bp: str, _user=Depends(verify_token)):
    """
    Return the solution map for an account:
    - solutions present in pipeline (latest snapshot)
    - suggestions (gaps not yet in pipeline)
    - saved competitors per solution + LAC pre-suggestions
    """
    # 1. Pipeline solutions for this account
    pip_rows = query(f"""
        SELECT DISTINCT solution_area_l1 AS sol
        FROM pipeline
        WHERE account_bp = {_PH}
          AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND solution_area_l1 IS NOT NULL AND solution_area_l1 <> ''
    """, [account_bp])
    pip_rows += query(f"""
        SELECT DISTINCT sub_solution_area_l2 AS sol
        FROM pipeline
        WHERE account_bp = {_PH}
          AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
          AND sub_solution_area_l2 IS NOT NULL AND sub_solution_area_l2 <> ''
    """, [account_bp])
    solutions_in_pipeline = list({r["sol"] for r in pip_rows if r.get("sol")})

    # 2. Account name
    acct = query(f"""
        SELECT MAX(account_name) AS account_name, MAX(sap_mastercode) AS industry
        FROM pipeline
        WHERE account_bp = {_PH}
          AND snapshot_week = (SELECT MAX(snapshot_week) FROM pipeline)
    """, [account_bp])
    account_name = acct[0]["account_name"] if acct else account_bp
    industry     = acct[0]["industry"] if acct else ""

    # 3. Saved competitor rows for this account
    try:
        saved_rows = query(f"""
            SELECT solution, competitors, notes, updated_by, updated_at
            FROM account_solution_map
            WHERE account_bp = {_PH}
            ORDER BY solution
        """, [account_bp])
    except Exception:
        saved_rows = []

    saved_map = {r["solution"]: r for r in saved_rows}

    # 4. Build unified solution list:
    #    - all solutions from the LAC reference map (canonical full list)
    #    - plus any pipeline solutions not already in the map
    #    - plus any additional saved ones
    CANONICAL_SOLUTIONS = [
        # Cloud ERP
        "Cloud ERP Private (S/4HANA Cloud)",
        "Cloud ERP Public — GROW",
        "Cloud ERP Finance",
        "Cloud ERP Retail",
        "Cloud ERP Utilities",
        # Supply Chain
        "Supply Chain — SAP IBP (Planning)",
        "Supply Chain — SAP EWM (Warehouse)",
        "Supply Chain — SAP TM (Transportation)",
        # Finance & Spend
        "SAP Ariba (Procurement)",
        "SAP Concur (Travel & Expense)",
        "SAP Fieldglass (External Workforce)",
        # HCM
        "SAP SuccessFactors — Core HR / Payroll",
        "SAP SuccessFactors — Recruiting & Talent",
        "SAP SuccessFactors — Learning",
        "SAP SuccessFactors — Performance & Goals",
        # CX
        "SAP Commerce Cloud",
        "SAP Emarsys (Marketing Automation)",
        "SAP Sales Cloud (CRM)",
        "SAP Service Cloud",
        "SAP Customer Data Platform",
        # Data & Analytics
        "SAP Analytics Cloud — BI",
        "SAP Analytics Cloud — Planning / FP&A",
        "SAP Datasphere / HANA Cloud",
        # BTP
        "SAP Integration Suite (BTP)",
        "SAP Build — Low-code / Automation",
        "SAP Joule — Generative AI",
        # Business Transformation
        "SAP Signavio (Process Mining)",
        "SAP LeanIX (Enterprise Architecture)",
        "SAP Sustainability",
    ]

    all_solutions = list(dict.fromkeys(
        CANONICAL_SOLUTIONS
        + solutions_in_pipeline
        + [r["solution"] for r in saved_rows]
    ))

    # 5. Build response entries
    entries = []
    for sol in all_solutions:
        saved  = saved_map.get(sol, {})
        lac    = _get_lac_competitors(sol)
        try:
            saved_competitors = _json.loads(saved.get("competitors") or "[]")
        except Exception:
            saved_competitors = []

        # Pre-suggest tier1 + lac if nothing saved yet
        suggested = lac["tier1"] + lac["lac"] if not saved_competitors else []

        entries.append({
            "solution":            sol,
            "in_pipeline":         sol in solutions_in_pipeline,
            "saved_competitors":   saved_competitors,
            "suggested_competitors": suggested,
            "tier1":               lac["tier1"],
            "tier2":               lac["tier2"],
            "lac":                 lac["lac"],
            "notes":               saved.get("notes", ""),
            "updated_by":          saved.get("updated_by", ""),
            "updated_at":          str(saved.get("updated_at", "")) if saved.get("updated_at") else "",
        })

    return {
        "account_bp":   account_bp,
        "account_name": account_name,
        "industry":     industry,
        "solutions":    entries,
    }


@router.post("/account-solution-map")
def upsert_account_solution_map(body: dict, user=Depends(verify_token)):
    """Upsert a competitor entry for an account+solution."""
    account_bp   = body.get("account_bp", "").strip()
    account_name = body.get("account_name", "")
    solution     = body.get("solution", "").strip()
    if not account_bp or not solution:
        raise HTTPException(status_code=400, detail="account_bp and solution are required")

    competitors  = _json.dumps(body.get("competitors") or [])
    notes        = body.get("notes") or ""
    updated_by   = getattr(user, "name", None) or getattr(user, "email", "unknown")
    if isinstance(user, dict):
        updated_by = user.get("name") or user.get("email", "unknown")

    if _DRIVER == "hana":
        query(f"""
            MERGE INTO DBADMIN.ACCOUNT_SOLUTION_MAP AS t
            USING (SELECT ? AS account_bp, ? AS solution FROM DUMMY) AS s
            ON (t.account_bp = s.account_bp AND t.solution = s.solution)
            WHEN MATCHED THEN UPDATE SET
                account_name = ?, competitors = ?, notes = ?,
                updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (account_bp, account_name, solution, competitors, notes, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [account_bp, solution,
              account_name, competitors, notes, updated_by,
              account_bp, account_name, solution, competitors, notes, updated_by])
    else:
        query(f"""
            INSERT INTO account_solution_map
                (account_bp, account_name, solution, competitors, notes, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (account_bp, solution)
            DO UPDATE SET
                account_name = EXCLUDED.account_name,
                competitors  = EXCLUDED.competitors,
                notes        = EXCLUDED.notes,
                updated_by   = EXCLUDED.updated_by,
                updated_at   = NOW()
        """, [account_bp, account_name, solution, competitors, notes, updated_by])

    return {"ok": True, "account_bp": account_bp, "solution": solution}

