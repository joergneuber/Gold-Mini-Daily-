"""Regression tests for the Gold Mini economic-event layer."""
import datetime as dt
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import economic_events as m


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def test_normalizer_core_aliases():
    cases = {
        "ADP Weekly Employment Change": "ADP",
        "Non-Farm Employment Change": "NFP",
        "Core CPI y/y": "CPI",
        "PPI m/m": "PPI",
        "JOLTS Job Openings": "JOLTS",
        "PCE Price Index": "PCE",
        "Gross Domestic Product q/q": "GDP",
        "ISM Manufacturing PMI": "ISM",
        "Unemployment Claims": "JOBLESS_CLAIMS",
        "Federal Funds Rate": "FOMC",
        "FOMC Press Conference": "FOMC",
        "ECB Interest Rate Decision": "ECB",
    }
    for title, expected in cases.items():
        country = "EUR" if title.startswith("ECB") else "USD"
        actual = m._normalize_macro_event({"country": country, "title": title})
        _assert(actual == expected, f"{title}: expected {expected}, got {actual}")


def test_normalizer_false_positive_guards():
    _assert(m._normalize_macro_event({"country": "USD", "title": "ADP Employment Change"}) == "ADP",
            "ADP must not be classified as NFP")
    _assert(m._normalize_macro_event({"country": "USD", "title": "GDPNow"}) is None,
            "GDPNow must not be classified as GDP")
    _assert(m._normalize_macro_event({"country": "USD", "title": "CPI Expectations"}) is None,
            "CPI Expectations must not be classified as CPI")


def test_forexfactory_cache_is_24h_and_same_day():
    today = dt.date(2026, 9, 25)
    payload = [{"country": "USD", "date": today.isoformat(), "title": "ADP Weekly Employment Change"}]
    cache_file = ROOT / ".test_gold_ff_cache.json"
    with patch.object(m, "FOREXFACTORY_CACHE_FILE", cache_file):
        try:
            response = type("R", (), {"json": lambda self: payload, "raise_for_status": lambda self: None})()
            with patch.object(m.requests, "get", return_value=response) as mocked:
                first, status1 = m._forexfactory_events(today)
                second, status2 = m._forexfactory_events(today)
                _assert(first == payload and second == payload, "cache payload mismatch")
                _assert(status1 == "LIVE", "first call must be LIVE")
                _assert(status2 == "CACHE", "second call must use CACHE")
                _assert(mocked.call_count == 1, "ForexFactory must be fetched once per cache window")
        finally:
            cache_file.unlink(missing_ok=True)


def test_official_fomc_fallback_uses_project_json():
    with patch.object(m, "_get", side_effect=RuntimeError("Fed disabled in unit test")):
        events = m._fetch_fed()
    _assert(events, "FOMC fallback must return events")
    dates = {e["date"] for e in events if e["canonical"] == "FOMC"}
    _assert("2026-10-28" in dates, "2026-10-28 FOMC date missing from JSON fallback")


def test_official_ecb_parser_distinguishes_meeting_types():
    html = """
    <div>08/10/2026 Governing Council of the ECB: monetary policy meeting</div>
    <div>22/10/2026 Governing Council of the ECB: non-monetary policy meeting</div>
    """
    response = type("R", (), {"text": html, "raise_for_status": lambda self: None})()
    with patch.object(m, "_get", return_value=response):
        events = m._fetch_ecb()
    names = {(e["date"], e["name"], e["time_confirmed"]) for e in events}
    _assert(("2026-10-08", "EZB-Geldpolitische Sitzung", False) in names, "ECB monetary meeting missing")
    _assert(("2026-10-22", "EZB-Rat: Nicht-geldpolitische Sitzung", False) in names, "ECB non-monetary meeting missing")




def test_unconfirmed_event_today_survives_time_window():
    now = dt.datetime(2026, 9, 25, 8, 57, tzinfo=m.TZ_DE)
    end = now + dt.timedelta(days=7)
    event = {
        "name": "EZB-Geldpolitische Sitzung",
        "datetime": "2026-09-25T00:00:00+02:00",
        "date": "2026-09-25",
        "time_confirmed": False,
    }
    _assert(m._event_in_window(event, now, end),
            "an unconfirmed event dated today must remain in the calendar window")


def test_fallback_2026_restored_and_structured():
    events = m._fallback_2026()
    _assert(events, "official-calendar fallback must exist")
    names = {e["name"] for e in events}
    _assert("US CPI" in names, "CPI fallback missing")
    _assert("US Employment Situation / NFP" in names, "NFP fallback missing")
    _assert("FOMC / Fed-Zinsentscheid" in names, "FOMC fallback missing")
    _assert(all(e.get("source") == "Official-calendar fallback" for e in events),
            "fallback source metadata changed")


def test_lade_termine_integration_pipeline_runs_without_legacy_nameerror():
    """Durchläuft lade_termine() ohne die alte _sanitize_fed_events-Regression.

    Externe Quellen werden kontrolliert gemockt; der reale Kontrollfluss von
    lade_termine(), inklusive Sanitizing, Deduplizierung, Zeitfenster und Cache,
    wird dabei tatsächlich ausgeführt.
    """
    now = dt.date(2026, 9, 25)
    cache_file = ROOT / ".test_gold_calendar_cache.json"
    fake_fed = [
        {
            "name": "FOMC / Fed-Zinsentscheid",
            "priority": m.VERY_HIGH,
            "datetime": "2026-10-28T20:00:00+01:00",
            "date": "2026-10-28",
            "source": "Federal Reserve",
            "canonical": "FOMC",
            "time_confirmed": True,
        },
        {
            "name": "FOMC / Fed-Zinsentscheid",
            "priority": m.VERY_HIGH,
            "datetime": "2026-11-18T20:00:00+01:00",
            "date": "2026-11-18",
            "source": "Federal Reserve",
            "canonical": "FOMC",
            "time_confirmed": True,
        },
    ]
    fake_ecb = [{
        "name": "EZB-Geldpolitische Sitzung",
        "priority": m.VERY_HIGH,
        "datetime": "2026-09-25T00:00:00+02:00",
        "date": "2026-09-25",
        "source": "ECB official calendar",
        "canonical": "ECB",
        "time_confirmed": False,
    }]
    fake_bls = [{
        "name": "US CPI", "priority": m.VERY_HIGH,
        "datetime": "2026-09-30T14:30:00+02:00", "date": "2026-09-30",
        "source": "BLS", "canonical": "CPI", "time_confirmed": True,
    }]
    try:
        with patch.object(m, "CACHE_FILE", cache_file), \
             patch.object(m, "_fetch_bls", return_value=fake_bls), \
             patch.object(m, "_fetch_bls_html", return_value=[]), \
             patch.object(m, "_fetch_bea", return_value=[]), \
             patch.object(m, "_fetch_fed", return_value=fake_fed), \
             patch.object(m, "_fetch_ecb", return_value=fake_ecb), \
             patch.object(m, "_forexfactory_events", return_value=([], "CACHE")):
            events, errors = m.lade_termine(days_ahead=45)
        names_dates = {(e["name"], e["date"]) for e in events}
        _assert(not errors, f"unexpected source errors: {errors}")
        _assert(("FOMC / Fed-Zinsentscheid", "2026-10-28") in names_dates,
                "valid FOMC event missing after integration pipeline")
        _assert(("FOMC / Fed-Zinsentscheid", "2026-11-18") not in names_dates,
                "invalid FOMC date was not sanitized")
        _assert(("EZB-Geldpolitische Sitzung", "2026-09-25") in names_dates,
                "today's unconfirmed ECB event was filtered out")
        _assert(("US CPI", "2026-09-30") in names_dates,
                "BLS event missing after integration pipeline")
    finally:
        cache_file.unlink(missing_ok=True)


def test_gold_focus_has_no_directional_signal():
    today = dt.date(2026, 9, 25)
    events = [
        {"country": "USD", "date": today.isoformat(), "title": "ADP Weekly Employment Change"},
        {"country": "EUR", "date": "2026-09-26", "title": "ECB Interest Rate Decision"},
    ]
    focus, candidates = m._macro_focus_from_calendar(today, events)
    _assert(focus is not None, "focus missing")
    _assert(focus["canonical"] == "ADP", "today's event must take precedence")
    _assert(len(candidates) == 2, "candidate count incorrect")
    _assert("signal" not in focus, "MACRO_FOCUS must not contain a trading signal")
    _assert("buy" not in str(focus).lower(), "MACRO_FOCUS must not contain buy")
    _assert("sell" not in str(focus).lower(), "MACRO_FOCUS must not contain sell")


def test_briefing_interface_remains_compatible():
    today = dt.date(2026, 9, 25)
    events = [
        {"name": "ADP: ADP Weekly Employment Change", "priority": "HIGH", "datetime": "2026-09-25T14:15:00+02:00", "date": today.isoformat(), "source": "ForexFactory (LIVE)", "canonical": "ADP", "time_confirmed": False},
        {"name": "FOMC / Fed-Zinsentscheid", "priority": "VERY_HIGH", "datetime": "2026-10-28T20:00:00+01:00", "date": "2026-10-28", "source": "Federal Reserve", "canonical": "FOMC", "time_confirmed": True},
    ]
    with patch.object(m, "lade_termine", return_value=(events, [])):
        text, returned = m.briefing_block(days_ahead=7)
    _assert("GOLD – WICHTIGE MAKRO-EVENTS" in text, "Gold heading missing")
    _assert("MACRO_FOCUS=ADP" not in text, "technical MACRO_FOCUS metadata must stay hidden")
    _assert("MACRO_FOCUS_REGEL:" not in text, "technical MACRO_FOCUS rule must stay hidden")
    _assert(returned == events, "briefing return contract changed")


if __name__ == "__main__":
    tests = [
        test_normalizer_core_aliases,
        test_normalizer_false_positive_guards,
        test_forexfactory_cache_is_24h_and_same_day,
        test_official_fomc_fallback_uses_project_json,
        test_official_ecb_parser_distinguishes_meeting_types,
        test_unconfirmed_event_today_survives_time_window,
        test_fallback_2026_restored_and_structured,
        test_lade_termine_integration_pipeline_runs_without_legacy_nameerror,
        test_gold_focus_has_no_directional_signal,
        test_briefing_interface_remains_compatible,
    ]
    for test in tests:
        test()
        print("PASS:", test.__name__)
    print(f"GOLD_ECONOMIC_EVENTS_TESTS: {len(tests)}/{len(tests)} PASS")
