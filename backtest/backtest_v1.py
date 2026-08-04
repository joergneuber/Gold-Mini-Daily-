"""
Backtest V1: Long-only Gold-Future-Strategie (GC=F)

Regeln (Stand: gemeinsam festgelegt im Gespräch, 03.08.2026):
1. NUR Long-Positionen.
2. Einstieg: Ausbruch über den naechsten Pivot-Widerstand (aus dem
   Vortages-OHLC berechnet - bewusst KEIN Blick in die Zukunft, deshalb
   in V1 NUR Pivot-Basis, noch keine Range-Box/Umkehrzonen - siehe unten).
   ZUSAETZLICH (nach erstem Testlauf ergaenzt): nur wenn der rollierende
   Trend (lineare Regression ueber die letzten 144 Kerzen = ca. 12h) zu
   diesem Zeitpunkt bereits aufwaerts zeigt - filtert Ausbrueche gegen den
   uebergeordneten Trend heraus.
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

Datenquelle: yfinance (GC=F). ZEITRAUM (GEAENDERT, Nutzerwunsch): 1. Januar
bis gestern - dafuer STUNDENKERZEN statt 5-Minuten, da Yahoo 5-Minuten-Daten
nur fuer die letzten ca. 60 Tage kostenlos herausgibt, Stundenkerzen aber bis
zu 2 Jahre zurueck. Trade-off: weniger Praezision beim Einstiegs-/Stop-Preis
(eine Stundenkerze hat oft eine groessere Spanne als eine 5-Minuten-Kerze),
dafuer echte Langzeit-Stichprobe statt nur 60 Tage.

COOLDOWN (NEU, nach Auswertung der V1c/V1d-Ergebnisse ergaenzt): nach jedem
Trade-Ausstieg werden neue Einstiege fuer COOLDOWN_STUNDEN gesperrt - verhindert,
dass nach einem Stop sofort wieder auf demselben/aehnlichem Level eingestiegen
wird (beobachtetes Whipsaw-Muster: 15 "unabhaengige" Trades an einem einzigen Tag).
"""

import pandas as pd
import numpy as np
import yfinance as yf

TICKER = "GC=F"
START_DATUM = "2026-01-01"
COOLDOWN_STUNDEN = 6


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
    intraday = ticker.history(start=START_DATUM, interval="1h")
    # Daily-Reihe braucht etwas Vorlauf vor START_DATUM (für den Vortag am
    # allerersten Backtest-Tag sowie ggf. Trendfenster in anderen Varianten).
    daily = ticker.history(start="2025-11-01", interval="1d")
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


def berechne_trend_richtung(preise, fenster=12):
    """Rollierende Trendrichtung (True = Aufwärtstrend) - Steigung einer
    linearen Regression über die letzten `fenster` Kerzen (144 x 5-Min
    entspricht ca. 12 Stunden, dieselbe Größenordnung wie 'die jüngere
    Hälfte' in mini_daily_gold.py). Nutzt AUSSCHLIESSLICH die letzten
    `fenster` bereits vergangenen Kerzen zu jedem Zeitpunkt - kein
    Zukunftsblick, deshalb sicher fürs Backtesting."""
    def steigung(werte):
        x = np.arange(len(werte))
        m, _ = np.polyfit(x, werte, 1)
        return m
    steigungen = preise.rolling(fenster).apply(steigung, raw=True)
    return steigungen > 0


def backtest():
    intraday, daily = hole_daten()
    pivots_je_tag = baue_pivots_je_tag(daily)

    intraday = intraday.sort_index()
    aufwaertstrend = berechne_trend_richtung(intraday["Close"])
    trades = []

    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0  # 0 = noch nichts erreicht, 1 = TP1, 2 = TP2
    entry_zeit = None
    vorheriger_schluss = None
    cooldown_bis = None

    for zeit, bar in intraday.iterrows():
        tag = zeit.date()
        pivots = pivots_je_tag.get(tag)
        if pivots is None:
            continue  # kein Vortag verfügbar (erster Tag der Reihe)

        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])

        if not in_position:
            # Cooldown: nach einem gerade beendeten Trade für COOLDOWN_STUNDEN
            # keine neuen Einstiege - verhindert sofortiges Wieder-Einsteigen
            # auf demselben/ähnlichem Level (Whipsaw-Schutz).
            if cooldown_bis is not None and zeit < cooldown_bis:
                vorheriger_schluss = schluss
                continue

            # Echte Crossover-Erkennung: Vorheriger Schlusskurs lag UNTER einem
            # Widerstand, aktueller Schlusskurs liegt DARÜBER - erst dann gilt
            # der Ausbruch als bestätigt (nicht schon bei bloßer Docht-Berührung,
            # das reduziert Fehlausbrüche/Whipsaws deutlich).
            # ZUSÄTZLICH (neu): nur handeln, wenn der rollierende Trend zu diesem
            # Zeitpunkt bereits aufwärts zeigt - reine Ausbrüche gegen den
            # übergeordneten Trend (z.B. im Abwärtstrend Ende Mai-Juli) werden
            # so ausgefiltert.
            trend_ok = bool(aufwaertstrend.get(zeit, False))
            if vorheriger_schluss is not None and trend_ok:
                ausgebrochene = sorted(
                    r for r in pivots["r"] if vorheriger_schluss <= r < schluss
                )
                if ausgebrochene:
                    naechster_r = ausgebrochene[0]
                    entry = schluss
                    # Stop = Tief der Ausbruchskerze (nicht exakt am Widerstand) -
                    # sonst wird fast jeder Ausbruch beim typischen kurzen
                    # Rücktest sofort wieder ausgestoppt, ohne echte Chance.
                    stop = tief
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
                cooldown_bis = zeit + pd.Timedelta(hours=COOLDOWN_STUNDEN)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

        vorheriger_schluss = schluss

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
