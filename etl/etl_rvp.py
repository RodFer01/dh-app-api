"""
ETL: Master_RPV_Global_Rating_2026.xlsx → rvp table
Header is at row index 3 (0-based).
"""
import pandas as pd
from utils import get_conn, get_logger, upsert, clean_str, clean_num, clean_int, clean_date, truncate
from config import FILES

log = get_logger("etl.rvp")

_BOOL_COLS = {"R2Q_Flag", "R4Q_Flag", "Included_in_TCP"}


def _to_bool(val) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "x"):
        return True
    if s in ("0", "false", "no", "", "nan"):
        return False
    return None


def run() -> int:
    log.info("Loading RVP xlsx …")
    df = pd.read_excel(FILES["rvp"], sheet_name=0, header=3)
    df.columns = [str(c).strip() for c in df.columns]

    rows = []
    for _, r in df.iterrows():
        opp_id = clean_str(r.get("OpportunityId"))
        if not opp_id:
            continue
        rows.append({
            "opportunity_id":          opp_id,
            "account_id":              clean_str(r.get("Account_ID")),
            "crm_account_label":       clean_str(r.get("CRM_Account_Label")),
            "probability_pct":         clean_num(r.get("Probability_%")),
            "global_rank":             clean_int(r.get("Global_Rank")),
            "tier":                    clean_str(r.get("Tier")),
            "focus":                   clean_str(r.get("Focus")),
            "planning_entity":         clean_str(r.get("Planning_Entity")),
            "planning_entity_id":      clean_str(r.get("Planning_Entity_Id")),
            "url":                     clean_str(r.get("URL")),
            "region":                  clean_str(r.get("Region")),
            "region_l2":               clean_str(r.get("Region_L2")),
            "region_l3":               clean_str(r.get("Region_L3")),
            "region_l4":               clean_str(r.get("Region_L4")),
            "region_l5":               clean_str(r.get("Region_L5")),
            "opportunity_description": clean_str(r.get("Opportunity_Description")),
            "create_date":             clean_date(r.get("Create_Date")),
            "age":                     clean_int(r.get("Age")),
            "account_owner":           clean_str(r.get("Account_Owner")),
            "opportunity_source":      clean_str(r.get("Opportunity_Source")),
            "distribution_channel":    clean_str(r.get("Distribution_Channel")),
            "opportunity_origin":      clean_str(r.get("Opportunity_Origin")),
            "channel_partner":         clean_str(r.get("Channel_Partner")),
            "channel_partner_id":      clean_str(r.get("Channel_Partner_Id")),
            "internal_market_segment": clean_str(r.get("Internal_Market_Segment")),
            "industry_mastercode":     clean_str(r.get("Industry_Mastercode")),
            "mastercode_label":        clean_str(r.get("Mastercode_Label")),
            "rating_date":             clean_date(r.get("Rating_Date")),
            "expected_revenue_date":   clean_date(r.get("Expected_Revenue_Date")),
            "expected_revenue_quarter": clean_str(r.get("Expected_Revenue_Quarter")),
            "expected_revenue_year":   clean_int(r.get("Expected_Revenue_Year")),
            "r2q_flag":                _to_bool(r.get("R2Q_Flag")),
            "r4q_flag":                _to_bool(r.get("R4Q_Flag")),
            "sales_phase":             clean_str(r.get("Sales_Phase")),
            "sales_phase_age":         clean_int(r.get("Sales_Phase_Age")),
            "sales_phase_age_range":   clean_str(r.get("Sales_Phase_Age_Range")),
            "internal_sales_segment":  clean_str(r.get("Internal_Sales_Segment")),
            "internal_account_class":  clean_str(r.get("Internal_Account_Classification")),
            "buying_lifecycle":        clean_str(r.get("Buying_Lifecycle")),
            "forecast_qualification":  clean_str(r.get("Forecast_Qualification")),
            "slip_number":             clean_int(r.get("Slip_Number")),
            "mino_interactions":       clean_int(r.get("MINO_Interactions")),
            "maeo_interactions":       clean_int(r.get("MAEO_Interactions")),
            "nominal_value":           clean_num(r.get("Nominal_Value")),
            "included_in_tcp":         _to_bool(r.get("Included_in_TCP")),
            "tcp_value":               clean_num(r.get("TCP_Value")),
            "rpv":                     clean_num(r.get("RPV")),
            "region_rank":             clean_int(r.get("RegionRank")),
            "vp_rank":                 clean_int(r.get("VPRank")),
            "iss_rank":                clean_int(r.get("ISSRank")),
            "ims_rank":                clean_int(r.get("IMSRank")),
            "source_rank":             clean_int(r.get("SourceRank")),
            "l2_btp":                  clean_num(r.get("L2_Business_Technology_Platform")),
            "l2_btm":                  clean_num(r.get("L2_Business_Transformation_Management")),
            "l2_cloud_erp_private":    clean_num(r.get("L2_Cloud_ERP_Private")),
            "l2_cx":                   clean_num(r.get("L2_Customer_Experience")),
            "l2_data_ai":              clean_num(r.get("L2_Data_and_AI")),
            "l2_fsm":                  clean_num(r.get("L2_Finance_and_Spend_Management")),
            "l2_hcm":                  clean_num(r.get("L2_Human_Capital_Management")),
            "l2_other":                clean_num(r.get("L2_Other")),
            "l2_scm":                  clean_num(r.get("L2_Supply_Chain_Management")),
        })

    with get_conn() as conn:
        truncate(conn, "rvp")
        n = upsert(conn, "rvp", rows, ["opportunity_id"])

    log.info("rvp: %d rows loaded", n)
    return n


if __name__ == "__main__":
    run()
