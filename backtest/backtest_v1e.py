"""
Backtest V1e: Long-only Gold-Future-Strategie (GC=F) - POSITIONSTRADING (Tagesbasis)

Fuenfte Variante - anders als V1/V1b/V1c/V1d (alle Intraday, Halteperiode
Stunden bis maximal ein paar Tage): diese Version arbeitet komplett auf
TAGES-Kerzen, Halteperiode bewusst mehrere Tage bis Wochen. Grosser Vorteil:
yfinance hat fuer Tagesdaten KEINE 60-Tage-Grenze wie bei Intraday-Aufloesungen
- wir koennen mehrere Jahre zurueckgehen, damit eine deutlich robustere
Stichprobe bekommen als bei den Intraday-Varianten (die alle im selben
7-Monats-Zeitraum negativ waren).

Regeln:
1. NUR Long-Positionen.
2. TREND: rollierende lineare Regression ueber die letzten 50 TAGESSCHLUSS-
   KURSE (shift(1) - nur bis einschliesslich GESTERN, kein Zukunftsblick).
   Nur wenn aufwaerts, werden Einstiege ueberhaupt in Betracht gezogen.
3. EINSTIEG: rollierendes 10-Tage-Swing-Tief (shift(1), OHNE den aktuellen
   Tag). Einstieg, wenn die Tageskerze dieses Tief beruehrt/unterschreitet,
   aber der TAGESSCHLUSS wieder DARUEBER liegt (bestaetigter Bounce).
4. STOP: dieses Swing-Tief, FEST.
5. TP1 = Einstieg + 2R, TP2 = Einstieg + 3R (R = Einstieg - Stop).
6. Stufenregel (identisch zu allen anderen Varianten):
   - TP1 erreicht -> Stop auf Breakeven
   - TP2 erreicht -> Stop auf TP1-Niveau
7. COOLDOWN: 3 Handelstage nach einem Stop-Ausstieg keine neuen Einstiege
   (gleiche Lehre wie bei den Intraday-Varianten - verhindert sofortiges
   Wieder-Einsteigen auf demselben Level).

Datenquelle: yfinance (GC=F), Tageskerzen über mehrere Jahre.
"""

import pandas as pd
import numpy as np
import yfinance as yf

TICKER = "GC=F"
START_DATUM = "2019-01-01"   # mehrere Jahre Tagesdaten möglich (kein Yahoo-Limit)
TREND_FENSTER = 50           # Handelstage für die Trendrichtung
SWING_FENSTER = 10           # Handelstage für das Swing-Tief
COOLDOWN_TAGE = 3


def hole_daten():
    ticker = yf.Ticker(TICKER)
    daily = ticker.history(start=START_DATUM, interval="1d")
    return daily.sort_index()


def berechne_trend(schluss, fenster=TREND_FENSTER):
    def steigung(werte):
        x = np.arange(len(werte))
        m, _ = np.polyfit(x, werte, 1)
        return m
    return schluss.rolling(fenster).apply(steigung, raw=True).shift(1) > 0


def backtest():
    daily = hole_daten()
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
    trades_df = backtest()
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_v1e_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_pct"] > 0]
    verlierer = trades_df[trades_df["ergebnis_pct"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()
    avg_haltedauer = trades_df["haltedauer_tage"].mean()

    print(f"=== Backtest V1e: {TICKER}, Long-only, Positionstrading (Tagesbasis) ===")
    print(f"Zeitraum: {trades_df['einstieg_datum'].min()} bis {trades_df['ausstieg_datum'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Ø Haltedauer: {avg_haltedauer:.1f} Tage")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"Trade-Log gespeichert: backtest_v1e_trades.csv")


if __name__ == "__main__":
    main()
