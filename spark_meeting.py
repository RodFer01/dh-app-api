"""
spark_meeting.py — Firestore reader for Spark Meeting sessions.

Reads sessions from Firebase/Firestore and provides:
  - get_all_sessions()   → all 55+ sessions with parsed fields
  - get_sessions_for_opp(opportunity_id, pipeline_row) → matching sessions
    by mapping Solution Area L1 → Spark Meeting area
  - get_focus_opp_ids()  → set of opp IDs flagged as Spark Meeting focus
    (area match with at least one upcoming session)
"""
from __future__ import annotations
import os
from datetime import date, datetime
from functools import lru_cache
import requests

_FB_PROJECT = os.getenv("SPARK_FB_PROJECT", "spark-meeting-63e1b")
_FB_API_KEY  = os.getenv("SPARK_FB_API_KEY", "AIzaSyDu-8DaHrkbosQDMLRQZKvSyFNVavfl22s")

_SESSIONS_URL = (
    f"https://firestore.googleapis.com/v1/projects/{_FB_PROJECT}"
    f"/databases/(default)/documents/sessions?pageSize=500&key={_FB_API_KEY}"
)

# ── Area mapping: Pipeline Solution Area L1 → Spark Meeting area ──────────────
# Used to find relevant sessions for a given opportunity.
_L1_TO_SPARK: dict[str, list[str]] = {
    "Human Capital Management":           ["HCM", "HCM + Finance"],
    "Finance and Spend Management":       ["Finance", "HCM + Finance", "Procurement"],
    "Customer Experience":                ["CX"],
    "BAIP - Data & AI":                   ["BTP"],
    "BAIP - Technology Foundation":       ["BTP"],
    "Business Technology Platform":       ["BTP"],
    "Supply Chain Management":            ["SCM", "Supply Chain"],
    "Cloud ERP Private":                  ["Business Suite"],
    "Cloud ERP Public":                   ["Business Suite"],
}

# Inverse: spark area → list of L1 values it covers
_SPARK_TO_L1: dict[str, list[str]] = {}
for l1, spark_areas in _L1_TO_SPARK.items():
    for sa in spark_areas:
        _SPARK_TO_L1.setdefault(sa, []).append(l1)


def _parse_doc(doc: dict) -> dict | None:
    fields = doc.get("fields", {})
    if not fields:
        return None

    def _str(key: str) -> str:
        return fields.get(key, {}).get("stringValue", "") or ""

    def _bool(key: str) -> bool:
        return fields.get(key, {}).get("booleanValue", False)

    def _arr_str(key: str) -> list[str]:
        arr = fields.get(key, {}).get("arrayValue", {}).get("values", [])
        return [v.get("stringValue", "") for v in arr if v.get("stringValue")]

    def _arr_dates() -> list[str]:
        """Extract date strings from 'dates' array.
        Supports both old format (plain stringValue) and new format
        ({mapValue: {fields: {date: {stringValue: ...}, meetingLink, registrationLink}}}).
        """
        arr = fields.get("dates", {}).get("arrayValue", {}).get("values", [])
        result = []
        for v in arr:
            if "mapValue" in v:
                date_str = v["mapValue"].get("fields", {}).get("date", {}).get("stringValue", "")
                if date_str:
                    result.append(date_str)
            elif v.get("stringValue"):
                result.append(v["stringValue"])
        return result

    dates_raw = _arr_dates()
    dates_parsed: list[date] = []
    for d in dates_raw:
        try:
            dates_parsed.append(datetime.strptime(d, "%Y-%m-%d").date())
        except Exception:
            pass

    today = date.today()
    upcoming = sorted([d for d in dates_parsed if d >= today])
    past     = sorted([d for d in dates_parsed if d < today], reverse=True)

    return {
        "id":              _str("id"),
        "title":           _str("title"),
        "description":     _str("description"),
        "area":            _str("area"),
        "audience":        _str("audience"),
        "language":        _str("language"),
        "advisor_name":    _str("advisorName"),
        "advisor_email":   _str("advisorEmail"),
        "regions":         _arr_str("regions"),
        "dates":           dates_raw,
        "dates_parsed":    dates_parsed,
        "upcoming_dates":  upcoming,
        "past_dates":      past,
        "next_date":       upcoming[0].isoformat() if upcoming else None,
        "agenda_created":  _bool("agendaCreated"),
        "invite_created":  _bool("inviteCreated"),
        "last_edited_by":  _str("lastEditedBy"),
        "last_edited_at":  _str("lastEditedAt"),
    }


# ── Cached fetch — 5-minute TTL via Streamlit cache or lru_cache ──────────────

def _fetch_raw() -> list[dict]:
    r = requests.get(_SESSIONS_URL, timeout=10)
    r.raise_for_status()
    docs = r.json().get("documents", [])
    sessions = []
    for d in docs:
        parsed = _parse_doc(d)
        if parsed:
            sessions.append(parsed)
    return sorted(sessions, key=lambda s: s.get("next_date") or "9999")


@lru_cache(maxsize=1)
def get_all_sessions() -> list[dict]:
    """Cached at module level. Streamlit cache applied at call time to avoid import-time st.* access."""
    return _fetch_raw()


def _get_all_sessions_cached() -> list[dict]:
    """Returns sessions using st.cache_data if available, else lru_cache fallback."""
    try:
        import streamlit as _st
        # Bust lru_cache and delegate to streamlit cache
        if not hasattr(_get_all_sessions_cached, "_st_fn"):
            @_st.cache_data(ttl=300, show_spinner=False)
            def _st_cached():
                return _fetch_raw()
            _get_all_sessions_cached._st_fn = _st_cached
        return _get_all_sessions_cached._st_fn()
    except Exception:
        return get_all_sessions()


def get_upcoming_sessions(after: date | None = None) -> list[dict]:
    today = after or date.today()
    return [s for s in _get_all_sessions_cached() if s.get("next_date") and s["next_date"] >= today.isoformat()]


def get_sessions_for_areas(areas: list[str]) -> list[dict]:
    """Return sessions whose Spark area matches any of the given Solution Area L1 values."""
    target_spark_areas: set[str] = set()
    for l1 in areas:
        for sa in _L1_TO_SPARK.get(l1, []):
            target_spark_areas.add(sa)

    return [s for s in get_all_sessions() if s["area"] in target_spark_areas]


def get_sessions_for_opp(solution_areas_l1: list[str],
                          region: str = "",
                          language: str = "") -> list[dict]:
    """
    Return upcoming sessions relevant to an opportunity.
    Filters by area match; optionally by region and language.
    """
    candidates = get_sessions_for_areas(solution_areas_l1)
    upcoming   = [s for s in candidates if s.get("next_date")]

    # Optional region filter — partial case-insensitive match
    if region:
        r_lower = region.lower()
        region_filtered = [
            s for s in upcoming
            if not s["regions"]   # no region restriction = show to all
            or any(r_lower in reg.lower() or reg.lower() in r_lower
                   for reg in s["regions"])
        ]
        if region_filtered:
            upcoming = region_filtered

    # Optional language filter
    if language:
        lang_filtered = [s for s in upcoming if s["language"].upper() == language.upper()]
        if lang_filtered:
            upcoming = lang_filtered

    return upcoming


def areas_in_pipeline(solution_area_l1: str) -> list[str]:
    """Return Spark Meeting area labels that match a given Solution Area L1."""
    return _L1_TO_SPARK.get(solution_area_l1, [])


def format_sessions_for_email(sessions: list[dict], max_sessions: int = 5) -> str:
    """
    Format a list of sessions as a bullet list for email body.
    """
    if not sessions:
        return ""
    lines = []
    for s in sessions[:max_sessions]:
        dates_str = ", ".join(s["upcoming_dates"][i].strftime("%b %d") for i in range(min(3, len(s["upcoming_dates"]))))
        lang_flag  = "🇧🇷" if s["language"] == "PT" else "🇪🇸" if s["language"] == "ES" else ""
        lines.append(f"• {s['title']} {lang_flag} — {dates_str} ({s['area']})")
    return "\n".join(lines)
