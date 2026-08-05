"""
Mini Daily: Gold
-----------------
Holt aktuelle Goldkurse, berechnet Intraday-Pivot-Level (Widerstände/Unterstützungen),
lässt einen kurzen Rückblick-Text von Gemini generieren, baut einen Tageschart
und erzeugt daraus einen HTML-Report. Der Report wird anschließend (in main.py-
Aufrufern bzw. separaten Schritten) nach Google Drive hochgeladen und per Mail verschickt.

Datenquelle: Twelve Data (XAU/USD, Spot Gold). Erfordert TWELVEDATA_API_KEY.
Vorher yfinance/GC=F (Gold-Future) - Umstellung auf Spot + 1h-Intraday am
05.08.2026, weil yfinance keinen zuverlässigen Spot-Ticker bietet
(XAUUSD=X / XAU=X liefern 404) und APIFreaks (bereits fürs Backtest-Projekt
genutzt) nur Tages-OHLC liefert, kein Intraday-Intervall.
"""

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import requests
from google import genai

TICKER = "XAU/USD"  # Spot Gold über Twelve Data.
INTRADAY_INTERVALL = "1h"
TWELVEDATA_BASIS_URL = "https://api.twelvedata.com/time_series"
SEITWAERTS_SCHWELLE_PROZENT = 0.15  # +/- Band um Vortagesschluss für "Seitwärts"

# Positionstrading-Signal (Backtest V1e, ursprünglich auf GC=F-Future kalibriert:
# 34 Trades 2019-2026, Trefferquote 38%, Summe +49,77%). Läuft bei JEDEM
# Report-Lauf komplett neu von POSITIONSTRADING_START an durch, um den
# aktuellen Stand UND die aktuellen Backtest-Kennzahlen auf den echten
# Spot-Daten zu ermitteln (siehe backtest_kennzahlen_text() weiter unten) -
# kein gespeicherter Zustand zwischen den Läufen nötig, da die Simulation
# deterministisch aus den historischen Kursdaten reproduzierbar ist.
POSITIONSTRADING_START = "2019-01-01"
POSITIONSTRADING_TREND_FENSTER = 50
POSITIONSTRADING_SWING_FENSTER = 10
POSITIONSTRADING_COOLDOWN_TAGE = 3

POSITIONSTRADING_REGELN_TEXT = (
    "Regeln: Nur Long. Trend positiv (Tages-Regression über 50 Handelstage) "
    "und Kurs berührt ein rollierendes 10-Tage-Tief, schließt aber wieder "
    "darüber -> KAUF. Stop = dieses Tief. TP1/TP2 = 2R/3R (R = Einstieg-Stop): "
    "TP1 erreicht -> Stop auf Breakeven, TP2 erreicht -> Stop auf TP1, danach "
    "täglich am 10-Tage-Tief nachgezogen. Stop erreicht -> VERKAUF, danach "
    "3 Handelstage Cooldown ohne neuen Einstieg."
)

# Range-Ausbruch-Signal (Backtest 05.08.2026 auf XAU/USD 1h, Twelve Data:
# 144 Trades 24.01.2020-05.08.2026, Trefferquote 32,6%, Summe +110,82%,
# siehe backtest_range_ausbruch.py). Anders als beim V1e-Signal NICHT bei
# jedem Lauf über die volle Historie neu simuliert - das würde bei
# Stundenkerzen wegen des Twelve-Data-Rate-Limits (8 Credits/Minute) viele
# Chunk-Anfragen und mehrere Minuten pro Lauf kosten, 6x täglich unnötig.
# Stattdessen: RANGE_AUSBRUCH_HISTORIE_TAGE deckt die durchschnittliche
# Backtest-Haltedauer (13,3 Tage) mit reichlich Puffer ab und passt in EINE
# einzige Anfrage (max. ~208 Tage bei 1h-Kerzen/outputsize 5000). Die
# Backtest-Kennzahlen selbst sind deshalb ein Snapshot vom 05.08.2026, kein
# live nachgerechneter Wert - bei einem erneuten vollständigen Backtest-Lauf
# hier von Hand nachziehen.
RANGE_AUSBRUCH_FENSTER = 24  # Stunden-Kerzen für die Range-Hoch/-Tief-Referenz
RANGE_AUSBRUCH_COOLDOWN_STUNDEN = 12
RANGE_AUSBRUCH_HISTORIE_TAGE = 200
RANGE_AUSBRUCH_BACKTEST_TEXT = (
    "144 Trades 24.01.2020-05.08.2026, Trefferquote 32,6%, Summe +110,82% "
    "(Backtest-Snapshot vom 05.08.2026, XAU/USD 1h - nicht laufend neu berechnet)."
)
RANGE_AUSBRUCH_REGELN_TEXT = (
    "Regeln: Nur Long. Schlusskurs bricht über das rollierende 24h-Hoch aus "
    "(bestätigter Close, kein reiner Docht) -> KAUF. Stop = 24h-Tief zum "
    "Einstiegszeitpunkt. TP1/TP2 = 2R/3R: TP1 erreicht -> Stop auf Breakeven, "
    "TP2 erreicht -> Stop auf TP1, danach kontinuierlich am 24h-Tief "
    "nachgezogen. Stop erreicht -> VERKAUF, danach 12 Stunden Cooldown."
)


def hole_twelvedata_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY nicht gesetzt. Key aus dem Gold-Briefing-Setup "
            "wiederverwenden oder unter https://twelvedata.com neu anlegen und "
            "als GitHub Secret hinterlegen."
        )
    return key


def hole_zeitreihe(interval, outputsize=None, start_date=None, end_date=None):
    """Holt eine OHLC-Zeitreihe für XAU/USD von Twelve Data und liefert sie als
    DataFrame mit DatetimeIndex (Spalten Open/High/Low/Close, aufsteigend
    sortiert) - drop-in-Ersatz für die frühere yfinance-.history()-Nutzung."""
    params = {
        "symbol": TICKER,
        "interval": interval,
        "apikey": hole_twelvedata_key(),
        "timezone": "UTC",
        "order": "ASC",
    }
    if outputsize:
        params["outputsize"] = outputsize
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    antwort = requests.get(TWELVEDATA_BASIS_URL, params=params, timeout=20)
    antwort.raise_for_status()
    daten = antwort.json()
    if daten.get("status") == "error" or "values" not in daten:
        raise RuntimeError(f"Twelve-Data-Fehler ({interval}): {daten}")

    df = pd.DataFrame(daten["values"])
    df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    for spalte in ("Open", "High", "Low", "Close"):
        df[spalte] = df[spalte].astype(float)
    return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]


def hole_zeitreihe_taeglich(outputsize=None, start_date=None, end_date=None):
    """Wie hole_zeitreihe(), aber für Tagesdaten und mit Wochenend-Filter:
    Twelve Datas Tages-Feed für XAU/USD kann - analog zum Befund im
    Backtest-Projekt bei APIFreaks - Sa/So-Zeilen mit unplausibel breiter
    Range enthalten. Die Positionstrading-Regeln (10-Tage-Swing-Tief,
    50-Tage-Trend) wurden auf einer reinen Mo-Fr-Zeitreihe kalibriert
    (GC=F-Future), deshalb werden Wochenend-Zeilen hier sicherheitshalber
    konsequent rausgefiltert."""
    df = hole_zeitreihe("1day", outputsize=outputsize, start_date=start_date, end_date=end_date)
    vor_filter = len(df)
    df = df[df.index.dayofweek < 5]
    entfernt = vor_filter - len(df)
    if entfernt:
        print(f"Wochenend-Zeilen entfernt: {entfernt} von {vor_filter}")
    return df


def hole_kursdaten():
    """Liefert Realtime-Kurs, Vortages-OHLC und eine Intraday-Kursreihe (1h) für den Chart."""
    intraday = hole_zeitreihe(INTRADAY_INTERVALL, outputsize=72)  # ~3 Tage Puffer bei 1h
    if intraday.empty:
        raise RuntimeError("Keine Intraday-Daten von Twelve Data erhalten (XAU/USD).")

    realtime = float(intraday["Close"].iloc[-1])
    letzter_zeitpunkt = intraday.index[-1]

    # Twelve Data liefert über /price oft einen aktuelleren Live-Quote als die
    # 1h-Historie (deren letzte Kerze bis zu einer Stunde nachhinken kann).
    # Bleibt dieselbe Quelle (XAU/USD) - kein Konsistenzproblem zwischen
    # Pivots und Realtime-Wert.
    try:
        preis_antwort = requests.get(
            "https://api.twelvedata.com/price",
            params={"symbol": TICKER, "apikey": hole_twelvedata_key()},
            timeout=10,
        )
        preis_antwort.raise_for_status()
        live_preis = float(preis_antwort.json()["price"])
        if live_preis and live_preis > 0:
            realtime = live_preis
    except Exception as exc:
        print(f"/price nicht verfügbar, nutze 1h-Historie als Realtime-Wert ({exc}).")

    alter_minuten = (pd.Timestamp.now(tz="UTC") - letzter_zeitpunkt).total_seconds() / 60
    if alter_minuten > 120:
        print(f"WARNUNG: Letzte Intraday-Kerze ist {alter_minuten:.0f} Minuten alt "
              f"({letzter_zeitpunkt}) - Twelve Data liefert aktuell verzögerte Daten für XAU/USD.")

    # Tages-Reihe für Vortages-OHLC (Pivot-Basis)
    daily = hole_zeitreihe_taeglich(outputsize=5)
    if len(daily) < 2:
        raise RuntimeError("Nicht genug Tagesdaten für Pivot-Berechnung.")

    vortag = daily.iloc[-2]
    prev_high = float(vortag["High"])
    prev_low = float(vortag["Low"])
    prev_close = float(vortag["Close"])

    return {
        "realtime": realtime,
        "letzter_zeitpunkt": letzter_zeitpunkt,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "prev_close": prev_close,
        "intraday_reihe": intraday,
    }


def hole_langfrist_daten(monate=36):
    """Tageskurse der letzten `monate` Monate für die Reaktionszonen-Analyse
    (separat von den Intraday-Daten, die für Pivots/Chart genutzt werden)."""
    start = (pd.Timestamp.now() - pd.DateOffset(months=monate)).strftime("%Y-%m-%d")
    daily = hole_zeitreihe_taeglich(start_date=start, outputsize=5000)
    if len(daily) < 60:
        return None
    return daily


def analysiere_reaktionszonen(daily, fenster=3, bucket_usd=30, min_treffer=2, top_n=4):
    """Findet lokale Swing-Hochs/-Tiefs (Punkt ist Extremum in einem Fenster von
    +/- `fenster` Handelstagen) und gruppiert sie in Preis-Buckets. Nur Zonen mit
    mindestens `min_treffer` Reaktionen gelten als strukturell relevant - eine
    einzelne Berührung ist noch keine Zone, sondern Zufall."""
    highs = daily["High"].values
    lows = daily["Low"].values
    n = len(daily)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fenster_h = highs[i - fenster:i + fenster + 1]
        if highs[i] == fenster_h.max():
            swing_highs.append(highs[i])
        fenster_l = lows[i - fenster:i + fenster + 1]
        if lows[i] == fenster_l.min():
            swing_lows.append(lows[i])

    def clustern(punkte):
        buckets = {}
        for p in punkte:
            key = round(p / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append(p)
        zonen = [(np.mean(v), len(v)) for v in buckets.values() if len(v) >= min_treffer]
        zonen.sort(key=lambda z: -z[1])
        return zonen[:top_n]

    return {
        "widerstandszonen": clustern(swing_highs),
        "supportzonen": clustern(swing_lows),
    }


def kombiniere_zonen(zonen_je_zeitraum, bucket_usd=30, top_n=6, referenz_preis=None, max_abstand_pct=15):
    """Führt die Zonen aus mehreren Zeitfenstern (z.B. 3/4/36 Monate) zusammen.
    Zonen aus unterschiedlichen Fenstern, die preislich nah beieinander liegen,
    werden zu einer Zone verschmolzen (Trefferzahl = Maximum über die Fenster,
    Zeitfenster vermerkt). Absichtlich MAXIMUM statt Summe: kürzere Fenster (z.B.
    3 Monate) sind meist eine Teilmenge längerer Fenster (z.B. 4 Monate) - dieselben
    Swing-Punkte würden sonst doppelt gezählt, obwohl es nur eine echte Berührung ist.
    Falls referenz_preis übergeben wird, werden Zonen, die mehr als max_abstand_pct
    Prozent vom aktuellen Kurs entfernt liegen, verworfen - sonst können uralte,
    weit entfernte Zonen aus dem 36-Monats-Fenster (z.B. aus einem früheren
    Kurs-Niveau) die Charts unbrauchbar aufblähen, obwohl sie für die aktuelle
    Lage irrelevant sind."""
    def sammle(key):
        buckets = {}
        for monate, zonen in zonen_je_zeitraum.items():
            if not zonen:
                continue
            for preis, treffer in zonen[key]:
                bucket = round(preis / bucket_usd) * bucket_usd
                eintrag = buckets.setdefault(bucket, {"preise": [], "treffer": 0, "fenster": set()})
                eintrag["preise"].append(preis)
                eintrag["treffer"] = max(eintrag["treffer"], treffer)
                eintrag["fenster"].add(monate)
        ergebnis = [
            (sum(e["preise"]) / len(e["preise"]), e["treffer"], sorted(e["fenster"]))
            for e in buckets.values()
        ]
        if referenz_preis:
            ergebnis = [
                z for z in ergebnis
                if abs(z[0] - referenz_preis) / referenz_preis * 100 <= max_abstand_pct
            ]
        ergebnis.sort(key=lambda z: -z[1])
        return ergebnis[:top_n]

    return {
        "widerstandszonen": sammle("widerstandszonen"),
        "supportzonen": sammle("supportzonen"),
    }


def klassische_pivots(high, low, close):
    """Klassische Pivots (P/R1-3/S1-3) aus Vortages-OHLC, plus eine vierte,
    weiter entfernte Ebene (R4/S4, gängige Erweiterung: Abstand R2->R1 bzw.
    S1->S2 nochmal auf R3/S3 draufgeschlagen). Ohne diese vierte Ebene wirkt
    das Panel nach einem starken Ausbruch über R3/unter S3 "eingefroren" -
    der Kurs läuft weiter, aber es gibt keine nächste Marke mehr zu zeigen,
    weil R3/S3 die letzte feste Grenze der klassischen Formel sind (Befund
    05.08.2026: Kurs lag deutlich über den Widerständen, die sich trotzdem
    nicht mehr veränderten - kein Bug, sondern eine Grenze der 3-Ebenen-Formel)."""
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    r4 = r3 + (r2 - r1)
    s4 = s3 - (s1 - s2)
    return {"p": p, "r": [r1, r2, r3, r4], "s": [s1, s2, s3, s4]}


def bestimme_tendenz(realtime, prev_close):
    pct = (realtime - prev_close) / prev_close * 100
    if pct > SEITWAERTS_SCHWELLE_PROZENT:
        return "Steigend", pct
    elif pct < -SEITWAERTS_SCHWELLE_PROZENT:
        return "Fallend", pct
    return "Seitwärts", pct


def hole_saisonalitaet_text():
    """Rein kalenderbasierter Saisonalitäts-Kontext für Gold (Quelle: RealMoneyTrader
    Research, 43 Jahre Historie). Kein API-Aufruf, kein Signal/Qualitäts-Modifikator -
    nur zusätzlicher Kontext für den Rückblick-Text, analog zum Sektor-Analyse-Projekt."""
    heute = datetime.now(ZoneInfo("Europe/Berlin"))
    monat, tag = heute.month, heute.day

    if 5 <= monat <= 8:
        return ("Saisonal befindet sich Gold aktuell in der historisch long-geneigten Phase "
                "Mai bis August (43 Jahre Historie, RealMoneyTrader Research).")
    if (monat == 12 and tag >= 15) or (monat == 1 and tag <= 15):
        return ("Saisonal befindet sich Gold aktuell in der historisch long-geneigten Phase "
                "rund um den Jahreswechsel, Mitte Dezember bis Mitte Januar (43 Jahre Historie, "
                "RealMoneyTrader Research).")
    return None


def generiere_rueckblick(daten, pivots, tendenz, zonen_je_zeitraum):
    """Ruft Gemini auf, um einen kurzen charttechnischen Rückblick-Text zu erzeugen.
    zonen_je_zeitraum: dict {monate: reaktionszonen-dict oder None}, z.B. {3: {...}, 6: {...}, 36: {...}}."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "(Kein GEMINI_API_KEY gesetzt - Rückblick konnte nicht generiert werden.)"

    client = genai.Client(api_key=api_key)

    zonen_bloecke = []
    for monate in sorted(zonen_je_zeitraum.keys()):
        zonen = zonen_je_zeitraum[monate]
        if not zonen or (not zonen["widerstandszonen"] and not zonen["supportzonen"]):
            zonen_bloecke.append(f"{monate}-Monats-Fenster: keine Zonen mit mind. 2 Bestätigungen gefunden.")
            continue
        w_zeilen = "; ".join(
            f"ca. {preis:,.0f} USD ({treffer}x)".replace(",", ".")
            for preis, treffer in zonen["widerstandszonen"]
        )
        s_zeilen = "; ".join(
            f"ca. {preis:,.0f} USD ({treffer}x)".replace(",", ".")
            for preis, treffer in zonen["supportzonen"]
        )
        zonen_bloecke.append(
            f"{monate}-Monats-Fenster - Widerstandszonen: {w_zeilen or 'keine'} | "
            f"Supportzonen: {s_zeilen or 'keine'}"
        )
    zonen_block = "\n".join(zonen_bloecke)

    saisonalitaet = hole_saisonalitaet_text()
    saison_block = f"\nSaisonaler Kontext (nur Hintergrundinfo, kein Signal): {saisonalitaet}\n" if saisonalitaet else ""

    prompt = f"""Du bist ein nüchterner charttechnischer Kommentator für Gold Spot (XAU/USD).
Schreibe einen Rückblick-Absatz (genau 6-7 Sätze, deutsch, sachlich, ohne Anrede,
ohne Kauf-/Verkaufsempfehlung) im Stil eines Intraday-Briefings.

Intraday-Daten (kurzfristig):
- Realtime-Kurs: {daten['realtime']:.2f} USD
- Schlusskurs Vortag: {daten['prev_close']:.2f} USD
- Vortages-Hoch: {daten['prev_high']:.2f} USD
- Vortages-Tief: {daten['prev_low']:.2f} USD
- Intraday-Hoch (aktueller Zeitraum): {daten['intraday_reihe']['Close'].max():.2f} USD
- Intraday-Tief (aktueller Zeitraum): {daten['intraday_reihe']['Close'].min():.2f} USD
- Vorbörsliche Tendenz: {tendenz}
- Intraday-Pivot-Widerstände: {', '.join(f'{v:.0f}' for v in pivots['r'])} USD
- Intraday-Pivot-Unterstützungen: {', '.join(f'{v:.0f}' for v in pivots['s'])} USD
{saison_block}
Strukturelle Reaktionszonen (mehrfach bestätigte Hoch-/Tiefpunkte je Zeitfenster - diese
sind aussagekräftiger für eine Formationsbewertung als die reinen Intraday-Pivots; kürzere
Fenster zeigen eher aktuell relevante Zonen, längere Fenster eher übergeordnete Struktur):
{zonen_block}

Beschreibe zuerst die aktuelle Lage relativ zu den Intraday-Marken. Benenne dabei
EXPLIZIT zwei konkrete Kursszenarien für den Intraday-Horizont:
1. Aufwärtsszenario: welcher Trigger-Kurs einen Ausbruch nach oben auslösen würde und
   welches Kursziel/welche Widerstandsmarke danach als nächstes relevant wird
2. Abwärtsszenario: welcher Trigger-Kurs eine Trendwende/Korrektur nach unten auslösen
   würde und welches Kursziel/welche Unterstützungsmarke danach als nächstes relevant wird
Nenne in beiden Szenarien konkrete Kurswerte aus den Daten oben, keine vagen Formulierungen.

Ordne die Kursbewegung anschließend, gestützt auf die Reaktionszonen der verschiedenen
Zeitfenster (falls vorhanden - bevorzuge dabei das kürzeste Fenster mit brauchbaren
Zonen nahe am aktuellen Kurs), knapp einer gängigen charttechnischen Formation zu (z.B.
aufsteigendes/absteigendes/symmetrisches Dreieck, Seitwärtskanal, Doppel-Top,
Doppel-Boden, Flagge, Keil) und benenne sie explizit im Text. Falls vorhanden, kannst du
den saisonalen Kontext knapp als zusätzliche Einordnung erwähnen - er ersetzt aber nicht
die charttechnische Analyse und ist kein eigenständiges Signal. Falls auch über alle
Zeitfenster hinweg keine seriöse Einschätzung möglich ist, sag das knapp statt zu
spekulieren - keine erfundene Formation nennen, nur um etwas zu benennen.

Bleib trotz der zwei Szenarien und der Formationseinordnung im vorgegebenen Rahmen von
6-7 Sätzen - fasse dich pro Punkt knapp statt jeden Aspekt breit auszuführen.
Keine Übertreibungen, keine Prognosen mit Sicherheit formuliert."""

    try:
        antwort = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
        return antwort.text.strip()
    except Exception as exc:
        return f"(Rückblick-Generierung fehlgeschlagen: {exc})"


def finde_range_box(intraday_reihe, fenster=4, bucket_usd=6, min_treffer=2):
    """Findet eine horizontale Range: ein Widerstands- und ein Support-Level, die
    beide im Tagesverlauf mehrfach berührt wurden (Swing-Hochs/-Tiefs, analog zur
    Reaktionszonen-Erkennung, aber auf die Intraday-Kerzen angewendet statt auf
    Tagesdaten). Es geht dabei nicht um 'insgesamt wenig Bewegung', sondern um
    wiederholte Berührungen derselben beiden Ebenen - ein echtes Pendeln zwischen
    oben und unten.
    Gibt (start_zeit, end_zeit, tief, hoch) zurück oder None."""
    high = intraday_reihe["High"]
    low = intraday_reihe["Low"]
    n = len(intraday_reihe)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fh = high.iloc[i - fenster:i + fenster + 1]
        if high.iloc[i] == fh.max():
            swing_highs.append((high.index[i], high.iloc[i]))
        fl = low.iloc[i - fenster:i + fenster + 1]
        if low.iloc[i] == fl.min():
            swing_lows.append((low.index[i], low.iloc[i]))

    def groesstes_cluster(punkte):
        buckets = {}
        for zeit, preis in punkte:
            key = round(preis / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append((zeit, preis))
        if not buckets:
            return None
        bestes = max(buckets.values(), key=len)
        if len(bestes) < min_treffer:
            return None
        zeiten = [z for z, _ in bestes]
        preise = [p for _, p in bestes]
        return min(zeiten), max(zeiten), sum(preise) / len(preise)

    r_cluster = groesstes_cluster(swing_highs)
    s_cluster = groesstes_cluster(swing_lows)
    if not r_cluster or not s_cluster:
        return None

    start = min(r_cluster[0], s_cluster[0])
    # Bis zum aktuellsten Kurspunkt verlängern, nicht nur bis zur letzten Berührung -
    # die Range gilt als weiterhin gültig, solange der Kurs sie nicht verlassen hat.
    ende = intraday_reihe.index[-1]
    return start, ende, s_cluster[2], r_cluster[2]


def finde_range_boxen(preisreihe, fenster=5, bucket_usd=30, min_treffer=2, segmente=3):
    """Wie finde_range_box, aber für längere Zeiträume (z.B. 6 Monate) gedacht, in
    denen es mehrere zeitlich getrennte Ranges auf unterschiedlichen Kursniveaus
    geben kann (z.B. bei einem übergeordneten Trend, der durch mehrere Konsolidierungs-
    Phasen unterbrochen wird). Teilt den Zeitraum in `segmente` gleich große,
    chronologische Abschnitte und sucht in jedem Abschnitt separat nach einer Range.
    Gibt eine Liste von (start_zeit, end_zeit, tief, hoch) zurück, maximal
    `segmente` Einträge."""
    n = len(preisreihe)
    grenzen = np.linspace(0, n, segmente + 1).astype(int)
    boxen = []
    for i in range(segmente):
        teil = preisreihe.iloc[grenzen[i]:grenzen[i + 1]]
        if len(teil) < 2 * fenster + min_treffer:
            continue
        box = finde_range_box(teil, fenster=fenster, bucket_usd=bucket_usd, min_treffer=min_treffer)
        if box:
            boxen.append(box)
    return boxen


def finde_intraday_umkehrzonen(intraday_reihe, fenster=3, bucket_usd=5, min_treffer=2, top_n=3):
    """Analog zu analysiere_reaktionszonen (die für Tagesdaten schon existiert), aber
    auf Intraday-Kerzen angewendet: findet Swing-Hochs/-Tiefs und gruppiert sie zu
    Umkehrzonen mit mehreren Berührungen. Anders als finde_range_box werden hier
    NICHT zwei Level zu einer Box gepaart, sondern jede für sich mehrfach bestätigte
    Zone einzeln zurückgegeben - näher an dem, was man beim manuellen Einzeichnen
    mehrerer Widerstands-/Support-Linien im Chart machen würde."""
    high = intraday_reihe["High"]
    low = intraday_reihe["Low"]
    n = len(intraday_reihe)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fh = high.iloc[i - fenster:i + fenster + 1]
        if high.iloc[i] == fh.max():
            swing_highs.append(high.iloc[i])
        fl = low.iloc[i - fenster:i + fenster + 1]
        if low.iloc[i] == fl.min():
            swing_lows.append(low.iloc[i])

    def clustern(punkte):
        buckets = {}
        for p in punkte:
            key = round(p / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append(p)
        zonen = [(sum(v) / len(v), len(v)) for v in buckets.values() if len(v) >= min_treffer]
        zonen.sort(key=lambda z: -z[1])
        return zonen[:top_n]

    return {"widerstandszonen": clustern(swing_highs), "supportzonen": clustern(swing_lows)}


def baue_chart(intraday_reihe, pivots, strukturzonen=None, range_ausbruch_status=None, pfad="chart.png"):
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    preise = intraday_reihe["Close"]
    ax.plot(intraday_reihe.index, preise, color="#e8b95c", linewidth=1.6)

    # Trendlinie: einfache lineare Regression über die letzte Hälfte der Kursreihe
    # (aktuellerer Trend statt über den gesamten 2-Tage-Zeitraum gemittelt)
    trend_ausschnitt = preise.iloc[len(preise) // 2:]
    x_num = mdates.date2num(trend_ausschnitt.index)
    steigung, achsenabschnitt = np.polyfit(x_num, trend_ausschnitt.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
    trend_label = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
    ax.plot(trend_ausschnitt.index, trend_werte, color=trend_farbe, linewidth=1.8,
             linestyle="-", alpha=0.9, zorder=5)
    ax.text(trend_ausschnitt.index[-1], trend_werte[-1], f"  {trend_label}", color=trend_farbe,
             fontsize=10, fontweight="bold", va="bottom" if steigung > 0 else "top", ha="left")

    # Umkehrzonen vorab berechnen (wird weiter unten auch fürs Zeichnen genutzt), damit
    # die Range-Box nur gezeigt wird, wenn sie sich mit einer Umkehrzone deckt - sonst
    # zeigen beide fast dieselbe Information doppelt und übereinander im Bild.
    umkehrzonen = finde_intraday_umkehrzonen(intraday_reihe, top_n=2)
    alle_umkehr_preise = (
        [p for p, _ in umkehrzonen["widerstandszonen"]]
        + [p for p, _ in umkehrzonen["supportzonen"]]
    )

    # Range-Box: Widerstand + Support, die beide mehrfach berührt wurden (Swing-Hochs/
    # -Tiefs in 20-Min-Fenstern, min. 2 Berührungen je Seite) - anders als die reine
    # Spannen-Prüfung erkennt das auch Tage mit echtem Pendeln zwischen zwei Levels.
    range_box = finde_range_box(intraday_reihe, fenster=4, bucket_usd=6, min_treffer=2)
    box_bereich = None
    if range_box:
        start_zeit, end_zeit, tief, hoch = range_box
        ueberschneidet_sich = any(tief <= p <= hoch for p in alle_umkehr_preise)
        if ueberschneidet_sich:
            box_bereich = (tief, hoch)
            x_start = mdates.date2num(start_zeit)
            x_end = mdates.date2num(end_zeit)
            ax.add_patch(Rectangle(
                (x_start, tief), x_end - x_start, hoch - tief,
                linewidth=1.5, edgecolor="#e8e0c8", facecolor="none", alpha=0.85, zorder=4,
            ))
            ax.text(start_zeit, hoch, "Range  ", color="#e8e0c8", fontsize=8.5,
                     style="italic", va="bottom", ha="right")

    # Basis-Range: Kursbereich + Puffer
    puffer = (preise.max() - preise.min()) * 0.15
    y_unten = preise.min() - puffer
    y_oben = preise.max() + puffer

    # Range-Ausbruch-Signal (1h): falls aktuell offen, zieht Einstieg/Stop/TP1/TP2
    # die Achse mit auf, genau wie die Pivot-/Struktur-Level weiter unten - diese
    # Marken können außerhalb des sichtbaren ~3-Tage-Fensters liegen, weil Positionen
    # im Backtest im Schnitt 13 Tage laufen.
    if range_ausbruch_status and range_ausbruch_status.get("status") == "offen":
        for wert in (range_ausbruch_status["einstieg"], range_ausbruch_status["stop"],
                     range_ausbruch_status["tp1"], range_ausbruch_status["tp2"]):
            y_oben = max(y_oben, wert * 1.002)
            y_unten = min(y_unten, wert * 0.998)

    # Nur die EINE wirklich nächstgelegene Marke pro Richtung zieht die Achse (egal ob
    # Pivot oder übergeordnetes Struktur-Level) - vorher zogen beide unabhängig
    # voneinander, wodurch eine weit entfernte Struktur-Zone versehentlich auch alle
    # dazwischenliegenden Pivot-Level mit ins Bild zog und die Skala unnötig aufblähte.
    r_kandidaten = [("pivot", r) for r in pivots["r"] if r > y_oben]
    s_kandidaten = [("pivot", s) for s in pivots["s"] if s < y_unten]
    if strukturzonen:
        r_kandidaten += [("struktur", p) for p, *_ in strukturzonen["widerstandszonen"] if p > y_oben]
        s_kandidaten += [("struktur", p) for p, *_ in strukturzonen["supportzonen"] if p < y_unten]

    naechster_r_typ = naechster_r = None
    if r_kandidaten:
        naechster_r_typ, naechster_r = min(r_kandidaten, key=lambda kv: kv[1])
        y_oben = max(y_oben, naechster_r * 1.002)
    naechster_s_typ = naechster_s = None
    if s_kandidaten:
        naechster_s_typ, naechster_s = max(s_kandidaten, key=lambda kv: kv[1])
        y_unten = min(y_unten, naechster_s * 0.998)

    if naechster_r_typ == "pivot":
        ax.axhline(naechster_r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
        ax.text(intraday_reihe.index[-1], naechster_r, f" Widerstand {naechster_r:,.0f}", color="#e8887a",
                 fontsize=9.5, fontweight="bold", va="center", ha="left")
    elif naechster_r_typ == "struktur":
        ax.axhline(naechster_r, color="#b5654f", linewidth=1.3, linestyle=":", alpha=0.6)
        ax.text(intraday_reihe.index[-1], naechster_r, f" Struktur-Widerstand {naechster_r:,.0f}",
                 color="#e8887a", fontsize=8.5, style="italic", va="center", ha="left")

    if naechster_s_typ == "pivot":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
        ax.text(intraday_reihe.index[-1], naechster_s, f" Support {naechster_s:,.0f}", color="#9fcf8f",
                 fontsize=9.5, fontweight="bold", va="center", ha="left")
    elif naechster_s_typ == "struktur":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.3, linestyle=":", alpha=0.6)
        ax.text(intraday_reihe.index[-1], naechster_s, f" Struktur-Support {naechster_s:,.0f}",
                 color="#9fcf8f", fontsize=8.5, style="italic", va="center", ha="left")

    # Pivot- und Struktur-Level, die zufällig auch noch in die (dadurch minimal
    # erweiterte) Achse passen, zusätzlich einzeichnen - aber nichts zieht die
    # Achse weiter auf als die eine oben ermittelte nächste Marke je Richtung.
    for r in pivots["r"]:
        if r != naechster_r and y_unten <= r <= y_oben:
            ax.axhline(r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], r, f" Widerstand {r:,.0f}", color="#e8887a",
                     fontsize=9.5, fontweight="bold", va="center", ha="left")
    for s in pivots["s"]:
        if s != naechster_s and y_unten <= s <= y_oben:
            ax.axhline(s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], s, f" Support {s:,.0f}", color="#9fcf8f",
                     fontsize=9.5, fontweight="bold", va="center", ha="left")

    # Tatsächliches Intraday-Hoch/-Tief zusätzlich als schlichte Referenzlinien -
    # ergänzt die rechnerischen Pivot-Level um die real erreichten Extrempunkte.
    intraday_hoch = preise.max()
    intraday_tief = preise.min()
    ax.axhline(intraday_hoch, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    ax.text(intraday_reihe.index[0], intraday_hoch, "Tageshoch  ", color="#c9c2b0",
             fontsize=8.5, va="bottom", ha="left")
    ax.axhline(intraday_tief, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    ax.text(intraday_reihe.index[0], intraday_tief, "Tagestief  ", color="#c9c2b0",
             fontsize=8.5, va="top", ha="left")

    # Umkehrzonen zeichnen: mehrfach berührte Swing-Hochs/-Tiefs, jede einzeln als Linie -
    # nur innerhalb des bereits feststehenden Achsenbereichs, damit sie die Skala nicht
    # erneut aufblähen. Eigene Farbe (Blau) statt Creme, unterscheidbar von der Range-Box.
    # Zonen INNERHALB einer bereits gezeichneten Range-Box werden übersprungen - die
    # Box deckt diesen Preisbereich schon ab, eine zusätzliche Linie wäre redundant
    # und sorgt nur für überlappende Beschriftungen.
    def in_box(p):
        return box_bereich is not None and box_bereich[0] <= p <= box_bereich[1]

    for preis, treffer in umkehrzonen["widerstandszonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            ax.axhline(preis, color="#6fa8dc", linewidth=1.0, linestyle="-", alpha=0.6)
            ax.text(intraday_reihe.index[-1], preis, f"  Umkehrzone {preis:,.0f} ({treffer}x)".replace(",", "."),
                     color="#6fa8dc", fontsize=7.5, va="bottom", ha="right")
    for preis, treffer in umkehrzonen["supportzonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            ax.axhline(preis, color="#6fa8dc", linewidth=1.0, linestyle="-", alpha=0.6)
            ax.text(intraday_reihe.index[-1], preis, f"  Umkehrzone {preis:,.0f} ({treffer}x)".replace(",", "."),
                     color="#6fa8dc", fontsize=7.5, va="bottom", ha="right")

    ax.set_ylim(y_unten, y_oben)
    ax.margins(x=0.08)  # Platz rechts für die Level-Beschriftungen

    # Range-Ausbruch-Signal (1h): Einstieg/Stop/TP1/TP2 einzeichnen, falls offen -
    # "RA-"-Präfix in der Beschriftung, damit es nicht mit den Pivot-Widerstand/
    # Support-Linien verwechselt wird, die dieselbe Farbpalette nutzen.
    if range_ausbruch_status and range_ausbruch_status.get("status") == "offen":
        ra = range_ausbruch_status
        ax.axhline(ra["einstieg"], color="#c9c2b0", linewidth=1.0, linestyle=":", alpha=0.8)
        ax.text(intraday_reihe.index[0], ra["einstieg"], "RA-Einstieg  ", color="#c9c2b0",
                 fontsize=8, va="bottom", ha="right")
        ax.axhline(ra["stop"], color="#d9534f", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(intraday_reihe.index[0], ra["stop"], f"RA-Stop {ra['stop']:,.0f}  ".replace(",", "."),
                 color="#e8887a", fontsize=8, fontweight="bold", va="center", ha="right")
        ax.axhline(ra["tp1"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.7)
        ax.text(intraday_reihe.index[0], ra["tp1"], f"RA-TP1 {ra['tp1']:,.0f}  ".replace(",", "."),
                 color="#9fcf8f", fontsize=7.5, va="center", ha="right")
        ax.axhline(ra["tp2"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.5)
        ax.text(intraday_reihe.index[0], ra["tp2"], f"RA-TP2 {ra['tp2']:,.0f}  ".replace(",", "."),
                 color="#9fcf8f", fontsize=7.5, va="center", ha="right")

    # Feineres Gitter: Hauptlinien + gedämpfte Zwischenlinien für bessere Ablesbarkeit
    spanne = y_oben - y_unten
    schrittweite = 5 if spanne < 80 else (10 if spanne < 160 else 20)
    ax.yaxis.set_major_locator(plt.MultipleLocator(schrittweite))
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.set_title("Gold Spot (XAU/USD) - Intraday", color="#ece6d9", fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    fig.savefig(pfad, facecolor=fig.get_facecolor())
    plt.close(fig)
    return pfad


def baue_tageschart(daily, status, pfad="chart_tages.png"):
    """Tageschart (ca. 12 Monate) auf genau der Datenbasis, auf der das
    Positionstrading-Signal beruht: 50-Tage-Trend, 10-Tage-Swing-Tief-Referenz,
    plus Einstieg/Stop/TP1/TP2, falls aktuell eine Position offen ist. Macht
    das Signal aus dem Positionstrading-Abschnitt visuell nachvollziehbar,
    statt nur als Zahlen im Text zu stehen."""
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    schluss = daily["Close"]
    ax.plot(daily.index, schluss, color="#e8b95c", linewidth=1.3)

    # 50-Tage-Trend (gleiche Methode wie im Positionstrading-Signal) über den
    # letzten verfügbaren Ausschnitt dieses Charts eingezeichnet.
    trend_ausschnitt = schluss.iloc[-POSITIONSTRADING_TREND_FENSTER:] if len(schluss) >= POSITIONSTRADING_TREND_FENSTER else schluss
    x_num = mdates.date2num(trend_ausschnitt.index)
    steigung, achsenabschnitt = np.polyfit(x_num, trend_ausschnitt.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
    trend_label = "Aufwärtstrend (50T)" if steigung > 0 else "Abwärtstrend (50T)"
    ax.plot(trend_ausschnitt.index, trend_werte, color=trend_farbe, linewidth=1.8, zorder=5)
    ax.text(trend_ausschnitt.index[0], trend_werte[0], f"{trend_label}  ", color=trend_farbe,
             fontsize=9.5, fontweight="bold", va="bottom", ha="right")

    # Rollierendes 10-Tage-Swing-Tief - dieselbe Referenz, die für Einstieg/Stop genutzt wird.
    swing_tief = daily["Low"].rolling(POSITIONSTRADING_SWING_FENSTER).min().shift(1)
    ax.plot(daily.index, swing_tief, color="#6fa8dc", linewidth=0.9, linestyle=":", alpha=0.7)
    ax.text(daily.index[-1], swing_tief.iloc[-1], "  10T-Swing-Tief", color="#6fa8dc",
             fontsize=8, style="italic", va="center", ha="left")

    # Falls aktuell eine Position offen ist: Einstieg/Stop/TP1/TP2 einzeichnen.
    if status["status"] == "offen":
        ax.axhline(status["einstieg"], color="#c9c2b0", linewidth=1.0, linestyle=":", alpha=0.8)
        ax.text(daily.index[0], status["einstieg"], "Einstieg  ", color="#c9c2b0",
                 fontsize=8.5, va="bottom", ha="right")
        ax.axhline(status["stop"], color="#d9534f", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(daily.index[-1], status["stop"], f" Stop {status['stop']:,.0f}".replace(",", "."),
                 color="#e8887a", fontsize=8.5, fontweight="bold", va="center", ha="left")
        ax.axhline(status["tp1"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.7)
        ax.text(daily.index[-1], status["tp1"], f" TP1 {status['tp1']:,.0f}".replace(",", "."),
                 color="#9fcf8f", fontsize=8, va="center", ha="left")
        ax.axhline(status["tp2"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.5)
        ax.text(daily.index[-1], status["tp2"], f" TP2 {status['tp2']:,.0f}".replace(",", "."),
                 color="#9fcf8f", fontsize=8, va="center", ha="left")

    ax.margins(x=0.10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)
    ax.set_title("Gold Spot (XAU/USD) - Tageschart (Positionstrading-Basis)", color="#ece6d9", fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    fig.savefig(pfad, facecolor=fig.get_facecolor())
    plt.close(fig)
    return pfad


def baue_langfrist_chart(daily, zonen, pfad="chart_langfrist.png"):
    """4-Monats-Tageschart mit den bereits berechneten Reaktionszonen als Linien -
    macht sichtbar, wo die im Rückblick-Text genannten strukturellen Zonen herkommen."""
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    schluss = daily["Close"]
    ax.plot(daily.index, schluss, color="#e8b95c", linewidth=1.3)

    # Trendlinie über den gesamten dargestellten Zeitraum (anders als beim Intraday-Chart,
    # wo nur die jüngere Hälfte genutzt wird - hier soll die übergeordnete 4-Monats-Bewegung
    # abgebildet werden, nicht nur ein kurzer Ausschnitt)
    x_num = mdates.date2num(schluss.index)
    steigung, achsenabschnitt = np.polyfit(x_num, schluss.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
    trend_label = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
    ax.plot(schluss.index, trend_werte, color=trend_farbe, linewidth=1.8,
             linestyle="-", alpha=0.9, zorder=5)
    ax.text(schluss.index[0], trend_werte[0], f"{trend_label}  ", color=trend_farbe,
             fontsize=10, fontweight="bold", va="bottom", ha="left")

    # Range-Boxen: gleiche Berührungs-basierte Erkennung wie im Intraday-Chart, aber
    # in 3 zeitliche Abschnitte segmentiert - über 4 Monate kann es mehrere getrennte
    # Ranges auf unterschiedlichen Kursniveaus geben, nicht nur eine einzige.
    range_boxen = finde_range_boxen(daily, fenster=5, bucket_usd=30, min_treffer=2, segmente=3)
    for start_zeit, end_zeit, tief, hoch in range_boxen:
        x_start = mdates.date2num(start_zeit)
        x_end = mdates.date2num(end_zeit)
        ax.add_patch(Rectangle(
            (x_start, tief), x_end - x_start, hoch - tief,
            linewidth=1.5, edgecolor="#e8e0c8", facecolor="none", alpha=0.85, zorder=4,
        ))
        ax.text(end_zeit, hoch, " Range", color="#e8e0c8", fontsize=8.5,
                 style="italic", va="bottom", ha="left")

    if zonen:
        for preis, treffer, fenster in zonen["widerstandszonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#b5654f", linewidth=1.0, linestyle="--", alpha=0.8)
            ax.text(daily.index[-1], preis, f" Widerstand {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                     color="#e8887a", fontsize=9, fontweight="bold", va="center", ha="left")
        for preis, treffer, fenster in zonen["supportzonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#7fae6f", linewidth=1.0, linestyle="--", alpha=0.8)
            ax.text(daily.index[-1], preis, f" Support {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                     color="#9fcf8f", fontsize=9, fontweight="bold", va="center", ha="left")

    ax.margins(x=0.10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)
    ax.set_title("Gold Spot (XAU/USD) - 4 Monate, strukturelle Reaktionszonen", color="#ece6d9",
                 fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    fig.savefig(pfad, facecolor=fig.get_facecolor())
    plt.close(fig)
    return pfad


WOCHENTAGE_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember"]


def deutsches_datum(dt):
    """Formatiert ein datetime als 'Wochentag, DD. Monat YYYY' auf Deutsch -
    bewusst OHNE strftime('%A'/'%B'), da das von der System-Locale des
    GitHub-Actions-Runners abhängt (dort standardmäßig Englisch: 'Tuesday'
    statt 'Dienstag') statt einer Locale-Installation zu bedürfen."""
    wochentag = WOCHENTAGE_DE[dt.weekday()]
    monat = MONATE_DE[dt.month - 1]
    return f"{wochentag}, {dt.day:02d}. {monat} {dt.year}"


def formatiere_positionstrading(status):
    """Baut aus dem Ergebnis von berechne_positionstrading_status() einen
    lesbaren Text-Absatz - für Text- und HTML-Report gemeinsam genutzt.
    Erste Zeile ist IMMER das explizite Tages-Signal (KAUF/VERKAUF/HALTEN/
    KEIN SIGNAL), danach folgt die Begründung/der Kontext."""
    def de_zahl(n, nachkomma=2, vorzeichen=False):
        """Deutsches Zahlenformat (Punkt=Tausender, Komma=Dezimal) NUR für den
        übergebenen Zahlenwert - wird gezielt pro Wert aufgerufen, nicht als
        globales Text-Replace (das würde auch Datumsangaben wie '13.03.2026'
        verfälschen, siehe frühere Version dieser Funktion)."""
        praefix = "+" if vorzeichen and n >= 0 else ""
        return f"{praefix}{n:,.{nachkomma}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if status["status"] == "keine_daten":
        return "SIGNAL: nicht verfügbar (zu wenig Datenhistorie)."

    signal_text = {
        "KAUF": "SIGNAL: KAUF - heute ausgelöst",
        "VERKAUF": "SIGNAL: VERKAUF - heute ausgelöst (Stop erreicht)",
        "HALTEN": "SIGNAL: HALTEN - Position bereits offen, kein neues Ereignis heute",
        "KEIN_SIGNAL": "SIGNAL: KEIN SIGNAL - keine offene Position, kein neuer Einstieg heute",
    }[status["signal"]]

    if status["status"] == "offen":
        stufe_text = {0: "noch kein Ziel erreicht", 1: "TP1 erreicht, Stop auf Breakeven",
                      2: "TP2 erreicht, Stop wird laufend nachgezogen"}[status["stufe"]]
        kontext = (
            f"Simulierte Position seit {status['einstieg_datum'].strftime('%d.%m.%Y')} OFFEN "
            f"({status['haltedauer_tage']} Tage). Einstieg {de_zahl(status['einstieg'])} USD, "
            f"aktuell {de_zahl(status['aktueller_kurs'])} USD ({de_zahl(status['unrealisiert_pct'], vorzeichen=True)}% unrealisiert). "
            f"Stop bei {de_zahl(status['stop'])} USD, TP1 {de_zahl(status['tp1'])} USD, TP2 {de_zahl(status['tp2'])} USD "
            f"({stufe_text})."
        )
    else:
        letzter = status.get("letzter_trade")
        cooldown_text = " (aktuell in Cooldown nach Stop)" if status.get("im_cooldown") else ""
        if letzter:
            kontext = (
                f"Keine offene Position{cooldown_text}. Letzter simulierter Trade: "
                f"{letzter['einstieg_datum'].strftime('%d.%m.%Y')} bis {letzter['ausstieg_datum'].strftime('%d.%m.%Y')}, "
                f"Ergebnis {de_zahl(letzter['ergebnis_pct'], vorzeichen=True)}%."
            )
        else:
            kontext = f"Keine offene Position{cooldown_text}. Noch kein abgeschlossener Trade in der Historie."

    return signal_text + "\n" + kontext


def formatiere_range_ausbruch(status):
    """Wie formatiere_positionstrading(), aber für das Range-Ausbruch-Signal
    (Stunden statt Tage als Zeiteinheit)."""
    def de_zahl(n, nachkomma=2, vorzeichen=False):
        praefix = "+" if vorzeichen and n >= 0 else ""
        return f"{praefix}{n:,.{nachkomma}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if status["status"] == "keine_daten":
        return "SIGNAL: nicht verfügbar (zu wenig Datenhistorie)."

    signal_text = {
        "KAUF": "SIGNAL: KAUF - heute ausgelöst",
        "VERKAUF": "SIGNAL: VERKAUF - heute ausgelöst (Stop erreicht)",
        "HALTEN": "SIGNAL: HALTEN - Position bereits offen, kein neues Ereignis heute",
        "KEIN_SIGNAL": "SIGNAL: KEIN SIGNAL - keine offene Position, kein neuer Einstieg heute",
    }[status["signal"]]

    if status["status"] == "offen":
        stufe_text = {0: "noch kein Ziel erreicht", 1: "TP1 erreicht, Stop auf Breakeven",
                      2: "TP2 erreicht, Stop wird laufend nachgezogen"}[status["stufe"]]
        kontext = (
            f"Simulierte Position seit {status['einstieg_zeit'].strftime('%d.%m.%Y %H:%M')} UTC OFFEN "
            f"({status['haltedauer_stunden']:.0f} Std.). Einstieg {de_zahl(status['einstieg'])} USD, "
            f"aktuell {de_zahl(status['aktueller_kurs'])} USD ({de_zahl(status['unrealisiert_pct'], vorzeichen=True)}% unrealisiert). "
            f"Stop bei {de_zahl(status['stop'])} USD, TP1 {de_zahl(status['tp1'])} USD, TP2 {de_zahl(status['tp2'])} USD "
            f"({stufe_text})."
        )
    else:
        letzter = status.get("letzter_trade")
        cooldown_text = " (aktuell in Cooldown nach Stop)" if status.get("im_cooldown") else ""
        if letzter:
            kontext = (
                f"Keine offene Position{cooldown_text}. Letzter simulierter Trade: "
                f"{letzter['einstieg_zeit'].strftime('%d.%m.%Y %H:%M')} bis {letzter['ausstieg_zeit'].strftime('%d.%m.%Y %H:%M')} UTC, "
                f"Ergebnis {de_zahl(letzter['ergebnis_pct'], vorzeichen=True)}%."
            )
        else:
            kontext = f"Keine offene Position{cooldown_text}. Noch kein abgeschlossener Trade im betrachteten Zeitraum."

    return signal_text + "\n" + kontext



def baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, positionstrading_status, range_ausbruch_status):
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    heute = deutsches_datum(jetzt)
    erstellt_zeit = jetzt.strftime("%d.%m. %H:%M")
    daten_zeit = daten["letzter_zeitpunkt"].astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m. %H:%M")
    alter_minuten = (jetzt - daten["letzter_zeitpunkt"]).total_seconds() / 60

    warnzeile = ""
    if alter_minuten > 120:
        warnzeile = (
            f"\n⚠ HINWEIS: Die letzte verfügbare Kursdaten-Kerze ist {alter_minuten / 60:.1f} Stunden alt "
            f"({daten_zeit}) - Twelve Data liefert gerade verzögerte Daten für XAU/USD. Der Realtime-Kurs oben "
            f"kann trotzdem aktueller sein (separate Live-Quote), Pivot-/Chart-Basis ist aber diese Kerze.\n"
        )

    positionstrading_text = formatiere_positionstrading(positionstrading_status)
    range_ausbruch_text = formatiere_range_ausbruch(range_ausbruch_status)

    def liste(werte):
        return " / ".join(f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") for v in werte)

    def fmt(n):
        return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    text = f"""MINI DAILY: GOLD
{heute} - Erstellt um {erstellt_zeit} Uhr - Kursdaten Stand {daten_zeit} Uhr
{warnzeile}
VORBOERSLICHE TENDENZ
{tendenz_label} ({tendenz_pct:+.2f}%)

WIDERSTAENDE (INTRADAY)
{liste(pivots['r'])} USD

UNTERSTUETZUNGEN (INTRADAY)
{liste(pivots['s'])} USD

REALTIME INDIKATION
{fmt(daten['realtime'])} USD

SCHLUSSKURS (VORTAG)
{fmt(daten['prev_close'])} USD

RUECKBLICK
{rueckblick_text}

POSITIONSTRADING-SIGNAL (Backtest V1e, Halteperiode Tage bis Wochen)
{positionstrading_text}
{POSITIONSTRADING_REGELN_TEXT}
Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
{positionstrading_status.get('backtest_kennzahlen', '(keine Kennzahlen verfügbar)')}

RANGE-AUSBRUCH-SIGNAL (1h, Halteperiode Stunden bis Tage)
{range_ausbruch_text}
{RANGE_AUSBRUCH_REGELN_TEXT}
Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
{RANGE_AUSBRUCH_BACKTEST_TEXT}

---
Kein Kauf-/Verkaufssignal - reine charttechnische Orientierung - Datenquelle: Twelve Data (XAU/USD)
"""
    return text


def baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_dateiname, chart_tages_dateiname, positionstrading_status, range_ausbruch_status):
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    heute = deutsches_datum(jetzt)
    erstellt_zeit = jetzt.strftime("%d.%m. %H:%M")
    daten_zeit = daten["letzter_zeitpunkt"].astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m. %H:%M")
    alter_minuten = (jetzt - daten["letzter_zeitpunkt"]).total_seconds() / 60

    warnblock = ""
    if alter_minuten > 120:
        warnblock = f"""
    <p style="background:#3a2a1a;border-left:3px solid #d9a441;padding:10px 14px;color:#e8c98a;font-size:12.5px;">
    ⚠ Die letzte verfügbare Kursdaten-Kerze ist {alter_minuten / 60:.1f} Stunden alt ({daten_zeit}) -
    Twelve Data liefert gerade verzögerte Daten für XAU/USD. Der Realtime-Kurs unten kann trotzdem aktueller
    sein (separate Live-Quote), Pivot-/Chart-Basis ist aber diese Kerze.
    </p>"""

    positionstrading_text = formatiere_positionstrading(positionstrading_status)
    range_ausbruch_text = formatiere_range_ausbruch(range_ausbruch_status)

    def level_liste(werte, farbe):
        return "".join(
            f'<span style="display:inline-block;background:#241f16;border-left:3px solid {farbe};'
            f'padding:6px 12px;margin:4px 6px 4px 0;border-radius:2px;font-family:monospace;">'
            f'{v:,.2f}</span>'.replace(",", "X").replace(".", ",").replace("X", ".")
            for v in werte
        )

    html = f"""
    <html><body style="background:#14110d;color:#ece6d9;font-family:monospace;padding:20px;">
    <h1 style="color:#e8b95c;font-family:serif;">Mini Daily: Gold</h1>
    <p style="color:#a89d87;">{heute} - Erstellt um {erstellt_zeit} Uhr - Kursdaten Stand {daten_zeit} Uhr</p>
    {warnblock}
    <hr style="border-color:#3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Vorbörsliche Tendenz</h3>
    <p style="font-size:20px;font-family:serif;">{tendenz_label} ({tendenz_pct:+.2f}%)</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Widerstände (Intraday)</h3>
    <div>{level_liste(pivots['r'], '#b5654f')}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Unterstützungen (Intraday)</h3>
    <div>{level_liste(pivots['s'], '#7fae6f')}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Realtime Indikation</h3>
    <p style="font-size:28px;font-family:serif;color:#e8b95c;">{daten['realtime']:,.2f} USD</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Schlusskurs (Vortag)</h3>
    <p style="font-size:20px;font-family:serif;">{daten['prev_close']:,.2f} USD</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Rückblick</h3>
    <p style="line-height:1.6;">{rueckblick_text}</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Tageschart (Intraday)</h3>
    <img src="cid:chart" style="max-width:100%;border:1px solid #3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Tageschart (Positionstrading-Basis)</h3>
    <img src="cid:chart_tages" style="max-width:100%;border:1px solid #3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Struktureller Chart (4 Monate)</h3>
    <img src="cid:chart_lang" style="max-width:100%;border:1px solid #3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Positionstrading-Signal (Backtest V1e)</h3>
    <p style="line-height:1.6;"><strong style="color:#e8b95c;">{positionstrading_text.split(chr(10), 1)[0]}</strong><br>{positionstrading_text.split(chr(10), 1)[1] if chr(10) in positionstrading_text else ''}</p>
    <p style="color:#a89d87;font-size:11px;line-height:1.5;">{POSITIONSTRADING_REGELN_TEXT}</p>
    <p style="color:#a89d87;font-size:10.5px;">
    Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
    {positionstrading_status.get('backtest_kennzahlen', '(keine Kennzahlen verfügbar)')}
    </p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Range-Ausbruch-Signal (1h)</h3>
    <p style="line-height:1.6;"><strong style="color:#e8b95c;">{range_ausbruch_text.split(chr(10), 1)[0]}</strong><br>{range_ausbruch_text.split(chr(10), 1)[1] if chr(10) in range_ausbruch_text else ''}</p>
    <p style="color:#a89d87;font-size:11px;line-height:1.5;">{RANGE_AUSBRUCH_REGELN_TEXT}</p>
    <p style="color:#a89d87;font-size:10.5px;">
    Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
    {RANGE_AUSBRUCH_BACKTEST_TEXT}
    </p>

    <p style="color:#a89d87;font-size:10px;margin-top:24px;">
    Kein Kauf-/Verkaufssignal · reine charttechnische Orientierung · Datenquelle: Twelve Data (XAU/USD)
    </p>
    </body></html>
    """
    return html


def berechne_positionstrading_status():
    """Simuliert die V1e-Positionstrading-Regeln (Trendfolge + Swing-Tief-Bounce,
    Halteperiode Tage bis Wochen) von POSITIONSTRADING_START bis heute und
    liefert den AKTUELLEN Stand - läuft bei jedem Report-Lauf komplett neu
    durch (deterministisch aus den historischen Kursdaten), kein gespeicherter
    Zustand zwischen den Läufen nötig.

    Regeln (identisch zu backtest_v1e.py, dort ausführlicher dokumentiert):
    - Nur Long. Trend: rollierende Regression über die letzten 50 Handelstage
      (nur bis gestern). Einstieg: bestätigter Bounce an einem rollierenden
      10-Tage-Swing-Tief. Stop: dieses Tief, fest. TP1/TP2 = 2R/3R.
      Stufenregel: TP1->Breakeven, TP2->TP1-Niveau, danach kontinuierliches
      Nachziehen am Swing-Tief. Cooldown 3 Handelstage nach einem Stop.

    Rein informativ - siehe Disclaimer im Report. Kein automatisiertes
    Handelssignal, keine Anlageempfehlung.
    """
    daily = hole_zeitreihe_taeglich(start_date=POSITIONSTRADING_START, outputsize=5000)
    if len(daily) < POSITIONSTRADING_TREND_FENSTER + 5:
        return {"status": "keine_daten"}

    def steigung(werte):
        x = np.arange(len(werte))
        m, _ = np.polyfit(x, werte, 1)
        return m

    aufwaertstrend = daily["Close"].rolling(POSITIONSTRADING_TREND_FENSTER).apply(steigung, raw=True).shift(1) > 0
    swing_tief_referenz = daily["Low"].rolling(POSITIONSTRADING_SWING_FENSTER).min().shift(1)

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_datum = None
    cooldown_bis = None
    letzter_abgeschlossener_trade = None
    alle_trades = []  # sammelt jeden abgeschlossenen Trade für die Live-Backtest-Kennzahlen im Footer

    for datum, bar in daily.iterrows():
        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        trend_auf = aufwaertstrend.get(datum)
        ref_tief = swing_tief_referenz.get(datum)

        if not in_position:
            if cooldown_bis is not None and datum < cooldown_bis:
                continue
            if pd.notna(trend_auf) and trend_auf and pd.notna(ref_tief):
                ref_tief = float(ref_tief)
                if tief <= ref_tief and schluss > ref_tief:
                    entry = schluss
                    stop = ref_tief
                    if stop < entry:
                        r = entry - stop
                        tp1 = entry + 2 * r
                        tp2 = entry + 3 * r
                        in_position = True
                        stufe = 0
                        entry_datum = datum
        else:
            if stufe == 2 and pd.notna(ref_tief):
                stop = max(stop, float(ref_tief))
            if tief <= stop:
                letzter_abgeschlossener_trade = {
                    "einstieg_datum": entry_datum, "ausstieg_datum": datum,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                }
                alle_trades.append(letzter_abgeschlossener_trade["ergebnis_pct"])
                in_position = False
                cooldown_bis = datum + pd.Timedelta(days=POSITIONSTRADING_COOLDOWN_TAGE)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    def backtest_kennzahlen_text():
        """Baut den Footer-Satz live aus alle_trades statt aus einer fest
        einprogrammierten Zahl - die stammte zuletzt noch vom alten
        GC=F-Future-Backtest und war nach der Umstellung auf Spot (05.08.2026)
        falsch (zeigte Future-Kennzahlen für ein Spot-Signal)."""
        if not alle_trades:
            return f"(Keine abgeschlossenen Trades seit {POSITIONSTRADING_START} in dieser Simulation.)"
        n = len(alle_trades)
        gewinner = [t for t in alle_trades if t > 0]
        trefferquote = len(gewinner) / n * 100
        summe = sum(alle_trades)
        return (f"{n} Trades seit {POSITIONSTRADING_START[:4]}, Trefferquote {trefferquote:.0f}%, "
                f"Summe {summe:+.2f}% (auf Spot-Daten/XAU-USD, live bei jedem Lauf neu simuliert).")

    letzter_kurs = float(daily["Close"].iloc[-1])
    letztes_datum = daily.index[-1]

    if in_position:
        # War der EINSTIEG genau die letzte (heutige) Kerze -> heute ausgelöstes
        # Kaufsignal. Sonst: Position läuft bereits, heutiges Signal = Halten.
        heutiges_signal = "KAUF" if entry_datum == letztes_datum else "HALTEN"
        return {
            "status": "offen",
            "signal": heutiges_signal,
            "einstieg_datum": entry_datum, "einstieg": entry,
            "stop": stop, "tp1": tp1, "tp2": tp2, "stufe": stufe,
            "aktueller_kurs": letzter_kurs,
            "unrealisiert_pct": (letzter_kurs - entry) / entry * 100,
            "haltedauer_tage": (letztes_datum - entry_datum).days,
            "backtest_kennzahlen": backtest_kennzahlen_text(),
        }
    else:
        # War der AUSSTIEG (Stop) genau die letzte (heutige) Kerze -> heute
        # ausgelöstes Verkaufssignal. Sonst: schon länger flach, kein Signal heute.
        heutiges_signal = "VERKAUF"
        if not (letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_datum"] == letztes_datum):
            heutiges_signal = "KEIN_SIGNAL"
        return {
            "status": "keine_position",
            "signal": heutiges_signal,
            "letzter_trade": letzter_abgeschlossener_trade,
            "im_cooldown": cooldown_bis is not None and letztes_datum < cooldown_bis,
            "backtest_kennzahlen": backtest_kennzahlen_text(),
        }


def berechne_range_ausbruch_status():
    """Simuliert das Range-Ausbruch-Signal (1h, rollierendes 24h-Hoch/-Tief,
    TP1/TP2=2R/3R analog zum V1e-System) über die letzten
    RANGE_AUSBRUCH_HISTORIE_TAGE Tage und liefert den aktuellen Stand -
    holt dafür genau EINE zusätzliche Twelve-Data-Anfrage (siehe Kommentar
    bei den RANGE_AUSBRUCH_*-Konstanten weiter oben, warum nicht die volle
    Historie wie beim V1e-Signal).

    Rein informativ - siehe Disclaimer im Report. Kein automatisiertes
    Handelssignal, keine Anlageempfehlung.
    """
    start = (pd.Timestamp.now() - pd.Timedelta(days=RANGE_AUSBRUCH_HISTORIE_TAGE)).strftime("%Y-%m-%d")
    stunden = hole_zeitreihe(INTRADAY_INTERVALL, start_date=start, outputsize=5000)
    if len(stunden) < RANGE_AUSBRUCH_FENSTER + 5:
        return {"status": "keine_daten"}

    range_hoch_referenz = stunden["High"].rolling(RANGE_AUSBRUCH_FENSTER).max().shift(1)
    range_tief_referenz = stunden["Low"].rolling(RANGE_AUSBRUCH_FENSTER).min().shift(1)

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_zeit = None
    cooldown_bis = None
    letzter_abgeschlossener_trade = None

    for zeit, bar in stunden.iterrows():
        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        ref_hoch = range_hoch_referenz.get(zeit)
        ref_tief = range_tief_referenz.get(zeit)

        if not in_position:
            if cooldown_bis is not None and zeit < cooldown_bis:
                continue
            if pd.notna(ref_hoch) and pd.notna(ref_tief) and schluss > float(ref_hoch):
                entry = schluss
                stop = float(ref_tief)
                if stop < entry:
                    r = entry - stop
                    tp1 = entry + 2 * r
                    tp2 = entry + 3 * r
                    in_position = True
                    stufe = 0
                    entry_zeit = zeit
        else:
            if stufe == 2 and pd.notna(ref_tief):
                stop = max(stop, float(ref_tief))
            if tief <= stop:
                letzter_abgeschlossener_trade = {
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                }
                in_position = False
                cooldown_bis = zeit + pd.Timedelta(hours=RANGE_AUSBRUCH_COOLDOWN_STUNDEN)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    letzter_kurs = float(stunden["Close"].iloc[-1])
    letzte_zeit = stunden.index[-1]

    if in_position:
        heutiges_signal = "KAUF" if entry_zeit == letzte_zeit else "HALTEN"
        return {
            "status": "offen",
            "signal": heutiges_signal,
            "einstieg_zeit": entry_zeit, "einstieg": entry,
            "stop": stop, "tp1": tp1, "tp2": tp2, "stufe": stufe,
            "aktueller_kurs": letzter_kurs,
            "unrealisiert_pct": (letzter_kurs - entry) / entry * 100,
            "haltedauer_stunden": (letzte_zeit - entry_zeit).total_seconds() / 3600,
        }
    else:
        heutiges_signal = "VERKAUF"
        if not (letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_zeit"] == letzte_zeit):
            heutiges_signal = "KEIN_SIGNAL"
        return {
            "status": "keine_position",
            "signal": heutiges_signal,
            "letzter_trade": letzter_abgeschlossener_trade,
            "im_cooldown": cooldown_bis is not None and letzte_zeit < cooldown_bis,
        }


def main():
    daten = hole_kursdaten()
    pivots = klassische_pivots(daten["prev_high"], daten["prev_low"], daten["prev_close"])
    tendenz_label, tendenz_pct = bestimme_tendenz(daten["realtime"], daten["prev_close"])

    zonen_je_zeitraum = {}
    daily_lang = None
    for monate in (3, 4, 36):
        langfrist = hole_langfrist_daten(monate=monate)
        if monate == 4:
            daily_lang = langfrist
        zonen_je_zeitraum[monate] = analysiere_reaktionszonen(langfrist) if langfrist is not None else None
        if zonen_je_zeitraum[monate]:
            print(f"Widerstandszonen ({monate}M): {zonen_je_zeitraum[monate]['widerstandszonen']}")
            print(f"Supportzonen ({monate}M): {zonen_je_zeitraum[monate]['supportzonen']}")
        else:
            print(f"Keine ausreichenden Daten für {monate}-Monats-Zonen.")

    rueckblick_text = generiere_rueckblick(daten, pivots, tendenz_label, zonen_je_zeitraum)
    # Zwei getrennte Toleranzen: der Intraday-Chart soll nur wirklich naheliegende
    # Struktur-Level zeigen (enger Zeithorizont), der 4-Monats-Chart darf großzügiger sein.
    kombinierte_zonen_intraday = kombiniere_zonen(zonen_je_zeitraum, referenz_preis=daten["realtime"], max_abstand_pct=5)
    # Für den 4-Monats-Chart bewusst OHNE Preisnähe-Filter, aber nur aus den 3-/4-Monats-
    # Fenstern (nicht 36M) - deren Zonen stammen aus Daten, die ohnehin im sichtbaren
    # 4-Monats-Preisbereich liegen, können die Achse also nicht aufblähen. Das 36-Monats-
    # Fenster bleibt außen vor, weil es auch Zonen aus einem ganz anderen (viel tieferen)
    # historischen Kursniveau liefern kann.
    kombinierte_zonen_lang = kombiniere_zonen(
        {k: v for k, v in zonen_je_zeitraum.items() if k in (3, 4)}
    )

    range_ausbruch_status = berechne_range_ausbruch_status()
    print(f"Range-Ausbruch-Status: {range_ausbruch_status['status']}")

    chart_pfad = baue_chart(daten["intraday_reihe"], pivots, strukturzonen=kombinierte_zonen_intraday,
                             range_ausbruch_status=range_ausbruch_status)
    chart_lang_pfad = None
    if daily_lang is not None:
        chart_lang_pfad = baue_langfrist_chart(daily_lang, kombinierte_zonen_lang)

    positionstrading_status = berechne_positionstrading_status()
    print(f"Positionstrading-Status: {positionstrading_status['status']}")

    # Für den neuen Tageschart reicht ein 12-Monats-Ausschnitt (genug für 50-Tage-
    # Trend + 10-Tage-Swing-Tief-Referenz, aber übersichtlicher als die vollen
    # ~7 Jahre, die für das Positionstrading-Signal selbst durchgerechnet werden).
    daily_fuer_tageschart = hole_langfrist_daten(monate=12)
    chart_tages_pfad = None
    if daily_fuer_tageschart is not None:
        chart_tages_pfad = baue_tageschart(daily_fuer_tageschart, positionstrading_status)

    html = baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_pfad, chart_tages_pfad, positionstrading_status, range_ausbruch_status)
    text = baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, positionstrading_status, range_ausbruch_status)

    with open("mini_daily_gold.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("mini_daily_gold.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Realtime: {daten['realtime']:.2f} USD | Tendenz: {tendenz_label} ({tendenz_pct:+.2f}%)")
    print(f"Widerstände: {pivots['r']}")
    print(f"Unterstützungen: {pivots['s']}")
    print("Report geschrieben: mini_daily_gold.html, mini_daily_gold.txt, chart.png, chart_tages.png, chart_langfrist.png")


if __name__ == "__main__":
    sys.exit(main() or 0)
