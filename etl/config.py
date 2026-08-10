"""
ETL config — BTP version.
FILES dict is only used as fallback; in production files come via upload.
"""
import os
from pathlib import Path

# FILES paths are unused in BTP (files arrive via HTTP upload).
# Kept here so imports don't break — ETL modules receive file path as argument.
SOURCE_DIR = Path(os.getenv("DH_SOURCE_DIR", "/tmp"))

FILES = {k: SOURCE_DIR / v for k, v in {
    "pipeline":      "Global Cloud Pipeline Data Analyzer_2026.csv",
    "pipeline_lw":   "Global Cloud Pipeline Data Analyzer_2026_last week.csv",
    "melody":        "CA Melody Activity Details Data Analyzer_2026.csv",
    "presales":      "Deal Management Cloud Opportunity Details_2026.csv",
    "full_vat":      "Sales Team VAT Report Data Analyzer_2026_full.csv",
    "dh_vat":        "Sales Team VAT Report Data Analyzer_DH_2026.csv",
    "dda":           "DET.xlsx",
    "lac_cloud_erp": "LAC Cloud ERP.xlsx",
    "targets":       "Targets.xlsx",
    "rvp":           "Master_RPV_Global_Rating_2026.xlsx",
    "pipeline_2025": "Global Cloud Pipeline Data Analyzer_2025.csv",
    "full_vat_2025": "Sales Team VAT Report Data Analyzer_2025_full.csv",
    "dh_vat_2025":   "Sales Team VAT Report Data Analyzer_DH_2025.csv",
    "melody_2025":   "CA Melody Activity Details Data Analyzer_2025.csv",
}.items()}

LOBS_SUB_TEAMS = {
    "BAIP": ["Mariane Pacheco", "Christian Barrios", "Matias Gomez",
             "Marcelo Ferreira Rossi de Viveiros"],
    "HCM":  ["Cesar Berrios", "Maria Rosario Sebastian", "John Tu"],
    "FSM":  ["Viviana Ramirez", "Alison Ferreira", "Catalina Clopatofsky"],
    "CX":   ["Lucas Suassuna"],
    "SCM":  ["Alex Santili", "Natalia Torres"],
}

SCM_MEMBERS = set(LOBS_SUB_TEAMS["SCM"])
LOBS_INUMBER_SUBTEAM: dict[str, str] = {}
