"""
Backtest V1b: Long-only Gold-Future-Strategie (GC=F) - MEAN-REVERSION-Variante

Anders als V1 (Ausbruch/Momentum: Kauf beim Durchbrechen eines Widerstands),
testet diese Version das Gegenteil: Kauf an einer Unterstuetzung, Ziel
Rueckkehr zur Mitte/zum naechsten Widerstand.

Regeln (gemeinsam festgelegt, 03.08.2026):
1. NUR Long-Positionen.
2. Einstieg: Kurs beruehrt/unterschreitet S1 (Pivot-Unterstuetzung), schliesst
   in DERSELBEN Kerze wieder darueber (bestaetigte Umkehr nach oben - Low <= S1,
   aber Close > S1). Pivots stammen aus dem VORTAGES-OHLC (kein Zukunftsblick).
3. Stop: S2 (die naechsttiefere Pivot-Unterstuetzung) - FEST, nicht variabel
   wie in V1 (dort hatte der "Tief-der-Kerze"-Stop zu stark schwankenden
   Risiko-Groessen gefuehrt, siehe Auswertung der V1-Trades).
4. TP1 = Pivot-Punkt (P). TP2 = R1 (naechster Pivot-Widerstand).
5. Stufenregel (identisch zu V1 / positionen_tracker.py):
   - TP1 erreicht -> Stop EINMALIG auf Breakeven (Einstiegskurs)
   - TP2 erreicht -> Stop EINMALIG auf TP1-Niveau (= P)
   - Nur verbessern, nie verschlechtern; nur einmal je Stufe.

Kein Trendfilter in dieser Variante (bewusst) - Mean-Reversion an einer
Unterstuetzung soll gerade AUCH in Abwaertsphasen (kurzfristige Erholungen)
funktionieren koennen, ein Aufwaertstrend-Filter wuerde das Konzept
konterkarieren.

Datenquelle: yfinance (GC=F), 5-Min-Kerzen (~60 Tage Yahoo-Limit).
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
    return {"p": p, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


def hole_daten():
    ticker = yf.Ticker(TICKER)
    intraday = ticker.history(period="60d", interval="5m")
    daily = ticker.history(period="80d", interval="1d")
    return intraday, daily


def baue_pivots_je_tag(daily):
    """Pivots eines Tages aus dem OHLC des JEWEILS VORHERIGEN Handelstages."""
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
    stufe = 0
    entry_zeit = None

    for zeit, bar in intraday.iterrows():
        tag = zeit.date()
        pivots = pivots_je_tag.get(tag)
        if pivots is None:
            continue

        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])

        if not in_position:
            s1 = pivots["s1"]
            # Bestätigte Umkehr an S1: Kerze taucht bei/unter S1, schließt
            # aber wieder darüber (Docht-Umkehr an der Unterstützung).
            if tief <= s1 and schluss > s1:
                entry = schluss
                stop = pivots["s2"]
                tp1 = pivots["p"]
                tp2 = pivots["r1"]
                # Sicherheitscheck: Stop muss tatsächlich unter Einstieg liegen
                # und TP1/TP2 aufsteigend über Einstieg - sonst diesen Setup
                # überspringen (kann bei sehr ungewöhnlichen Pivot-Konstellationen
                # theoretisch vorkommen).
                if stop < entry < tp1 < tp2:
                    in_position = True
                    stufe = 0
                    entry_zeit = zeit
                else:
                    entry = None
        else:
            if tief <= stop:
                trades.append({
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "einstieg": entry, "ausstieg": stop,
                    "ergebnis_usd": stop - entry, "ergebnis_pct": (stop - entry) / entry * 100,
                    "stufe_bei_ausstieg": stufe,
                })
                in_position = False
            elif stufe < 2 and hoch >= tp2:
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

    trades_df.to_csv("backtest_v1b_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_usd"] > 0]
    verlierer = trades_df[trades_df["ergebnis_usd"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()

    print(f"=== Backtest V1b: {TICKER}, Long-only, Mean-Reversion an S1 ===")
    print(f"Zeitraum: {trades_df['einstieg_zeit'].min()} bis {trades_df['ausstieg_zeit'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"Trade-Log gespeichert: backtest_v1b_trades.csv")


if __name__ == "__main__":
    main()
