"""Gold-relevanter Wirtschafts-/Makrotermin-Kalender.

Quellenhierarchie:
- offizielle Federal Reserve / FOMC-Termine
- offizieller EZB-Kalender
- offizieller BLS- und BEA-Veröffentlichungskalender
- ForexFactory als freie Event-Discovery-Schicht für zusätzliche US-/Euro-Events

ForexFactory liefert dabei nur Termin-Metadaten. Es werden daraus weder
Makrodatenwerte noch Buy-/Sell-Richtungen abgeleitet.

Das Modul erzeugt ausschließlich Kontext für den Gold-Report. Es blockiert
keine Trades und verändert keine Entry-/TP-/Stop-Regeln.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ_ET = ZoneInfo("America/New_York")
TZ_DE = ZoneInfo("Europe/Berlin")

CACHE_FILE = Path("economic_events_cache.json")
FOREXFACTORY_CACHE_FILE = Path("economic_events_forexfactory_cache.json")
FOREXFACTORY_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FOREXFACTORY_CACHE_MAX_AGE_HOURS = 24
ECB_MEETING_CALENDAR_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
FOMC_TERMS_FILE = Path(__file__).with_name("fomc_termine.json")

TIMEOUT = 15
HEADERS = {"User-Agent": "MiniDailyGold/2.0"}
HIGH = "HIGH"
MEDIUM = "MEDIUM"
VERY_HIGH = "VERY_HIGH"
_PRIORITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "VERY_HIGH": 4}


# Die Regeln definieren nur die Relevanz des Termins für den Kontext.
# Sie erzeugen ausdrücklich keine Handelsrichtung.
MACRO_EVENT_RULES = {
    "ADP": {
        "aliases": ("adp employment", "adp weekly employment", "adp national employment"),
        "priority": HIGH, "focus": "US_LABOR_MARKET", "fed": HIGH,
    },
    "NFP": {
        "aliases": ("non-farm", "non farm", "nonfarm", "non-farm payroll", "nonfarm payroll", "employment situation"),
        "priority": VERY_HIGH, "focus": "US_LABOR_MARKET", "fed": VERY_HIGH,
    },
    "CPI": {
        "aliases": ("consumer price index", "cpi"),
        "priority": VERY_HIGH, "focus": "US_INFLATION", "fed": VERY_HIGH,
    },
    "PPI": {
        "aliases": ("producer price index", "ppi"),
        "priority": HIGH, "focus": "US_INFLATION", "fed": HIGH,
    },
    "JOLTS": {
        "aliases": ("jolts", "job openings"),
        "priority": HIGH, "focus": "US_LABOR_MARKET", "fed": HIGH,
    },
    "PCE": {
        "aliases": ("personal consumption expenditures", "pce price", "pce"),
        "priority": VERY_HIGH, "focus": "US_INFLATION", "fed": VERY_HIGH,
    },
    "GDP": {
        "aliases": ("gross domestic product", "gdp"),
        "priority": HIGH, "focus": "US_GROWTH", "fed": HIGH,
    },
    "ISM": {
        "aliases": ("ism manufacturing", "ism services", "ism manufacturing pmi", "ism services pmi"),
        "priority": HIGH, "focus": "US_GROWTH", "fed": HIGH,
    },
    "JOBLESS_CLAIMS": {
        "aliases": ("initial jobless claims", "unemployment claims", "jobless claims", "continuing claims"),
        "priority": HIGH, "focus": "US_LABOR_MARKET", "fed": HIGH,
    },
    "FOMC": {
        "aliases": ("federal funds rate", "fomc", "fomc statement", "fomc press conference", "economic projections"),
        "priority": VERY_HIGH, "focus": "US_MONETARY_POLICY", "fed": VERY_HIGH,
    },
    "ECB": {
        "aliases": ("ecb interest rate decision", "ecb rate decision", "ecb monetary policy", "ecb press conference", "ecb deposit facility rate", "european central bank"),
        "priority": VERY_HIGH, "focus": "EUROPEAN_MONETARY_POLICY", "fed": MEDIUM,
    },
}

_MACRO_EVENT_NEGATIVE = {
    "NFP": ("adp",),
    "CPI": ("cpi expectations",),
    "PPI": ("ppi expectations",),
    "GDP": ("gdpnow",),
}


def _get(url: str, timeout: int = TIMEOUT):
    r = requests.get(url, timeout=timeout, headers=HEADERS)
    r.raise_for_status()
    return r


def _parse_ics(text: str):
    events = []
    blocks = re.split(r"BEGIN:VEVENT", text, flags=re.I)[1:]
    for block in blocks:
        block = block.split("END:VEVENT", 1)[0]

        def field(name):
            m = re.search(rf"^{re.escape(name)}[^:]*:(.+)$", block, re.I | re.M)
            return m.group(1).strip() if m else ""

        summary = field("SUMMARY")
        dtstart = field("DTSTART")
        if not summary or not dtstart:
            continue
        try:
            if re.fullmatch(r"\d{8}T\d{6}Z", dtstart):
                dt = datetime.strptime(dtstart, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            elif re.fullmatch(r"\d{8}T\d{6}", dtstart):
                dt = datetime.strptime(dtstart, "%Y%m%dT%H%M%S").replace(tzinfo=TZ_ET)
            elif re.fullmatch(r"\d{8}", dtstart):
                dt = datetime.strptime(dtstart, "%Y%m%d").replace(hour=8, tzinfo=TZ_ET)
            else:
                continue
        except ValueError:
            continue
        events.append((summary, dt.astimezone(TZ_DE)))
    return events


def _classify_bls(summary: str):
    s = summary.lower()
    if "consumer price index" in s:
        return "CPI", "US CPI", HIGH
    if "employment situation" in s:
        return "NFP", "US Employment Situation / NFP", VERY_HIGH
    if "producer price index" in s:
        return "PPI", "US PPI", HIGH
    if "job openings and labor turnover survey" in s:
        return "JOLTS", "US JOLTS", HIGH
    if "initial claims" in s or "unemployment insurance weekly claims" in s:
        return "JOBLESS_CLAIMS", "US Jobless Claims", HIGH
    return None


def _fetch_bls():
    text = _get(BLS_ICS_URL).text
    out = []
    for summary, dt in _parse_ics(text):
        item = _classify_bls(summary)
        if item:
            canonical, name, priority = item
            out.append(_event(name, priority, dt=dt, source="BLS", canonical=canonical))
    return out


def _fetch_bls_html():
    """HTML-Fallback für den offiziellen BLS-Monatskalender."""
    now = datetime.now(TZ_DE)
    out = []
    month_years = {(now.year, now.month), ((now + timedelta(days=31)).year, (now + timedelta(days=31)).month)}
    for year, month in sorted(month_years):
        url = f"https://www.bls.gov/schedule/{year}/{month:02d}_sched_list.htm"
        try:
            text = re.sub(r"<[^>]+>", " ", _get(url).text)
            text = re.sub(r"&nbsp;", " ", text, flags=re.I)
            text = re.sub(r"\s+", " ", text)
        except Exception:
            continue
        pattern = re.compile(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2}),\s+(\d{4})\s+(\d{1,2}:\d{2})\s+(AM|PM)\s+(.*?)(?=\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+|\s+NOTE:|$)",
            re.I,
        )
        for m in pattern.finditer(text):
            month_name, day, year_s, hhmm, ampm, desc = m.groups()
            item = _classify_bls(desc)
            if not item:
                continue
            try:
                dt = datetime.strptime(
                    f"{month_name} {day} {year_s} {hhmm} {ampm.upper()}",
                    "%B %d %Y %I:%M %p",
                ).replace(tzinfo=TZ_ET)
            except ValueError:
                continue
            canonical, name, priority = item
            out.append(_event(name, priority, dt=dt.astimezone(TZ_DE), source="BLS", canonical=canonical))
    return out


def _fallback_2026():
    """Notfall-Fallback für wichtige bereits terminierte 2026-Termine.

    Der Fallback ergänzt die Live-Quellen weiterhin, damit ein partiell
    erreichbarer oder unvollständiger Kalender nicht wichtige Termine verliert.
    Die Termine werden anschließend wie alle anderen Events dedupliziert und
    auf das angefragte Zeitfenster begrenzt.
    """
    dates = [
        ("US CPI", HIGH, "2026-08-12T08:30:00"),
        ("US PPI", MEDIUM, "2026-08-13T08:30:00"),
        ("FOMC Minutes (Juli-Sitzung)", HIGH, "2026-08-19T14:00:00"),
        ("US JOLTS", MEDIUM, "2026-09-01T10:00:00"),
        ("US Employment Situation / NFP", HIGH, "2026-09-04T08:30:00"),
        ("US PPI", MEDIUM, "2026-09-10T08:30:00"),
        ("US CPI", HIGH, "2026-09-11T08:30:00"),
        ("FOMC / Fed-Zinsentscheid", HIGH, "2026-09-16T14:00:00"),
    ]
    return [
        {
            "name": name,
            "priority": priority,
            "datetime": datetime.fromisoformat(ts).replace(tzinfo=TZ_ET).astimezone(TZ_DE).isoformat(),
            "source": "Official-calendar fallback",
            "date": ts[:10],
            "time_confirmed": True,
        }
        for name, priority, ts in dates
    ]


def _fetch_bea():
    html_text = _get(BEA_SCHEDULE_URL).text
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"\s+", " ", text)
    out = []
    year = datetime.now(TZ_DE).year
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+"
        r"(\d{1,2}:\d{2})\s*(AM|PM)\s+.*?(GDP[^|]*|Personal Income and Outlays[^|]*)",
        re.I,
    )
    for m in pattern.finditer(text):
        month, day, hhmm, ampm, desc = m.groups()
        try:
            dt = datetime.strptime(
                f"{month} {day} {year} {hhmm} {ampm.upper()}",
                "%B %d %Y %I:%M %p",
            ).replace(tzinfo=TZ_ET)
        except ValueError:
            continue
        if "personal income and outlays" in desc.lower():
            out.append(_event("US PCE / Personal Income & Outlays", VERY_HIGH, dt=dt, source="BEA", canonical="PCE"))
        elif "gdp" in desc.lower():
            out.append(_event("US GDP", HIGH, dt=dt, source="BEA", canonical="GDP"))
    return out


def _load_fomc_dates():
    try:
        payload = json.loads(FOMC_TERMS_FILE.read_text(encoding="utf-8"))
        dates = payload.get("termine", [])
        return [date.fromisoformat(str(x)) for x in dates]
    except Exception:
        return []


def _fetch_fed():
    """Offizieller FOMC-Kalender; die JSON-Datei ist der deterministische Fallback."""
    out = []
    try:
        text = re.sub(r"<[^>]+>", " ", _get(FOMC_CALENDAR_URL).text)
        text = re.sub(r"\s+", " ", text)
        year = datetime.now(TZ_DE).year
        months = {m: i for i, m in enumerate([
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ], 1)}
        section = re.search(rf"{year} FOMC Meetings(.*?)(?:{year+1} FOMC Meetings|$)", text, re.I)
        if section:
            for month, num in months.items():
                m = re.search(rf"\b{re.escape(month)}\s+(\d{{1,2}})(?:-(\d{{1,2}}))?", section.group(1))
                if not m:
                    continue
                try:
                    # FOMC decision is published on the second meeting day at 14:00 ET.
                    day = int(m.group(2) or m.group(1))
                    dt = datetime(year, num, day, 14, 0, tzinfo=TZ_ET)
                except ValueError:
                    continue
                out.append(_event("FOMC / Fed-Zinsentscheid", VERY_HIGH, dt=dt, source="Federal Reserve", canonical="FOMC"))
    except Exception:
        pass

    if not out:
        for d in _load_fomc_dates():
            dt = datetime(d.year, d.month, d.day, 14, 0, tzinfo=TZ_ET)
            out.append(_event("FOMC / Fed-Zinsentscheid", VERY_HIGH, dt=dt, source="Federal Reserve / fomc_termine.json", canonical="FOMC"))

    # The official Fed calendar does not expose historical Minutes as a simple
    # annual meeting table. Keep the known 2026 July Minutes fallback only.
    if datetime.now(TZ_DE).year == 2026:
        dt = datetime(2026, 8, 19, 14, 0, tzinfo=TZ_ET)
        out.append(_event("FOMC Minutes (Juli-Sitzung)", HIGH, dt=dt, source="Federal Reserve", canonical="FOMC"))
    return out


def _fetch_ecb():
    """Offizieller EZB-Kalender; Datum wird bewusst ohne erfundene Uhrzeit geführt."""
    try:
        text = re.sub(r"<[^>]+>", " ", _get(ECB_MEETING_CALENDAR_URL).text)
        text = re.sub(r"\s+", " ", html.unescape(text))
    except Exception:
        return []

    patterns = (
        (r"(\d{2}/\d{2}/\d{4})((?!\d{2}/\d{2}/\d{4}).){0,500}?Governing Council of the ECB:\s*non-monetary policy meeting",
         "EZB-Rat: Nicht-geldpolitische Sitzung", MEDIUM),
        (r"(\d{2}/\d{2}/\d{4})((?!\d{2}/\d{2}/\d{4}).){0,500}?Governing Council of the ECB:\s*monetary policy meeting",
         "EZB-Geldpolitische Sitzung", VERY_HIGH),
    )
    out = []
    seen = set()
    for pattern, name, priority in patterns:
        for m in re.finditer(pattern, text, re.I):
            try:
                d = datetime.strptime(m.group(1), "%d/%m/%Y").date()
            except ValueError:
                continue
            key = (d, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(_event(name, priority, event_date=d, source="ECB official calendar", canonical="ECB", time_confirmed=False))
    return out


def _event(name, priority, *, dt=None, event_date=None, source="", canonical=None, time_confirmed=True):
    if dt is not None:
        dt = dt.astimezone(TZ_DE) if dt.tzinfo else dt.replace(tzinfo=TZ_DE)
        event_date = dt.date()
        iso = dt.isoformat()
    else:
        if event_date is None:
            raise ValueError("event_date or dt required")
        iso = datetime.combine(event_date, datetime.min.time(), tzinfo=TZ_DE).isoformat()
    return {
        "name": name,
        "priority": priority,
        "datetime": iso,
        "date": event_date.isoformat(),
        "source": source,
        "canonical": canonical,
        "time_confirmed": bool(time_confirmed),
    }


def _macro_event_text(event):
    return re.sub(r"\s+", " ", " ".join(
        str(event.get(field, ""))
        for field in ("title", "event", "category", "name", "description")
        if event.get(field) is not None
    )).strip().lower()


def _normalize_macro_event(event):
    text = _macro_event_text(event)
    hits = []
    for canonical, rule in MACRO_EVENT_RULES.items():
        if any(alias in text for alias in rule["aliases"]) and not any(
            negative in text for negative in _MACRO_EVENT_NEGATIVE.get(canonical, ())
        ):
            hits.append(canonical)
    if "FOMC" in hits:
        return "FOMC"
    if "ECB" in hits:
        return "ECB"
    return hits[0] if len(hits) == 1 else (f"AMBIGUOUS:{','.join(hits)}" if hits else None)


def _is_gold_relevant_calendar_event(event):
    country = str(event.get("country", "")).strip().lower()
    return country in {"usd", "us", "usa", "united states", "united states of america", "eur", "euro", "eurozone", "europe"}


def _forexfactory_cache_load(today: date):
    try:
        if not FOREXFACTORY_CACHE_FILE.exists():
            return None
        payload = json.loads(FOREXFACTORY_CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("schema") != "GOLD_FOREXFACTORY_CALENDAR_V1":
            return None
        saved_at = datetime.fromisoformat(str(payload["saved_at"]))
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc)
        if age.total_seconds() < 0 or age.total_seconds() > FOREXFACTORY_CACHE_MAX_AGE_HOURS * 3600:
            return None
        if payload.get("week_date") != today.isoformat():
            return None
        events = payload.get("events")
        return events if isinstance(events, list) else None
    except Exception as exc:
        print(f"WARNUNG-FOREXFACTORY: Cache nicht lesbar: {type(exc).__name__}: {exc}")
        return None


def _forexfactory_cache_save(today: date, events):
    try:
        payload = {
            "schema": "GOLD_FOREXFACTORY_CALENDAR_V1",
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "week_date": today.isoformat(),
            "events": events,
        }
        tmp = FOREXFACTORY_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(FOREXFACTORY_CACHE_FILE)
    except Exception as exc:
        print(f"WARNUNG-FOREXFACTORY: Cache konnte nicht gespeichert werden: {type(exc).__name__}: {exc}")


def _forexfactory_events(today: date):
    cached = _forexfactory_cache_load(today)
    if cached is not None:
        return cached, "CACHE"
    try:
        r = _get(FOREXFACTORY_CALENDAR_URL, timeout=20)
        events = r.json()
        if not isinstance(events, list):
            raise ValueError("unerwarteter JSON-Typ")
        events = [e for e in events if isinstance(e, dict)]
        _forexfactory_cache_save(today, events)
        return events, "LIVE"
    except Exception as exc:
        print(f"WARNUNG-FOREXFACTORY: Kalender nicht verfuegbar: {type(exc).__name__}: {exc}")
        return [], "UNAVAILABLE"


def _macro_focus_from_calendar(today: date, events):
    candidates = []
    for event in events:
        if not _is_gold_relevant_calendar_event(event):
            continue
        canonical = _normalize_macro_event(event)
        if not canonical or canonical.startswith("AMBIGUOUS:"):
            continue
        raw_date = str(event.get("date", "")).strip()[:10]
        try:
            event_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if event_date < today or event_date > today + timedelta(days=14):
            continue
        rule = MACRO_EVENT_RULES[canonical]
        candidates.append({
            "date": event_date,
            "canonical": canonical,
            "title": str(event.get("title", event.get("event", canonical))).strip(),
            "priority": rule["priority"],
            "priority_rank": _PRIORITY_RANK[rule["priority"]],
            "focus": rule["focus"],
            "fed": rule["fed"],
            "source": "ForexFactory",
        })

    today_candidates = [c for c in candidates if c["date"] == today]
    pool = today_candidates or candidates
    if not pool:
        return None, []
    pool.sort(key=lambda c: (c["date"], -c["priority_rank"], c["canonical"]))
    return pool[0], candidates


def _fomc_dates():
    return _load_fomc_dates()


def _upcoming_macro_events(today: date, ff_events=None, ff_status=None):
    events = []
    horizon = today + timedelta(days=14)

    for d in _fomc_dates():
        if today <= d <= horizon:
            events.append((d, "FOMC-Zinsentscheid", "Federal Reserve / fomc_termine.json", True))

    if ff_events is None:
        ff_events, ff_status = _forexfactory_events(today)
    for event in ff_events:
        if not _is_gold_relevant_calendar_event(event):
            continue
        canonical = _normalize_macro_event(event)
        if not canonical or canonical.startswith("AMBIGUOUS:"):
            continue
        raw_date = str(event.get("date", "")).strip()[:10]
        try:
            event_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if not today <= event_date <= horizon:
            continue
        if canonical == "FOMC":
            continue  # Fed official source remains authoritative.
        title = str(event.get("title", event.get("event", canonical))).strip()
        events.append((event_date, f"{canonical}: {title}", f"ForexFactory ({ff_status})", True))

    # Official ECB calendar is authoritative for ECB meeting dates.
    for e in _fetch_ecb():
        d = date.fromisoformat(e["date"])
        if today <= d <= horizon:
            events.append((d, e["name"], e["source"], bool(e.get("time_confirmed"))))

    unique = {}
    for item in events:
        d, title, source, time_confirmed = item
        unique[(d, title)] = item
    return sorted(unique.values(), key=lambda x: (x[0], x[1]))


def _dedupe(events):
    seen = set()
    out = []
    for e in sorted(events, key=lambda x: (x.get("datetime", ""), x.get("name", ""))):
        key = (e.get("name"), e.get("datetime", "")[:16])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _cache_load():
    try:
        if not CACHE_FILE.exists():
            return None
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("schema") != "GOLD_ECONOMIC_EVENTS_V2":
            return None
        saved_at = datetime.fromisoformat(str(payload["updated_at"]))
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=TZ_DE)
        age = datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc)
        if age.total_seconds() < 0 or age.total_seconds() > FOREXFACTORY_CACHE_MAX_AGE_HOURS * 3600:
            return None
        events = payload.get("events")
        return events if isinstance(events, list) else None
    except Exception as exc:
        print(f"WARNUNG: Wirtschaftskalender-Cache nicht lesbar: {type(exc).__name__}: {exc}")
        return None


def _cache_save(now, events):
    try:
        payload = {
            "schema": "GOLD_ECONOMIC_EVENTS_V2",
            "updated_at": now.isoformat(),
            "events": events,
        }
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _event_in_window(event, start, end):
    """Prüft ein Event-Zeitfenster ohne unbestätigte Tageszeiten zu erfinden."""
    if not event.get("time_confirmed", True):
        raw_date = str(event.get("date", event.get("datetime", "")))[:10]
        try:
            event_date = date.fromisoformat(raw_date)
        except ValueError:
            return False
        return start.date() <= event_date <= end.date()
    try:
        dt = datetime.fromisoformat(event["datetime"])
    except (KeyError, TypeError, ValueError):
        return False
    return start - timedelta(minutes=15) <= dt <= end


def lade_termine(days_ahead: int = 14):
    now = datetime.now(TZ_DE)
    end = now + timedelta(days=days_ahead)
    today = now.date()
    all_events = []
    errors = []

    # Offizielle Quellen.
    for fetcher in (_fetch_bls, _fetch_bls_html, _fetch_bea, _fetch_fed, _fetch_ecb):
        try:
            all_events.extend(fetcher())
        except Exception as exc:
            errors.append(type(exc).__name__)

    # Freie Event-Discovery. Die Rohdaten werden separat 24h gecacht.
    ff_events, ff_status = _forexfactory_events(today)
    for event in ff_events:
        if not _is_gold_relevant_calendar_event(event):
            continue
        canonical = _normalize_macro_event(event)
        if not canonical or canonical.startswith("AMBIGUOUS:"):
            continue
        raw_date = str(event.get("date", "")).strip()[:10]
        try:
            event_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if not today <= event_date <= today + timedelta(days=days_ahead):
            continue
        rule = MACRO_EVENT_RULES[canonical]
        # FOMC wird ausschließlich über den offiziellen Fed-Kalender geführt.
        if canonical == "FOMC":
            continue
        title = str(event.get("title", event.get("event", canonical))).strip()
        time_raw = str(event.get("time", "")).strip()
        # ForexFactory-Zeit wird nur übernommen, wenn sie als HH:MM erkennbar ist.
        dt = None
        if re.fullmatch(r"\d{1,2}:\d{2}", time_raw):
            try:
                hh, mm = map(int, time_raw.split(":"))
                # ForexFactory liefert die Eventzeit in der Kalender-Zeitzone;
                # die Zeitzone ist für diese Discovery-Schicht nicht zuverlässig
                # genug für eine harte Ortszeitangabe. Deshalb Datum statt Uhrzeit.
            except ValueError:
                pass
        all_events.append(_event(
            f"{canonical}: {title}", rule["priority"], event_date=event_date,
            source=f"ForexFactory ({ff_status})", canonical=canonical, time_confirmed=False,
        ))

    # Bewährten offiziellen Fallback weiterhin ergänzen; er verdrängt keine
    # Live-Daten und wird durch _dedupe() mit ihnen zusammengeführt.
    all_events.extend(_fallback_2026())
    all_events = _sanitize_fed_events(all_events)
    all_events = _dedupe(all_events)
    all_events = [e for e in all_events if _event_in_window(e, now, end)]

    if all_events:
        _cache_save(now, all_events)
    else:
        cached = _cache_load()
        if cached is not None:
            all_events = [
                e for e in cached
                if _event_in_window(e, now, end)
            ]

    return all_events, errors


def _display_when(event, now):
    dt = datetime.fromisoformat(event["datetime"]).astimezone(TZ_DE)
    if not event.get("time_confirmed", True):
        if dt.date() == now.date():
            return "heute (Termin)"
        return dt.strftime("%d.%m.%Y (Termin)")
    delta_h = (dt - now).total_seconds() / 3600
    if delta_h < 1:
        return f"in {max(0, int(delta_h * 60))} Min."
    if dt.date() == now.date():
        return f"heute {dt.strftime('%H:%M')} Uhr"
    return dt.strftime("%d.%m.%Y %H:%M Uhr")


def macro_events_snapshot(today: date | None = None):
    today = today or datetime.now(TZ_DE).date()
    ff_events, ff_status = _forexfactory_events(today)
    events = _upcoming_macro_events(today, ff_events=ff_events, ff_status=ff_status)
    out = ["GOLD – MAKRO-EVENTS / WICHTIGE IMPULSE VORAUS"]
    if not events:
        out.append("Keine verifizierten relevanten Makro-Events in den naechsten 14 Tagen | STATUS=UNAVAILABLE")
    else:
        for d, title, source, time_confirmed in events[:10]:
            days = (d - today).days
            when = "HEUTE" if days == 0 else f"in {days} Tag(en)"
            out.append(f"{title}: {when} ({d.isoformat()}) | SOURCE={source} | TIME_CONFIRMED={'YES' if time_confirmed else 'NO'} | STATUS=REAL_PUBLIC")

    focus, candidates = _macro_focus_from_calendar(today, ff_events)
    # Offizielle ECB/Fed-Termine können den Fokus setzen, wenn ForexFactory
    # nichts Passendes liefert. Priorität und Datum bleiben deterministisch.
    official_focus = []
    for d, title, source, time_confirmed in events:
        canonical = "FOMC" if "FOMC" in title else ("ECB" if "EZB-" in title else None)
        if not canonical:
            continue
        rule = MACRO_EVENT_RULES[canonical]
        official_focus.append({
            "date": d, "canonical": canonical, "title": title,
            "priority": rule["priority"], "priority_rank": _PRIORITY_RANK[rule["priority"]],
            "focus": rule["focus"], "fed": rule["fed"], "source": source,
        })
    if official_focus:
        today_official = [x for x in official_focus if x["date"] == today]
        official_pool = today_official or official_focus
        official_pool.sort(key=lambda x: (x["date"], -x["priority_rank"], x["canonical"]))
        if focus is None or (official_pool[0]["date"], -official_pool[0]["priority_rank"]) < (focus["date"], -focus["priority_rank"]):
            focus = official_pool[0]

    out.append(f"FOREXFACTORY-EVENTDISCOVERY: STATUS={ff_status} | EVENTS={len(ff_events)}")
    if focus:
        out.append(
            f"MACRO_FOCUS={focus['canonical']} | FOCUS_EVENT={focus['title']} | "
            f"EVENT_STATUS={'TODAY' if focus['date'] == today else 'UPCOMING'} | "
            f"PRIORITY={focus['priority']} | FOCUS_DATE={focus['date'].isoformat()} | "
            f"FED_RELEVANCE={focus['fed']} | SOURCE={focus['source']}"
        )
        out.append("MACRO_FOCUS_REGEL: Deterministische Ereignispriorisierung; keine Richtungsentscheidung, kein Buy/Sell-Signal.")
    else:
        out.append("MACRO_FOCUS=NONE | STATUS=UNAVAILABLE")
    out.append("GOLD-MAKRO-REGEL: Fed/EZB offiziell; ForexFactory nur Event-Discovery; keine Datenwert-/Richtungsauthoritaet.")
    return "\n".join(out), events


def _focus_from_normalized_events(today: date, events):
    candidates = []
    for event in events:
        d = event.get("date")
        canonical = event.get("canonical")
        if not d or not canonical or canonical not in MACRO_EVENT_RULES:
            continue
        try:
            event_date = date.fromisoformat(d)
        except ValueError:
            continue
        if event_date < today or event_date > today + timedelta(days=14):
            continue
        rule = MACRO_EVENT_RULES[canonical]
        candidates.append({
            "date": event_date,
            "canonical": canonical,
            "title": event.get("name", canonical),
            "priority": rule["priority"],
            "priority_rank": _PRIORITY_RANK[rule["priority"]],
            "focus": rule["focus"],
            "fed": rule["fed"],
            "source": event.get("source", ""),
        })
    today_candidates = [c for c in candidates if c["date"] == today]
    pool = today_candidates or candidates
    if not pool:
        return None
    pool.sort(key=lambda c: (c["date"], -c["priority_rank"], c["canonical"]))
    return pool[0]


def briefing_block(days_ahead: int = 7):
    # Kompatibilitaet zur bestehenden Gold-Mini-Schnittstelle.
    events, errors = lade_termine(days_ahead)
    now = datetime.now(TZ_DE)

    # Die Detailausgabe kommt aus der bestehenden lade_termine()-Pipeline, damit
    # das Briefing weiterhin exakt den gewaehlten Horizont respektiert.
    future = [
        e for e in events
        if (
            (not e.get("time_confirmed", True) and str(e.get("date", e.get("datetime", "")))[:10] >= now.date().isoformat())
            or (e.get("time_confirmed", True) and datetime.fromisoformat(e["datetime"]) >= now - timedelta(minutes=15))
        )
    ]
    lines = ["⚠️ GOLD – WICHTIGE MAKRO-EVENTS"]
    if not future:
        lines.append("Keine verifizierten relevanten Makrotermine in den nächsten Tagen.")
    else:
        for e in future[:10]:
            icon = "🔴" if e["priority"] == VERY_HIGH else ("🟠" if e["priority"] == HIGH else "🟡")
            lines.append(f"{icon} {e['name']} – {_display_when(e, now)} | Quelle: {e['source']}")

    focus = _focus_from_normalized_events(now.date(), future)
    if focus:
        lines.append(
            f"MACRO_FOCUS={focus['canonical']} | FOCUS_EVENT={focus['title']} | "
            f"EVENT_STATUS={'TODAY' if focus['date'] == now.date() else 'UPCOMING'} | "
            f"PRIORITY={focus['priority']} | FOCUS_DATE={focus['date'].isoformat()} | "
            f"FED_RELEVANCE={focus['fed']} | SOURCE={focus['source']}"
        )
        lines.append("MACRO_FOCUS_REGEL: Deterministische Ereignispriorisierung; keine Richtungsentscheidung, kein Buy/Sell-Signal.")
    else:
        lines.append("MACRO_FOCUS=NONE | STATUS=UNAVAILABLE")
    lines.append("Hinweis: Termine können Gold/Volatilität deutlich bewegen. Kein automatisches Trading-Verbot.")
    return "\n".join(lines), future
