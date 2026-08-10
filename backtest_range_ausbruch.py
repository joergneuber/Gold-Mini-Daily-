#!/usr/bin/env python3
# MINI DAILY GOLD – Range-Ausbruch 1h
# Vergleich A / C1 / C2 / C3
#
# A  : TP1 = 2R, TP2 = 3R
# C1 : TP1 = nächste bestätigte 1h-Widerstandszone >= 1R
#      TP2 = max(nächster bestätigter Widerstand, 3R)
# C2 : TP1 = nächste bestätigte 1h-Widerstandszone >= 1.5R
#      TP2 = max(nächster bestätigter Widerstand, 3R)
# C3 : TP1 = nächste bestätigte 1h-Widerstandszone >= 2R
#      TP2 = max(nächster bestätigter Widerstand, 3R)
#
# Gemeinsame Regeln:
# - Long only
# - bestätigter Close über vorherigem 24h-Hoch
# - Stop = vorheriges 24h-Tief
# - Stop-Abstand > 0,60 % => Trade wird abgelehnt
# - nach TP1: Stop auf Break-even
# - nach TP2: Stop auf TP1; danach 24h-Tief-Trailing
# - nach Stop: 12h Cooldown
#
# Wichtig: Die 1h-Widerstände werden nur verwendet, wenn der Swing-High
# zum Entry-Zeitpunkt bereits durch die nachfolgenden Kerzen bestätigt war.
# Dadurch kein Look-ahead.

import os
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SYMBOL = "XAU/USD"
INTERVALL = "1h"
START_DATUM = date(2019, 1, 1)
CHUNK_TAGE = 180
RANGE_FENSTER = 24
COOLDOWN_STUNDEN = 12
MAX_STOP_PCT = 0.006

TWELVEDATA_BASIS_URL = "https://api.twelvedata.com/time_series"

def hole_api_key():
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY nicht gesetzt. "
            "Bitte denselben GitHub Secret wie beim MINI DAILY GOLD verwenden."
        )
    return key

def hole_ausschnitt(api_key, start, ende, max_versuche=4):
    for versuch in range(1, max_versuche + 1):
        try:
            antwort = requests.get(
                TWELVEDATA_BASIS_URL,
                params={
                    "symbol": SYMBOL,
                    "interval": INTERVALL,
                    "apikey": api_key,
                    "timezone": "UTC",
                    "order": "ASC",
                    "start_date": start.isoformat(),
                    "end_date": ende.isoformat(),
                    "outputsize": 5000,
                },
                timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            print(f"  Netzwerkfehler {start} bis {ende} "
                  f"(Versuch {versuch}/{max_versuche}): {exc}")
            if versuch < max_versuche:
                time.sleep(10 * versuch)
                continue
            return pd.DataFrame()

        try:
            daten = antwort.json()
        except ValueError:
            if versuch < max_versuche:
                time.sleep(10 * versuch)
                continue
            antwort.raise_for_status()
            raise RuntimeError("Unerwartete Antwort ohne JSON-Body.")

        if antwort.status_code == 429:
            wartezeit = 65
            print(f"  Rate-Limit bei {start} bis {ende} "
                  f"(Versuch {versuch}/{max_versuche}) – warte {wartezeit}s...")
            if versuch < max_versuche:
                time.sleep(wartezeit)
                continue
            return pd.DataFrame()

        if antwort.status_code != 200 or daten.get("status") == "error":
            print(f"  Kein Ausschnitt {start} bis {ende} "
                  f"(HTTP {antwort.status_code}): "
                  f"{daten.get('message', daten)}")
            return pd.DataFrame()

        if "values" not in daten:
            return pd.DataFrame()

        df = pd.DataFrame(daten["values"])
        df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
        })
        for spalte in ("Open", "High", "Low", "Close"):
            df[spalte] = pd.to_numeric(df[spalte], errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

    return pd.DataFrame()

def hole_daten():
    api_key = hole_api_key()
    heute = date.today()
    teile = []
    fenster_start = START_DATUM

    while fenster_start < heute:
        fenster_ende = min(
            fenster_start + timedelta(days=CHUNK_TAGE - 1),
            heute
        )
        print(f"Hole {fenster_start} bis {fenster_ende}...")
        teil = hole_ausschnitt(api_key, fenster_start, fenster_ende)
        if not teil.empty:
            teile.append(teil)
        fenster_start = fenster_ende + timedelta(days=1)
        time.sleep(8)

    if not teile:
        raise RuntimeError("Keine Daten von Twelve Data erhalten.")

    stunden = pd.concat(teile)
    stunden = stunden[~stunden.index.duplicated()].sort_index()
    return stunden

def bestaetigte_swing_highs(stunden, left=2, right=2):
    """Liste bestätigter Swing-Highs als (Bestätigungsindex, Preis).

    Ein Hoch bei i ist erst ab i+right bekannt. Dadurch wird bei der
    TP-Ermittlung kein zukünftiges Wissen verwendet.
    """
    highs = stunden["High"].to_numpy(dtype=float)
    result = []

    for i in range(left, len(stunden) - right):
        links = highs[i-left:i]
        rechts = highs[i+1:i+right+1]
        if highs[i] >= links.max() and highs[i] > rechts.max():
            confirmation_idx = i + right
            result.append((confirmation_idx, float(highs[i])))

    return result

def widerstand_ab_entry(swing_highs, entry_idx, min_price):
    """Erster bereits bestätigter Widerstand >= min_price."""
    for confirmation_idx, price in swing_highs:
        if confirmation_idx <= entry_idx:
            if price >= min_price:
                return price
    return None

def alle_widerstaende_nach_entry(swing_highs, entry_idx):
    """Bereits bekannte Widerstände oberhalb des Entry, absteigend nach Zeit."""
    return [
        (confirmation_idx, price)
        for confirmation_idx, price in swing_highs
        if confirmation_idx <= entry_idx
    ]

def tp_ziele(stunden, swing_highs, entry_idx, entry, stop, variant):
    r = entry - stop

    if variant == "A":
        return entry + 2*r, entry + 3*r

    min_r = {"C1": 1.0, "C2": 1.5, "C3": 2.0}[variant]
    min_tp1 = entry + min_r*r

    # Für den Backtest werden nur Widerstände verwendet, die zum Entry
    # bereits bestätigt waren. Der "nächste" Widerstand ist der nächstgelegene
    # Preis oberhalb der jeweiligen Mindestmarke.
    known = sorted(
        {round(price, 8) for _, price in alle_widerstaende_nach_entry(swing_highs, entry_idx)},
        key=lambda x: x
    )

    tp1_candidates = [p for p in known if p >= min_tp1]
    tp1 = min(tp1_candidates) if tp1_candidates else entry + 2*r

    min_tp2 = max(tp1, entry + 3*r)
    tp2_candidates = [p for p in known if p >= min_tp2]
    tp2 = min(tp2_candidates) if tp2_candidates else entry + 3*r

    if tp2 <= tp1:
        tp2 = max(entry + 3*r, tp1)

    return float(tp1), float(tp2)

def backtest(stunden, variant, swing_highs):
    ref_high = stunden["High"].rolling(RANGE_FENSTER).max().shift(1)
    ref_low = stunden["Low"].rolling(RANGE_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_idx = None
    entry_zeit = None
    cooldown_bis = None

    for i, (zeit, bar) in enumerate(stunden.iterrows()):
        hoch = float(bar["High"])
        tief = float(bar["Low"])
        schluss = float(bar["Close"])

        if not in_position:
            if cooldown_bis is not None and zeit < cooldown_bis:
                continue

            rh = ref_high.iloc[i]
            rl = ref_low.iloc[i]

            if pd.notna(rh) and pd.notna(rl) and schluss > float(rh):
                entry_candidate = schluss
                stop_candidate = float(rl)

                if stop_candidate >= entry_candidate:
                    continue

                risk = entry_candidate - stop_candidate
                stop_pct = risk / entry_candidate

                if stop_pct > MAX_STOP_PCT:
                    continue

                entry = entry_candidate
                stop = stop_candidate
                tp1, tp2 = tp_ziele(
                    stunden, swing_highs, i, entry, stop, variant
                )

                entry_idx = i
                entry_zeit = zeit
                stufe = 0
                in_position = True
            continue

        # Nach TP2: 24h-Tief-Trailing.
        if stufe == 2:
            trail = ref_low.iloc[i]
            if pd.notna(trail):
                stop = max(stop, float(trail))

        # Konservativ: Stop zuerst prüfen, dann Ziele.
        if tief <= stop:
            trades.append({
                "variante": variant,
                "einstieg_zeit": entry_zeit,
                "ausstieg_zeit": zeit,
                "haltedauer_stunden": (zeit-entry_zeit).total_seconds()/3600,
                "einstieg": entry,
                "stop_initial": entry - (entry-stop if stufe == 0 else 0),
                "tp1": tp1,
                "tp2": tp2,
                "ausstieg": stop,
                "ergebnis_pct": (stop-entry)/entry*100,
                "stufe_bei_ausstieg": stufe,
                "exit_typ": "STOP" if stufe == 0 else ("BE" if stufe == 1 else "TRAIL"),
            })
            in_position = False
            cooldown_bis = zeit + pd.Timedelta(hours=COOLDOWN_STUNDEN)
            continue

        if stufe < 1 and hoch >= tp1:
            stufe = 1
            stop = max(stop, entry)

        if stufe < 2 and hoch >= tp2:
            stufe = 2
            stop = max(stop, tp1)

    if in_position:
        letzter_preis = float(stunden["Close"].iloc[-1])
        letzte_zeit = stunden.index[-1]
        trades.append({
            "variante": variant,
            "einstieg_zeit": entry_zeit,
            "ausstieg_zeit": letzte_zeit,
            "haltedauer_stunden": (letzte_zeit-entry_zeit).total_seconds()/3600,
            "einstieg": entry,
            "stop_initial": None,
            "tp1": tp1,
            "tp2": tp2,
            "ausstieg": letzter_preis,
            "ergebnis_pct": (letzter_preis-entry)/entry*100,
            "stufe_bei_ausstieg": stufe,
            "exit_typ": "END",
        })

    return pd.DataFrame(trades)

def statistik(trades):
    if trades.empty:
        return {
            "Trades": 0,
            "Trefferquote_%": 0.0,
            "Summe_%": 0.0,
            "Ø_Trade_%": 0.0,
            "Ø_Gewinner_%": 0.0,
            "Ø_Verlierer_%": 0.0,
            "Stop_BE": 0,
            "TP1": 0,
            "TP2": 0,
        }

    gewinner = trades[trades["ergebnis_pct"] > 0]
    verlierer = trades[trades["ergebnis_pct"] < 0]

    return {
        "Trades": len(trades),
        "Trefferquote_%": round(len(gewinner)/len(trades)*100, 1),
        "Summe_%": round(trades["ergebnis_pct"].sum(), 2),
        "Ø_Trade_%": round(trades["ergebnis_pct"].mean(), 2),
        "Ø_Gewinner_%": round(gewinner["ergebnis_pct"].mean(), 2) if len(gewinner) else 0.0,
        "Ø_Verlierer_%": round(verlierer["ergebnis_pct"].mean(), 2) if len(verlierer) else 0.0,
        "Stop_BE": int(trades["exit_typ"].isin(["STOP", "BE", "TRAIL"]).sum()),
        "TP1": int((trades["stufe_bei_ausstieg"] >= 1).sum()),
        "TP2": int((trades["stufe_bei_ausstieg"] >= 2).sum()),
    }

def main():
    stunden = hole_daten()
    print(
        f"\n{len(stunden)} Stundenkerzen geladen, "
        f"{stunden.index.min()} bis {stunden.index.max()}"
    )

    stunden.to_csv("range_ausbruch_stundendaten_roh.csv")
    swing_highs = bestaetigte_swing_highs(stunden)
    print(f"Bestätigte Swing-Highs für charttechnische TPs: {len(swing_highs)}")

    varianten = [
        ("A", "A – 2R/3R (Referenz)"),
        ("C1", "C1 – TP1 >= 1R"),
        ("C2", "C2 – TP1 >= 1.5R"),
        ("C3", "C3 – TP1 >= 2R"),
    ]

    vergleich = []
    alle = []

    for code, label in varianten:
        trades = backtest(stunden, code, swing_highs)
        stats = statistik(trades)
        vergleich.append({"Variante": label, **stats})
        if not trades.empty:
            alle.append(trades)

        print(f"\n=== {label} ===")
        for key, value in stats.items():
            print(f"{key}: {value}")

    vergleich_df = pd.DataFrame(vergleich)
    vergleich_df.to_csv(
        "backtest_range_ausbruch_C1C2C3_vergleich.csv",
        index=False,
        encoding="utf-8-sig",
    )

    if alle:
        pd.concat(alle, ignore_index=True).to_csv(
            "backtest_range_ausbruch_C1C2C3_trades.csv",
            index=False,
            encoding="utf-8-sig",
        )

    print("\nDateien gespeichert:")
    print("- backtest_range_ausbruch_C1C2C3_vergleich.csv")
    print("- backtest_range_ausbruch_C1C2C3_trades.csv")
    print("- range_ausbruch_stundendaten_roh.csv")

if __name__ == "__main__":
    main()
