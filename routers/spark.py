"""Spark Meeting sessions + FX rates router."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "presales-pipeline-app"))

from typing import Optional
from fastapi import APIRouter, Depends
from auth import verify_token
from database import query_one

try:
    import spark_meeting as sm
    _SPARK_AVAILABLE = True
except ImportError:
    sm = None
    _SPARK_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["spark", "fx"])


@router.get("/spark/sessions")
def get_all_sessions(_user=Depends(verify_token)):
    if not _SPARK_AVAILABLE:
        return {"sessions": [], "error": "Spark Meeting not available in this environment"}
    try:
        return sm._fetch_raw()
    except Exception as e:
        return {"error": str(e), "sessions": []}


@router.get("/spark/sessions/for-opp")
def sessions_for_opp(
    sol_l1: str,
    region: Optional[str] = None,
    language: Optional[str] = None,
    _user=Depends(verify_token),
):
    if not _SPARK_AVAILABLE:
        return []
    return sm.get_sessions_for_opp([sol_l1], region=region or "", language=language or "")


@router.get("/fx/rate")
def get_fx_rate(from_currency: str = "USD", to_currency: str = "EUR", _user=Depends(verify_token)):
    row = query_one(
        """SELECT rate, rate_date::text, source FROM fx_rates
           WHERE from_currency=%s AND to_currency=%s AND rate_date<=CURRENT_DATE
           ORDER BY rate_date DESC LIMIT 1""",
        [from_currency, to_currency]
    )
    return row or {"rate": 1.0, "rate_date": None, "source": "fallback"}
