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
import json
import sys
import time
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
from economic_events import briefing_block

TICKER = "XAU/USD"  # Spot Gold über Twelve Data.
INTRADAY_INTERVALL = "1h"
INTRADAY_ANALYSE_INTERVALLE = ("1h", "30min", "15min")
INTRADAY_ANALYSE_BARS = {"1h": 72, "30min": 96, "15min": 160}
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

# "Neustart" der SIGNAL-ANZEIGE (nicht der Backtest-Kennzahlen!) ab diesem Datum,
# auf Wunsch des Nutzers (05.08.2026): wir gehen davon aus, dass bis dahin kein
# tatsächlicher Auftrag erteilt wurde. Eine Position, die die Simulation schon
# VOR diesem Datum als eröffnet ansieht, wird für die SIGNAL-Zeile nicht mehr
# als "aktuell offen" ausgewiesen, und ein abgeschlossener Trade davor nicht
# mehr als "letzter Trade" gezeigt - auch wenn die Simulation selbst (für
# korrekte Trend-/Referenzberechnung) weiterhin die volle Historie durchläuft.
SIGNAL_NEUSTART_DATUM = pd.Timestamp("2026-08-05", tz="UTC")

# Volatilitätsfilter (05.08.2026, Sicherheitsnetz für V1e UND Range-Ausbruch):
# beide bestehenden Signale sind Trendfolge-/Ausbruchssysteme und kaufen in
# eine laufende Bewegung hinein - in ungewöhnlich chaotischen/volatilen Phasen
# ist die Gefahr von Fehlausbrüchen (Stop kurz nach Einstieg) erhöht. Der
# Filter blockiert deshalb NEUE Einstiege (bestehende offene Positionen laufen
# unverändert weiter), wenn die kurzfristige Volatilität (ATR) deutlich über
# dem langfristigen Schnitt liegt. Default AUS, bis der Effekt gegen echte
# Historie verglichen wurde (siehe backtest_*.py, dort derselbe Schalter) -
# auf True setzen, um live zu aktivieren.
VOLATILITAETS_FILTER_AKTIV = False
VOLATILITAETS_SCHWELLE = 1.8  # ATR(kurz) darf max. das X-fache von ATR(lang) sein
VOLATILITAETS_FENSTER_KURZ_TAGE = 14
VOLATILITAETS_FENSTER_LANG_TAGE = 100
VOLATILITAETS_FENSTER_KURZ_STUNDEN = 14
VOLATILITAETS_FENSTER_LANG_STUNDEN = 200

# Trade-Alert-Vorwarnung: rein für die Mail-Logik, verändert keine
# Signalberechnung und keine Entry-/Exit-Regel. Eine PREPARE-Mail wird nur
# gesendet, wenn der aktuelle Schlusskurs höchstens 0,5 % vom jeweiligen
# bestätigten Entry-Trigger entfernt ist und noch keine Position offen ist.
TRADE_ALERT_PREPARE_ABSTAND_PCT = 0.5

# Eigene Formations-/Struktur-Parameter für den TAGESCHART (Positionstrading-Basis,
# ca. 12 Monate Tagesdaten). Bewusst UNABHÄNGIG von den Intraday-Parametern in
# baue_chart() (finde_trendkanal/finde_range_boxen dort laufen mit ihren eigenen
# Default-Werten weiter) - ein Trendkanal auf Stundenkerzen braucht ein ganz
# anderes Zeitfenster als einer auf Tageskerzen.
TAGESCHART_KANAL_FENSTER = 5          # Swing-Erkennung: +/- 5 Handelstage
TAGESCHART_KANAL_MIN_PUNKTE = 3       # mind. 3 Swing-Hochs UND 3 Swing-Tiefs für eine Kanal-/Dreiecksformation
TAGESCHART_RANGE_FENSTER = 6
TAGESCHART_RANGE_BUCKET_USD = 45  # erhöht von 25: 45 USD war zu eng, echte Touches einer Range streuen mehr
TAGESCHART_RANGE_SEGMENTE = 3
TAGESCHART_ZONEN_FENSTER = 4
TAGESCHART_ZONEN_BUCKET_USD = 25
TAGESCHART_ZONEN_MIN_TREFFER = 2
TAGESCHART_ZONEN_TOP_N = 4
TAGESCHART_ZONEN_MIN_ABSTAND_USD = 50  # zwei Zonen näher als dieser Wert werden zusammengelegt

# Eigene Formations-/Struktur-Parameter für den 6-MONATS-CHART (vormals 4 Monate).
# Größere Swing-/Bucket-Fenster als beim Tageschart, weil hier die übergeordnete
# Bewegung interessiert, nicht die Feinstruktur der letzten Wochen.
LANGFRIST_MONATE = 6
LANGFRIST_KANAL_FENSTER = 8
LANGFRIST_KANAL_MIN_PUNKTE = 3
LANGFRIST_RANGE_FENSTER = 8
LANGFRIST_RANGE_BUCKET_USD = 70  # erhöht von 45: reale Widerstands-Touches über 6M streuen oft 100+ USD
LANGFRIST_RANGE_SEGMENTE = 3
LANGFRIST_ZONEN_FENSTER = 6
LANGFRIST_ZONEN_BUCKET_USD = 45
LANGFRIST_ZONEN_MIN_TREFFER = 2
LANGFRIST_ZONEN_TOP_N = 4
LANGFRIST_ZONEN_MIN_ABSTAND_USD = 90  # zwei Zonen näher als dieser Wert werden zusammengelegt

# Zusatzkanal "seit dem letzten großen Hoch/Tief" - der normale Kanal oben rechnet
# immer über den GESAMTEN Chart-Zeitraum, wodurch z.B. eine lange vorherige
# Aufwärtsbewegung eine anschließende steilere Korrektur "verwässert" und der Kanal
# flacher ausfällt als die tatsächliche aktuelle Bewegung. Der Zusatzkanal läuft
# stattdessen nur ab dem letzten signifikanten Wendepunkt (eigener, GRÖßERER
# Such-Fenster als die normale Kanal-Formationserkennung, damit nicht jede kleine
# Zwischenzacke als "der letzte Wendepunkt" zählt).
TAGESCHART_WENDEPUNKT_FENSTER = 15
TAGESCHART_ZUSATZKANAL_MIN_LAENGE = 20
LANGFRIST_WENDEPUNKT_FENSTER = 10
LANGFRIST_ZUSATZKANAL_MIN_LAENGE = 15
INTRADAY_WENDEPUNKT_FENSTER = 6
INTRADAY_ZUSATZKANAL_MIN_LAENGE = 10

# Intraday: eigene, benannte Parameter (vorher direkt als Zahlen im Code) - jetzt
# konsistent mit Tages-/6M-Chart als eigene Konstanten, unabhängig einstellbar.
INTRADAY_KANAL_FENSTER = 3
INTRADAY_KANAL_MIN_PUNKTE = 2
INTRADAY_RANGE_FENSTER = 4
INTRADAY_RANGE_BUCKET_USD = 6

# Daytrading-Zukunftsanalyse: 1h = Richtung, 30m = Setup, 15m = Trigger/Bestätigung.
# Bewusst getrennt von den bestehenden Pivot-/Range-Systemen.
INTRADAY_EMA_KURZ = 20
INTRADAY_EMA_LANG = 50
INTRADAY_ATR_FENSTER = 14
INTRADAY_SWING_FENSTER = 2
INTRADAY_BREAKOUT_LOOKBACK = {"1h": 8, "30min": 8, "15min": 12}


def berechne_atr(daten, fenster):
    """Average True Range über `fenster` Perioden - berücksichtigt auch
    Kurslücken zwischen den Perioden (nicht nur Hoch-Tief-Spanne). Nur mit
    Vergangenheitsdaten (Vortagesschluss via .shift(1)), kein Lookahead."""
    hoch, tief, schluss_vorperiode = daten["High"], daten["Low"], daten["Close"].shift(1)
    true_range = pd.concat([
        hoch - tief,
        (hoch - schluss_vorperiode).abs(),
        (tief - schluss_vorperiode).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(fenster).mean()


def berechne_volatilitaets_erlaubt(daten, fenster_kurz, fenster_lang, schwelle=VOLATILITAETS_SCHWELLE):
    """True = Volatilität im normalen Bereich, neue Einstiege erlaubt.
    False = ATR(kurz) liegt mehr als `schwelle`-mal über ATR(lang) - Markt
    wirkt aktuell ungewöhnlich chaotisch für ein neues Setup. Ergebnis ist
    bereits um eine Periode verschoben (nur bis zur Vorperiode bekannt,
    kein Lookahead)."""
    atr_kurz = berechne_atr(daten, fenster_kurz)
    atr_lang = berechne_atr(daten, fenster_lang)
    return ((atr_kurz / atr_lang) <= schwelle).shift(1)


def berechne_trend_schwelle(bekannte_schluesse, fenster):
    """Löst algebraisch, bei welchem hypothetischen NÄCHSTEN Schlusskurs die
    rollierende `fenster`-Tage-Regressionssteigung genau auf 0 kippen würde -
    der Schwellenwert, ab dem der V1e-Trendfilter von 'nicht erfüllt' auf
    'erfüllt' umschaltet. `bekannte_schluesse`: die letzten (fenster-1)
    bekannten Schlusskurse (chronologisch aufsteigend); der gesuchte Wert wäre
    der (fenster)-te, also der nächste noch unbekannte Schlusskurs.

    Funktioniert, weil die OLS-Steigung linear vom letzten Punkt abhängt, wenn
    die übrigen (fenster-1) Punkte fest bleiben - deshalb reichen zwei
    Stützpunkte (v0, v1) und eine lineare Interpolation auf Steigung=0, statt
    für viele Kandidatenpreise die Regression neu zu rechnen."""
    if len(bekannte_schluesse) != fenster - 1:
        return None
    x = np.arange(fenster)

    def steigung_mit_letztem(v):
        y = np.concatenate([bekannte_schluesse, [v]])
        m, _ = np.polyfit(x, y, 1)
        return m

    v0 = float(bekannte_schluesse[-1])
    v1 = v0 * 1.05 if v0 else v0 + 100.0  # zweiter Stützpunkt, +5% reicht für die lineare Interpolation
    m0, m1 = steigung_mit_letztem(v0), steigung_mit_letztem(v1)
    if m1 == m0:
        return None
    return v0 - m0 * (v1 - v0) / (m1 - m0)


def berechne_tage_bis_trendwechsel(bekannte_schluesse, fenster, angenommener_kurs, max_tage=None):
    """Wie viele weitere Handelstage bei angenommen GLEICHBLEIBENDEM Kurs
    vergehen müssten, bis die 50-Tage-Regressionssteigung positiv wird - weil
    ältere, stärker fallende Tage nach und nach aus dem rollierenden Fenster
    rausrutschen. Meist aussagekräftiger als berechne_trend_schwelle(): ein
    einzelner künftiger Tag hat bei einem 50-Punkte-Fenster nur begrenzten
    Hebel auf die Gesamtsteigung (Gewicht ca. (fenster-1)/2 geteilt durch die
    Varianz der x-Werte) - wenn die übrigen 49 Tage noch einen kräftigen
    Abwärtstrend zeigen, kann der rechnerische Schwellenwert für EINEN Tag
    unrealistisch weit vom aktuellen Kurs liegen (Befund 06.08.2026: 6.153 USD
    bei einem aktuellen Kurs von ca. 4.250 USD). Gibt None zurück, falls der
    Trend auch nach max_tage (Default: `fenster`) noch nicht dreht - dann
    reicht reines Abwarten nicht, der Kurs müsste tatsächlich steigen."""
    if max_tage is None:
        max_tage = fenster
    fenster_werte = list(bekannte_schluesse[-fenster:])
    x = np.arange(fenster)
    for tag in range(1, max_tage + 1):
        fenster_werte = fenster_werte[1:] + [angenommener_kurs]
        m, _ = np.polyfit(x, fenster_werte, 1)
        if m > 0:
            return tag
    return None


def volatilitaets_filter_text(fenster_kurz, fenster_lang):
    if VOLATILITAETS_FILTER_AKTIV:
        return (f" Zusätzlicher Volatilitätsfilter AKTIV: kein neuer Einstieg, wenn ATR({fenster_kurz}) "
                f"mehr als das {VOLATILITAETS_SCHWELLE:.1f}-fache von ATR({fenster_lang}) beträgt.")
    return " Volatilitätsfilter aktuell AUS (Konstante VOLATILITAETS_FILTER_AKTIV im Code)."


POSITIONSTRADING_REGELN_TEXT = (
    "Regeln: Nur Long. Trend positiv (Tages-Regression über 50 Handelstage) "
    "und Kurs berührt ein rollierendes 10-Tage-Tief, schließt aber wieder "
    "darüber -> KAUF. Stop = dieses Tief. TP1/TP2 = 2R/3R (R = Einstieg-Stop): "
    "TP1 erreicht -> Stop auf Breakeven, TP2 erreicht -> Stop auf TP1, danach "
    "täglich am 10-Tage-Tief nachgezogen. Stop erreicht -> VERKAUF, danach "
    "3 Handelstage Cooldown ohne neuen Einstieg."
) + volatilitaets_filter_text(VOLATILITAETS_FENSTER_KURZ_TAGE, VOLATILITAETS_FENSTER_LANG_TAGE)

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
) + volatilitaets_filter_text(VOLATILITAETS_FENSTER_KURZ_STUNDEN, VOLATILITAETS_FENSTER_LANG_STUNDEN)


def hole_twelvedata_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY nicht gesetzt. Key aus dem Gold-Briefing-Setup "
            "wiederverwenden oder unter https://twelvedata.com neu anlegen und "
            "als GitHub Secret hinterlegen."
        )
    return key


def hole_zeitreihe(interval, outputsize=None, start_date=None, end_date=None, max_versuche=4):
    """Holt eine OHLC-Zeitreihe für XAU/USD von Twelve Data und liefert sie als
    DataFrame mit DatetimeIndex (Spalten Open/High/Low/Close, aufsteigend
    sortiert) - drop-in-Ersatz für die frühere yfinance-.history()-Nutzung.

    Bei HTTP 429 (Rate-Limit, 8 Credits/Minute auf diesem Tarif) wird bis zu
    max_versuche mal mit Wartezeit erneut versucht statt sofort abzubrechen -
    ein einzelner Report-Lauf braucht mittlerweile ~8-9 Anfragen (V1e- UND
    Range-Ausbruch-Signal, drei Reaktionszonen-Fenster, Tages-/Intraday-Basis)
    und lief deshalb ohne diese Behandlung gelegentlich ins Limit (Befund
    05.08.2026, gleiches Problem wie zuvor schon im Backtest-Skript gelöst)."""
    for versuch in range(1, max_versuche + 1):
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
        if antwort.status_code == 429:
            wartezeit = 65
            print(f"  Rate-Limit bei Twelve-Data-Anfrage ({interval}, Versuch {versuch}/{max_versuche}) - "
                  f"warte {wartezeit}s und versuche es erneut...")
            time.sleep(wartezeit)
            continue

        antwort.raise_for_status()
        daten = antwort.json()
        if daten.get("status") == "error" or "values" not in daten:
            raise RuntimeError(f"Twelve-Data-Fehler ({interval}): {daten}")
        break
    else:
        raise RuntimeError(f"Twelve-Data-Rate-Limit nach {max_versuche} Versuchen nicht überwunden ({interval}).")

    # Kurze Pause NACH jeder erfolgreichen Anfrage: hält die ~8-9 Calls eines
    # Report-Laufs unter 8 Credits/Minute, statt erst auf den Rate-Limit-Retry
    # oben angewiesen zu sein (der kostet pro Treffer 65s statt hier 8s).
    time.sleep(8)

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
    """Liefert Realtime-Kurs, Vortages-OHLC sowie 1h/30m/15m-Daten für Chart und Daytrading-Analyse."""
    intraday = hole_zeitreihe(INTRADAY_INTERVALL, outputsize=INTRADAY_ANALYSE_BARS["1h"])
    intraday_30m = hole_zeitreihe("30min", outputsize=INTRADAY_ANALYSE_BARS["30min"])
    intraday_15m = hole_zeitreihe("15min", outputsize=INTRADAY_ANALYSE_BARS["15min"])
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
        "intraday_30m": intraday_30m,
        "intraday_15m": intraday_15m,
    }


def _intraday_trendinfo(df, lookback):
    """Ermittelt eine einfache, reproduzierbare MTF-Struktur ohne Lookahead."""
    if df is None or len(df) < max(INTRADAY_EMA_LANG + 5, lookback + 5):
        return None
    x = df.copy()
    x["EMA20"] = x["Close"].ewm(span=INTRADAY_EMA_KURZ, adjust=False).mean()
    x["EMA50"] = x["Close"].ewm(span=INTRADAY_EMA_LANG, adjust=False).mean()
    x["ATR14"] = berechne_atr(x, INTRADAY_ATR_FENSTER)
    letzte = x.iloc[-1]
    vorher = x.iloc[-2]
    ema20_slope = float(letzte["EMA20"] - x["EMA20"].iloc[-4])
    close = float(letzte["Close"])
    ema20 = float(letzte["EMA20"])
    ema50 = float(letzte["EMA50"])
    if close > ema20 and ema20 > ema50 and ema20_slope > 0:
        trend = "bullisch"
    elif close < ema20 and ema20 < ema50 and ema20_slope < 0:
        trend = "bärisch"
    else:
        trend = "neutral"
    recent = x.iloc[-lookback:]
    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())
    atr = float(letzte["ATR14"]) if pd.notna(letzte["ATR14"]) else None
    momentum = "steigend" if close > float(vorher["Close"]) else "fallend" if close < float(vorher["Close"]) else "unverändert"
    swing_highs, swing_lows = [], []
    closes = x["Close"]
    f = INTRADAY_SWING_FENSTER
    for i in range(f, len(x) - f):
        v = float(closes.iloc[i])
        if v >= float(closes.iloc[i-f:i+f+1].max()): swing_highs.append(v)
        if v <= float(closes.iloc[i-f:i+f+1].min()): swing_lows.append(v)
    struktur = "neutral"
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1] > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1] < swing_lows[-2]
        if hh and hl: struktur = "höhere Hochs/Höhere Tiefs"
        elif lh and ll: struktur = "tiefere Hochs/Tiefere Tiefs"
        elif hh or hl: struktur = "bullische Verbesserung"
        elif lh or ll: struktur = "bärische Verschlechterung"
    return {
        "close": close, "ema20": ema20, "ema50": ema50, "atr14": atr,
        "trend": trend, "struktur": struktur, "momentum": momentum,
        "high": recent_high, "low": recent_low, "zeitpunkt": x.index[-1],
    }


def analysiere_intraday_zukunft(daten, szenarien):
    """1h Richtung, 30m Setup, 15m Bestätigung; nur konditionale Szenarien."""
    frames = {
        "1h": _intraday_trendinfo(daten.get("intraday_reihe"), INTRADAY_BREAKOUT_LOOKBACK["1h"]),
        "30m": _intraday_trendinfo(daten.get("intraday_30m"), INTRADAY_BREAKOUT_LOOKBACK["30min"]),
        "15m": _intraday_trendinfo(daten.get("intraday_15m"), INTRADAY_BREAKOUT_LOOKBACK["15min"]),
    }
    if any(v is None for v in frames.values()):
        return {"status": "nicht_genug_daten", "frames": frames}
    gewicht = {"1h": 2, "30m": 2, "15m": 1}
    score = sum(gewicht[k] * (1 if frames[k]["trend"] == "bullisch" else -1 if frames[k]["trend"] == "bärisch" else 0) for k in frames)
    bias = "bullisch" if score >= 3 else "bärisch" if score <= -3 else "neutral / gemischt"
    f30, f15 = frames["30m"], frames["15m"]
    if f30["trend"] == "bullisch" and f15["trend"] == "bullisch": setup = "bullische Fortsetzung"
    elif f30["trend"] == "bärisch" and f15["trend"] == "bärisch": setup = "bärische Fortsetzung"
    elif f30["trend"] == "bullisch" and f15["trend"] == "bärisch": setup = "Pullback / kurzfristige Gegenbewegung"
    elif f30["trend"] == "bärisch" and f15["trend"] == "bullisch": setup = "Erholung innerhalb der Abwärtsstruktur"
    else: setup = "Range / unklare Struktur"
    return {
        "status": "ok", "frames": frames, "bias": bias, "score": score,
        "setup": setup, "bull_trigger": szenarien.get("naechster_widerstand"),
        "bear_trigger": szenarien.get("naechster_support"),
        "bull_confirm": f30["trend"] == "bullisch" and f15["trend"] == "bullisch",
        "bear_confirm": f30["trend"] == "bärisch" and f15["trend"] == "bärisch",
        "daytrade_resistance": max(f30["high"], f15["high"]),
        "daytrade_support": min(f30["low"], f15["low"]),
    }


def formatiere_intraday_zukunft(zukunft, fmt):
    if not zukunft or zukunft.get("status") != "ok":
        return "INTRADAY-ZUKUNFTSANALYSE: nicht genug 1h/30m/15m-Daten vorhanden."
    f = zukunft["frames"]
    def zeile(label, x):
        atr = f" | ATR14 {fmt(x['atr14'])}" if x.get("atr14") else ""
        return (f"{label}: Trend {x['trend']}, Struktur {x['struktur']}, Momentum {x['momentum']}, "
                f"Close {fmt(x['close'])}, EMA20 {fmt(x['ema20'])}, EMA50 {fmt(x['ema50'])}, "
                f"Range {fmt(x['low'])}-{fmt(x['high'])}{atr}")
    bull = f"über {fmt(zukunft['bull_trigger'])}" if zukunft.get("bull_trigger") else "kein bullischer Trigger"
    bear = f"unter {fmt(zukunft['bear_trigger'])}" if zukunft.get("bear_trigger") else "kein bärischer Trigger"
    daytrade_long = f"über {fmt(zukunft['daytrade_resistance'])}"
    daytrade_short = f"unter {fmt(zukunft['daytrade_support'])}"
    return "\n".join([
        f"INTRADAY-ZUKUNFTSANALYSE | Bias: {zukunft['bias']} | Setup: {zukunft['setup']} | MTF-Score: {zukunft['score']:+d}",
        zeile("1h", f["1h"]), zeile("30m", f["30m"]), zeile("15m", f["15m"]),
        f"Bullisches Szenario: {bull} + 30m/15m-Bestätigung={zukunft['bull_confirm']}; Ziel gemäß bestehendem Pivot-Szenario.",
        f"Bärisches Szenario: {bear} + 30m/15m-Bestätigung={zukunft['bear_confirm']}; Ziel gemäß bestehendem Pivot-Szenario.",
        f"Daytrading-Trigger: Long {daytrade_long} | Short {daytrade_short} | Ausbruch möglichst mit 15m-Close bestätigen; diese Trigger ersetzen NICHT die großen Pivot-Szenario-Marken.",
        "Neutral/kein Trade: bei gemischter 15m/30m-Struktur keine Richtungsbestätigung erzwingen.",
    ])


def hole_langfrist_daten(monate=36):
    """Tageskurse der letzten `monate` Monate für die Reaktionszonen-Analyse
    (separat von den Intraday-Daten, die für Pivots/Chart genutzt werden)."""
    start = (pd.Timestamp.now() - pd.DateOffset(months=monate)).strftime("%Y-%m-%d")
    daily = hole_zeitreihe_taeglich(start_date=start, outputsize=5000)
    if len(daily) < 60:
        return None
    return daily



def berechne_tages_ma_struktur(daily, wma_fenster=200):
    """EMA20/50/100/200 und WMA200 aus echten Tagesdaten.
    Diese gemeinsame Tagesdatenbasis wird für 6M und Position verwendet.
    """
    if daily is None or len(daily) < wma_fenster:
        return None
    close = pd.to_numeric(daily["Close"], errors="coerce").dropna()
    if len(close) < wma_fenster:
        return None
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema100 = close.ewm(span=100, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    gewichte = np.arange(1, wma_fenster + 1, dtype=float)
    gewicht_summe = gewichte.sum()
    wma200 = close.rolling(wma_fenster).apply(
        lambda x: float(np.dot(x, gewichte) / gewicht_summe), raw=True
    )
    if pd.isna(wma200.iloc[-1]):
        return None
    letzter_close = float(close.iloc[-1])
    letzter_wma = float(wma200.iloc[-1])
    wma_vorher = float(wma200.iloc[-6]) if pd.notna(wma200.iloc[-6]) else letzter_wma
    if letzter_close > float(ema200.iloc[-1]) and letzter_close > letzter_wma:
        trendlage = "Kurs über EMA200 und WMA200"
    elif letzter_close < float(ema200.iloc[-1]) and letzter_close < letzter_wma:
        trendlage = "Kurs unter EMA200 und WMA200"
    else:
        trendlage = "gemischte Lage um EMA200/WMA200"
    wma_richtung = "steigend" if letzter_wma > wma_vorher else "fallend" if letzter_wma < wma_vorher else "seitwärts"
    return {
        "close": letzter_close,
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "ema100": float(ema100.iloc[-1]),
        "ema200": float(ema200.iloc[-1]),
        "wma200": letzter_wma,
        "wma200_richtung": wma_richtung,
        "trendlage": trendlage,
        "zeitpunkt": close.index[-1],
        "datenpunkte": int(len(close)),
    }


def formatiere_tages_ma_struktur(ma, fmt):
    if not ma:
        return "TAGESDATEN-MA-STRUKTUR: nicht genug Tagesdaten für EMA/WMA200 vorhanden."
    return (
        f"TAGESDATEN-MA-STRUKTUR (6M / POSITION): Close {fmt(ma['close'])} | "
        f"EMA20 {fmt(ma['ema20'])} | EMA50 {fmt(ma['ema50'])} | "
        f"EMA100 {fmt(ma['ema100'])} | EMA200 {fmt(ma['ema200'])} | "
        f"WMA200 {fmt(ma['wma200'])} | Trendlage: {ma['trendlage']} | "
        f"WMA200 {ma['wma200_richtung']} | Stand {ma['zeitpunkt']}"
    )

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

    # Die letzten `fenster` Tage können nach obiger Regel NIE als Swing-Punkt zählen,
    # weil ihnen die künftigen Tage zur Bestätigung fehlen - der aktuelle Kurs kann
    # dadurch nie eine bestehende Zone bestätigen, selbst wenn er gerade jetzt genau
    # ein altes Level erneut testet. Deshalb zusätzlich ein einseitiger (nur
    # rückwärts schauender) Test für die letzten `fenster` Tage: zählt als Swing,
    # wenn der Tag ein Extremum der bis dahin bekannten (rückwärtigen) Kerzen ist.
    for i in range(max(fenster, n - fenster), n):
        fenster_h = highs[max(0, i - fenster):i + 1]
        if highs[i] == fenster_h.max():
            swing_highs.append(highs[i])
        fenster_l = lows[max(0, i - fenster):i + 1]
        if lows[i] == fenster_l.min():
            swing_lows.append(lows[i])

    def clustern(punkte):
        # Gleiche Toleranz-Kette wie in finde_range_box, statt starres Preis-Raster
        # (siehe dortiger Kommentar für den Hintergrund) - PLUS Obergrenze für die
        # Gesamtspanne eines Clusters (max. 2,5x bucket_usd), sonst kann sich die
        # Kette durch eine choppy Phase hindurch "durchhangeln" (P1 nah an P2, P2 nah
        # an P3, ...) und am Ende eine 150 USD breite "Zone" mit 15+ Treffern liefern,
        # obwohl die Randpunkte preislich nichts mehr miteinander zu tun haben.
        if not punkte:
            return []
        max_spanne = bucket_usd * 2.5
        punkte_sortiert = sorted(punkte)
        cluster = [[punkte_sortiert[0]]]
        for p in punkte_sortiert[1:]:
            if p - cluster[-1][-1] <= bucket_usd and p - cluster[-1][0] <= max_spanne:
                cluster[-1].append(p)
            else:
                cluster.append([p])
        zonen = [(np.mean(c), len(c)) for c in cluster if len(c) >= min_treffer]
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


def berechne_szenarien(realtime, pivots):
    """Leitet aus den acht vorhandenen Pivot-Leveln (klassische_pivots) eine
    priorisierte Szenario-Einordnung ab: Bullisch (über dem nächsten
    Widerstand, mit Ziel am übernächsten Level), Neutral (dazwischen),
    Bärisch (unter dem nächsten Support, mit Ziel am übernächsten Level).
    Keine neue Berechnung, keine neue Datenquelle - reine Neuanordnung dessen,
    was die Pivot-Funktion schon liefert, zugespitzt auf 'was als Nächstes
    passieren müsste' statt einer flachen Liste von acht Einzelwerten."""
    alle_level = sorted(pivots["r"] + pivots["s"])
    ueber = [w for w in alle_level if w > realtime]
    unter = [w for w in alle_level if w < realtime]
    return {
        "naechster_widerstand": ueber[0] if ueber else None,
        "ziel_bullisch": ueber[1] if len(ueber) > 1 else None,
        "naechster_support": unter[-1] if unter else None,
        "ziel_baerisch": unter[-2] if len(unter) > 1 else None,
    }


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


def generiere_rueckblick(daten, pivots, tendenz, zonen_je_zeitraum, szenarien, langfrist_formation=None, mittelfristige_szenarien=None, intraday_zukunft=None, tages_ma_struktur=None):
    """Ruft Gemini auf, um einen kurzen charttechnischen Rückblick-Text zu erzeugen.
    zonen_je_zeitraum: dict {monate: reaktionszonen-dict oder None}, z.B. {3: {...}, 6: {...}, 36: {...}}.
    langfrist_formation: automatisch erkannte Formation des langfristigen Tagescharts.
    szenarien: Ergebnis von berechne_szenarien() - wird dem Prompt als FESTE Trigger-
    Marken vorgegeben, damit der Rückblick-Text dieselben Zahlen nennt wie der
    SZENARIEN-Block im Report. Vorher leitete Gemini eigene Trigger aus dem
    Intraday-Hoch/-Tief her, unabhängig vom Pivot-basierten Szenarien-Block - das
    führte zu zwei unterschiedlichen Zahlen für denselben Ausbruchspunkt im selben
    Report (Befund 06.08.2026: Rückblick nannte 4.295,87 USD als Aufwärts-Trigger,
    der Szenarien-Block 4.320,79 USD)."""
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

    if langfrist_formation == "Abwärtskanal":
        langfrist_block = (
            "\nLangfristige Chartstruktur (Tageschart): ÜBERGEORDNETER ABWÄRTSKANAL. "
            "Die aktuelle Bewegung ist als kurzfristige bullische Erholung innerhalb dieser "
            "übergeordneten Abwärtsstruktur einzuordnen. Formuliere NICHT, der mittelfristige "
            "Abwärtstrend sei bereits beendet oder durch einen intakten Aufwärtstrendkanal "
            "ersetzt.\n"
        )
    elif langfrist_formation:
        langfrist_block = f"\nLangfristige Chartstruktur (Tageschart): {langfrist_formation}.\n"
    else:
        langfrist_block = ""

    lokale_daytrading_block = ""
    if intraday_zukunft and intraday_zukunft.get("status") == "ok":
        lokale_daytrading_block = (
            f"Lokale Daytrading-Trigger (nur für die nächsten Handelsstunden, NICHT die großen Pivot-Szenario-Marken): "
            f"Long über {intraday_zukunft['daytrade_resistance']:.2f} USD | "
            f"Short unter {intraday_zukunft['daytrade_support']:.2f} USD. "
            "Ein Ausbruch soll möglichst mit einem 15m-Close bestätigt werden.\n"
        )

    tages_ma_block = (
        f"Tagesdaten-MA-Struktur (verbindlich für 6M/Position): Close {tages_ma_struktur['close']:.2f} | "
        f"EMA20 {tages_ma_struktur['ema20']:.2f} | EMA50 {tages_ma_struktur['ema50']:.2f} | "
        f"EMA100 {tages_ma_struktur['ema100']:.2f} | EMA200 {tages_ma_struktur['ema200']:.2f} | "
        f"WMA200 {tages_ma_struktur['wma200']:.2f} | Trendlage: {tages_ma_struktur['trendlage']} | "
        f"WMA200 {tages_ma_struktur['wma200_richtung']}. Diese Werte stammen aus echten Tagesdaten; "
        "keine MA-Werte erfinden oder verändern.\n"
        if tages_ma_struktur else
        "Tagesdaten-MA-Struktur: nicht verfügbar; keine MA-Werte erfinden.\n"
    )

    szenarien_block = "Bereits festgelegte Szenario-Marken (aus den Pivots abgeleitet, im Report separat als eigener Block gezeigt - NICHT selbst neu herleiten, sondern genau diese Zahlen im Text verwenden):\n"
    if szenarien["naechster_widerstand"] is not None:
        szenarien_block += f"- Aufwärts-Trigger: Ausbruch über {szenarien['naechster_widerstand']:.2f} USD"
        if szenarien["ziel_bullisch"] is not None:
            szenarien_block += f", Ziel danach {szenarien['ziel_bullisch']:.2f} USD"
        szenarien_block += "\n"
    if szenarien["naechster_support"] is not None:
        szenarien_block += f"- Abwärts-Trigger: Bruch unter {szenarien['naechster_support']:.2f} USD"
        if szenarien["ziel_baerisch"] is not None:
            szenarien_block += f", Ziel danach {szenarien['ziel_baerisch']:.2f} USD"
        szenarien_block += "\n"

    prompt = f"""Du bist ein nüchterner charttechnischer Kommentator für Gold Spot (XAU/USD).


    ZEITHORIZONTE FÜR DIE INTERPRETATION:
    - KURZFRISTIG / INTRADAY: Horizont heute bzw. nächste Handelsstunden.
      Beurteile ausschließlich die kurzfristige Intraday-Situation.
    - MITTELFRISTIG / 6M-STRUKTUR: Horizont mehrere Wochen bis einige Monate.
      Beurteile ausschließlich die übergeordnete 6M-Struktur.
    - LANGFRISTIG / POSITION: Horizont mehrere Monate bis langfristig.
      Beurteile ausschließlich die langfristige Positionstrading-Struktur.

    WICHTIG:
    - Nenne bei jedem Ausblick ausdrücklich den zugehörigen Zeithorizont.
    - Vermische keine Marken oder Aussagen verschiedener Zeithorizonte.
    - Die vom Programm gelieferten Kursmarken sind verbindlich. Erfinde,
      verändere oder verschiebe keine Kursmarken.

Schreibe einen detaillierteren charttechnischen Ausblick in GENAU 8 Sätzen,
deutsch, sachlich, ohne Anrede und ohne Kauf-/Verkaufsempfehlung. Die 8 Sätze sind
verbindlich in exakt dieser Reihenfolge auszugeben:

1. INTRADAY 1h – genau 1 Satz. Beginne den Satz mit "Intraday 1h:" und beschreibe ausschließlich die übergeordnete Intraday-Richtung aus der 1h-Struktur.
2. INTRADAY 30m – genau 1 Satz. Beginne den Satz mit "Intraday 30m:" und beschreibe ausschließlich das kurzfristige Setup aus der 30m-Struktur.
3. INTRADAY 15m – genau 1 Satz. Beginne den Satz mit "Intraday 15m:" und beschreibe ausschließlich die 15m-Bestätigung bzw. den nächsten Trigger.
4. INTRADAY SZENARIO – genau 1 Satz. Beginne den Satz mit "Intraday Szenario:" und führe die 1h/30m/15m-Informationen zu einem konkreten Bull-/Bear-/Neutral-Szenario zusammen. Nutze ausschließlich Intraday-Daten und Intraday-Marken.
5. DAYTRADING-FOKUS – genau 1 Satz. Beginne den Satz mit "Daytrading-Fokus:" und ordne ausschließlich die nächsten Handelsstunden ein: welches Intraday-Szenario hat aktuell Priorität, welche Bestätigung bzw. welcher bereits definierte lokale Daytrading-Trigger ist entscheidend und was würde das Szenario invalidieren. Verwende ausschließlich die vorgegebenen lokalen Daytrading-Trigger und andere vorhandene Intraday-Informationen; erfinde keine neuen Kursmarken.
6. 6M-STRUKTUR – genau 1 Satz. Beginne den Satz mit "6M:" und beschreibe ausschließlich die mittelfristige 6M-Struktur. Nutze keine reinen Intraday-Pivots.
7. POSITION – genau 1 Satz. Beginne den Satz mit "Position:" und beschreibe ausschließlich die übergeordnete Tageschart-/Positionstrading-Struktur.
8. GESAMTBILD – genau 1 Satz. Beginne den Satz mit "Gesamtbild:" und fasse die Aussagen aus Intraday, Daytrading-Fokus, 6M und Position knapp zusammen, ohne neue Kursmarken einzuführen.

Jeder Satz muss exakt seine vorgegebene Kennzeichnung verwenden. Keine zusätzlichen Sätze, Aufzählungen oder Satzfragmente. Die Reihenfolge darf nicht verändert werden. Die 1h/30m/15m-Rollen sind strikt: 1h bestimmt die Richtung, 30m das Setup, 15m die Bestätigung. Der Daytrading-Fokus kommt unmittelbar nach dem Intraday-Szenario und vor 6M/Position.


Intraday-Daten (kurzfristig):
- Realtime-Kurs: {daten['realtime']:.2f} USD
- Schlusskurs Vortag: {daten['prev_close']:.2f} USD
- Vortages-Hoch: {daten['prev_high']:.2f} USD
- Vortages-Tief: {daten['prev_low']:.2f} USD
- Intraday-Hoch (aktueller Zeitraum): {daten['intraday_reihe']['Close'].max():.2f} USD
- Intraday-Tief (aktueller Zeitraum): {daten['intraday_reihe']['Close'].min():.2f} USD
- Tendenz zum Vortagesschluss: {tendenz}
- Intraday-Pivot-Widerstände: {', '.join(f'{v:.0f}' for v in pivots['r'])} USD
- Intraday-Pivot-Unterstützungen: {', '.join(f'{v:.0f}' for v in pivots['s'])} USD

{formatiere_intraday_zukunft(intraday_zukunft, lambda n: f'{n:.2f}') if intraday_zukunft else 'Keine MTF-Intradayanalyse verfügbar.'}

{lokale_daytrading_block}
    {szenarien_block}
Mittelfristige Szenario-Ergebnisse aus der mittleren Karte (VERBINDLICH, bereits vom Programm berechnet):
- Bullisch: über {mittelfristige_szenarien["bull"]} USD
- Bullisches Ziel: {mittelfristige_szenarien["ziel_bull"] or "keines"} USD
- Neutral: {mittelfristige_szenarien["neutral"]}
- Bärisch: unter {mittelfristige_szenarien["baer"]} USD
- Bärisches Ziel: {mittelfristige_szenarien["ziel_baer"] or "keines"} USD
Verwende diese mittelfristigen Ergebnisse unverändert. Leite für den mittelfristigen Satz keine eigenen Trigger oder Ziele aus anderen Daten ab.

{tages_ma_block}
{langfrist_block}
{saison_block}
Strukturelle Reaktionszonen (mehrfach bestätigte Hoch-/Tiefpunkte je Zeitfenster - diese
sind aussagekräftiger für eine Formationsbewertung als die reinen Intraday-Pivots; kürzere
Fenster zeigen eher aktuell relevante Zonen, längere Fenster eher übergeordnete Struktur):
{zonen_block}

Für KURZFRISTIG / INTRADAY müssen die beiden vorgegebenen großen Szenario-Marken
(Aufwärts-Trigger und Abwärts-Trigger samt Ziele) unverändert verwendet werden. Für den
DAYTRADING-FOKUS gelten zusätzlich ausschließlich die ausdrücklich vorgegebenen lokalen
30m/15m-Daytrading-Trigger; erfinde keine weiteren Trigger-Kurse.

Für MITTELFRISTIG / 6M-STRUKTUR verwende die oben ausdrücklich bereitgestellten
Ergebnisse der mittleren Szenario-Karte unverändert. Verwende dafür keine Intraday-Pivotmarken
und keine langfristigen Marken als eigene mittelfristige Trigger oder Ziele. Verändere oder
erfinde keine mittelfristigen Kursmarken.

Für LANGFRISTIG / POSITION ordne ausschließlich die übergeordnete Tageschart-/Positionstrading-
Struktur ein. Wenn ein übergeordneter Abwärtskanal vorgegeben ist, muss klar zwischen
kurzfristiger Erholung und übergeordneter Abwärtsstruktur unterschieden werden.

Für 6M und POSITION berücksichtige zusätzlich die vorgegebene Tagesdaten-MA-Struktur. EMA20/50/100/200 beschreiben die Trendstruktur; der WMA200 ist eine zentrale Schlüsselmarke. Nenne seine Lage zum Kurs und seine Richtung nur dann, wenn es für den jeweiligen Horizont relevant ist.

Ordne die Kursbewegung dort, wo es für den jeweiligen Horizont seriös möglich ist, knapp
einer gängigen charttechnischen Formation zu. Falls keine seriöse Formation erkennbar
ist, sage das statt zu spekulieren. Saisonaler Kontext darf nur ergänzend erwähnt werden.


Die Sätze 5 und 8 sind Teil der exakt 8 Sätze und dürfen keine neuen Kursmarken
einführen. Satz 5 konzentriert sich ausschließlich auf die nächsten Handelsstunden; Satz 8
fasst am Ende alle Horizonte zum Gesamtbild zusammen."""

    # Gemini-Fallback: bei temporaeren API-/Ueberlastungsfehlern werden
    # mehrere verfuegbare Modelle nacheinander versucht. Pro Modell maximal
    # zwei Versuche mit exponentiellem Backoff.
    #
    # Reihenfolge:
    # 1. Gemini 3.5 Flash
    # 2. Gemini 3.1 Flash-Lite
    # 3. Gemini 2.5 Flash
    #
    # Die Modellnamen entsprechen den aktuell von Google dokumentierten
    # Gemini-API-Endpunkten.
    modelle = [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]

    def ist_temporaerer_gemini_fehler(exc):
        text = str(exc).lower()
        status = getattr(exc, "status_code", None)
        return (
            status in (429, 500, 502, 503, 504)
            or any(code in text for code in (
                "429", "500", "502", "503", "504",
                "unavailable", "resource exhausted",
                "temporarily unavailable", "service unavailable",
                "deadline exceeded", "internal server error",
            ))
        )

    letzter_fehler = None
    fehler_pro_modell = []

    for modell in modelle:
        for versuch in range(1, 3):
            try:
                print(
                    f"Gemini-Rueckblick: Modell {modell} "
                    f"(Versuch {versuch}/2)"
                )
                antwort = client.models.generate_content(
                    model=modell,
                    contents=prompt,
                )
                text = (antwort.text or "").strip()
                if text:
                    print(f"Gemini-Rueckblick erfolgreich mit {modell}.")
                    return text

                letzter_fehler = RuntimeError(
                    f"Gemini-Modell {modell} lieferte eine leere Antwort."
                )
                fehler_pro_modell.append(f"{modell}: leere Antwort")
                break

            except Exception as exc:
                letzter_fehler = exc
                fehler_pro_modell.append(f"{modell}: {exc}")

                if not ist_temporaerer_gemini_fehler(exc):
                    print(
                        f"Gemini-Rueckblick: permanenter Fehler bei {modell}: {exc}"
                    )
                    break

                if versuch < 2:
                    wartezeit = 10 * (2 ** (versuch - 1))
                    print(
                        f"Gemini-Rueckblick: temporaerer Fehler bei {modell}; "
                        f"erneuter Versuch in {wartezeit}s."
                    )
                    time.sleep(wartezeit)
                else:
                    print(
                        f"Gemini-Rueckblick: {modell} nach 2 Versuchen nicht "
                        f"verfuegbar; wechsle zum naechsten Modell."
                    )

    details = " | ".join(fehler_pro_modell[-6:])
    return (
        "(Rückblick-Generierung fehlgeschlagen: alle Gemini-Modelle nicht verfügbar. "
        f"Letzte Fehler: {details or letzter_fehler})"
    )


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
        # Toleranz-Kette statt starres Preis-Raster: Punkte werden nach Preis sortiert
        # und so lange in denselben Cluster gepackt, wie der Abstand zum jeweils letzten
        # Punkt <= bucket_usd ist. Beim starren Raster (round(preis/bucket_usd)*bucket_usd)
        # können zwei Punkte, die nur wenige USD auseinanderliegen, aber knapp auf
        # verschiedenen Seiten einer Rastergrenze landen, fälschlich in getrennte Fächer
        # fallen - beobachtet bei drei Swing-Hochs 150 USD auseinander (4701/4752/4851),
        # die inhaltlich klar eine Zone bildeten, aber am 45-USD-Raster vorbeigerundet
        # wurden und dadurch als drei Einzeltreffer statt einem 3er-Cluster zählten.
        # ABER: die Kette selbst kann sich genauso "durchketten" (P1 nah an P2, P2 nah
        # an P3, ... obwohl P1 und P10 preislich längst nichts mehr gemeinsam haben) -
        # deshalb zusätzlich eine Obergrenze für die GESAMTSPANNE eines Clusters
        # (max. 2,5x bucket_usd), sonst kann ein einzelnes Fenster bereits für sich
        # allein eine viel zu breite "Zone" liefern.
        if not punkte:
            return None
        max_spanne = bucket_usd * 2.5
        punkte_sortiert = sorted(punkte, key=lambda pt: pt[1])
        cluster = [[punkte_sortiert[0]]]
        for zeit, preis in punkte_sortiert[1:]:
            if preis - cluster[-1][-1][1] <= bucket_usd and preis - cluster[-1][0][1] <= max_spanne:
                cluster[-1].append((zeit, preis))
            else:
                cluster.append([(zeit, preis)])
        bestes = max(cluster, key=len)
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


def finde_range_boxen(preisreihe, fenster=5, bucket_usd=30, min_treffer=2, segmente=3, max_kern_laenge=45):
    """Wie finde_range_box, aber für längere Zeiträume (z.B. 6 Monate) gedacht, in
    denen es mehrere zeitlich getrennte Ranges auf unterschiedlichen Kursniveaus
    geben kann. Arbeitet mit einem GLEITENDEN Fenster (Fensterlänge ~ Gesamtlänge/
    segmente, Schrittweite ein Drittel davon) statt einer festen Drittelung des
    Zeitraums - Bugfix: bei fester Drittelung fiel eine Konsolidierung, die zufällig
    genau über einer Segmentgrenze lag, komplett durchs Raster, weil ihre Berührungen
    auf zwei Segmente aufgeteilt wurden und in keinem davon allein die Mindestanzahl
    erreichten (in der Praxis beobachtet: eine mehrwöchige Range wurde dadurch gar
    nicht erkannt, obwohl sie im Chart deutlich sichtbar war). Überlappende Treffer
    aus benachbarten Fensterpositionen werden anschließend zu einer Box verschmolzen.
    max_kern_laenge deckelt die Fenstergröße nach OBEN (in Perioden der preisreihe):
    ohne diesen Deckel wächst n//segmente mit der Datenmenge unbegrenzt mit, wodurch
    bei einem längeren Datensatz (z.B. 14 statt 4 Monate) einzelne Kandidatenfenster
    schon für sich genommen viele Monate lang werden - eine 'Range' soll aber eine
    zeitlich begrenzte, erkennbare Konsolidierung bleiben und nicht praktisch der
    gesamte Chart-Zeitraum sein.
    Gibt eine Liste von (start_zeit, end_zeit, tief, hoch) zurück."""
    n = len(preisreihe)
    kern_laenge = min(max(min_treffer + 3, n // segmente), max_kern_laenge)
    fensterlaenge = kern_laenge + 2 * fenster  # Rand dazurechnen: Punkte nah am Fensterrand
    # brauchen selbst innerhalb des Fensters noch `fenster` Nachbarn auf beiden Seiten,
    # um überhaupt als Swing erkannt zu werden - ohne diesen Rand verpasst man Touches,
    # die knapp am Rand des jeweiligen Fensters liegen.
    schrittweite = max(1, fenster)  # feine Schrittweite, damit irgendeine Fensterposition
    # die komplette Range mit ausreichend Rand auf beiden Seiten erfasst

    kandidaten = []
    start = 0
    while start < n:
        ende = min(start + fensterlaenge, n)
        teil = preisreihe.iloc[start:ende]
        if len(teil) >= 2 * fenster + min_treffer:
            box = finde_range_box(teil, fenster=fenster, bucket_usd=bucket_usd, min_treffer=min_treffer)
            if box:
                kandidaten.append(box)
        if ende >= n:
            break
        start += schrittweite

    if not kandidaten:
        return []

    # Kandidaten aus überlappenden Fensterpositionen zu je einer Box verschmelzen -
    # sonst würde dieselbe Range mehrfach (leicht versetzt) auftauchen, einmal pro
    # Fensterposition, die sie erfasst hat. ABER mit Obergrenze für Höhe und Dauer:
    # ohne die kann sich das Verschmelzen durch eine lange, stetige Trendbewegung
    # "durchketten" (Box A überlappt B, B überlappt C, ... obwohl A und der 20.
    # Nachfolger preislich längst nichts mehr miteinander zu tun haben) und am Ende
    # eine einzige Box entstehen, die fast den gesamten Chart-Zeitraum überdeckt -
    # beobachtet bei einem durchgehenden 14-Monats-Aufwärtstrend, wo daraus eine
    # "Range" über 289 USD und 413 Tage wurde, obwohl das klar kein echtes Pendeln
    # zwischen zwei Levels war, sondern nur viele überlappende Zwischenschritte.
    typischer_abstand = pd.Series(preisreihe.index).diff().median()

    def signifikante_ueberlappung(a, b, min_anteil=0.4):
        # Ersetzt die vorherige feste Obergrenze für Höhe/Dauer der fusionierten Box:
        # die verhinderte auch echte Überlappungen, sobald die kombinierte Box größer
        # als ein typisches Kandidatenfenster wurde (beobachtet an zwei Boxen mit
        # >50% Zeit- und 93% Preis-Überlappung, die an einer 103-Tage-Dauergrenze
        # scheiterten, obwohl sie inhaltlich klar dieselbe Range waren). Jetzt zählt
        # stattdessen der ÜBERLAPPUNGSANTEIL relativ zur kleineren der beiden Boxen -
        # verhindert weiterhin das Durchketten durch einen langen Trend (dort
        # überlappen sich aufeinanderfolgende Kandidaten nur an einem kleinen Rand-
        # stück, nicht großflächig), lässt aber echte, weitgehend deckungsgleiche
        # Ranges unabhängig von ihrer Gesamtausdehnung zu einer Box verschmelzen.
        start_a, end_a, tief_a, hoch_a = a
        start_b, end_b, tief_b, hoch_b = b
        zeit_overlap = min(end_a, end_b) - max(start_a, start_b)
        if zeit_overlap <= pd.Timedelta(0):
            return False
        zeit_anteil = zeit_overlap / min(end_a - start_a, end_b - start_b)
        preis_overlap = min(hoch_a, hoch_b) - max(tief_a, tief_b)
        if preis_overlap <= 0:
            return False
        preis_anteil = preis_overlap / min(hoch_a - tief_a, hoch_b - tief_b)
        return zeit_anteil >= min_anteil and preis_anteil >= min_anteil

    # Gegen JEDE bereits gemergte Box prüfen, nicht nur die zuletzt hinzugefügte:
    # sonst kann ein Kandidat B, der zwischen A und C liegt, aber selbst nicht mit A
    # mergen darf, den "Merge-Faden" abreißen lassen - C würde dann nur noch gegen B
    # geprüft und nicht mehr gegen A, obwohl C und A sich eigentlich überlappen.
    kandidaten.sort(key=lambda b: b[0])
    verschmolzen = [list(kandidaten[0])]
    for start_zeit, end_zeit, tief, hoch in kandidaten[1:]:
        gemerged = False
        for box in verschmolzen:
            if signifikante_ueberlappung(tuple(box), (start_zeit, end_zeit, tief, hoch)):
                box[0] = min(box[0], start_zeit)
                box[1] = max(box[1], end_zeit)
                box[2] = min(box[2], tief)
                box[3] = max(box[3], hoch)
                gemerged = True
                break
        if not gemerged:
            verschmolzen.append([start_zeit, end_zeit, tief, hoch])

    # Nach dem Mergen können jetzt (durch die erweiterten Grenzen einzelner Boxen)
    # neue Überlappungen zwischen bereits gemergten Boxen entstanden sein - deshalb
    # wiederholen, bis sich nichts mehr ändert (kommt in der Praxis selten öfter als
    # 1-2x vor, da wenige Boxen übrig bleiben).
    aenderung = True
    while aenderung and len(verschmolzen) > 1:
        aenderung = False
        neu = []
        for box in verschmolzen:
            box = list(box)
            for ziel in neu:
                if signifikante_ueberlappung(tuple(ziel), tuple(box)):
                    ziel[0] = min(ziel[0], box[0]); ziel[1] = max(ziel[1], box[1])
                    ziel[2] = min(ziel[2], box[2]); ziel[3] = max(ziel[3], box[3])
                    aenderung = True
                    break
            else:
                neu.append(box)
        verschmolzen = neu

    # Nachträgliche Qualitätsprüfung: jede fertig gemergte Box gegen die TATSÄCHLICHEN
    # Kursdaten in ihrem eigenen (nach dem Mergen ggf. vergrößerten) Zeitfenster
    # validieren, statt sich nur auf die Konstruktion aus den einzelnen Kandidaten-
    # fenstern zu verlassen. Beobachtet: eine Box mit nur 2 Berührungen am Tief aber
    # 24 am Hoch und 12 von 50 Schlusskursen außerhalb der Box - konstruktionsbedingt
    # "gültig" (min_treffer=2 auf beiden Seiten erreicht), aber keine echte, beidseitig
    # bestätigte Range. Verworfen wird, wenn eine Seite nach dem Mergen nicht mehr
    # ausreichend bestätigt ist ODER der Kurs zu oft außerhalb der Box geschlossen hat.
    geprueft = []
    for start_zeit, end_zeit, tief, hoch in verschmolzen:
        ausschnitt = preisreihe.loc[start_zeit:end_zeit]
        puffer = (hoch - tief) * 0.15
        tage_nah_tief = (ausschnitt["Low"] <= tief + puffer).sum()
        tage_nah_hoch = (ausschnitt["High"] >= hoch - puffer).sum()
        ausserhalb_anteil = ((ausschnitt["Close"] < tief) | (ausschnitt["Close"] > hoch)).mean()
        if tage_nah_tief >= min_treffer and tage_nah_hoch >= min_treffer and ausserhalb_anteil <= 0.20:
            geprueft.append([start_zeit, end_zeit, tief, hoch])
    verschmolzen = geprueft

    # Bei mehr Kandidaten als angezeigt werden sollen: die am LÄNGSTEN andauernden
    # Ranges behalten (längere Dauer typischerweise ist ein stärkeres Signal für
    # eine "echte" Konsolidierung als eine kurze, zufällige Zwischenpause) statt
    # einfach die chronologisch ersten zu nehmen.
    verschmolzen.sort(key=lambda b: b[1] - b[0], reverse=True)
    return [tuple(b) for b in verschmolzen[:segmente]]


def finde_intraday_umkehrzonen(intraday_reihe, fenster=3, bucket_usd=15, min_treffer=2, top_n=3):
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

    # Einseitiger Rückwärts-Check für die letzten `fenster` Kerzen - gleicher Grund
    # wie in analysiere_reaktionszonen: ohne das kann der AKTUELLE Kurs nie eine
    # bestehende Umkehrzone bestätigen, selbst wenn er sie gerade jetzt erneut testet.
    for i in range(max(fenster, n - fenster), n):
        fh = high.iloc[max(0, i - fenster):i + 1]
        if high.iloc[i] == fh.max():
            swing_highs.append(high.iloc[i])
        fl = low.iloc[max(0, i - fenster):i + 1]
        if low.iloc[i] == fl.min():
            swing_lows.append(low.iloc[i])

    def clustern(punkte):
        # Toleranz-Kette statt starres Preis-Raster (siehe finde_range_box für den
        # Hintergrund - gleicher Bug betraf auch diese Funktion) + Obergrenze für
        # die Gesamtspanne eines Clusters, damit sich die Kette nicht durch eine
        # choppy Phase hindurch "durchhangeln" kann (siehe analysiere_reaktionszonen).
        if not punkte:
            return []
        max_spanne = bucket_usd * 2.5
        punkte_sortiert = sorted(punkte)
        cluster = [[punkte_sortiert[0]]]
        for p in punkte_sortiert[1:]:
            if p - cluster[-1][-1] <= bucket_usd and p - cluster[-1][0] <= max_spanne:
                cluster[-1].append(p)
            else:
                cluster.append([p])
        zonen = [(sum(c) / len(c), len(c)) for c in cluster if len(c) >= min_treffer]
        zonen.sort(key=lambda z: -z[1])
        return zonen[:top_n]

    return {"widerstandszonen": clustern(swing_highs), "supportzonen": clustern(swing_lows)}


def finde_swing_punkte(reihe, fenster=3):
    """Findet lokale Hochs/Tiefs (Swing-Punkte): ein Punkt gilt als Swing-Hoch,
    wenn er innerhalb von `fenster` Perioden vor UND nach ihm das Maximum ist
    (analog Swing-Tief mit Minimum). Gibt zwei Listen von (Zeitpunkt, Wert)
    zurück, chronologisch sortiert."""
    werte = reihe.to_numpy()
    zeiten = reihe.index
    n = len(werte)
    hochs, tiefs = [], []
    for i in range(fenster, n - fenster):
        ausschnitt = werte[i - fenster:i + fenster + 1]
        if werte[i] == ausschnitt.max() and werte[i] > werte[i - fenster] and werte[i] > werte[i + fenster]:
            hochs.append((zeiten[i], float(werte[i])))
        if werte[i] == ausschnitt.min() and werte[i] < werte[i - fenster] and werte[i] < werte[i + fenster]:
            tiefs.append((zeiten[i], float(werte[i])))

    # Einseitiger Rückwärts-Check für die letzten `fenster` Punkte - ohne den können
    # die aktuellsten Tage NIE als Swing-Punkt zählen (ihnen fehlen die künftigen
    # Bestätigungstage), selbst wenn genau dort gerade das extremste Tief/Hoch der
    # ganzen Reihe liegt. Folge: die Kanal-Hüllkurve (siehe finde_trendkanal) konnte
    # den aktuellen Rand gar nicht erreichen, obwohl sie ihn erreichen sollte - genau
    # das wurde bemängelt ("untere Linie müsste unter dem letzten Tief liegen").
    for i in range(max(fenster, n - fenster), n):
        ausschnitt = werte[max(0, i - fenster):i + 1]
        if werte[i] == ausschnitt.max():
            hochs.append((zeiten[i], float(werte[i])))
        if werte[i] == ausschnitt.min():
            tiefs.append((zeiten[i], float(werte[i])))
    return hochs, tiefs


def finde_trendkanal(intraday_reihe, fenster=3, min_punkte=2, flach_schwelle_pct=0.3):
    """Sucht eine Kanal- oder Dreiecksformation: fittet je eine Linie durch die
    Swing-Hochs (Widerstandsseite) und die Swing-Tiefs (Supportseite) und
    klassifiziert die Formation anhand der beiden Steigungen (auf Prozent des
    aktuellen Kurses normiert, damit die Schwellen unabhängig vom Kursniveau
    funktionieren). Gibt None zurück, wenn nicht auf JEDER Seite mindestens
    min_punkte Swing-Punkte gefunden wurden - dann greift im Chart der
    Fallback auf die einfache Einzel-Trendlinie."""
    hochs, tiefs = finde_swing_punkte(intraday_reihe["Close"], fenster)
    if len(hochs) < min_punkte or len(tiefs) < min_punkte:
        return None

    x_hochs = mdates.date2num([t for t, _ in hochs])
    y_hochs = np.array([v for _, v in hochs])
    x_tiefs = mdates.date2num([t for t, _ in tiefs])
    y_tiefs = np.array([v for _, v in tiefs])

    # Neuere Swing-Punkte stärker gewichten als ältere (linear von 1 auf 3): eine
    # unGEWICHTETE Regression durch alle Swing-Punkte lässt sich von einem einzelnen
    # älteren, mittig liegenden Punkt zu stark dominieren - beobachtet an einem Fall,
    # in dem ein Tief aus der Mitte der Teilreihe (nicht das aktuellste) die Steigung
    # bestimmte und die Linie dadurch am rechten Rand weit unter die tatsächlich
    # aktuellen Tiefs zog ("Linie müsste unter dem LETZTEN Tief liegen, nicht
    # irgendwo weit darunter"). Mit Gewichtung zählt die jüngere Marktstruktur mehr.
    gewichte_hochs = np.linspace(1, 3, len(x_hochs)) if len(x_hochs) > 2 else None
    gewichte_tiefs = np.linspace(1, 3, len(x_tiefs)) if len(x_tiefs) > 2 else None
    steigung_oben, achse_oben = np.polyfit(x_hochs, y_hochs, 1, w=gewichte_hochs)
    steigung_unten, achse_unten = np.polyfit(x_tiefs, y_tiefs, 1, w=gewichte_tiefs)

    # WICHTIG: die beiden Linien danach zu einer echten HÜLLKURVE verschieben, statt
    # sie als reine Ausgleichsgerade (Regression) stehen zu lassen. Eine Regression
    # minimiert nur die quadrierte Abweichung ALLER Punkte und kann dadurch locker
    # UNTER einzelnen Hochs (bzw. ÜBER einzelnen Tiefs) verlaufen - genau das wurde
    # bemängelt ("die obere Linie liegt unter den Hochs"). Ein Trader zeichnet eine
    # Widerstandslinie aber so, dass sie die Hochs oben begrenzt (mind. den höchsten
    # Punkt berührt), keine Ausgleichsgerade mittendurch. Deshalb: Steigung aus der
    # Regression übernehmen (bestimmt weiterhin Trendrichtung/Formation), aber die
    # Linie parallel so weit verschieben, bis sie den extremsten Punkt berührt.
    achse_oben += float(np.max(y_hochs - (steigung_oben * x_hochs + achse_oben)))
    achse_unten += float(np.min(y_tiefs - (steigung_unten * x_tiefs + achse_unten)))

    referenz_preis = float(intraday_reihe["Close"].iloc[-1])
    tage_gesamt = (intraday_reihe.index[-1] - intraday_reihe.index[0]).total_seconds() / 86400
    if tage_gesamt <= 0 or referenz_preis <= 0:
        return None
    delta_oben_pct = (steigung_oben * tage_gesamt) / referenz_preis * 100
    delta_unten_pct = (steigung_unten * tage_gesamt) / referenz_preis * 100

    oben_flach = abs(delta_oben_pct) < flach_schwelle_pct
    unten_flach = abs(delta_unten_pct) < flach_schwelle_pct
    oben_steigt = delta_oben_pct >= flach_schwelle_pct
    unten_steigt = delta_unten_pct >= flach_schwelle_pct
    oben_faellt = delta_oben_pct <= -flach_schwelle_pct
    unten_faellt = delta_unten_pct <= -flach_schwelle_pct

    if oben_flach and unten_steigt:
        formation = "Aufsteigendes Dreieck"
    elif unten_flach and oben_faellt:
        formation = "Absteigendes Dreieck"
    elif oben_flach and unten_flach:
        formation = "Seitwärtsrange"
    elif oben_faellt and unten_steigt:
        formation = "Symmetrisches Dreieck"
    elif oben_steigt and unten_steigt:
        formation = "Aufwärtskanal"
    elif oben_faellt and unten_faellt:
        formation = "Abwärtskanal"
    elif oben_steigt and unten_faellt:
        formation = "Erweiternde Formation (Keil)"
    else:
        formation = "Keine eindeutige Formation"

    return {
        "obere_linie": (steigung_oben, achse_oben),
        "untere_linie": (steigung_unten, achse_unten),
        "formation": formation,
        "anzahl_hochs": len(hochs),
        "anzahl_tiefs": len(tiefs),
    }


def kanal_seit_wendepunkt(reihe, fenster=None, min_punkte=3, min_anteil=0.15, max_anteil=0.85):
    """Ergänzt die übergeordnete Kanal-/Formationserkennung (die immer über den
    GESAMTEN Chart-Zeitraum läuft) um eine zweite, kürzere Berechnung nur für die
    Zeit SEIT dem letzten großen Hoch/Tief. Grund: ein Kanal über den gesamten
    Zeitraum wird von einer langen vorangegangenen Bewegung (z.B. einem monatelangen
    Aufwärtstrend) dominiert und bildet eine seitdem einsetzende, steilere Gegen-
    bewegung nur gedämpft/verzögert ab - beobachtet an einem Fall, in dem der
    Gesamtkanal einen 'Abwärtskanal' zeigte, der viel flacher war als die tatsächliche,
    steile Bewegung seit dem Hoch.
    Prüft BEIDE globalen Extrempunkte (höchstes Hoch UND tiefstes Tief) als
    möglichen Wendepunkt - nicht nur den jüngeren der beiden: ein ganz frisches
    Tief von vor wenigen Tagen liefert z.B. eine viel zu kurze, statistisch nicht
    belastbare Teilreihe, obwohl das eigentlich relevante Hoch (der Start der
    aktuellen Abwärtsbewegung) schon deutlich länger zurückliegt und eine sinnvolle
    Länge ergäbe. Von den Kandidaten, deren Teilreihen-Länge zwischen min_anteil
    und max_anteil der Gesamtlänge liegt, wird der mit dem KLEINEREN Anteil gewählt
    (die fokussiertere, aktuellere Formation) - zu kurz ist nicht belastbar, zu lang
    (nahe der Gesamtlänge) wäre nur eine Wiederholung des Gesamtkanals.
    Gibt None zurück, wenn kein Kandidat passt, sonst ein Dict mit 'start'
    (Zeitpunkt des Wendepunkts), 'reihe' (Teilreihe ab dort), 'typ' ('kanal' oder
    'trend') und 'daten' (Kanal-Dict bzw. (steigung, achsenabschnitt))."""
    if len(reihe) < 20:
        return None
    # WICHTIG: Wendepunkt anhand des SCHLUSSKURSES suchen, nicht anhand von High/Low
    # (Tagesdochten) - der Chart zeichnet die Close-Linie, nicht High/Low. Ein
    # einzelner Docht-Ausreißer (z.B. ein kurzes Hoch, das im Schlusskurs gar nicht
    # nachvollziehbar ist) würde sonst als "der" Wendepunkt gewählt und die Zusatz-
    # linie an einem Punkt starten lassen, den man in der sichtbaren Kurslinie gar
    # nicht als Hoch/Tief erkennt - beobachtet an einem Fall, in dem ein Docht-Hoch
    # vom 14.04. (High 4.252) den optisch klar erkennbaren Schlusskurs-Peak vom
    # 05.05. (Close 4.243, dort aber nur High 4.248) als Wendepunkt verdrängte.
    kandidaten_zeiten = sorted({reihe["Close"].idxmax(), reihe["Close"].idxmin()})
    gueltige = []
    for wendepunkt in kandidaten_zeiten:
        sub = reihe.loc[wendepunkt:]
        anteil = len(sub) / len(reihe)
        if min_anteil <= anteil <= max_anteil:
            gueltige.append((anteil, wendepunkt, sub))
    if not gueltige:
        return None
    _, wendepunkt, sub = min(gueltige, key=lambda g: g[0])

    eigenes_fenster = fenster or max(2, len(sub) // 12)
    kanal = finde_trendkanal(sub, fenster=eigenes_fenster, min_punkte=min_punkte)
    if kanal is not None:
        return {"start": wendepunkt, "reihe": sub, "typ": "kanal", "daten": kanal}

    x_num = mdates.date2num(sub.index)
    steigung, achsenabschnitt = np.polyfit(x_num, sub["Close"].values, 1)
    return {"start": wendepunkt, "reihe": sub, "typ": "trend", "daten": (steigung, achsenabschnitt)}


def zeichne_kanal_seit_wendepunkt(ax, reihe, rechte_labels, farbe="#f0d060", zeitformat="%d.%m."):
    """Zeichnet das Ergebnis von kanal_seit_wendepunkt() (falls vorhanden) als
    gestrichelte, farblich abgesetzte Zusatzlinie(n) und reiht das Label in die
    übergebene rechte_labels-Liste ein (Spalte A: aktueller Zustand)."""
    info = kanal_seit_wendepunkt(reihe)
    if info is None:
        return
    sub = info["reihe"]
    x_num_rand = mdates.date2num([sub.index[0], sub.index[-1]])
    praefix = f"Seit {sub.index[0].strftime(zeitformat)}: "
    if info["typ"] == "kanal":
        k = info["daten"]
        steigung_oben, achse_oben = k["obere_linie"]
        steigung_unten, achse_unten = k["untere_linie"]
        y_oben = steigung_oben * x_num_rand + achse_oben
        y_unten = steigung_unten * x_num_rand + achse_unten
        ax.plot(sub.index[[0, -1]], y_oben, color=farbe, linewidth=1.4, linestyle="--", alpha=0.9, zorder=6)
        ax.plot(sub.index[[0, -1]], y_unten, color=farbe, linewidth=1.4, linestyle="--", alpha=0.9, zorder=6)
        label_y = max(y_oben[-1], y_unten[-1])
        label_text = f"{praefix}{k['formation']}"
    else:
        steigung, achsenabschnitt = info["daten"]
        y = steigung * x_num_rand + achsenabschnitt
        ax.plot(sub.index[[0, -1]], y, color=farbe, linewidth=1.4, linestyle="--", alpha=0.9, zorder=6)
        label_y = y[-1]
        richtung = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
        label_text = f"{praefix}{richtung}"
    rechte_labels.append({"y": label_y, "text": f"  {label_text}", "color": farbe,
                           "fontsize": 8.5, "fontweight": "bold", "style": "italic"})


def baue_chart(intraday_reihe, pivots, strukturzonen=None, range_ausbruch_status=None, pfad="chart.png"):
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    preise = intraday_reihe["Close"]
    ax.plot(intraday_reihe.index, preise, color="#e8b95c", linewidth=1.6)

    # Zwei rechte Spalten wie bei Tages-/6M-Chart: Spalte A (nah am Chartende) für
    # den aktuellen Zustand/Trade, Spalte B (weiter rechts versetzt) für strukturelle
    # Zonen - siehe platziere_labels_kollisionsfrei.
    rechte_labels = []      # Spalte A: Kanal/Zusatzkanal/Tageshoch-Tief/RA-Einstieg-Stop-TP
    ferne_labels = []       # Spalte B: Widerstand/Support/Struktur/Umkehrzonen
    gesamtspanne = intraday_reihe.index[-1] - intraday_reihe.index[0]
    # Größerer Abstand als bei Tages-/6M-Chart (dort 0.30): die AKTUELL-Label hier
    # enthalten oft lange Zeitstempel-Texte (z.B. "Seit 12.08. 15:00: Aufwärtstrend"),
    # die bei der kurzen Intraday-Zeitspanne sonst optisch bis in die STRUKTUR-Spalte
    # hineinragen und deren Label überlappen, obwohl beide korrekt an unterschiedlichen
    # X-Positionen verankert sind - das lange Textlabel selbst reicht einfach zu weit.
    x_spalte_b = intraday_reihe.index[-1] + gesamtspanne * 0.55

    # Trendkanal: zwei Linien durch Swing-Hochs/-Tiefs, klassifiziert als Kanal-
    # oder Dreieck-Formation (siehe finde_trendkanal). Nur wenn genug Swing-
    # Punkte für beide Linien gefunden wurden - sonst Fallback auf die
    # einfache Einzel-Trendlinie (lineare Regression über die letzte Hälfte).
    kanal = finde_trendkanal(intraday_reihe, fenster=INTRADAY_KANAL_FENSTER, min_punkte=INTRADAY_KANAL_MIN_PUNKTE)
    kanal_werte_fuer_achse = []
    if kanal is not None:
        x_num_rand = mdates.date2num([intraday_reihe.index[0], intraday_reihe.index[-1]])
        steigung_oben, achse_oben = kanal["obere_linie"]
        steigung_unten, achse_unten = kanal["untere_linie"]
        y_oben_linie = steigung_oben * x_num_rand + achse_oben
        y_unten_linie = steigung_unten * x_num_rand + achse_unten
        kanal_werte_fuer_achse = list(y_oben_linie) + list(y_unten_linie)

        ax.plot(intraday_reihe.index[[0, -1]], y_oben_linie, color="#d9534f", linewidth=1.6,
                 linestyle="-", alpha=0.85, zorder=5)
        ax.plot(intraday_reihe.index[[0, -1]], y_unten_linie, color="#5cb85c", linewidth=1.6,
                 linestyle="-", alpha=0.85, zorder=5)
        rechte_labels.append({"y": max(y_oben_linie[-1], y_unten_linie[-1]), "text": f"  {kanal['formation']}",
                               "color": "#e8b95c", "fontsize": 10, "fontweight": "bold"})
    else:
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
        kanal_werte_fuer_achse += [trend_werte[0], trend_werte[-1]]
        rechte_labels.append({"y": trend_werte[-1], "text": f"  {trend_label}", "color": trend_farbe,
                               "fontsize": 10, "fontweight": "bold"})

    # Zusatzkanal "seit dem letzten großen Hoch/Tief" (siehe kanal_seit_wendepunkt) -
    # dessen Linien vorab einmal berechnen (nicht zeichnen), um ihre Werte in die
    # Achsen-Erweiterung mit einzubeziehen - baue_chart setzt die Y-Achse weiter
    # unten manuell fest (anders als Tages-/6M-Chart, die sich auf Matplotlibs
    # Autoscale verlassen), ohne diesen Schritt würde der Zusatzkanal ggf. abgeschnitten.
    zusatzkanal_info = kanal_seit_wendepunkt(intraday_reihe) if len(intraday_reihe) >= INTRADAY_ZUSATZKANAL_MIN_LAENGE else None
    if zusatzkanal_info is not None:
        sub = zusatzkanal_info["reihe"]
        x_num_rand_sub = mdates.date2num([sub.index[0], sub.index[-1]])
        if zusatzkanal_info["typ"] == "kanal":
            k = zusatzkanal_info["daten"]
            so, ao = k["obere_linie"]; su, au = k["untere_linie"]
            kanal_werte_fuer_achse += list(so * x_num_rand_sub + ao) + list(su * x_num_rand_sub + au)
        else:
            steigung, achsenabschnitt = zusatzkanal_info["daten"]
            kanal_werte_fuer_achse += list(steigung * x_num_rand_sub + achsenabschnitt)

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
    range_box = finde_range_box(intraday_reihe, fenster=INTRADAY_RANGE_FENSTER,
                                  bucket_usd=INTRADAY_RANGE_BUCKET_USD, min_treffer=2)
    box_bereich = None
    if range_box:
        start_zeit, end_zeit, tief, hoch = range_box
        ueberschneidet_sich = any(tief <= p <= hoch for p in alle_umkehr_preise)
        if ueberschneidet_sich:
            box_bereich = (tief, hoch)
            x_start = mdates.date2num(start_zeit)
            x_end = mdates.date2num(end_zeit)
            referenz_spanne = float(intraday_reihe["High"].max() - intraday_reihe["Low"].min())
            hoch_sichtbar = zeichne_range_box(ax, x_start, x_end, tief, hoch, referenz_spanne)
            # Nah am rechten Rand: Label in Spalte B einreihen statt separat zu zeichnen
            # (sonst Kollisionsrisiko mit den anderen rechten Labels - siehe gleiche
            # Logik im Tages-/6M-Chart).
            if (intraday_reihe.index[-1] - end_zeit) < gesamtspanne * 0.15:
                ferne_labels.append({"y": hoch_sichtbar, "text": "Range", "color": "#e8e0c8",
                                      "fontsize": 8.5, "style": "italic"})
            else:
                ax.text(start_zeit, hoch_sichtbar, "Range  ", color="#e8e0c8", fontsize=8.5,
                         style="italic", va="bottom", ha="right")

    # Basis-Range: Kursbereich + Puffer
    puffer = (preise.max() - preise.min()) * 0.15
    y_unten = preise.min() - puffer
    y_oben = preise.max() + puffer

    # Trendkanal-Linien können am rechten/linken Rand leicht über die reine
    # Kursspanne hinaus extrapolieren (Geradengleichung über den vollen
    # Zeitraum) - Achse bei Bedarf mitziehen, damit nichts abgeschnitten wird.
    for wert in kanal_werte_fuer_achse:
        y_oben = max(y_oben, wert + puffer * 0.2)
        y_unten = min(y_unten, wert - puffer * 0.2)

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
        ferne_labels.append({"y": naechster_r, "text": f"Widerstand {naechster_r:,.0f}".replace(",", "."),
                              "color": "#e8887a", "fontsize": 9.5, "fontweight": "bold"})
    elif naechster_r_typ == "struktur":
        ax.axhline(naechster_r, color="#b5654f", linewidth=1.3, linestyle=":", alpha=0.6)
        ferne_labels.append({"y": naechster_r, "text": f"Struktur-Widerstand {naechster_r:,.0f}".replace(",", "."),
                              "color": "#e8887a", "fontsize": 8.5, "style": "italic"})

    if naechster_s_typ == "pivot":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
        ferne_labels.append({"y": naechster_s, "text": f"Support {naechster_s:,.0f}".replace(",", "."),
                              "color": "#9fcf8f", "fontsize": 9.5, "fontweight": "bold"})
    elif naechster_s_typ == "struktur":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.3, linestyle=":", alpha=0.6)
        ferne_labels.append({"y": naechster_s, "text": f"Struktur-Support {naechster_s:,.0f}".replace(",", "."),
                              "color": "#9fcf8f", "fontsize": 8.5, "style": "italic"})

    # Pivot- und Struktur-Level, die zufällig auch noch in die (dadurch minimal
    # erweiterte) Achse passen, zusätzlich einzeichnen - aber nichts zieht die
    # Achse weiter auf als die eine oben ermittelte nächste Marke je Richtung.
    for r in pivots["r"]:
        if r != naechster_r and y_unten <= r <= y_oben:
            ax.axhline(r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
            ferne_labels.append({"y": r, "text": f"Widerstand {r:,.0f}".replace(",", "."),
                                  "color": "#e8887a", "fontsize": 9.5, "fontweight": "bold"})
    for s in pivots["s"]:
        if s != naechster_s and y_unten <= s <= y_oben:
            ax.axhline(s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
            ferne_labels.append({"y": s, "text": f"Support {s:,.0f}".replace(",", "."),
                                  "color": "#9fcf8f", "fontsize": 9.5, "fontweight": "bold"})

    # Tatsächliches Intraday-Hoch/-Tief zusätzlich als schlichte Referenzlinien -
    # ergänzt die rechnerischen Pivot-Level um die real erreichten Extrempunkte.
    # Spalte A (aktueller Zustand), konsistent mit Einstieg/Stop im Tageschart.
    intraday_hoch = preise.max()
    intraday_tief = preise.min()
    ax.axhline(intraday_hoch, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    rechte_labels.append({"y": intraday_hoch, "text": "  Tageshoch", "color": "#c9c2b0", "fontsize": 8.5})
    ax.axhline(intraday_tief, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    rechte_labels.append({"y": intraday_tief, "text": "  Tagestief", "color": "#c9c2b0", "fontsize": 8.5})

    # Umkehrzonen zeichnen: sichtbares 15-USD-Band mit klarer Mittellinie.
    # Die Umkehrzonen-Erkennung selbst bleibt unverändert. Der bestätigte
    # mittlere Preis ist die Mittellinie; das Band liegt exakt +/- 7,5 USD darum.
    def in_box(p):
        return box_bereich is not None and box_bereich[0] <= p <= box_bereich[1]

    BUCKET_UMKEHR_USD = 15.0
    HALBES_BUCKET = BUCKET_UMKEHR_USD / 2.0

    def zeichne_umkehrzone(preis, treffer):
        zone_unten = preis - HALBES_BUCKET
        zone_oben = preis + HALBES_BUCKET
        ax.axhspan(zone_unten, zone_oben, color="#6fa8dc", alpha=0.18, zorder=2)
        # Begrenzung des 15-USD-Buckets
        ax.axhline(zone_unten, color="#6fa8dc", linewidth=0.8, linestyle="-", alpha=0.45, zorder=5)
        ax.axhline(zone_oben, color="#6fa8dc", linewidth=0.8, linestyle="-", alpha=0.45, zorder=5)
        # Klare Mittellinie innerhalb des 15-USD-Buckets
        ax.axhline(preis, color="#6fa8dc", linewidth=1.8, linestyle="-", alpha=1.0, zorder=7)
        ferne_labels.append({"y": preis, "text": f"Umkehrzone {preis:,.0f} ({treffer}x)".replace(",", "."),
                              "color": "#6fa8dc", "fontsize": 7.5})

    for preis, treffer in umkehrzonen["widerstandszonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            zeichne_umkehrzone(preis, treffer)

    for preis, treffer in umkehrzonen["supportzonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            zeichne_umkehrzone(preis, treffer)

    ax.set_ylim(y_unten, y_oben)
    ax.margins(x=0.14)

    # Zusatzkanal jetzt tatsächlich zeichnen (Werte wurden oben schon für die
    # Achsen-Erweiterung berücksichtigt) - Label landet automatisch in Spalte A.
    if len(intraday_reihe) >= INTRADAY_ZUSATZKANAL_MIN_LAENGE:
        zeichne_kanal_seit_wendepunkt(ax, intraday_reihe, rechte_labels, zeitformat="%d.%m. %H:%M")

    # Range-Ausbruch-Signal (1h): Einstieg/Stop/TP1/TP2 einzeichnen, falls offen -
    # "RA-"-Präfix in der Beschriftung, damit es nicht mit den Pivot-Widerstand/
    # Support-Linien verwechselt wird, die dieselbe Farbpalette nutzen. Spalte A
    # (aktueller Zustand/Trade), konsistent mit Einstieg/Stop im Tageschart.
    if range_ausbruch_status and range_ausbruch_status.get("status") == "offen":
        ra = range_ausbruch_status
        ax.axhline(ra["einstieg"], color="#c9c2b0", linewidth=1.0, linestyle=":", alpha=0.8)
        rechte_labels.append({"y": ra["einstieg"], "text": "  RA-Einstieg", "color": "#c9c2b0", "fontsize": 8})
        ax.axhline(ra["stop"], color="#d9534f", linewidth=1.2, linestyle="--", alpha=0.85)
        rechte_labels.append({"y": ra["stop"], "text": f"  RA-Stop {ra['stop']:,.0f}".replace(",", "."),
                               "color": "#e8887a", "fontsize": 8, "fontweight": "bold"})
        ax.axhline(ra["tp1"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.7)
        rechte_labels.append({"y": ra["tp1"], "text": f"  RA-TP1 {ra['tp1']:,.0f}".replace(",", "."),
                               "color": "#9fcf8f", "fontsize": 7.5})
        ax.axhline(ra["tp2"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.5)
        rechte_labels.append({"y": ra["tp2"], "text": f"  RA-TP2 {ra['tp2']:,.0f}".replace(",", "."),
                               "color": "#9fcf8f", "fontsize": 7.5})

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
    # Erst jetzt, nachdem die endgültigen Achsengrenzen feststehen, die gesammelten
    # Label kollisionsfrei setzen (siehe platziere_labels_kollisionsfrei) - Spalte A
    # (aktueller Zustand/Trade) nah am Chart, Spalte B (Widerstand/Support/Umkehr-
    # zonen) versetzt weiter rechts, mit kleiner Kopfzeile zur Orientierung.
    if rechte_labels:
        ax.text(intraday_reihe.index[-1], ax.get_ylim()[1], "AKTUELL", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    if ferne_labels:
        ax.text(x_spalte_b, ax.get_ylim()[1], "STRUKTUR", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    platziere_labels_kollisionsfrei(ax, intraday_reihe.index[-1], rechte_labels, ha="left")
    platziere_labels_kollisionsfrei(ax, x_spalte_b, ferne_labels, ha="left")
    fig.savefig(pfad, facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return pfad


def zonen_naechste_filter(zonen, referenz_preis, min_abstand_usd, top_n):
    """Zwei Nachbesserungen an den rohen Zonen-Clustern aus analysiere_reaktionszonen():
    1) Zonen, die näher als min_abstand_usd beieinander liegen, werden zu einer
       Zone zusammengelegt (treffer-gewichteter Mittelpreis, Trefferzahl = Maximum,
       gleiche Logik wie in kombiniere_zonen - sonst zeigt der Chart zwei fast
       identische Linien mit eigenen Labels übereinander).
    2) Statt der treffer-stärksten Zonen werden die top_n Zonen NÄCHSTEN zum
       aktuellen Kurs behalten - relevanter für die aktuelle Lage als weit
       entfernte, aber oft bestätigte alte Level, und reduziert nebenbei die
       Label-Dichte dort, wo der Kurs gerade nicht steht."""
    def bearbeite(liste):
        if not liste:
            return []
        liste = sorted(liste, key=lambda z: z[0])
        zusammengefasst = []
        for preis, treffer in liste:
            if zusammengefasst and preis - zusammengefasst[-1][0] < min_abstand_usd:
                alter_preis, alter_treffer = zusammengefasst[-1]
                neuer_preis = (alter_preis * alter_treffer + preis * treffer) / (alter_treffer + treffer)
                zusammengefasst[-1] = (neuer_preis, max(alter_treffer, treffer))
            else:
                zusammengefasst.append((preis, treffer))
        zusammengefasst.sort(key=lambda z: abs(z[0] - referenz_preis))
        return zusammengefasst[:top_n]

    return {
        "widerstandszonen": bearbeite(zonen.get("widerstandszonen", [])),
        "supportzonen": bearbeite(zonen.get("supportzonen", [])),
    }


def platziere_labels_kollisionsfrei(ax, x_pos, eintraege, ha="left", min_abstand_px=15):
    """Platziert mehrere Text-Label auf derselben X-Position (z.B. rechter oder
    linker Chartrand) so, dass sie sich nicht überlappen: nach dem eigentlichen
    Y-Wert (Preis) sortiert, bei zu geringem Pixelabstand wird das jeweils höher
    liegende Label so weit nach oben verschoben, bis der Mindestabstand
    eingehalten ist - Reihenfolge bleibt erhalten, nur der Abstand wird korrigiert.
    Der nötige Abstand skaliert dabei mit der Schriftgröße der beiden beteiligten
    Label (größere/fette Label brauchen mehr Platz als die kleinen Struktur-Zonen-
    Label) - min_abstand_px ist nur die Untergrenze für die kleinsten Label.
    Arbeitet in Pixelkoordinaten (nicht Preis-USD), damit der Mindestabstand
    unabhängig vom Kursniveau und der Chart-Skalierung funktioniert. Muss
    aufgerufen werden, NACHDEM die endgültigen Achsengrenzen feststehen (nach
    ax.margins()/tight_layout()), sonst stimmt die Pixel-Umrechnung nicht.
    eintraege: Liste von Dicts mit mind. 'y' (Preis) und 'text', optional 'color',
    'fontsize', 'fontweight', 'style'."""
    if not eintraege:
        return
    eintraege = sorted(eintraege, key=lambda e: e["y"])
    y_px = [ax.transData.transform((0, e["y"]))[1] for e in eintraege]
    for i in range(1, len(y_px)):
        noetiger_abstand = max(min_abstand_px,
                                 (eintraege[i].get("fontsize", 8.5) + eintraege[i - 1].get("fontsize", 8.5)) * 1.15)
        if y_px[i] - y_px[i - 1] < noetiger_abstand:
            y_px[i] = y_px[i - 1] + noetiger_abstand
    inv = ax.transData.inverted()
    for e, ypx in zip(eintraege, y_px):
        y_data = inv.transform((0, ypx))[1]
        ax.text(x_pos, y_data, e["text"], color=e.get("color", "#ece6d9"),
                 fontsize=e.get("fontsize", 8.5), fontweight=e.get("fontweight", "normal"),
                 style=e.get("style", "normal"), va="center", ha=ha, zorder=e.get("zorder", 6))


def zeichne_range_box(ax, x_start_num, x_end_num, tief, hoch, referenz_spanne, min_anteil=0.012,
                        farbe="#e8e0c8", zorder=3):
    """Zeichnet eine Range-Box mit erzwungener Mindesthöhe (min_anteil der sichtbaren
    Kursspanne) UND einer schwachen Flächenfüllung. Ohne beides kann eine sehr enge
    Range (Support/Widerstand nur wenige USD auseinander) bei der Chart-Skalierung
    als reine Umriss-Linie praktisch unsichtbar werden, während das zugehörige
    'Range'-Textlabel daneben trotzdem normal groß und sichtbar bleibt - wirkt dann
    wie ein Label ohne zugehörige Box. Gibt den (ggf. angepassten) oberen Rand zurück,
    damit das Textlabel an der tatsächlich gezeichneten Boxkante andockt."""
    hoehe = hoch - tief
    mindesthoehe = referenz_spanne * min_anteil
    if hoehe < mindesthoehe:
        mitte = (hoch + tief) / 2
        tief = mitte - mindesthoehe / 2
        hoch = mitte + mindesthoehe / 2
    breite = x_end_num - x_start_num
    ax.add_patch(Rectangle((x_start_num, tief), breite, hoch - tief,
                             linewidth=0, facecolor=farbe, alpha=0.12, zorder=zorder))
    ax.add_patch(Rectangle((x_start_num, tief), breite, hoch - tief,
                             linewidth=1.3, edgecolor=farbe, facecolor="none", alpha=0.85, zorder=zorder + 1))
    return hoch


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

    # Zwei rechte Spalten statt einer einzigen Liste: Spalte A (nah am Chartende)
    # für den aktuellen Zustand/Trade, Spalte B (weiter rechts versetzt) für die
    # strukturellen Zonen. So bleibt alles auf der rechten Seite, ohne dass eine
    # einzige lange Liste aus Trade-Info und Hintergrund-Zonen durcheinandergerät.
    rechte_labels = []      # Spalte A: Kanal/Trend/Swing-Tief/Einstieg/Stop/TP
    ferne_labels = []       # Spalte B: strukturelle Zonen + ggf. Range am rechten Rand
    gesamtspanne_tage = (daily.index[-1] - daily.index[0]).days or 1
    x_spalte_b = daily.index[-1] + pd.Timedelta(days=int(gesamtspanne_tage * 0.40))

    # NEU: eigener Trendkanal + Formationserkennung (Swing-Hochs/-Tiefs über die
    # gesamte dargestellte Historie, eigene Parameter TAGESCHART_KANAL_*, unabhängig
    # vom Intraday-Chart). Läuft zusätzlich zur bestehenden 50T-Trendlinie unten,
    # nicht anstelle davon - beide beantworten unterschiedliche Fragen (Positions-
    # trading-Trendfilter vs. sichtbare Kanal-/Dreiecksstruktur).
    tages_kanal = finde_trendkanal(daily, fenster=TAGESCHART_KANAL_FENSTER, min_punkte=TAGESCHART_KANAL_MIN_PUNKTE)
    if tages_kanal is not None:
        x_num_rand = mdates.date2num([daily.index[0], daily.index[-1]])
        steigung_oben, achse_oben = tages_kanal["obere_linie"]
        steigung_unten, achse_unten = tages_kanal["untere_linie"]
        y_oben_linie = steigung_oben * x_num_rand + achse_oben
        y_unten_linie = steigung_unten * x_num_rand + achse_unten
        ax.plot(daily.index[[0, -1]], y_oben_linie, color="#d9534f", linewidth=1.3,
                 linestyle="-", alpha=0.75, zorder=4)
        ax.plot(daily.index[[0, -1]], y_unten_linie, color="#5cb85c", linewidth=1.3,
                 linestyle="-", alpha=0.75, zorder=4)
        rechte_labels.append({"y": max(y_oben_linie[-1], y_unten_linie[-1]), "text": f"  {tages_kanal['formation']}",
                               "color": "#e8b95c", "fontsize": 9.5, "fontweight": "bold"})

    # NEU: zusätzlicher, kürzerer Kanal seit dem letzten großen Hoch/Tief - siehe
    # kanal_seit_wendepunkt() für den Hintergrund (Gesamtkanal kann eine seither
    # eingesetzte, steilere Gegenbewegung nur gedämpft abbilden).
    zeichne_kanal_seit_wendepunkt(ax, daily, rechte_labels)

    # NEU: Range-Boxen (mehrfach berührte Support-/Widerstandslevel innerhalb eines
    # Zeitabschnitts), eigene Parameter TAGESCHART_RANGE_*.
    referenz_spanne = float(daily["High"].max() - daily["Low"].min())
    tages_range_boxen = finde_range_boxen(daily, fenster=TAGESCHART_RANGE_FENSTER,
                                            bucket_usd=TAGESCHART_RANGE_BUCKET_USD,
                                            min_treffer=2, segmente=TAGESCHART_RANGE_SEGMENTE)
    for start_zeit, end_zeit, tief, hoch in tages_range_boxen:
        x_start = mdates.date2num(start_zeit)
        x_end = mdates.date2num(end_zeit)
        hoch_sichtbar = zeichne_range_box(ax, x_start, x_end, tief, hoch, referenz_spanne)
        # Liegt die Box nah am aktuellen (rechten) Rand, landet ihr Label sonst genau
        # in Spalte A und kann mit Kanal/Trend/Swing-Tief kollidieren, ohne dass das
        # Kollisionssystem davon weiß. Dann in Spalte B einreihen (strukturell, passt
        # dort ohnehin besser hin) statt separat direkt an der Box zu zeichnen - bei
        # Boxen weiter links bleibt das Label wie bisher direkt neben der Box.
        if (daily.index[-1] - end_zeit).days < gesamtspanne_tage * 0.15:
            ferne_labels.append({"y": hoch_sichtbar, "text": "Range", "color": "#e8e0c8",
                                  "fontsize": 8, "style": "italic"})
        else:
            ax.text(end_zeit, hoch_sichtbar, " Range", color="#e8e0c8", fontsize=8, style="italic", va="bottom", ha="left")

    # NEU: eigene strukturelle Support-/Widerstandszonen direkt aus diesem Chart-
    # Zeitraum (nicht die Intraday-Pivot-Level und nicht die 3M/6M/36M-Zonen des
    # 6-Monats-Charts - eine dritte, unabhängige Berechnung auf Tagesbasis).
    # Danach zonen_naechste_filter: nah beieinanderliegende Zonen zusammenlegen und
    # nur die top_n NÄCHSTEN zum aktuellen Kurs behalten (statt der treffer-stärksten).
    tages_zonen_roh = analysiere_reaktionszonen(daily, fenster=TAGESCHART_ZONEN_FENSTER,
                                                  bucket_usd=TAGESCHART_ZONEN_BUCKET_USD,
                                                  min_treffer=TAGESCHART_ZONEN_MIN_TREFFER,
                                                  top_n=TAGESCHART_ZONEN_TOP_N * 3)
    tages_zonen = zonen_naechste_filter(tages_zonen_roh, referenz_preis=float(schluss.iloc[-1]),
                                          min_abstand_usd=TAGESCHART_ZONEN_MIN_ABSTAND_USD,
                                          top_n=TAGESCHART_ZONEN_TOP_N)
    aktueller_kurs_tages = float(schluss.iloc[-1])
    for preis, treffer in tages_zonen["widerstandszonen"]:
        ax.axhline(preis, color="#8a5245", linewidth=0.9, linestyle=":", alpha=0.65, zorder=2)
        bezeichnung = "Support" if preis < aktueller_kurs_tages else "Widerstand"
        ferne_labels.append({"y": preis, "text": f"{bezeichnung} {preis:,.0f} ({treffer}x)".replace(",", "."),
                              "color": "#c98f7f", "fontsize": 7.5})
    for preis, treffer in tages_zonen["supportzonen"]:
        ax.axhline(preis, color="#4f6f47", linewidth=0.9, linestyle=":", alpha=0.65, zorder=2)
        bezeichnung = "Support" if preis < aktueller_kurs_tages else "Widerstand"
        ferne_labels.append({"y": preis, "text": f"{bezeichnung} {preis:,.0f} ({treffer}x)".replace(",", "."),
                              "color": "#9fcf8f", "fontsize": 7.5})

    # 50-Tage-Trend (gleiche Methode wie im Positionstrading-Signal) über den
    # letzten verfügbaren Ausschnitt dieses Charts eingezeichnet. Label rechts,
    # am aktuellen (rechten) Ende der Linie - konsistent mit allen anderen
    # "aktueller Zustand"-Labels, statt mitten im Chart am Linienanfang zu kleben.
    trend_ausschnitt = schluss.iloc[-POSITIONSTRADING_TREND_FENSTER:] if len(schluss) >= POSITIONSTRADING_TREND_FENSTER else schluss
    x_num = mdates.date2num(trend_ausschnitt.index)
    steigung, achsenabschnitt = np.polyfit(x_num, trend_ausschnitt.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    # Eigene, von Kanal (rot/grün) und Zusatzkanal (gelb) klar unterscheidbare Farbe -
    # vorher nutzte diese Linie dieselben rot/grün-Töne wie die Aufwärtskanal-Ränder,
    # wodurch das kurze 50T-Segment wie ein loses, abgerissenes Stück der viel
    # längeren Kanallinie wirkte ("komisch kurz") statt als eigenständige Linie erkennbar zu sein.
    trend_farbe = "#c9a0dc"
    trend_label = "Aufwärtstrend (50T)" if steigung > 0 else "Abwärtstrend (50T)"
    ax.plot(trend_ausschnitt.index, trend_werte, color=trend_farbe, linewidth=1.8, zorder=5)
    rechte_labels.append({"y": trend_werte[-1], "text": f"  {trend_label}", "color": trend_farbe,
                           "fontsize": 9.5, "fontweight": "bold"})

    # Rollierendes 10-Tage-Swing-Tief - dieselbe Referenz, die für Einstieg/Stop genutzt wird.
    swing_tief = daily["Low"].rolling(POSITIONSTRADING_SWING_FENSTER).min().shift(1)
    ax.plot(daily.index, swing_tief, color="#6fa8dc", linewidth=0.9, linestyle=":", alpha=0.7)
    rechte_labels.append({"y": swing_tief.iloc[-1], "text": "  10T-Swing-Tief", "color": "#6fa8dc",
                           "fontsize": 8, "style": "italic"})

    # Falls aktuell eine Position offen ist: Einstieg/Stop/TP1/TP2 einzeichnen.
    # Alles rechts (wie Stop/TP1/TP2), damit die gesamte Trade-Information an einer
    # Stelle steht statt über beide Chartseiten verteilt.
    if status["status"] == "offen":
        ax.axhline(status["einstieg"], color="#c9c2b0", linewidth=1.0, linestyle=":", alpha=0.8)
        rechte_labels.append({"y": status["einstieg"], "text": " Einstieg", "color": "#c9c2b0", "fontsize": 8.5})
        ax.axhline(status["stop"], color="#d9534f", linewidth=1.2, linestyle="--", alpha=0.85)
        rechte_labels.append({"y": status["stop"], "text": f" Stop {status['stop']:,.0f}".replace(",", "."),
                               "color": "#e8887a", "fontsize": 8.5, "fontweight": "bold"})
        ax.axhline(status["tp1"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.7)
        rechte_labels.append({"y": status["tp1"], "text": f" TP1 {status['tp1']:,.0f}".replace(",", "."),
                               "color": "#9fcf8f", "fontsize": 8})
        ax.axhline(status["tp2"], color="#5cb85c", linewidth=1.0, linestyle="--", alpha=0.5)
        rechte_labels.append({"y": status["tp2"], "text": f" TP2 {status['tp2']:,.0f}".replace(",", "."),
                               "color": "#9fcf8f", "fontsize": 8})

    ax.margins(x=0.14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)
    ax.set_title("Gold Spot (XAU/USD) - Tageschart (Positionstrading-Basis)", color="#ece6d9", fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    # Erst jetzt, nachdem die endgültigen Achsengrenzen feststehen, die gesammelten
    # Label kollisionsfrei setzen (siehe platziere_labels_kollisionsfrei) - Spalte A
    # (Trade/aktueller Zustand) nah am Chart, Spalte B (strukturelle Zonen) versetzt
    # weiter rechts, mit kleiner Kopfzeile zur Orientierung.
    if rechte_labels:
        ax.text(daily.index[-1], ax.get_ylim()[1], "AKTUELL", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    if ferne_labels:
        ax.text(x_spalte_b, ax.get_ylim()[1], "STRUKTUR", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    platziere_labels_kollisionsfrei(ax, daily.index[-1], rechte_labels, ha="left")
    platziere_labels_kollisionsfrei(ax, x_spalte_b, ferne_labels, ha="left")
    fig.savefig(pfad, facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return pfad


def berechne_6m_strukturzonen(daily):
    """Berechnet exakt die 6M-Strukturzonen, die im sichtbaren 6M-Chart
    dargestellt werden. Chart und mittelfristige Szenario-Karte greifen damit
    auf dieselbe Datenquelle und dieselben Parameter zurück."""
    if daily is None or len(daily) == 0:
        return {"widerstandszonen": [], "supportzonen": []}

    roh = analysiere_reaktionszonen(
        daily,
        fenster=LANGFRIST_ZONEN_FENSTER,
        bucket_usd=LANGFRIST_ZONEN_BUCKET_USD,
        min_treffer=LANGFRIST_ZONEN_MIN_TREFFER,
        top_n=LANGFRIST_ZONEN_TOP_N * 3,
    )
    return zonen_naechste_filter(
        roh,
        referenz_preis=float(daily["Close"].iloc[-1]),
        min_abstand_usd=LANGFRIST_ZONEN_MIN_ABSTAND_USD,
        top_n=LANGFRIST_ZONEN_TOP_N,
    )


def berechne_mittelfristige_szenarien(struktur_6m_daten, struktur_6m_szenario_zonen, struktur_6m_reaktionszonen):
    """Liefert exakt die bereits verwendeten Ergebnisse der mittelfristigen
    Szenario-Karte. Keine neue Logik: dieselbe 6M-Zonenquelle und dieselben
    3M/6M-Reaktionszonen werden verwendet wie in der Karte selbst."""
    def fmt_mittel(preis):
        return f"{preis:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    mittel_bull = mittel_baer = "keine Zone"
    mittel_ziel_bull = mittel_ziel_baer = None

    if struktur_6m_daten is not None and len(struktur_6m_daten) > 0:
        mittel_kurs = float(struktur_6m_daten["Close"].iloc[-1])

        # Exakt dasselbe 6M-Zonenobjekt verwenden, das der 6M-Chart erhält.
        mittel_6m = struktur_6m_szenario_zonen or {"widerstandszonen": [], "supportzonen": []}

        # Exakt dieselbe Preis-Klassifikation wie im 6M-Chart:
        # preis > aktueller Kurs = Widerstand, preis < aktueller Kurs = Support.
        alle_6m_preise = sorted(set(
            float(x[0])
            for key in ("widerstandszonen", "supportzonen")
            for x in mittel_6m.get(key, []) or []
        ))

        widerstaende_6m = [x for x in alle_6m_preise if x > mittel_kurs]
        supports_6m = [x for x in alle_6m_preise if x < mittel_kurs]

        bull_trigger = min(widerstaende_6m) if widerstaende_6m else None
        bear_trigger = max(supports_6m) if supports_6m else None

        if bull_trigger is not None:
            mittel_bull = fmt_mittel(bull_trigger)
        if bear_trigger is not None:
            mittel_baer = fmt_mittel(bear_trigger)

        # Ziele: nächste kombinierte Reaktionszone jenseits des jeweiligen
        # 6M-Triggers - identisch zur bisherigen Kartenlogik.
        alle_reaktionspreise = sorted(set(
            float(x[0])
            for key in ("widerstandszonen", "supportzonen")
            for x in (struktur_6m_reaktionszonen or {}).get(key, []) or []
        ))

        if bull_trigger is not None:
            ziel = next((x for x in alle_reaktionspreise if x > bull_trigger + 1e-6), None)
            if ziel is not None:
                mittel_ziel_bull = fmt_mittel(ziel)

        if bear_trigger is not None:
            ziel = next((x for x in reversed(alle_reaktionspreise) if x < bear_trigger - 1e-6), None)
            if ziel is not None:
                mittel_ziel_baer = fmt_mittel(ziel)

    return {
        "bull": mittel_bull,
        "ziel_bull": mittel_ziel_bull,
        "neutral": f"{mittel_baer} bis {mittel_bull} USD",
        "baer": mittel_baer,
        "ziel_baer": mittel_ziel_baer,
    }


def baue_langfrist_chart(daily, zonen, pfad="chart_langfrist.png", struktur_zonen=None):
    """6-Monats-Tageschart mit eigenem Trendkanal, Formationserkennung, Range-Boxen
    und strukturellen Zonen (eigene, größere Parameter, siehe LANGFRIST_*), plus
    den bereits berechneten Reaktionszonen als Linien - macht sichtbar, wo die im
    Rückblick-Text genannten strukturellen Zonen herkommen."""
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    schluss = daily["Close"]
    ax.plot(daily.index, schluss, color="#e8b95c", linewidth=1.3)

    rechte_labels = []      # Spalte A: Kanal-/Trendformation (aktueller Zustand)
    ferne_labels = []       # Spalte B: alle strukturellen Zonen (6M-Struktur + bestehende Reaktionszonen)
    gesamtspanne_tage = (daily.index[-1] - daily.index[0]).days or 1
    x_spalte_b = daily.index[-1] + pd.Timedelta(days=int(gesamtspanne_tage * 0.40))

    # Übergeordneter Trendkanal + Formationserkennung - gleiche Methode wie im
    # Tageschart (finde_trendkanal), aber mit größeren Swing-Parametern
    # (LANGFRIST_KANAL_*, eigenständig von TAGESCHART_KANAL_* und vom Intraday-
    # Chart). Fällt auf die einfache lineare Regression zurück, wenn zu wenig
    # Swing-Punkte für eine Kanal-/Dreiecksformation gefunden wurden.
    lang_kanal = finde_trendkanal(daily, fenster=LANGFRIST_KANAL_FENSTER, min_punkte=LANGFRIST_KANAL_MIN_PUNKTE)
    if lang_kanal is not None:
        x_num_rand = mdates.date2num([daily.index[0], daily.index[-1]])
        steigung_oben, achse_oben = lang_kanal["obere_linie"]
        steigung_unten, achse_unten = lang_kanal["untere_linie"]
        y_oben_linie = steigung_oben * x_num_rand + achse_oben
        y_unten_linie = steigung_unten * x_num_rand + achse_unten
        ax.plot(daily.index[[0, -1]], y_oben_linie, color="#d9534f", linewidth=1.6,
                 linestyle="-", alpha=0.85, zorder=5)
        ax.plot(daily.index[[0, -1]], y_unten_linie, color="#5cb85c", linewidth=1.6,
                 linestyle="-", alpha=0.85, zorder=5)
        rechte_labels.append({"y": max(y_oben_linie[-1], y_unten_linie[-1]), "text": f"  {lang_kanal['formation']}",
                               "color": "#e8b95c", "fontsize": 10, "fontweight": "bold"})
    else:
        # Fallback: einfache lineare Regression über den gesamten dargestellten
        # Zeitraum (bisheriges Verhalten, falls die Kanalerkennung mangels
        # Swing-Punkten kein Ergebnis liefert). Label rechts, konsistent mit dem
        # Kanal-Fall oben.
        x_num = mdates.date2num(schluss.index)
        steigung, achsenabschnitt = np.polyfit(x_num, schluss.values, 1)
        trend_werte = steigung * x_num + achsenabschnitt
        trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
        trend_label = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
        ax.plot(schluss.index, trend_werte, color=trend_farbe, linewidth=1.8,
                 linestyle="-", alpha=0.9, zorder=5)
        rechte_labels.append({"y": trend_werte[-1], "text": f"  {trend_label}", "color": trend_farbe,
                               "fontsize": 10, "fontweight": "bold"})

    # NEU: zusätzlicher, kürzerer Kanal seit dem letzten großen Hoch/Tief - siehe
    # kanal_seit_wendepunkt() für den Hintergrund.
    zeichne_kanal_seit_wendepunkt(ax, daily, rechte_labels)

    # Range-Boxen: gleiche Berührungs-basierte Erkennung wie im Tageschart, aber mit
    # größeren Struktur-Parametern (LANGFRIST_RANGE_*) - über 6 Monate kann es
    # mehrere getrennte Ranges auf unterschiedlichen Kursniveaus geben.
    range_boxen = finde_range_boxen(daily, fenster=LANGFRIST_RANGE_FENSTER,
                                      bucket_usd=LANGFRIST_RANGE_BUCKET_USD, min_treffer=2,
                                      segmente=LANGFRIST_RANGE_SEGMENTE)
    referenz_spanne = float(daily["High"].max() - daily["Low"].min())
    for start_zeit, end_zeit, tief, hoch in range_boxen:
        x_start = mdates.date2num(start_zeit)
        x_end = mdates.date2num(end_zeit)
        hoch_sichtbar = zeichne_range_box(ax, x_start, x_end, tief, hoch, referenz_spanne)
        ax.text(end_zeit, hoch_sichtbar, " Range", color="#e8e0c8", fontsize=8.5,
                 style="italic", va="bottom", ha="left")

    # NEU: eigene 6M-strukturelle Support-/Widerstandszonen, direkt aus diesem
    # 6-Monats-Zeitraum berechnet (LANGFRIST_ZONEN_*) - unabhängig von den unten
    # weiterhin gezeigten, bestehenden Reaktionszonen (die aus dem 3M/6M/36M-
    # Zonenvergleich in main() stammen). Andere Linienart, damit beide im Chart
    # unterscheidbar bleiben. zonen_naechste_filter: nah beieinanderliegende Zonen
    # zusammenlegen und nur die top_n NÄCHSTEN zum aktuellen Kurs behalten.
    lang_struktur_zonen = struktur_zonen if struktur_zonen is not None else berechne_6m_strukturzonen(daily)
    aktueller_kurs_lang = float(schluss.iloc[-1])
    for preis, treffer in lang_struktur_zonen["widerstandszonen"]:
        ax.axhline(preis, color="#8a5245", linewidth=1.0, linestyle="-.", alpha=0.6, zorder=2)
        bezeichnung = "6M-Support" if preis < aktueller_kurs_lang else "6M-Widerstand"
        ferne_labels.append({"y": preis, "text": f"{bezeichnung} {preis:,.0f} ({treffer}x)".replace(",", "."),
                              "color": "#c98f7f", "fontsize": 7.5})
    for preis, treffer in lang_struktur_zonen["supportzonen"]:
        ax.axhline(preis, color="#4f6f47", linewidth=1.0, linestyle="-.", alpha=0.6, zorder=2)
        bezeichnung = "6M-Support" if preis < aktueller_kurs_lang else "6M-Widerstand"
        ferne_labels.append({"y": preis, "text": f"{bezeichnung} {preis:,.0f} ({treffer}x)".replace(",", "."),
                              "color": "#9fcf8f", "fontsize": 7.5})

    # Bestehende Reaktionszonen (3M/6M/36M-Vergleich aus main()) - Spalte B, im
    # selben preis-sortierten Block wie die 6M-Struktur-Zonen (beides Zonen-Info).
    if zonen:
        for preis, treffer, fenster in zonen["widerstandszonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#b5654f", linewidth=1.0, linestyle="--", alpha=0.8)
            ferne_labels.append({"y": preis, "text": f"Widerstand {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                                  "color": "#e8887a", "fontsize": 9, "fontweight": "bold"})
        for preis, treffer, fenster in zonen["supportzonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#7fae6f", linewidth=1.0, linestyle="--", alpha=0.8)
            ferne_labels.append({"y": preis, "text": f"Support {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                                  "color": "#9fcf8f", "fontsize": 9, "fontweight": "bold"})

    ax.margins(x=0.14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)
    ax.set_title("Gold Spot (XAU/USD) - 6 Monate, Trendkanal & strukturelle Zonen", color="#ece6d9",
                 fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    # Erst jetzt, nachdem die endgültigen Achsengrenzen feststehen, die gesammelten
    # Label kollisionsfrei setzen (siehe platziere_labels_kollisionsfrei) - Spalte A
    # (Kanal/Trend) nah am Chart, Spalte B (strukturelle Zonen) versetzt weiter
    # rechts, mit kleiner Kopfzeile zur Orientierung.
    if rechte_labels:
        ax.text(daily.index[-1], ax.get_ylim()[1], "AKTUELL", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    if ferne_labels:
        ax.text(x_spalte_b, ax.get_ylim()[1], "STRUKTUR", color="#6b6354", fontsize=7,
                 fontweight="bold", va="bottom", ha="left")
    platziere_labels_kollisionsfrei(ax, daily.index[-1], rechte_labels, ha="left")
    platziere_labels_kollisionsfrei(ax, x_spalte_b, ferne_labels, ha="left")
    fig.savefig(pfad, facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.15)
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


def formatiere_szenarien(szenarien, fmt):
    """fmt: deutsche Zahlenformatierungsfunktion, z.B. lambda n: f'{n:,.2f}'...
    Ampel-Darstellung (🟢/🟡/🔴), funktioniert unverändert in Text- und
    HTML-Version (E-Mail-Clients stellen die Emoji normalerweise dar)."""
    zeilen = []
    if szenarien["naechster_widerstand"] is not None:
        ziel = f" -> Ziel {fmt(szenarien['ziel_bullisch'])} USD" if szenarien["ziel_bullisch"] is not None else ""
        zeilen.append(f"🟢 BULLISCH über {fmt(szenarien['naechster_widerstand'])} USD{ziel}")
    if szenarien["naechster_support"] is not None and szenarien["naechster_widerstand"] is not None:
        zeilen.append(
            f"🟡 NEUTRAL zwischen {fmt(szenarien['naechster_support'])} und "
            f"{fmt(szenarien['naechster_widerstand'])} USD -> abwarten"
        )
    if szenarien["naechster_support"] is not None:
        ziel = f" -> Ziel {fmt(szenarien['ziel_baerisch'])} USD" if szenarien["ziel_baerisch"] is not None else ""
        zeilen.append(f"🔴 BÄRISCH unter {fmt(szenarien['naechster_support'])} USD{ziel}")
    return "\n".join(zeilen)


def formatiere_crv(status, fmt):
    """CRV (Chance-Risiko-Verhältnis) für TP1 und TP2, nur wenn eine Position
    offen ist - nutzt exakt die Einstieg/Stop/TP1/TP2-Werte, die die
    Simulation ohnehin schon berechnet hat (die selbst schon an anderer
    Stelle im Text stehen, hier nur das CRV daraus, keine Wiederholung)."""
    if status.get("status") != "offen":
        return None
    risiko = status["einstieg"] - status["stop"]
    if risiko <= 0:
        return None
    crv1 = (status["tp1"] - status["einstieg"]) / risiko
    crv2 = (status["tp2"] - status["einstieg"]) / risiko
    return f"Ziel1 {crv1:.1f} / Ziel2 {crv2:.1f}"


def formatiere_vorschau(status, fmt):
    """Vorschau auf Stop/Einstieg/TP1/TP2/CRV, wenn AKTUELL keine Position
    offen ist - beantwortet 'wie sähe ein Trade aus, falls das System gleich
    triggert'. Der Stop (bzw. bei Range-Ausbruch auch der Einstieg) ist ein
    exakter, schon jetzt bekannter Wert; bei V1e ist der Einstieg selbst nur
    eine Näherung auf Basis des aktuellen Kurses, weil der echte Trigger
    einen Bounce mit vorher unbekanntem Schlusskurs braucht (siehe
    einstieg_praezise-Flag)."""
    vorschau = status.get("vorschau")
    if not vorschau:
        return None
    risiko = vorschau["hypothetischer_einstieg"] - vorschau["stop"]
    if risiko <= 0:
        return None
    crv1 = (vorschau["tp1"] - vorschau["hypothetischer_einstieg"]) / risiko
    crv2 = (vorschau["tp2"] - vorschau["hypothetischer_einstieg"]) / risiko
    if vorschau.get("einstieg_praezise"):
        einstieg_label = f"{fmt(vorschau['hypothetischer_einstieg'])} USD (exakter künftiger Trigger, kein Näherungswert)"
    else:
        einstieg_label = (
            f"nahe {fmt(vorschau['hypothetischer_einstieg'])} USD (Näherung auf Basis des aktuellen Kurses - "
            f"der tatsächliche künftige Einstieg beim Bounce kann abweichen)"
        )
    zeile = (
        f"Vorschau (kein aktives Signal): Einstieg {einstieg_label}, Stop {fmt(vorschau['stop'])} USD, "
        f"TP1 {fmt(vorschau['tp1'])} USD (CRV {crv1:.1f}), TP2 {fmt(vorschau['tp2'])} USD (CRV {crv2:.1f})"
    )
    if vorschau.get("trend_erfuellt") is False:
        zeile += ". Trendbedingung aktuell NICHT erfüllt - Vorschau daher rein illustrativ, kein gültiges Setup."
        tage = vorschau.get("tage_bis_trendwechsel")
        if tage is not None:
            zeile += (
                f" Bei etwa gleichbleibendem Kurs würde der Trendfilter in ca. {tage} Handelstagen "
                f"'erfüllt' zeigen, weil ältere, stärker fallende Tage aus dem 50-Tage-Fenster rausrutschen"
            )
        else:
            zeile += (
                " Bei gleichbleibendem Kurs bleibt der Trendfilter auch mittelfristig unerfüllt - "
                "dafür müsste der Kurs tatsächlich steigen, reines Abwarten reicht nicht"
            )
        if vorschau.get("trend_schwelle") is not None:
            zeile += (
                f"; alternativ würde ein einzelner Schlusskurs von ca. {fmt(vorschau['trend_schwelle'])} USD "
                f"denselben Effekt sofort auslösen"
            )
        if vorschau.get("sma_aktuell") is not None:
            zeile += f". Zum Vergleich der einfachere 50-Tage-Durchschnitt: {fmt(vorschau['sma_aktuell'])} USD"
        zeile += "."
    return zeile


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

    crv_text = formatiere_crv(status, de_zahl)
    if crv_text:
        kontext += f"\nCRV: {crv_text}"

    vorschau_text = formatiere_vorschau(status, de_zahl)
    if vorschau_text:
        kontext += f"\n{vorschau_text}"

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

    crv_text = formatiere_crv(status, de_zahl)
    if crv_text:
        kontext += f"\nCRV: {crv_text}"

    vorschau_text = formatiere_vorschau(status, de_zahl)
    if vorschau_text:
        kontext += f"\n{vorschau_text}"

    return signal_text + "\n" + kontext



def baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, positionstrading_status, range_ausbruch_status, economic_events_block, intraday_zukunft=None, tages_ma_struktur=None):
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

    szenarien = berechne_szenarien(daten["realtime"], pivots)
    szenarien_text = formatiere_szenarien(szenarien, fmt)

    # Charttechnischer Trigger und tatsächlicher Range-System-Einstieg sind
    # bewusst unterschiedliche Größen: Das Range-System löst erst bei einem
    # bestätigten 1h-Schlusskurs ÜBER der Schwelle aus.
    system_einordnung = ""
    range_vorschau = range_ausbruch_status.get("vorschau") if range_ausbruch_status else None
    if szenarien.get("naechster_widerstand") is not None and range_vorschau:
        system_einordnung = (
            f"System-Einordnung: {fmt(szenarien['naechster_widerstand'])} USD ist der "
            f"charttechnische bullische Trigger. Das Range-Breakout-System benötigt "
            f"einen bestätigten 1h-Schlusskurs darüber; der aktuelle System-Einstieg "
            f"liegt daher bei {fmt(range_vorschau['hypothetischer_einstieg'])} USD "
            f"und kann oberhalb der Trigger-Schwelle liegen."
        )

    text = f"""NEUBER PRECIOUS METALS
MINI DAILY: GOLD
{heute} - Erstellt um {erstellt_zeit} Uhr - Kursdaten Stand {daten_zeit} Uhr
{warnzeile}
{economic_events_block}

TENDENZ
{tendenz_label} ({tendenz_pct:+.2f}%)

SZENARIEN
{szenarien_text}
{system_einordnung}

WIDERSTAENDE (INTRADAY)
{liste(pivots['r'])} USD

UNTERSTUETZUNGEN (INTRADAY)
{liste(pivots['s'])} USD

INTRADAY-ZUKUNFTSANALYSE (1h / 30m / 15m)
{formatiere_intraday_zukunft(intraday_zukunft, fmt)}

REALTIME INDIKATION
{fmt(daten['realtime'])} USD

SCHLUSSKURS (VORTAG)
{fmt(daten['prev_close'])} USD

RUECKBLICK
{formatiere_tages_ma_struktur(tages_ma_struktur, fmt)}

{rueckblick_text}

RANGE-AUSBRUCH-SIGNAL (1h, Halteperiode Stunden bis Tage, gehört zum Intraday-Chart chart.png)
{range_ausbruch_text}
{RANGE_AUSBRUCH_REGELN_TEXT}
Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
{RANGE_AUSBRUCH_BACKTEST_TEXT}

POSITIONSTRADING-SIGNAL (Backtest V1e, Halteperiode Tage bis Wochen, gehört zum Tageschart chart_tages.png)
{positionstrading_text}
{POSITIONSTRADING_REGELN_TEXT}
Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
{positionstrading_status.get('backtest_kennzahlen', '(keine Kennzahlen verfügbar)')}

---
Kein Kauf-/Verkaufssignal - reine charttechnische Orientierung - Datenquelle: Twelve Data (XAU/USD)
"""
    return text


def baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_dateiname, chart_tages_dateiname, positionstrading_status, range_ausbruch_status, economic_events_block, zonen_je_zeitraum, struktur_6m_daten=None, positionstrading_daten=None, struktur_6m_szenario_zonen=None, struktur_6m_reaktionszonen=None, mittelfristige_szenarien=None, intraday_zukunft=None, tages_ma_struktur=None):
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
    economic_events_html = economic_events_block.replace("\n", "<br>")

    def fmt(n):
        return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def level_liste(werte, farbe):
        return "".join(
            f'<span style="display:inline-block;background:#241f16;border-left:3px solid {farbe};'
            f'padding:6px 12px;margin:4px 6px 4px 0;border-radius:2px;font-family:monospace;">'
            f'{v:,.2f}</span>'.replace(",", "X").replace(".", ",").replace("X", ".")
            for v in werte
        )

    szenarien = berechne_szenarien(daten["realtime"], pivots)

    def szenario_zeile(emoji, label, farbe, hintergrund, bedingung, ziel):
        return (
            f'<p style="background:{hintergrund};border-left:3px solid {farbe};padding:8px 14px;'
            f'margin:4px 0;">{emoji} <strong>{label}</strong> {bedingung}{ziel}</p>'
        )

    def szenario_balken(punkt_farbe, label, label_farbe, hintergrund, rand_farbe, bedingung, ziel):
        return (
            f'<tr><td style="background:{hintergrund};border-left:4px solid {rand_farbe};padding:12px 16px;'
            f'border-radius:4px;" bgcolor="{hintergrund}">'
            f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{punkt_farbe};'
            f'margin-right:10px;"></span>'
            f'<strong style="color:{label_farbe};">{label}</strong> '
            f'<span style="color:#ece6d9;">{bedingung}{ziel}</span>'
            f'</td></tr><tr><td style="height:8px;line-height:8px;font-size:1px;">&nbsp;</td></tr>'
        )

    szenarien_html = ""
    if szenarien["naechster_widerstand"] is not None:
        ziel = f" → Ziel {fmt(szenarien['ziel_bullisch'])} USD" if szenarien["ziel_bullisch"] is not None else ""
        szenarien_html += szenario_balken("#5cb85c", "BULLISCH", "#9fe39f", "#132a16", "#3f8f4a",
                                           f"über {fmt(szenarien['naechster_widerstand'])} USD", ziel)
    if szenarien["naechster_support"] is not None and szenarien["naechster_widerstand"] is not None:
        szenarien_html += szenario_balken("#e0b04a", "NEUTRAL", "#f0d495", "#2e2612", "#a67f2e",
                                           f"zwischen {fmt(szenarien['naechster_support'])} und "
                                           f"{fmt(szenarien['naechster_widerstand'])} USD", " → abwarten")
    if szenarien["naechster_support"] is not None:
        ziel = f" → Ziel {fmt(szenarien['ziel_baerisch'])} USD" if szenarien["ziel_baerisch"] is not None else ""
        szenarien_html += szenario_balken("#d9534f", "BÄRISCH", "#f0a49f", "#2e1414", "#a13f3a",
                                           f"unter {fmt(szenarien['naechster_support'])} USD", ziel)


    # Neues Layout: drei gleich breite Zeithorizonte. Alle drei Spalten verwenden
    # exakt dieselbe Kartenstruktur; die kurzfristigen Werte bleiben die bestehende
    # Intraday-Szenario-Logik. Mittel- und Langfristwerte werden direkt aus den
    # Analyseparametern der beiden sichtbaren Struktur-Charts abgeleitet, damit die
    # Szenario-Marken mit den im jeweiligen Chart gezeigten Marken übereinstimmen.

    def szenario_zeilen(bull_marke, bull_ziel, neutral_text, baer_marke, baer_ziel,
                        bull_text="Ausbruch bestätigt", baer_text="Struktur wird schwächer"):
        ziel_bull = f" → Ziel {bull_ziel} USD" if bull_ziel else ""
        ziel_baer = f" → Ziel {baer_ziel} USD" if baer_ziel else ""
        return f"""
        <p style="background:#132a16;border-left:4px solid #3f8f4a;padding:8px 12px;margin:4px 0;">🟢 <strong style="color:#9fe39f;">BULLISCH</strong><br>über {bull_marke} USD{ziel_bull}<br>{bull_text}</p>
        <p style="background:#2e2612;border-left:4px solid #a67f2e;padding:8px 12px;margin:4px 0;">🟡 <strong style="color:#f0d495;">NEUTRAL</strong><br>{neutral_text}<br>abwarten</p>
        <p style="background:#2e1414;border-left:4px solid #a13f3a;padding:8px 12px;margin:4px 0;">🔴 <strong style="color:#f0a49f;">BÄRISCH</strong><br>unter {baer_marke} USD{ziel_baer}<br>{baer_text}</p>
        """

    def fmt_szenario(preis):
        # Deutsche Kursdarstellung: Tausenderpunkt + Dezimalkomma.
        return f"{preis:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def naechste_zonen(zonen, aktueller_kurs):
        if not zonen:
            return [], []
        def preise(items, oberhalb):
            ergebnis = []
            for item in items or []:
                if isinstance(item, (tuple, list)) and item:
                    p = float(item[0])
                    if (oberhalb and p > aktueller_kurs) or ((not oberhalb) and p < aktueller_kurs):
                        ergebnis.append(p)
            return ergebnis
        widerstaende = sorted(preise(zonen.get("widerstandszonen", []), True))
        supports = sorted(preise(zonen.get("supportzonen", []), False), reverse=True)
        return widerstaende, supports

    # Kurzfristig: exakt die bestehende Intraday-Szenario-Logik, aber in derselben
    # Kartenstruktur wie Mittel-/Langfristig.
    kurz_bull = fmt_szenario(szenarien["naechster_widerstand"]) if szenarien.get("naechster_widerstand") is not None else "keine Zone"
    kurz_baer = fmt_szenario(szenarien["naechster_support"]) if szenarien.get("naechster_support") is not None else "keine Zone"
    kurz_ziel_bull = fmt_szenario(szenarien["ziel_bullisch"]) if szenarien.get("ziel_bullisch") is not None else None
    kurz_ziel_baer = fmt_szenario(szenarien["ziel_baerisch"]) if szenarien.get("ziel_baerisch") is not None else None
    kurz_neutral = f"{kurz_baer} bis {kurz_bull} USD"
    kurzfristig_html = szenario_zeilen(kurz_bull, kurz_ziel_bull, kurz_neutral, kurz_baer, kurz_ziel_baer)

    # Mittelfristig: exakt die bereits berechneten Ergebnisse der mittleren
    # Szenario-Karte verwenden. Keine erneute Herleitung innerhalb von baue_html().
    mittelfristige_szenarien = mittelfristige_szenarien or {
        "bull": "keine Zone",
        "ziel_bull": None,
        "neutral": "keine Zone bis keine Zone USD",
        "baer": "keine Zone",
        "ziel_baer": None,
    }
    mittel_bull = mittelfristige_szenarien["bull"]
    mittel_ziel_bull = mittelfristige_szenarien["ziel_bull"]
    mittel_neutral = mittelfristige_szenarien["neutral"]
    mittel_baer = mittelfristige_szenarien["baer"]
    mittel_ziel_baer = mittelfristige_szenarien["ziel_baer"]

    mittelfristig_html = szenario_zeilen(
        mittel_bull, mittel_ziel_bull, mittel_neutral, mittel_baer, mittel_ziel_baer,
        bull_text="6M-Strukturwiderstand überwunden",
        baer_text="6M-Strukturunterstützung gebrochen",
    )

    # Langfristig: dieselbe Tageschart-Zonenberechnung wie im sichtbaren
    # Positionstrading-Tageschart. NICHT die 36M-Reaktionszonen verwenden.
    lang_w = lang_s = []
    if positionstrading_daten is not None and len(positionstrading_daten) > 0:
        lang_roh = analysiere_reaktionszonen(
            positionstrading_daten,
            fenster=TAGESCHART_ZONEN_FENSTER,
            bucket_usd=TAGESCHART_ZONEN_BUCKET_USD,
            min_treffer=TAGESCHART_ZONEN_MIN_TREFFER,
            top_n=TAGESCHART_ZONEN_TOP_N * 3,
        )
        lang_zonen = zonen_naechste_filter(
            lang_roh,
            referenz_preis=float(positionstrading_daten["Close"].iloc[-1]),
            min_abstand_usd=TAGESCHART_ZONEN_MIN_ABSTAND_USD,
            top_n=TAGESCHART_ZONEN_TOP_N,
        )
        lang_w, lang_s = naechste_zonen(lang_zonen, float(positionstrading_daten["Close"].iloc[-1]))

    lang_bull = fmt_szenario(lang_w[0]) if lang_w else "keine Zone"
    lang_baer = fmt_szenario(lang_s[0]) if lang_s else "keine Zone"
    lang_neutral = f"{lang_baer} bis {lang_bull} USD"
    lang_ziel = fmt_szenario(lang_w[1]) if len(lang_w) > 1 else None
    lang_ziel_baer = fmt_szenario(lang_s[1]) if len(lang_s) > 1 else None
    langfristig_html = szenario_zeilen(
        lang_bull, lang_ziel, lang_neutral, lang_baer, lang_ziel_baer,
        bull_text="Positionstrading-Struktur überwunden",
        baer_text="Positionstrading-Unterstützung gebrochen",
    )

    # Alle drei Spalten identisch aufgebaut und mit table-layout:fixed auf exakt
    # 33,333 % gezwungen. Das entspricht dem Raster der drei Statuskarten darüber.
    szenarien_html = f"""
    <tr>
    <td width="33.3333%" valign="top" style="width:33.3333%;padding:0 6px;box-sizing:border-box;">
    <div style="width:100%;box-sizing:border-box;background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:12px;">
    <p style="color:#a89d87;font-size:11px;letter-spacing:1px;text-transform:uppercase;margin:0 0 8px 0;">KURZFRISTIG<br>INTRADAY</p>
    {kurzfristig_html}
    </div></td>
    <td width="33.3333%" valign="top" style="width:33.3333%;padding:0 6px;box-sizing:border-box;">
    <div style="width:100%;box-sizing:border-box;background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:12px;">
    <p style="color:#a89d87;font-size:11px;letter-spacing:1px;text-transform:uppercase;margin:0 0 8px 0;">MITTELFRISTIG<br>STRUKTUR</p>
    {mittelfristig_html}
    </div></td>
    <td width="33.3333%" valign="top" style="width:33.3333%;padding:0 6px;box-sizing:border-box;">
    <div style="width:100%;box-sizing:border-box;background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:12px;">
    <p style="color:#a89d87;font-size:11px;letter-spacing:1px;text-transform:uppercase;margin:0 0 8px 0;">LANGFRISTIG<br>POSITION</p>
    {langfristig_html}
    </div></td>
    </tr>
    """

    def level_boxen(werte, rand_farbe):
        """Kachel-Reihe wie im Screenshot: gleich breite, umrandete Boxen nebeneinander.
        Tabellenbasiert (statt flexbox/grid), damit auch E-Mail-Clients wie Outlook das
        Layout darstellen; bricht bei schmalen Ansichten automatisch um."""
        zellen = "".join(
            f'<td style="padding:4px;">'
            f'<div style="background:#1c1712;border:1px solid {rand_farbe};border-radius:6px;'
            f'padding:14px 10px;text-align:center;color:#ece6d9;font-weight:bold;font-size:15px;">'
            + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            + "</div></td>"
            for v in werte
        )
        return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{zellen}</tr></table>'

    def kachel(titel, wert_html):
        return (
            f'<td style="padding:6px;" valign="top">'
            f'<div style="background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:16px 18px;">'
            f'<p style="color:#a89d87;font-size:11px;letter-spacing:1px;text-transform:uppercase;margin:0 0 8px 0;">{titel}</p>'
            f'{wert_html}'
            f'</div></td>'
        )

    def chart_block(titel, cid, signal_text, regeln_text, backtest_text):
        zeilen = signal_text.split("\n", 1)
        kopf = zeilen[0]
        rest = zeilen[1] if len(zeilen) > 1 else ""
        return f"""
    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin:26px 0 10px 0;">{titel}</h3>
    <img src="cid:{cid}" style="max-width:100%;border:1px solid #3a3226;border-radius:6px;">
    <p style="line-height:1.6;margin-top:12px;"><strong style="color:#e8b95c;">{kopf}</strong><br>{rest}</p>
    <p style="color:#a89d87;font-size:11px;line-height:1.5;">{regeln_text}</p>
    <p style="color:#a89d87;font-size:10.5px;">
    Rein informativ, kein automatisiertes Handelssignal - Backtest-Kennzahlen
    {backtest_text}
    </p>"""

    marktevents_html = ""
    if economic_events_html.strip():
        marktevents_html = f"""
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:14px 0 20px 0;">
    <tr><td style="background:#241a0e;border-left:4px solid #d9a441;border-radius:4px;padding:14px 18px;">
    <p style="color:#e8c98a;font-size:12.5px;line-height:1.6;margin:0;">
    {economic_events_html}
    </p>
    </td></tr></table>"""

    html = f"""
    <html><body style="background:#14110d;color:#ece6d9;font-family:monospace;padding:20px;">
    <p style="color:#a89d87;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-bottom:2px;">Neuber Precious Metals</p>
    <h1 style="color:#e8b95c;font-family:serif;margin-top:0;">Mini Daily: Gold</h1>
    <p style="color:#a89d87;">{heute} - Erstellt um {erstellt_zeit} Uhr - Kursdaten Stand {daten_zeit} Uhr</p>
    {warnblock}
    {marktevents_html}

    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;table-layout:fixed;margin-bottom:6px;">
    <tr>
    {kachel("Realtime Indikation", f'<p style="font-size:24px;font-family:serif;color:#e8b95c;margin:0;">{fmt(daten["realtime"])} USD</p>')}
    {kachel("Tendenz (zum Schlusskurs)", f'<p style="font-size:19px;font-family:serif;margin:0;">{tendenz_label} ({tendenz_pct:+.2f}%)</p>')}
    {kachel("Schlusskurs", f'<p style="font-size:24px;font-family:serif;margin:0;">{fmt(daten["prev_close"])} USD</p>')}
    </tr>
    </table>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin:20px 0 8px 0;">Szenarien</h3>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;table-layout:fixed;">{szenarien_html}</table>

    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:10px;">
    <tr>
    <td width="50%" valign="top" style="padding-right:8px;">
    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Widerstände (Intraday)</h3>
    {level_boxen(pivots['r'], '#8a4a42')}
    </td>
    <td width="50%" valign="top" style="padding-left:8px;">
    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Unterstützungen (Intraday)</h3>
    {level_boxen(pivots['s'], '#4a7a42')}
    </td>
    </tr>
    </table>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-top:22px;">Intraday-Zukunftsanalyse · Daytrading</h3>
    <div style="background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:14px 16px;line-height:1.55;white-space:pre-line;">{formatiere_intraday_zukunft(intraday_zukunft, fmt)}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-top:22px;">Tagesdaten-MA-Struktur · 6M / Position</h3>
    <div style="background:#1c1712;border:1px solid #3a3226;border-radius:6px;padding:14px 16px;line-height:1.55;">{formatiere_tages_ma_struktur(tages_ma_struktur, fmt)}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-top:22px;">Rückblick</h3>
    <p style="line-height:1.6;">{rueckblick_text}</p>
    {chart_block("Tageschart (Intraday)", "chart", range_ausbruch_text, RANGE_AUSBRUCH_REGELN_TEXT, RANGE_AUSBRUCH_BACKTEST_TEXT)}
    {chart_block("Tageschart (Positionstrading-Basis)", "chart_tages", positionstrading_text, POSITIONSTRADING_REGELN_TEXT, positionstrading_status.get('backtest_kennzahlen', '(keine Kennzahlen verfügbar)'))}

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;margin:26px 0 10px 0;">Struktureller Chart ({LANGFRIST_MONATE} Monate)</h3>
    <img src="cid:chart_lang" style="max-width:100%;border:1px solid #3a3226;border-radius:6px;">

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
    vola_erlaubt = berechne_volatilitaets_erlaubt(daily, VOLATILITAETS_FENSTER_KURZ_TAGE, VOLATILITAETS_FENSTER_LANG_TAGE)

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_datum = None
    cooldown_bis = None
    letzter_abgeschlossener_trade = None
    letztes_alert_event = None
    alle_trades = []  # sammelt jeden abgeschlossenen Trade für die Live-Backtest-Kennzahlen im Footer

    for datum, bar in daily.iterrows():
        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        trend_auf = aufwaertstrend.get(datum)
        ref_tief = swing_tief_referenz.get(datum)
        vola_ok = (not VOLATILITAETS_FILTER_AKTIV) or bool(vola_erlaubt.get(datum, False))

        if not in_position:
            if cooldown_bis is not None and datum < cooldown_bis:
                continue
            if pd.notna(trend_auf) and trend_auf and pd.notna(ref_tief) and vola_ok:
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
                        letztes_alert_event = {
                            "event": "ENTRY", "zeit": datum.isoformat(),
                            "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                        }
        else:
            if stufe == 2 and pd.notna(ref_tief):
                stop = max(stop, float(ref_tief))
            if tief <= stop:
                letzter_abgeschlossener_trade = {
                    "einstieg_datum": entry_datum, "ausstieg_datum": datum,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                }
                alle_trades.append(letzter_abgeschlossener_trade["ergebnis_pct"])
                letztes_alert_event = {
                    "event": "STOP", "zeit": datum.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }
                in_position = False
                cooldown_bis = datum + pd.Timedelta(days=POSITIONSTRADING_COOLDOWN_TAGE)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
                letztes_alert_event = {
                    "event": "TP2", "zeit": datum.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)
                letztes_alert_event = {
                    "event": "TP1", "zeit": datum.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }

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

    # Neustart-Regel (siehe SIGNAL_NEUSTART_DATUM oben): eine schon vor dem
    # Stichtag eröffnete Position gilt für die Anzeige nicht mehr als offen,
    # ein Trade davor nicht mehr als "letzter Trade".
    if in_position and entry_datum < SIGNAL_NEUSTART_DATUM:
        in_position = False
    if letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_datum"] < SIGNAL_NEUSTART_DATUM:
        letzter_abgeschlossener_trade = None

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
            "_alert_event": (letztes_alert_event if letztes_alert_event and letztes_alert_event["zeit"] == letztes_datum.isoformat() else None),
        }
    else:
        # War der AUSSTIEG (Stop) genau die letzte (heutige) Kerze -> heute
        # ausgelöstes Verkaufssignal. Sonst: schon länger flach, kein Signal heute.
        heutiges_signal = "VERKAUF"
        if not (letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_datum"] == letztes_datum):
            heutiges_signal = "KEIN_SIGNAL"

        # Vorschau, falls gerade keine Position offen ist: der STOP ist exakt
        # bekannt (aktuelles 10-Tage-Tief), der EINSTIEG dagegen nicht - der
        # echte Trigger braucht einen Bounce (Berührung + Schluss darüber),
        # dessen genauer Schlusskurs vorher unbekannt ist. Als Näherung dient
        # der aktuelle Kurs als Platzhalter-Einstieg - klar als Näherung
        # gekennzeichnet, nicht als tatsächlicher künftiger Preis.
        vorschau = None
        ref_tief_aktuell = swing_tief_referenz.get(letztes_datum)
        trend_aktuell = aufwaertstrend.get(letztes_datum)
        if pd.notna(ref_tief_aktuell):
            ref_tief_aktuell = float(ref_tief_aktuell)
            if ref_tief_aktuell < letzter_kurs:
                r = letzter_kurs - ref_tief_aktuell
                vorschau = {
                    "stop": ref_tief_aktuell,
                    "stop_praezise": True,
                    "hypothetischer_einstieg": letzter_kurs,
                    "tp1": letzter_kurs + 2 * r,
                    "tp2": letzter_kurs + 3 * r,
                    "trend_erfuellt": bool(trend_aktuell) if pd.notna(trend_aktuell) else None,
                }
                # Trendbedingung aktuell nicht erfüllt: zusätzlich ausrechnen, ab
                # wann/welchem Kursniveau der Trendfilter kippen würde -
                # beantwortet direkt "ab welchem Kursniveau schaltet der Filter
                # wieder auf grün". Die "Tage bis Trendwechsel"-Größe (bei
                # angenommen gleichbleibendem Kurs) ist die realistischere
                # Antwort; der Einzeltag-Schwellenwert wird nur gezeigt, wenn er
                # nicht unrealistisch weit vom aktuellen Kurs liegt (siehe
                # berechne_trend_schwelle()-Docstring, warum er sonst
                # irreführend sein kann).
                if vorschau["trend_erfuellt"] is False:
                    letzte_49 = daily["Close"].iloc[-(POSITIONSTRADING_TREND_FENSTER - 1):].to_numpy()
                    schwelle = berechne_trend_schwelle(letzte_49, POSITIONSTRADING_TREND_FENSTER)
                    if schwelle is not None and abs(schwelle - letzter_kurs) / letzter_kurs <= 0.10:
                        vorschau["trend_schwelle"] = schwelle
                    vorschau["sma_aktuell"] = float(daily["Close"].iloc[-POSITIONSTRADING_TREND_FENSTER:].mean())
                    vorschau["tage_bis_trendwechsel"] = berechne_tage_bis_trendwechsel(
                        daily["Close"].to_numpy(), POSITIONSTRADING_TREND_FENSTER, letzter_kurs
                    )

        alert_event = None
        if (
            heutiges_signal == "KEIN_SIGNAL"
            and not (cooldown_bis is not None and letztes_datum < cooldown_bis)
            and vorschau is not None
            and vorschau.get("trend_erfuellt") is True
        ):
            trigger = float(vorschau["stop"])
            aktueller = float(letzter_kurs)
            abstand_pct = abs(aktueller - trigger) / trigger * 100 if trigger else 999.0
            if aktueller >= trigger and abstand_pct <= TRADE_ALERT_PREPARE_ABSTAND_PCT:
                alert_event = {
                    "event": "PREPARE",
                    "zeit": letztes_datum.isoformat(),
                    "einstieg": float(vorschau["hypothetischer_einstieg"]),
                    "stop": float(vorschau["stop"]),
                    "tp1": float(vorschau["tp1"]),
                    "tp2": float(vorschau["tp2"]),
                    "trigger_typ": "Swing-Tief-Bounce",
                    "trigger_abstand_pct": abstand_pct,
                }

        return {
            "status": "keine_position",
            "signal": heutiges_signal,
            "letzter_trade": letzter_abgeschlossener_trade,
            "im_cooldown": cooldown_bis is not None and letztes_datum < cooldown_bis,
            "backtest_kennzahlen": backtest_kennzahlen_text(),
            "vorschau": vorschau,
            "_alert_event": alert_event,
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
    vola_erlaubt = berechne_volatilitaets_erlaubt(stunden, VOLATILITAETS_FENSTER_KURZ_STUNDEN, VOLATILITAETS_FENSTER_LANG_STUNDEN)

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_zeit = None
    cooldown_bis = None
    letzter_abgeschlossener_trade = None
    letztes_alert_event = None

    for zeit, bar in stunden.iterrows():
        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        ref_hoch = range_hoch_referenz.get(zeit)
        ref_tief = range_tief_referenz.get(zeit)
        vola_ok = (not VOLATILITAETS_FILTER_AKTIV) or bool(vola_erlaubt.get(zeit, False))

        if not in_position:
            if cooldown_bis is not None and zeit < cooldown_bis:
                continue
            if pd.notna(ref_hoch) and pd.notna(ref_tief) and schluss > float(ref_hoch) and vola_ok:
                entry = schluss
                stop = float(ref_tief)
                if stop < entry:
                    r = entry - stop
                    tp1 = entry + 2 * r
                    tp2 = entry + 3 * r
                    in_position = True
                    stufe = 0
                    entry_zeit = zeit
                    letztes_alert_event = {
                        "event": "ENTRY", "zeit": zeit.isoformat(),
                        "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                    }
        else:
            if stufe == 2 and pd.notna(ref_tief):
                stop = max(stop, float(ref_tief))
            if tief <= stop:
                letzter_abgeschlossener_trade = {
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                }
                letztes_alert_event = {
                    "event": "STOP", "zeit": zeit.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }
                in_position = False
                cooldown_bis = zeit + pd.Timedelta(hours=RANGE_AUSBRUCH_COOLDOWN_STUNDEN)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
                letztes_alert_event = {
                    "event": "TP2", "zeit": zeit.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)
                letztes_alert_event = {
                    "event": "TP1", "zeit": zeit.isoformat(),
                    "einstieg": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                }

    letzter_kurs = float(stunden["Close"].iloc[-1])
    letzte_zeit = stunden.index[-1]

    # Neustart-Regel (siehe SIGNAL_NEUSTART_DATUM oben): eine schon vor dem
    # Stichtag eröffnete Position gilt für die Anzeige nicht mehr als offen,
    # ein Trade davor nicht mehr als "letzter Trade".
    if in_position and entry_zeit < SIGNAL_NEUSTART_DATUM:
        in_position = False
    if letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_zeit"] < SIGNAL_NEUSTART_DATUM:
        letzter_abgeschlossener_trade = None

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
            "_alert_event": (letztes_alert_event if letztes_alert_event and letztes_alert_event["zeit"] == letzte_zeit.isoformat() else None),
        }
    else:
        heutiges_signal = "VERKAUF"
        if not (letzter_abgeschlossener_trade and letzter_abgeschlossener_trade["ausstieg_zeit"] == letzte_zeit):
            heutiges_signal = "KEIN_SIGNAL"

        # Vorschau, falls gerade keine Position offen ist: hier ist - anders als
        # bei V1e - auch der EINSTIEG exakt bekannt, nicht nur der Stop: das
        # aktuelle rollierende 24h-Hoch IST der künftige Trigger-Kurs selbst
        # (ein bestätigter Schluss darüber löst genau dort aus), kein
        # Näherungswert wie beim Swing-Tief-Bounce-System.
        vorschau = None
        ref_hoch_aktuell = range_hoch_referenz.get(letzte_zeit)
        ref_tief_aktuell = range_tief_referenz.get(letzte_zeit)
        if pd.notna(ref_hoch_aktuell) and pd.notna(ref_tief_aktuell):
            ref_hoch_aktuell = float(ref_hoch_aktuell)
            ref_tief_aktuell = float(ref_tief_aktuell)
            if ref_tief_aktuell < ref_hoch_aktuell:
                r = ref_hoch_aktuell - ref_tief_aktuell
                vorschau = {
                    "stop": ref_tief_aktuell,
                    "hypothetischer_einstieg": ref_hoch_aktuell,
                    "einstieg_praezise": True,
                    "tp1": ref_hoch_aktuell + 2 * r,
                    "tp2": ref_hoch_aktuell + 3 * r,
                }

        alert_event = None
        if (
            heutiges_signal == "KEIN_SIGNAL"
            and not (cooldown_bis is not None and letzte_zeit < cooldown_bis)
            and vorschau is not None
        ):
            trigger = float(vorschau["hypothetischer_einstieg"])
            aktueller = float(letzter_kurs)
            abstand_pct = (trigger - aktueller) / trigger * 100 if trigger else 999.0
            if 0 <= abstand_pct <= TRADE_ALERT_PREPARE_ABSTAND_PCT:
                alert_event = {
                    "event": "PREPARE",
                    "zeit": letzte_zeit.isoformat(),
                    "einstieg": trigger,
                    "stop": float(vorschau["stop"]),
                    "tp1": float(vorschau["tp1"]),
                    "tp2": float(vorschau["tp2"]),
                    "trigger_typ": "24h-Hoch-Ausbruch",
                    "trigger_abstand_pct": abstand_pct,
                }

        return {
            "status": "keine_position",
            "signal": heutiges_signal,
            "letzter_trade": letzter_abgeschlossener_trade,
            "im_cooldown": cooldown_bis is not None and letzte_zeit < cooldown_bis,
            "vorschau": vorschau,
            "_alert_event": alert_event,
        }


def schreibe_trade_alerts(positionstrading_status, range_ausbruch_status):
    """Schreibt ausschließlich die aktuell neu auslösbaren Trade-Events.
    Die Signalberechnung bleibt vollständig in den beiden Statusfunktionen;
    diese Funktion übersetzt nur deren letzten Event-/Vorwarnungszustand in
    das von send_trade_alerts.py erwartete JSON-Format.
    """
    events = []
    for system, status in (
        ("POSITIONSTRADING", positionstrading_status),
        ("RANGE_AUSBRUCH_1H", range_ausbruch_status),
    ):
        event = status.get("_alert_event") if isinstance(status, dict) else None
        if not event:
            continue
        event = dict(event)
        event["system"] = system
        if event["event"] == "PREPARE":
            event_id = (
                f"{system}:PREPARE:"
                f"{float(event['einstieg']):.2f}:"
                f"{float(event['stop']):.2f}"
            )
        else:
            event_id = f"{system}:{event['event']}:{event['zeit']}"
        event["event_id"] = event_id
        events.append(event)

    with open("trade_alerts.json", "w", encoding="utf-8") as f:
        json.dump({"events": events}, f, ensure_ascii=False, indent=2)
    print(f"Trade-Alerts geschrieben: {len(events)} Event(s)")


def main():
    daten = hole_kursdaten()
    pivots = klassische_pivots(daten["prev_high"], daten["prev_low"], daten["prev_close"])
    tendenz_label, tendenz_pct = bestimme_tendenz(daten["realtime"], daten["prev_close"])

    zonen_je_zeitraum = {}
    daily_lang = None
    daily_ma_basis = None
    for monate in (3, LANGFRIST_MONATE, 36):
        langfrist = hole_langfrist_daten(monate=monate)
        if monate == LANGFRIST_MONATE:
            daily_lang = langfrist
        if monate == 36:
            daily_ma_basis = langfrist
        zonen_je_zeitraum[monate] = analysiere_reaktionszonen(langfrist) if langfrist is not None else None
        if zonen_je_zeitraum[monate]:
            print(f"Widerstandszonen ({monate}M): {zonen_je_zeitraum[monate]['widerstandszonen']}")
            print(f"Supportzonen ({monate}M): {zonen_je_zeitraum[monate]['supportzonen']}")
        else:
            print(f"Keine ausreichenden Daten für {monate}-Monats-Zonen.")

    szenarien = berechne_szenarien(daten["realtime"], pivots)
    intraday_zukunft = analysiere_intraday_zukunft(daten, szenarien)

    # Tages-MA-Struktur für 6M / Position aus der vollständigen 36M-Tageshistorie.
    # Die 6M-Reihe allein enthält ggf. weniger als 200 Handelstage und reicht
    # deshalb nicht zuverlässig für einen WMA200.
    tages_ma_struktur = (
        berechne_tages_ma_struktur(daily_ma_basis)
        if daily_ma_basis is not None
        else None
    )
    if tages_ma_struktur:
        print(formatiere_tages_ma_struktur(tages_ma_struktur, lambda n: f"{n:,.2f}"))
    else:
        print("Tagesdaten-MA-Struktur: nicht verfügbar.")
    print(formatiere_intraday_zukunft(intraday_zukunft, lambda n: f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")))

    # Dieselben bereits für die mittlere Karte verwendeten 6M-Strukturdaten
    # vor dem Gemini-Aufruf bereitstellen, damit Gemini exakt diese Ergebnisse
    # übernimmt und keine eigenen mittelfristigen Marken erfindet.
    kombinierte_zonen_lang = kombiniere_zonen(
        {k: v for k, v in zonen_je_zeitraum.items() if k in (3, LANGFRIST_MONATE)}
    )
    struktur_6m_szenario_zonen = berechne_6m_strukturzonen(daily_lang) if daily_lang is not None else None
    struktur_6m_reaktionszonen = kombinierte_zonen_lang
    mittelfristige_szenarien = berechne_mittelfristige_szenarien(
        daily_lang, struktur_6m_szenario_zonen, struktur_6m_reaktionszonen
    )

    langfrist_formation = None
    if daily_lang is not None:
        lang_kanal = finde_trendkanal(
            daily_lang,
            fenster=LANGFRIST_KANAL_FENSTER,
            min_punkte=LANGFRIST_KANAL_MIN_PUNKTE,
        )
        if lang_kanal is not None:
            langfrist_formation = lang_kanal.get("formation")
    economic_events_block, _ = briefing_block(days_ahead=7)
    rueckblick_text = generiere_rueckblick(
        daten, pivots, tendenz_label, zonen_je_zeitraum, szenarien,
        langfrist_formation=langfrist_formation,
        mittelfristige_szenarien=mittelfristige_szenarien,
        intraday_zukunft=intraday_zukunft,
        tages_ma_struktur=tages_ma_struktur,
    )
    # Zwei getrennte Toleranzen: der Intraday-Chart soll nur wirklich naheliegende
    # Struktur-Level zeigen (enger Zeithorizont), der 4-Monats-Chart darf großzügiger sein.
    kombinierte_zonen_intraday = kombiniere_zonen(zonen_je_zeitraum, referenz_preis=daten["realtime"], max_abstand_pct=5)
    # Für den 6-Monats-Chart bewusst OHNE Preisnähe-Filter, aber nur aus den 3-/6-Monats-
    # Fenstern (nicht 36M) - deren Zonen stammen aus Daten, die ohnehin im sichtbaren
    # 6-Monats-Preisbereich liegen, können die Achse also nicht aufblähen. Das 36-Monats-
    # Fenster bleibt außen vor, weil es auch Zonen aus einem ganz anderen (viel tieferen)
    # historischen Kursniveau liefern kann.
    kombinierte_zonen_lang = kombiniere_zonen(
        {k: v for k, v in zonen_je_zeitraum.items() if k in (3, LANGFRIST_MONATE)}
    )

    range_ausbruch_status = berechne_range_ausbruch_status()
    print(f"Range-Ausbruch-Status: {range_ausbruch_status['status']}")

    chart_pfad = baue_chart(daten["intraday_reihe"], pivots, strukturzonen=kombinierte_zonen_intraday,
                             range_ausbruch_status=range_ausbruch_status)
    chart_lang_pfad = None
    if daily_lang is not None:
        chart_lang_pfad = baue_langfrist_chart(daily_lang, kombinierte_zonen_lang, struktur_zonen=struktur_6m_szenario_zonen)

    positionstrading_status = berechne_positionstrading_status()
    print(f"Positionstrading-Status: {positionstrading_status['status']}")

    schreibe_trade_alerts(positionstrading_status, range_ausbruch_status)

    # Für den neuen Tageschart reicht ein 12-Monats-Ausschnitt (genug für 50-Tage-
    # Trend + 10-Tage-Swing-Tief-Referenz, aber übersichtlicher als die vollen
    # ~7 Jahre, die für das Positionstrading-Signal selbst durchgerechnet werden).
    daily_fuer_tageschart = hole_langfrist_daten(monate=12)
    chart_tages_pfad = None
    if daily_fuer_tageschart is not None:
        chart_tages_pfad = baue_tageschart(daily_fuer_tageschart, positionstrading_status)

    html = baue_html(
        daten, pivots, tendenz_label, tendenz_pct, rueckblick_text,
        chart_pfad, chart_tages_pfad, positionstrading_status,
        range_ausbruch_status, economic_events_block, zonen_je_zeitraum,
        daily_lang, daily_fuer_tageschart, struktur_6m_szenario_zonen,
        struktur_6m_reaktionszonen, mittelfristige_szenarien, intraday_zukunft, tages_ma_struktur
    )
    text = baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, positionstrading_status, range_ausbruch_status, economic_events_block, intraday_zukunft, tages_ma_struktur)

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
