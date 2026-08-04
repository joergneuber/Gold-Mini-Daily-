"""
Backtest V1d: Long-only Gold-Future-Strategie (GC=F) - TRENDFOLGE + SCALING-OUT

Vierte Variante, direkt auf V1c aufbauend (gleicher Trend-/Liquiditaets-
Einstieg), aber mit einem qualitativ anderen Ausstiegs-Mechanismus:
ECHTE Teilgewinnmitnahme (Scaling-out) statt nur Stop-Nachziehen wie bei
V1/V1b/V1c - abgeleitet aus einer vom Nutzer geteilten Strategie-
Beschreibung (03.08.2026): "Realisieren Sie an vorherigen Widerstaenden
gestaffelte Teilgewinne (z.B. 30-40% der Position), statt alles auf
einen Punkt zu setzen."

Regeln (Einstieg/Trend/Stop identisch zu V1c):
1. NUR Long. Grosser Trend: rollierende Tages-Regression (20 Tage, nur bis
   gestern) muss aufwaerts zeigen.
2. Einstieg: bestaetigter Bounce an einem rollierenden Swing-Tief (36
   Kerzen ~3h, OHNE aktuelle Kerze).
3. Stop: dieses Swing-Tief, FEST.
4. TP1 = Einstieg + 2R, TP2 = Einstieg + 3R (R = Einstieg - Stop).

NEU in V1d - Scaling-out statt Stufenregel:
5. Bei TP1: 35% der Position werden HIER REALISIERT (Gewinn/Verlust fuer
   diesen Teil steht fest), fuer die VERBLEIBENDEN 65% wird der Stop auf
   Breakeven nachgezogen (bleibt also weiter im Markt, ohne Verlustrisiko).
6. Bei TP2 (nur relevant fuer die verbliebenen 65%): komplette Restposition
   wird realisiert.
7. Faellt der Kurs vorher auf den (nachgezogenen) Stop zurueck: Restposition
   wird dort geschlossen.

Der Gesamt-Trade-Ertrag ist der GEWICHTETE Durchschnitt aus beiden Teilen
(35% zum TP1-Ergebnis, 65% zum jeweiligen Ausstiegsergebnis der Restposition).

Datenquelle: yfinance (GC=F). ZEITRAUM (GEAENDERT, Nutzerwunsch): 1. Januar
bis gestern, Stundenkerzen statt 5-Minuten (Yahoo-Limit fuer 5-Min: ~60 Tage).

COOLDOWN (NEU): nach jedem Trade-Ausstieg COOLDOWN_STUNDEN keine neuen
Einstiege - siehe V1c fuer die Begruendung (Whipsaw-Cluster in der Vorversion).
"""

import pandas as pd
import numpy as np
import yfinance as yf

TICKER = "GC=F"
START_DATUM = "2026-01-01"
TAGESTREND_FENSTER = 20
LIQUIDITAET_FENSTER = 3      # Stundenkerzen (~3h), umgerechnet von vorher 36x5min
TEILGEWINN_ANTEIL = 0.35  # 35% der Position bei TP1 realisieren
COOLDOWN_STUNDEN = 6


def hole_daten():
    ticker = yf.Ticker(TICKER)
    intraday = ticker.history(start=START_DATUM, interval="1h")
    daily = ticker.history(start="2025-11-01", interval="1d")
    return intraday, daily


def berechne_tagestrend(daily, fenster=TAGESTREND_FENSTER):
    schluss = daily.sort_index()["Close"]

    def steigung(werte):
        x = np.arange(len(werte))
        m, _ = np.polyfit(x, werte, 1)
        return m

    steigungen = schluss.rolling(fenster).apply(steigung, raw=True).shift(1)
    return {
        datum.date(): bool(wert > 0)
        for datum, wert in steigungen.items() if pd.notna(wert)
    }


def backtest():
    intraday, daily = hole_daten()
    tagestrend = berechne_tagestrend(daily)
    intraday = intraday.sort_index()
    swing_tief_referenz = intraday["Low"].rolling(LIQUIDITAET_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    tp1_realisiert = False
    entry_zeit = None
    tp1_zeit = tp1_ergebnis_pct = None
    cooldown_bis = None

    for zeit, bar in intraday.iterrows():
        tag = zeit.date()
        trend_auf = tagestrend.get(tag)
        if trend_auf is None:
            continue

        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        ref_tief = swing_tief_referenz.get(zeit)

        if not in_position:
            if cooldown_bis is not None and zeit < cooldown_bis:
                continue

            if trend_auf and pd.notna(ref_tief):
                ref_tief = float(ref_tief)
                if tief <= ref_tief and schluss > ref_tief:
                    entry = schluss
                    stop = ref_tief
                    if stop < entry:
                        r = entry - stop
                        tp1 = entry + 2 * r
                        tp2 = entry + 3 * r
                        in_position = True
                        tp1_realisiert = False
                        entry_zeit = zeit
                        tp1_zeit = tp1_ergebnis_pct = None
        else:
            # Stop-Treffer (vor oder nach TP1 möglich)
            if tief <= stop:
                rest_ergebnis_pct = (stop - entry) / entry * 100
                if tp1_realisiert:
                    gesamt_pct = TEILGEWINN_ANTEIL * tp1_ergebnis_pct + (1 - TEILGEWINN_ANTEIL) * rest_ergebnis_pct
                else:
                    gesamt_pct = rest_ergebnis_pct
                trades.append({
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "einstieg": entry, "tp1_realisiert": tp1_realisiert,
                    "tp1_zeit": tp1_zeit, "rest_ausstieg": stop,
                    "gesamt_ergebnis_pct": gesamt_pct,
                })
                in_position = False
                cooldown_bis = zeit + pd.Timedelta(hours=COOLDOWN_STUNDEN)
                continue

            # TP2 (nur relevant, wenn TP1 schon realisiert ist - Restposition voll geschlossen)
            if tp1_realisiert and hoch >= tp2:
                rest_ergebnis_pct = (tp2 - entry) / entry * 100
                gesamt_pct = TEILGEWINN_ANTEIL * tp1_ergebnis_pct + (1 - TEILGEWINN_ANTEIL) * rest_ergebnis_pct
                trades.append({
                    "einstieg_zeit": entry_zeit, "ausstieg_zeit": zeit,
                    "einstieg": entry, "tp1_realisiert": tp1_realisiert,
                    "tp1_zeit": tp1_zeit, "rest_ausstieg": tp2,
                    "gesamt_ergebnis_pct": gesamt_pct,
                })
                in_position = False
                continue

            # TP1 erreicht: 35% realisieren, Stop für den Rest auf Breakeven
            if not tp1_realisiert and hoch >= tp1:
                tp1_realisiert = True
                tp1_zeit = zeit
                tp1_ergebnis_pct = (tp1 - entry) / entry * 100
                stop = max(stop, entry)  # Breakeven für die Restposition

    if in_position:
        letzter_preis = float(intraday["Close"].iloc[-1])
        rest_ergebnis_pct = (letzter_preis - entry) / entry * 100
        if tp1_realisiert:
            gesamt_pct = TEILGEWINN_ANTEIL * tp1_ergebnis_pct + (1 - TEILGEWINN_ANTEIL) * rest_ergebnis_pct
        else:
            gesamt_pct = rest_ergebnis_pct
        trades.append({
            "einstieg_zeit": entry_zeit, "ausstieg_zeit": intraday.index[-1],
            "einstieg": entry, "tp1_realisiert": tp1_realisiert,
            "tp1_zeit": tp1_zeit, "rest_ausstieg": letzter_preis,
            "gesamt_ergebnis_pct": gesamt_pct,
            "hinweis": "Backtest endete waehrend offener Position - mit letztem verfuegbaren Kurs geschlossen.",
        })

    return pd.DataFrame(trades)


def main():
    trades_df = backtest()
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_v1d_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["gesamt_ergebnis_pct"] > 0]
    verlierer = trades_df[trades_df["gesamt_ergebnis_pct"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["gesamt_ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["gesamt_ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["gesamt_ergebnis_pct"].sum()
    anteil_mit_teilgewinn = trades_df["tp1_realisiert"].mean() * 100

    print(f"=== Backtest V1d: {TICKER}, Long-only, Trendfolge + Scaling-out ({int(TEILGEWINN_ANTEIL*100)}% bei TP1) ===")
    print(f"Zeitraum: {trades_df['einstieg_zeit'].min()} bis {trades_df['ausstieg_zeit'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Davon mit realisiertem Teilgewinn (TP1 erreicht): {anteil_mit_teilgewinn:.1f}%")
    print(f"Trefferquote (Gesamt-Trade positiv): {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"Trade-Log gespeichert: backtest_v1d_trades.csv")


if __name__ == "__main__":
    main()
