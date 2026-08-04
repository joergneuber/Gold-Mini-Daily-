"""
Backtest V1c: Long-only Gold-Future-Strategie (GC=F) - TRENDFOLGE + LIQUIDITAET

Dritte Variante neben V1 (Breakout) und V1b (Mean-Reversion an Pivot-S1):
Trendfolge im TAGESCHART kombiniert mit einem Liquiditaets-/Swing-Tief-
Einstieg auf Intraday-Basis - abgeleitet aus einer vom Nutzer geteilten
Strategie-Beschreibung (03.08.2026).

Regeln:
1. NUR Long-Positionen.
2. GROSSER TREND: rollierende lineare Regression ueber die letzten 20
   TAGESSCHLUSSKURSE (nur bis einschliesslich GESTERN - kein Zukunftsblick).
   Nur wenn positiv (Aufwaertstrend im Tageschart), werden Einstiege ueberhaupt
   in Betracht gezogen.
3. LIQUIDITAETSZONE/EINSTIEG: rollierendes Tief der letzten 36 Kerzen (~3h,
   Naeherung an "juengeres wichtiges Tief/Liquiditaetspunkt") - OHNE die
   aktuelle Kerze (shift(1), sonst waere die Referenz zirkulaer). Einstieg,
   wenn die aktuelle Kerze dieses Tief beruehrt/unterschreitet, aber wieder
   DARUEBER schliesst (bestaetigter Bounce).
4. STOP: dieses Tief selbst - FEST (nicht "Tief der Kerze" wie in V1, das
   hatte dort zu stark schwankenden Risikogroessen gefuehrt).
5. TP1 = Einstieg + 2R, TP2 = Einstieg + 3R (R = Einstieg - Stop) - im
   Ausgangstext als Alternative zum Tageshoch explizit genannt, hier gewaehlt
   fuer Konsistenz mit dem R-Vielfachen-System aus positionen_tracker.py.
6. Stufenregel (identisch zu V1/V1b):
   - TP1 erreicht -> Stop auf Breakeven
   - TP2 erreicht -> Stop auf TP1-Niveau
   - Nur verbessern, nie verschlechtern; nur einmal je Stufe.

Datenquelle: yfinance (GC=F). 5-Min-Kerzen ~60 Tage (Yahoo-Limit),
Tagesdaten separat fuer den Trendfilter.
"""

import pandas as pd
import numpy as np
import yfinance as yf

TICKER = "GC=F"
TAGESTREND_FENSTER = 20      # Handelstage für die Trendrichtung
LIQUIDITAET_FENSTER = 36     # 5-Min-Kerzen (~3h) für das Swing-Tief


def hole_daten():
    ticker = yf.Ticker(TICKER)
    intraday = ticker.history(period="60d", interval="5m")
    daily = ticker.history(period="120d", interval="1d")
    return intraday, daily


def berechne_tagestrend(daily, fenster=TAGESTREND_FENSTER):
    """Liefert {datum: True/False}, ob der Tagestrend AN DIESEM TAG aufwärts
    zeigt - berechnet aus den `fenster` Schlusskursen BIS EINSCHLIESSLICH
    GESTERN (shift(1), kein Blick auf den heutigen/aktuellen Tag selbst)."""
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

    # Rollierendes Swing-Tief OHNE die aktuelle Kerze (shift(1)) - reine
    # Vergangenheits-Referenz, kein Zukunftsblick.
    swing_tief_referenz = intraday["Low"].rolling(LIQUIDITAET_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_zeit = None

    for zeit, bar in intraday.iterrows():
        tag = zeit.date()
        trend_auf = tagestrend.get(tag)
        if trend_auf is None:
            continue  # kein Trendwert verfügbar (Anfang der Datenreihe)

        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        ref_tief = swing_tief_referenz.get(zeit)

        if not in_position:
            if trend_auf and pd.notna(ref_tief):
                ref_tief = float(ref_tief)
                # Bounce-Bestätigung: Kerze berührt/unterschreitet das
                # rollierende Swing-Tief, schließt aber wieder darüber.
                if tief <= ref_tief and schluss > ref_tief:
                    entry = schluss
                    stop = ref_tief
                    if stop < entry:
                        r = entry - stop
                        tp1 = entry + 2 * r
                        tp2 = entry + 3 * r
                        in_position = True
                        stufe = 0
                        entry_zeit = zeit
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

    trades_df.to_csv("backtest_v1c_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_usd"] > 0]
    verlierer = trades_df[trades_df["ergebnis_usd"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()

    print(f"=== Backtest V1c: {TICKER}, Long-only, Trendfolge + Liquiditätszone ===")
    print(f"Zeitraum: {trades_df['einstieg_zeit'].min()} bis {trades_df['ausstieg_zeit'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"Trade-Log gespeichert: backtest_v1c_trades.csv")


if __name__ == "__main__":
    main()
