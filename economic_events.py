"""Automatischer US-Makrotermin-Kalender für MINI DAILY GOLD.

Quellen: offizielle BLS-, BEA- und Federal-Reserve-Veröffentlichungskalender.
Die Termine werden bei jedem Lauf online aktualisiert. Bei Nichterreichbarkeit
werden die zuletzt gespeicherten Termine aus economic_events_cache.json benutzt.

Wichtig: Das Modul erzeugt nur Risiko-Hinweise im Briefing. Es blockiert keine
Trades und verändert keine Entry-/TP-/Stop-Regeln.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ_ET = ZoneInfo("America/New_York")
TZ_DE = ZoneInfo("Europe/Berlin")
CACHE_FILE = Path("economic_events_cache.json")
TIMEOUT = 15
HEADERS = {"User-Agent": "MiniDailyGold/1.0"}

HIGH = "HIGH"
MEDIUM = "MEDIUM"


def _get(url: str) -> str:
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    return r.text


def _parse_ics(text: str):
    events = []
    # Minimaler ICS-Parser: reicht für den offiziellen BLS-Kalender.
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
        return "US CPI", HIGH
    if "employment situation" in s:
        return "US Employment Situation / NFP", HIGH
    if "producer price index" in s:
        return "US PPI", MEDIUM
    if "job openings and labor turnover survey" in s:
        return "US JOLTS", MEDIUM
    return None


def _fetch_bls():
    text = _get("https://www.bls.gov/schedule/news_release/bls.ics")
    out = []
    for summary, dt in _parse_ics(text):
        item = _classify_bls(summary)
        if item:
            name, priority = item
            out.append({"name": name, "priority": priority, "datetime": dt.isoformat(), "source": "BLS"})
    return out



def _fetch_bls_html():
    """Robuster Fallback: liest den offiziellen BLS-Monatskalender als HTML.
    Der BLS listet Datum, Uhrzeit und Release als Klartext; damit funktioniert
    die Erkennung auch dann, wenn der ICS-Feed im GitHub-Runner nicht abrufbar ist.
    """
    now = datetime.now(TZ_DE)
    out = []
    months = [now.strftime("%m"), (now + timedelta(days=31)).strftime("%m")]
    years = {now.strftime("%Y"), (now + timedelta(days=31)).strftime("%Y")}
    for year in sorted(years):
        for month in months:
            url = f"https://www.bls.gov/schedule/{year}/{month}_sched_list.htm"
            try:
                html = _get(url)
            except Exception:
                continue
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"&nbsp;", " ", text, flags=re.I)
            text = re.sub(r"\s+", " ", text)
            pattern = re.compile(
                r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                r"(\d{1,2}),\s+(\d{4})\s+(\d{1,2}:\d{2})\s+(AM|PM)\s+"
                r"(.*?)(?=\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+|\s+NOTE:|$)",
                re.I,
            )
            for m in pattern.finditer(text):
                month, day, year_s, hhmm, ampm, desc = m.groups()
                item = _classify_bls(desc)
                if not item:
                    continue
                try:
                    dt = datetime.strptime(
                        f"{month} {day} {year_s} {hhmm} {ampm.upper()}",
                        "%B %d %Y %I:%M %p",
                    ).replace(tzinfo=TZ_ET)
                except ValueError:
                    continue
                name, priority = item
                out.append({
                    "name": name,
                    "priority": priority,
                    "datetime": dt.astimezone(TZ_DE).isoformat(),
                    "source": "BLS",
                })
    return out


def _fallback_2026():
    """Notfall-Fallback für die wichtigsten bereits offiziell terminierten
    2026-Termine. Nur verwendet, wenn die offiziellen Feeds/Seiten nicht liefern."""
    dates = [
        ("US CPI", HIGH, "2026-08-12T08:30:00"),
        ("US PPI", MEDIUM, "2026-08-13T08:30:00"),
        # 19.08.2026 = Minutes der FOMC-Sitzung vom 28./29.07., KEIN Zinsentscheid.
        ("FOMC Minutes (Juli-Sitzung)", HIGH, "2026-08-19T14:00:00"),
        ("US JOLTS", MEDIUM, "2026-09-01T10:00:00"),
        ("US Employment Situation / NFP", HIGH, "2026-09-04T08:30:00"),
        ("US PPI", MEDIUM, "2026-09-10T08:30:00"),
        ("US CPI", HIGH, "2026-09-11T08:30:00"),
        # 16.09.2026 = zweiter Tag der regulären FOMC-Sitzung 15./16.09.
        ("FOMC / Fed-Zinsentscheid", HIGH, "2026-09-16T14:00:00"),
    ]
    return [
        {"name": n, "priority": p,
         "datetime": datetime.fromisoformat(ts).replace(tzinfo=TZ_ET).astimezone(TZ_DE).isoformat(),
         "source": "Official-calendar fallback"}
        for n, p, ts in dates
    ]


def _fetch_bea():
    html = _get("https://www.bea.gov/news/schedule")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out = []
    year = datetime.now(TZ_DE).year
    # Die BEA-Seite wird als Tabelle veröffentlicht. Wir extrahieren nur
    # Termine, deren Release-Text GDP oder Personal Income and Outlays enthält.
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+"
        r"(\d{1,2}:\d{2})\s*(AM|PM)\s+.*?"
        r"(GDP[^|]*|Personal Income and Outlays[^|]*)", re.I
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
        low = desc.lower()
        if "personal income and outlays" in low:
            name, priority = "US PCE / Personal Income & Outlays", HIGH
        elif "gdp" in low:
            name, priority = "US GDP", MEDIUM
        else:
            continue
        out.append({
            "name": name,
            "priority": priority,
            "datetime": dt.astimezone(TZ_DE).isoformat(),
            "source": "BEA",
        })
    return out


def _fetch_fed():
    """Liest den offiziellen FOMC-Kalender.
    Der Zinsentscheid wird auf den ZWEITEN Sitzungstag gelegt (14:00 ET),
    nicht auf den ersten Tag. Die offiziellen Minutes werden separat
    als eigenes Ereignis behandelt.
    """
    html = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out = []
    year = datetime.now(TZ_DE).year
    months = {m: i for i, m in enumerate([
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ], 1)}
    section = re.search(rf"{year} FOMC Meetings(.*?)(?:{year+1} FOMC Meetings|$)", text, re.I)
    if not section:
        return out
    section_text = section.group(1)
    for month, num in months.items():
        m = re.search(rf"\b{re.escape(month)}\s+(\d{{1,2}})(?:-(\d{{1,2}}))?", section_text)
        if not m:
            continue
        try:
            day = int(m.group(2) or m.group(1))
            dt = datetime(year, num, day, 14, 0, tzinfo=TZ_ET)
        except ValueError:
            continue
        out.append({
            "name": "FOMC / Fed-Zinsentscheid",
            "priority": HIGH,
            "datetime": dt.astimezone(TZ_DE).isoformat(),
            "source": "Federal Reserve",
        })
    if year == 2026:
        dt = datetime(2026, 8, 19, 14, 0, tzinfo=TZ_ET)
        out.append({
            "name": "FOMC Minutes (Juli-Sitzung)",
            "priority": HIGH,
            "datetime": dt.astimezone(TZ_DE).isoformat(),
            "source": "Federal Reserve",
        })
    return out


def _sanitize_fed_events(events):
    """Entfernt falsche/veraltete FOMC-Klassifizierungen, insbesondere aus
    einem alten Cache. Für 2026 sind die offiziellen Sitzungstage bekannt.
    """
    if datetime.now(TZ_DE).year != 2026:
        return events
    valid_decisions = {
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    }
    valid_minutes = {"2026-08-19"}
    out = []
    for e in events:
        if e.get("name") == "FOMC / Fed-Zinsentscheid":
            if e.get("datetime", "")[:10] not in valid_decisions:
                continue
        elif e.get("name") == "FOMC Minutes (Juli-Sitzung)":
            if e.get("datetime", "")[:10] not in valid_minutes:
                continue
        out.append(e)
    return out


def _dedupe(events):
    seen = set()
    out = []
    for e in sorted(events, key=lambda x: x["datetime"]):
        key = (e["name"], e["datetime"][:16])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def lade_termine(days_ahead: int = 14):
    now = datetime.now(TZ_DE)
    end = now + timedelta(days=days_ahead)
    all_events = []
    errors = []
    for fetcher in (_fetch_bls, _fetch_bls_html, _fetch_bea, _fetch_fed):
        try:
            all_events.extend(fetcher())
        except Exception as exc:
            errors.append(type(exc).__name__)

    all_events = _sanitize_fed_events(all_events)
    all_events = _dedupe(all_events)
    # Offizieller Feed darf den Fallback nicht "verdrängen", wenn er nur einen
    # Teil der Termine liefert. Die bekannten High-Impact-Termine werden daher
    # immer ergänzt und anschließend dedupliziert.
    all_events.extend(_fallback_2026())
    all_events = _sanitize_fed_events(all_events)
    all_events = _dedupe(all_events)

    all_events = [e for e in all_events if now - timedelta(minutes=15) <= datetime.fromisoformat(e["datetime"]) <= end]
    if all_events:
        try:
            CACHE_FILE.write_text(json.dumps({"updated_at": now.isoformat(), "events": all_events}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    elif CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_events = _sanitize_fed_events(cached.get("events", []))
            all_events = [e for e in cached_events if now - timedelta(minutes=15) <= datetime.fromisoformat(e["datetime"]) <= end]
        except Exception:
            all_events = []
    return all_events, errors


def briefing_block(days_ahead: int = 7):
    events, errors = lade_termine(days_ahead)
    now = datetime.now(TZ_DE)
    future = [e for e in events if datetime.fromisoformat(e["datetime"]) >= now - timedelta(minutes=15)]
    if not future:
        return "Keine wichtigen US-Makrotermine in den nächsten Tagen.", []
    lines = ["⚠️ WICHTIGE US-MARKTEVENTS"]
    for e in future[:6]:
        dt = datetime.fromisoformat(e["datetime"]).astimezone(TZ_DE)
        delta_h = (dt - now).total_seconds() / 3600
        if delta_h < 1:
            wann = f"in {max(0, int(delta_h * 60))} Min."
        elif dt.date() == now.date():
            wann = f"heute {dt.strftime('%H:%M')} Uhr"
        else:
            wann = dt.strftime("%d.%m.%Y %H:%M Uhr")
        icon = "🔴" if e["priority"] == HIGH else "🟠"
        lines.append(f"{icon} {e['name']} – {wann} (Europe/Berlin)")
    lines.append("Hinweis: Termine können Gold/Volatilität deutlich bewegen. Kein automatisches Trading-Verbot.")
    return "\n".join(lines), future

