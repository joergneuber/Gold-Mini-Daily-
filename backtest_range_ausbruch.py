"""
Backtest Range-Ausbruch (XAU/USD, 1h) - Twelve Data
-----------------------------------------------------
Hintergrund: Das bestehende V1e-Positionstrading-Signal im Mini-Daily-Report
ist bewusst träge (Haltedauer Tage bis Wochen) und zeigt bei 6 Läufen/Tag an
den allermeisten Tagen "KEIN SIGNAL". Diskutiert wurde ein zweites,
schneller reagierendes Signal auf Basis der 1h-Kerzen (Range-Ausbruch).
Bevor so etwas ins Briefing kommt, wird es hier genau wie das V1e-System
zuerst separat gegen echte Historie getestet - nicht ungeprüft eingebaut.

WICHTIG (Twelve Data Free/Basic-Tarife): Wie weit `start_date` für
Stundenkerzen tatsächlich zurückreicht, hängt vom gebuchten Plan ab - manche
Tarife liefern nur die letzten Monate an Intraday-Historie. Das Skript holt
einfach so viel wie die API hergibt und meldet den tatsächlich erhaltenen
Zeitraum im Log; ein kurzer Backtest-Zeitraum ist dann kein Bug, sondern
eine Tarif-Grenze.

Regeln (Vorschlag, noch nicht bestätigt - genau deshalb dieser Backtest):
1. Long-only. Range-Referenz: rollierendes Hoch/Tief der letzten
   RANGE_FENSTER Stunden-Kerzen (nur bis zur Vorkerze, kein Lookahead).
2. Einstieg: Schlusskurs bricht über das Range-Hoch aus (bestätigter Close,
   kein reiner Docht-Ausbruch).
3. Stop: Range-Tief zum Einstiegszeitpunkt, fest.
4. TP1/TP2 = 2R/3R. Stufenregel wie beim V1e-System: TP1 -> Breakeven,
   TP2 -> TP1-Niveau, danach kontinuierlich am aktuellen Range-Tief
   nachgezogen.
5. Cooldown: COOLDOWN_STUNDEN nach einem Stop, kein neuer Einstieg.

Datenquelle: Twelve Data Time-Series API (https://twelvedata.com), Symbol
XAU/USD, Intervall 1h. Erwartet TWELVEDATA_API_KEY als Umgebungsvariable
(gleicher Key wie im Mini-Daily-Gold-Projekt, als GitHub Secret hinterlegen).
"""

import os
import time
from datetime import date, timedelta
import requests
import pandas as pd
import numpy as np

TWELVEDATA_BASIS_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
INTERVALL = "1h"
START_DATUM = date(2019, 1, 1)  # so weit zurück wie möglich - der Tarif entscheidet, wie viel ankommt
CHUNK_TAGE = 180  # ~180*24=4320 Stundenkerzen pro Anfrage, unter dem 5000er-Limit
RANGE_FENSTER = 24  # Stunden-Kerzen für die Range-Referenz (=~1 Handelstag bei 24h-Notierung)
COOLDOWN_STUNDEN = 12


def hole_api_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY nicht gesetzt. Gleichen Key wie im Mini-Daily-Gold-Projekt "
            "verwenden und als GitHub Secret hinterlegen."
        )
    return key


def hole_ausschnitt(api_key, start, ende):
    antwort = requests.get(
        TWELVEDATA_BASIS_URL,
        params={
            "symbol": SYMBOL, "interval": INTERVALL, "apikey": api_key,
            "timezone": "UTC", "order": "ASC",
            "start_date": start.isoformat(), "end_date": ende.isoformat(),
            "outputsize": 5000,
        },
        timeout=20,
    )
    # Erst die JSON-Antwort auslesen, DANACH ggf. abbrechen - Twelve Data
    # liefert bei "kein Zugriff auf diesen Zeitraum" (typisch bei Free/Basic-
    # Tarifen für alte Intraday-Historie) HTTP 400 zusammen mit einer
    # erklärenden Fehlermeldung im Body. raise_for_status() VOR dem Auslesen
    # hätte diese Meldung nie gezeigt und das ganze Skript abgebrochen, statt
    # nur diesen einen Ausschnitt zu überspringen.
    try:
        daten = antwort.json()
    except ValueError:
        antwort.raise_for_status()
        raise RuntimeError(f"Unerwartete Antwort ohne JSON-Body: {antwort.text[:300]}")

    if antwort.status_code != 200 or daten.get("status") == "error":
        print(f"  Kein Ausschnitt {start} bis {ende} (HTTP {antwort.status_code}): "
              f"{daten.get('message', daten)}")
        return pd.DataFrame()
    if "values" not in daten:
        return pd.DataFrame()

    df = pd.DataFrame(daten["values"])
    df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    for spalte in ("Open", "High", "Low", "Close"):
        df[spalte] = df[spalte].astype(float)
    return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]


def hole_daten():
    api_key = hole_api_key()
    heute = date.today()

    teile = []
    fenster_start = START_DATUM
    while fenster_start < heute:
        fenster_ende = min(fenster_start + timedelta(days=CHUNK_TAGE - 1), heute)
        print(f"Hole {fenster_start} bis {fenster_ende}...")
        teil = hole_ausschnitt(api_key, fenster_start, fenster_ende)
        if not teil.empty:
            teile.append(teil)
        fenster_start = fenster_ende + timedelta(days=1)
        time.sleep(0.5)

    if not teile:
        raise RuntimeError("Keine Daten von Twelve Data erhalten - Tarif/Key prüfen.")

    stunden = pd.concat(teile)
    stunden = stunden[~stunden.index.duplicated()].sort_index()
    return stunden


def backtest(stunden):
    range_hoch_referenz = stunden["High"].rolling(RANGE_FENSTER).max().shift(1)
    range_tief_referenz = stunden["Low"].rolling(RANGE_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_zeit = None
    cooldown_bis = None

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
                trades.append({
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "haltedauer_stunden": (zeit - entry_zeit).total_seconds() / 3600,
                    "einstieg": entry, "ausstieg": stop,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                    "stufe_bei_ausstieg": stufe,
                })
                in_position = False
                cooldown_bis = zeit + pd.Timedelta(hours=COOLDOWN_STUNDEN)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    if in_position:
        letzter_preis = float(stunden["Close"].iloc[-1])
        letzte_zeit = stunden.index[-1]
        trades.append({
            "einstieg_zeit": entry_zeit, "ausstieg_zeit": letzte_zeit,
            "haltedauer_stunden": (letzte_zeit - entry_zeit).total_seconds() / 3600,
            "einstieg": entry, "ausstieg": letzter_preis,
            "ergebnis_pct": (letzter_preis - entry) / entry * 100,
            "stufe_bei_ausstieg": stufe,
            "hinweis": "Backtest endete waehrend offener Position - mit letztem verfuegbaren Kurs geschlossen.",
        })

    return pd.DataFrame(trades)


def main():
    stunden = hole_daten()
    print(f"\n{len(stunden)} Stundenkerzen geladen, {stunden.index.min()} bis {stunden.index.max()}")
    stunden.to_csv("range_ausbruch_stundendaten_roh.csv")

    trades_df = backtest(stunden)
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_range_ausbruch_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_pct"] > 0]
    verlierer = trades_df[trades_df["ergebnis_pct"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()
    avg_haltedauer = trades_df["haltedauer_stunden"].mean()

    print(f"\n=== Backtest Range-Ausbruch (XAU/USD, 1h) ===")
    print(f"Zeitraum: {trades_df['einstieg_zeit'].min()} bis {trades_df['ausstieg_zeit'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Ø Haltedauer: {avg_haltedauer:.1f} Stunden")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"\nZum Vergleich Positionstrading V1e (Spot, Backtest Spot-Gold-Projekt): "
          f"42 Trades, Trefferquote 19,0%, Summe +4,54% (Zeitraum 2019-2026)")
    print(f"Trade-Log gespeichert: backtest_range_ausbruch_trades.csv")


if __name__ == "__main__":
    main()
