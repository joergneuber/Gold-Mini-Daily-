"""
PAXG-Prototyp - Testet PAXG/USDT (Binance) als möglichen Ersatz-/Vergleichs-
Datenpunkt zu GC=F. Bewusst NUR Mo-Fr, 08:00-22:00 MEZ/MESZ nutzbar - außerhalb
dieses Fensters bricht das Skript ab, ohne Daten zu liefern (siehe Begründung
im Gespräch: Wochenend-/Nacht-Drift ohne Arbitrage zum echten Goldmarkt).

Kein API-Key nötig - Binance liefert öffentliche Marktdaten (Klines/Ticker)
ohne Authentifizierung.

Das hier ist NOCH NICHT ins Hauptskript (mini_daily_gold.py) integriert -
erst zum Gegenchecken der Werte gegen die bestehenden GC=F-Zahlen.
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd

SYMBOL = "PAXGUSDT"
BASIS_URL = "https://api.binance.com/api/v3"

# Erlaubtes Zeitfenster: Mo-Fr (1-5, Montag=1), 08:00-22:00 Uhr, Europe/Berlin
ERLAUBTE_WOCHENTAGE = {0, 1, 2, 3, 4}  # Python: Montag=0 ... Sonntag=6
START_STUNDE = 8
END_STUNDE = 22


def pruefe_zeitfenster():
    """Bricht mit klarer Meldung ab, wenn außerhalb Mo-Fr 08-22 Uhr MEZ/MESZ."""
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    wochentag_ok = jetzt.weekday() in ERLAUBTE_WOCHENTAGE
    uhrzeit_ok = START_STUNDE <= jetzt.hour < END_STUNDE

    if not (wochentag_ok and uhrzeit_ok):
        print(f"Außerhalb des erlaubten Fensters (Mo-Fr, {START_STUNDE}-{END_STUNDE} Uhr MEZ/MESZ).")
        print(f"Aktuell: {jetzt.strftime('%A, %d.%m.%Y %H:%M')} (Europe/Berlin)")
        print("PAXG-Abruf wird übersprungen - kein Ersatz für Wochenend-/Nachtdaten.")
        sys.exit(0)

    print(f"Zeitfenster OK: {jetzt.strftime('%A, %d.%m.%Y %H:%M')} (Europe/Berlin)")


def hole_realtime_preis():
    antwort = requests.get(f"{BASIS_URL}/ticker/price", params={"symbol": SYMBOL}, timeout=10)
    antwort.raise_for_status()
    return float(antwort.json()["price"])


def hole_klines(interval, limit):
    """Liefert OHLCV-Kerzen von Binance. interval z.B. '5m', '1d'."""
    antwort = requests.get(
        f"{BASIS_URL}/klines",
        params={"symbol": SYMBOL, "interval": interval, "limit": limit},
        timeout=10,
    )
    antwort.raise_for_status()
    rohdaten = antwort.json()

    df = pd.DataFrame(rohdaten, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    for spalte in ["open", "high", "low", "close", "volume"]:
        df[spalte] = df[spalte].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert("Europe/Berlin")
    df = df.set_index("open_time")
    return df


def klassische_pivots(high, low, close):
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return {"p": p, "r": [r1, r2, r3], "s": [s1, s2, s3]}


def main():
    pruefe_zeitfenster()

    print("\n--- PAXG/USDT (Binance) ---")
    realtime = hole_realtime_preis()
    print(f"Realtime: {realtime:.2f} USD")

    # Vortages-OHLC für Pivots (letzte 2 Tageskerzen, wir nutzen die vorletzte
    # = "gestern", analog zur GC=F-Logik in hole_kursdaten())
    tages_kerzen = hole_klines("1d", limit=3)
    vortag = tages_kerzen.iloc[-2]
    prev_high, prev_low, prev_close = vortag["high"], vortag["low"], vortag["close"]
    print(f"Vortag  - Hoch: {prev_high:.2f} | Tief: {prev_low:.2f} | Schluss: {prev_close:.2f}")

    pivots = klassische_pivots(prev_high, prev_low, prev_close)
    print(f"Pivot-Widerstände: {[round(r, 2) for r in pivots['r']]}")
    print(f"Pivot-Unterstützungen: {[round(s, 2) for s in pivots['s']]}")

    # Intraday-Reihe (5-Min, letzte 2 Tage) - fürs Chart/Trendlinie
    intraday = hole_klines("5m", limit=576)  # 576 * 5min = 48h
    print(f"\nIntraday-Kerzen geladen: {len(intraday)} Stück")
    print(f"Zeitspanne: {intraday.index[0]} bis {intraday.index[-1]}")
    print(f"Intraday-Hoch: {intraday['close'].max():.2f} | Intraday-Tief: {intraday['close'].min():.2f}")

    print("\n--- Zum Vergleich: trag hier die aktuellen GC=F-Werte aus deinem")
    print("    Mini-Daily-Gold-Report ein und schau, wie nah PAXG dran liegt ---")


if __name__ == "__main__":
    main()
