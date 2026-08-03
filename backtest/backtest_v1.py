"""
Backtest V1: Long-only Gold-Future-Strategie (GC=F)

Regeln (Stand: gemeinsam festgelegt im Gespräch, 03.08.2026):
1. NUR Long-Positionen.
2. Einstieg: Ausbruch über den naechsten Pivot-Widerstand (aus dem
   Vortages-OHLC berechnet - bewusst KEIN Blick in die Zukunft, deshalb
   in V1 NUR Pivot-Basis, noch keine Range-Box/Umkehrzonen - siehe unten).
3. Ausstieg (Stop): der durchbrochene Widerstand wird zum Stop (jetzt
   Support). Faellt der Kurs darunter zurueck, wird ausgestoppt.
4. TP1/TP2: die naechsten beiden Pivot-Widerstaende oberhalb des Einstiegs.
5. Stufenregel (1:1 uebernommen aus positionen_tracker.py im
   Sektor-Analyse-Projekt):
   - TP1 erreicht -> Stop EINMALIG auf Breakeven (Einstiegskurs)
   - TP2 erreicht -> Stop EINMALIG auf TP1-Niveau
   - Nur verbessern, nie verschlechtern; nur einmal je Stufe.

WICHTIGE EINSCHRAENKUNG (Lookahead-Bias): Range-Box, Umkehrzonen und
Struktur-Support/-Widerstand sind in mini_daily_gold.py ueber den
KOMPLETTEN Tag berechnet (inkl. Kerzen, die zum Signalzeitpunkt noch gar
nicht bekannt waeren) - fuer ein ehrliches Backtesting duerfen nur bereits
vergangene Kerzen einfliessen. Diese Version nutzt deshalb bewusst NUR
Pivot-Level (aus dem Vortag, also garantiert ohne Zukunftsblick). Range-Box/
Umkehrzonen/Struktur-Support als "expandierende" (nur Vergangenheit
nutzende) Version ist fuer eine V2 vorgesehen, falls diese erste Auswertung
vielversprechend aussieht.

Datenquelle: yfinance (GC=F). 5-Min-Kerzen sind bei Yahoo nur fuer die
letzten ca. 60 Tage verfuegbar (Yahoo-seitige Beschraenkung, nicht
aenderbar) - das ist die Stichprobengroesse dieses Backtests.
"""

import pandas as pd
import numpy as np
import yfinance as yf

TICKER = "GC=F"


def klassische_pivots(high, low, close):
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return {"r": [r1, r2, r3], "s": [s1, s2, s3]}


def hole_daten():
    ticker = yf.Ticker(TICKER)
    intraday = ticker.history(period="60d", interval="5m")
    daily = ticker.history(period="80d", interval="1d")
    return intraday, daily


def baue_pivots_je_tag(daily):
    """Liefert {datum: pivots} - Pivots eines Tages basieren auf dem
    OHLC des JEWEILS VORHERIGEN Handelstages (kein Zukunftsblick)."""
    pivots_je_tag = {}
    daily = daily.sort_index()
    for i in range(1, len(daily)):
        vortag = daily.iloc[i - 1]
        heutiges_datum = daily.index[i].date()
        pivots_je_tag[heutiges_datum] = klassische_pivots(
            float(vortag["High"]), float(vortag["Low"]), float(vortag["Close"])
        )
    return pivots_je_tag


def backtest():
    intraday, daily = hole_daten()
    pivots_je_tag = baue_pivots_je_tag(daily)

    intraday = intraday.sort_index()
    trades = []

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0  # 0 = noch nichts erreicht, 1 = TP1, 2 = TP2
    entry_zeit = None

    for zeit, bar in intraday.iterrows():
        tag = zeit.date()
        pivots = pivots_je_tag.get(tag)
        if pivots is None:
            continue  # kein Vortag verfügbar (erster Tag der Reihe)

        hoch, tief = float(bar["High"]), float(bar["Low"])

        if not in_position:
            # Nächsten Widerstand ÜBER dem aktuellen Kurs suchen (Basis: Bar-Close)
            preis = float(bar["Close"])
            kandidaten = sorted(r for r in pivots["r"] if r > preis)
            if not kandidaten:
                continue
            naechster_r = kandidaten[0]

            if hoch >= naechster_r:
                entry = naechster_r
                stop = naechster_r
                weitere = sorted(r for r in pivots["r"] if r > naechster_r)
                tp1 = weitere[0] if len(weitere) >= 1 else naechster_r * 1.01
                tp2 = weitere[1] if len(weitere) >= 2 else tp1 * 1.005
                in_position = True
                stufe = 0
                entry_zeit = zeit
        else:
            # Stop zuerst prüfen (konservative Annahme, falls Stop und TP im
            # selben 5-Min-Balken beide theoretisch treffbar wären)
            if tief <= stop:
                trades.append({
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "einstieg": entry, "ausstieg": stop,
                    "ergebnis_usd": stop - entry, "ergebnis_pct": (stop - entry) / entry * 100,
                    "stufe_bei_ausstieg": stufe,
                })
                in_position = False
                continue

            if stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    if in_position:
        letzter_preis = float(intraday["Close"].iloc[-1])
        trades.append({
            "einstieg_zeit": entry_zeit, "ausstieg_zeit": intraday.index[-1],
            "einstieg": entry, "ausstieg": letzter_preis,
            "ergebnis_usd": letzter_preis - entry, "ergebnis_pct": (letzter_preis - entry) / entry * 100,
            "stufe_bei_ausstieg": stufe,
            "hinweis": "Backtest endete waehrend offener Position - mit letztem verfuegbaren Kurs geschlossen.",
        })

    return pd.DataFrame(trades)


def main():
    trades_df = backtest()
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_usd"] > 0]
    verlierer = trades_df[trades_df["ergebnis_usd"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()

    print(f"=== Backtest V1: {TICKER}, Long-only, Pivot-Widerstand-Ausbruch ===")
    print(f"Zeitraum: {trades_df['einstieg_zeit'].min()} bis {trades_df['ausstieg_zeit'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"Trade-Log gespeichert: backtest_trades.csv")


if __name__ == "__main__":
    main()
