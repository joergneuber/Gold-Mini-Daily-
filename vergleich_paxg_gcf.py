"""
Vergleicht GC=F (Future, bisherige Datenquelle) mit PAXG/USDT (Binance,
Krypto-Proxy) im Log - rein informativ, beeinflusst den eigentlichen Report
(mini_daily_gold.py) nicht. PAXG-Teil läuft NUR Mo-Fr 08:00-22:00 MEZ/MESZ
(siehe Begründung: Wochenend-/Nacht-Drift ohne Arbitrage zum echten Goldmarkt).

Absichtlich als eigenständiges, robustes Skript gebaut: Ein Fehler hier soll
NICHT den Hauptreport zum Scheitern bringen (deshalb im Workflow mit
continue-on-error versehen).
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf

PAXG_SYMBOL = "PAXGUSDT"
BINANCE_BASIS_URL = "https://api.binance.com/api/v3"
GCF_TICKER = "GC=F"

ERLAUBTE_WOCHENTAGE = {0, 1, 2, 3, 4}  # Montag=0 ... Sonntag=6
START_STUNDE = 8
END_STUNDE = 22


def paxg_zeitfenster_aktiv():
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    return jetzt.weekday() in ERLAUBTE_WOCHENTAGE and START_STUNDE <= jetzt.hour < END_STUNDE, jetzt


def klassische_pivots(high, low, close):
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    return {"p": p, "r1": r1, "s1": s1}


def hole_gcf_werte():
    ticker = yf.Ticker(GCF_TICKER)
    intraday = ticker.history(period="2d", interval="5m")
    realtime = float(intraday["Close"].iloc[-1])
    daily = ticker.history(period="5d", interval="1d")
    vortag = daily.iloc[-2]
    pivots = klassische_pivots(float(vortag["High"]), float(vortag["Low"]), float(vortag["Close"]))
    return {"realtime": realtime, "pivots": pivots}


def hole_paxg_werte():
    antwort = requests.get(f"{BINANCE_BASIS_URL}/ticker/price", params={"symbol": PAXG_SYMBOL}, timeout=10)
    antwort.raise_for_status()
    realtime = float(antwort.json()["price"])

    kerzen_antwort = requests.get(
        f"{BINANCE_BASIS_URL}/klines",
        params={"symbol": PAXG_SYMBOL, "interval": "1d", "limit": 3},
        timeout=10,
    )
    kerzen_antwort.raise_for_status()
    rohdaten = kerzen_antwort.json()
    vortag = rohdaten[-2]  # [open_time, open, high, low, close, ...]
    high, low, close = float(vortag[2]), float(vortag[3]), float(vortag[4])
    pivots = klassische_pivots(high, low, close)
    return {"realtime": realtime, "pivots": pivots}


def main():
    print("=== Vergleich GC=F vs. PAXG/USDT ===\n")

    gcf = hole_gcf_werte()
    print(f"GC=F   Realtime: {gcf['realtime']:.2f} USD")
    print(f"GC=F   Pivot-P: {gcf['pivots']['p']:.2f} | R1: {gcf['pivots']['r1']:.2f} | S1: {gcf['pivots']['s1']:.2f}")

    fenster_aktiv, jetzt = paxg_zeitfenster_aktiv()
    if not fenster_aktiv:
        print(f"\nPAXG-Vergleich übersprungen - außerhalb Mo-Fr 08-22 Uhr MEZ/MESZ")
        print(f"(aktuell: {jetzt.strftime('%A, %d.%m.%Y %H:%M')})")
        return

    try:
        paxg = hole_paxg_werte()
    except Exception as exc:
        print(f"\nPAXG-Abruf fehlgeschlagen: {exc}")
        return

    print(f"\nPAXG   Realtime: {paxg['realtime']:.2f} USD")
    print(f"PAXG   Pivot-P: {paxg['pivots']['p']:.2f} | R1: {paxg['pivots']['r1']:.2f} | S1: {paxg['pivots']['s1']:.2f}")

    diff_realtime = paxg["realtime"] - gcf["realtime"]
    diff_realtime_pct = diff_realtime / gcf["realtime"] * 100
    diff_p = paxg["pivots"]["p"] - gcf["pivots"]["p"]

    print(f"\n--- Differenz ---")
    print(f"Realtime: {diff_realtime:+.2f} USD ({diff_realtime_pct:+.3f} %)")
    print(f"Pivot-P:  {diff_p:+.2f} USD")


if __name__ == "__main__":
    main()
