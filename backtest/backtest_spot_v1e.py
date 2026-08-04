"""
Backtest Spot-Gold (XAU/USD über APIFreaks) - V1e-Logik 1:1 übernommen

Testet DIESELBEN Positionstrading-Regeln wie backtest_v1e.py (GC=F, dort
34 Trades 2019-2026, Trefferquote 38%, Summe +49,77%) auf SPOT-Gold-Daten,
um zu prüfen, ob die Regeln unverändert übertragbar sind oder angepasst
werden müssen (Nutzerfrage vom 04.08.2026: "Möglicherweise müssen wir ja
auch das Trading System anpassen?!").

Datenquelle: APIFreaks Commodities Time Series API (https://apifreaks.com).
Kostenlos (10.000 Gratis-Credits, kein Kreditkarte nötig), aber NICHT
unbegrenzt: eine Zeitreihen-Abfrage kostet 41 Credits (40 + 1 pro Symbol),
maximal 365 Tage pro Abfrage. Für 7 Jahre Historie (analog zum GC=F-Test)
braucht's daher 7 Einzelabfragen (~287 Credits insgesamt) - reichlich
Spielraum im Rahmen der 10.000 Gratis-Credits für einen einmaligen
Backtest-Lauf.

WICHTIG: Erwartet die Umgebungsvariable APIFREAKS_API_KEY (als GitHub
Secret hinterlegen). Key kostenlos unter https://apifreaks.com/signup.

Regeln (identisch zu backtest_v1e.py):
1. Nur Long. Trend: rollierende Regression über 50 Handelstage (nur bis
   gestern). Einstieg: bestätigter Bounce an einem rollierenden 10-Tage-
   Swing-Tief. Stop: dieses Tief, fest. TP1/TP2 = 2R/3R. Stufenregel:
   TP1->Breakeven, TP2->TP1-Niveau, danach kontinuierlich nachgezogen.
   Cooldown 3 Handelstage nach einem Stop.
"""

import os
import time
from datetime import date, timedelta
import requests
import pandas as pd
import numpy as np

API_BASIS_URL = "https://api.apifreaks.com/v1.0/commodity/time-series"
SYMBOL = "XAU"
START_JAHR = 2019
TREND_FENSTER = 50
SWING_FENSTER = 10
COOLDOWN_TAGE = 3


def hole_api_key():
    key = os.environ.get("APIFREAKS_API_KEY")
    if not key:
        raise EnvironmentError(
            "APIFREAKS_API_KEY nicht gesetzt. Kostenlosen Key unter "
            "https://apifreaks.com/signup holen und als GitHub Secret hinterlegen."
        )
    return key


def hole_jahres_ausschnitt(api_key, start, ende):
    """Holt max. 365 Tage OHLC über die APIFreaks Time-Series-API."""
    antwort = requests.get(
        API_BASIS_URL,
        params={"symbols": SYMBOL, "startDate": start.isoformat(), "endDate": ende.isoformat()},
        headers={"X-apiKey": api_key},
        timeout=20,
    )
    antwort.raise_for_status()
    daten = antwort.json()
    if not daten.get("success"):
        raise RuntimeError(f"APIFreaks-Fehler: {daten}")

    zeilen = []
    for datum_str, werte in daten.get("rates", {}).items():
        xau = werte.get(SYMBOL)
        if xau:
            zeilen.append({
                "Datum": pd.Timestamp(datum_str),
                "Open": xau["open"], "High": xau["high"],
                "Low": xau["low"], "Close": xau["close"],
            })
    return pd.DataFrame(zeilen)


def hole_daten():
    """Holt die komplette Historie in 365-Tage-Häppchen (API-Limit) und fügt
    sie zu einer durchgehenden Tagesreihe zusammen."""
    api_key = hole_api_key()
    heute = date.today()
    start_gesamt = date(START_JAHR, 1, 1)

    teile = []
    fenster_start = start_gesamt
    while fenster_start < heute:
        fenster_ende = min(fenster_start + timedelta(days=364), heute)
        print(f"Hole {fenster_start} bis {fenster_ende}...")
        teil = hole_jahres_ausschnitt(api_key, fenster_start, fenster_ende)
        teile.append(teil)
        fenster_start = fenster_ende + timedelta(days=1)
        time.sleep(0.5)  # kleine Pause zwischen den Abfragen, freundlich zur API

    daily = pd.concat(teile, ignore_index=True)
    daily = daily.drop_duplicates(subset="Datum").sort_values("Datum").set_index("Datum")
    return daily


def berechne_trend(schluss, fenster=TREND_FENSTER):
    def steigung(werte):
        x = np.arange(len(werte))
        m, _ = np.polyfit(x, werte, 1)
        return m
    return schluss.rolling(fenster).apply(steigung, raw=True).shift(1) > 0


def backtest(daily):
    aufwaertstrend = berechne_trend(daily["Close"])
    swing_tief_referenz = daily["Low"].rolling(SWING_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_datum = None
    cooldown_bis = None

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
                trades.append({
                    "einstieg_datum": entry_datum, "ausstieg_datum": datum,
                    "haltedauer_tage": (datum - entry_datum).days,
                    "einstieg": entry, "ausstieg": stop,
                    "ergebnis_pct": (stop - entry) / entry * 100,
                    "stufe_bei_ausstieg": stufe,
                })
                in_position = False
                cooldown_bis = datum + pd.Timedelta(days=COOLDOWN_TAGE)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    if in_position:
        letzter_preis = float(daily["Close"].iloc[-1])
        letztes_datum = daily.index[-1]
        trades.append({
            "einstieg_datum": entry_datum, "ausstieg_datum": letztes_datum,
            "haltedauer_tage": (letztes_datum - entry_datum).days,
            "einstieg": entry, "ausstieg": letzter_preis,
            "ergebnis_pct": (letzter_preis - entry) / entry * 100,
            "stufe_bei_ausstieg": stufe,
            "hinweis": "Backtest endete waehrend offener Position - mit letztem verfuegbaren Kurs geschlossen.",
        })

    return pd.DataFrame(trades)


def main():
    daily = hole_daten()
    print(f"\n{len(daily)} Tageskerzen geladen, {daily.index.min().date()} bis {daily.index.max().date()}")
    daily.to_csv("spot_tagesdaten_roh.csv")

    trades_df = backtest(daily)
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_spot_v1e_trades.csv", index=False)

    # --- Wochenend-Kerzen rausfiltern (APIFreaks liefert verzerrte Sa/So-Ranges) ---
    df["Datum"] = pd.to_datetime(df["Datum"])
    vor_filter = len(df)
    df = df[df["Datum"].dt.dayofweek < 5].reset_index(drop=True)  # 0=Mo ... 4=Fr
    print(f"Wochenend-Zeilen entfernt: {vor_filter - len(df)} von {vor_filter}")
   
    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_pct"] > 0]
    verlierer = trades_df[trades_df["ergebnis_pct"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()
    avg_haltedauer = trades_df["haltedauer_tage"].mean()

    print(f"\n=== Backtest Spot-Gold (XAU/USD), Long-only, Positionstrading (V1e-Regeln) ===")
    print(f"Zeitraum: {trades_df['einstieg_datum'].min()} bis {trades_df['ausstieg_datum'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Ø Haltedauer: {avg_haltedauer:.1f} Tage")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"\nZum Vergleich GC=F (Future): 34 Trades, Trefferquote 38,2%, Summe +49,77%")
    print(f"Trade-Log gespeichert: backtest_spot_v1e_trades.csv")


if __name__ == "__main__":
    main()
