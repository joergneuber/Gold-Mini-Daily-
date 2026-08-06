"""
Backtest Mean-Reversion (XAU/USD, Tagesbasis) - Twelve Data
--------------------------------------------------------------
Hintergrund: V1e und Range-Ausbruch sind beide Trendfolge-/Ausbruchssysteme -
sie kaufen, wenn der Kurs in eine Richtung weiterläuft. Beide haben dieselbe
strukturelle Schwäche in einer Seitwärtsphase (viele False Breakouts, viele
kleine Stopps hintereinander). Dieses dritte System soll genau diese Lücke
abdecken: kaufen nahe einem mehrfach bestätigten Tief INNERHALB einer Range,
statt auf einen Ausbruch zu warten - und zwar NUR dann, wenn gar kein klarer
Trend vorliegt, damit es sich nicht mit V1e überschneidet (das explizit einen
Aufwärtstrend braucht).

Wie bei den anderen beiden Systemen: erst hier gegen echte Historie testen,
dann erst über die Aufnahme ins Briefing entscheiden - nicht ungeprüft
einbauen.

Regeln (Vorschlag, noch nicht bestätigt - genau deshalb dieser Backtest):
1. Nur Long. Seitwärts-Filter: |Steigung der 50-Tage-Regression| unter
   SEITWAERTS_STEIGUNG_SCHWELLE (dieselbe Regressionsmethode wie beim
   V1e-Trendfilter, hier aber als "kein klarer Trend" statt "Aufwärtstrend").
   Neue Einstiege NUR wenn diese Bedingung erfüllt ist.
2. Referenz-Tief: rollierendes Tief der letzten SUPPORT_FENSTER Handelstage
   (nur bis gestern, kein Lookahead).
3. Einstieg: Tagestief berührt/unterschreitet dieses Referenz-Tief, Schluss
   aber wieder darüber (Bounce-Bestätigung, wie beim V1e-Einstieg, aber ohne
   Trendbedingung - dafür mit dem Seitwärts-Filter aus Punkt 1).
4. Stop: das Referenz-Tief selbst, fest.
5. TP1/TP2 = 1,5R/2,5R (bewusst enger als bei den Trendfolge-Systemen 2R/3R -
   das Ziel ist der obere Rand der Range, nicht ein neuer Trend). Stufenregel
   wie bei V1e: TP1 -> Breakeven, TP2 -> TP1-Niveau, danach kontinuierlich am
   Referenz-Tief nachgezogen.
6. Cooldown: COOLDOWN_TAGE Handelstage nach einem Stop.

Enthält denselben Volatilitätsfilter-Schalter wie im Hauptskript
(mini_daily_gold.py) und im Range-Ausbruch-Backtest (VOLA_FILTER_AKTIV) - hier
standardmäßig AUS, damit erst die reine Regelwirkung sichtbar wird, bevor der
Filter mit hineingerechnet wird. Zum Vergleichen einfach auf True setzen und
den Lauf wiederholen.

Datenquelle: Twelve Data Time-Series API (https://twelvedata.com), Symbol
XAU/USD, Tagesintervall. Erwartet TWELVEDATA_API_KEY als Umgebungsvariable
(gleicher Key wie im Mini-Daily-Gold-Projekt, als GitHub Secret hinterlegen).
"""

import os
import time
from datetime import date
import requests
import pandas as pd
import numpy as np

TWELVEDATA_BASIS_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
START_DATUM = "2019-01-01"  # so weit zurück wie möglich - der Tarif entscheidet, wie viel ankommt

SEITWAERTS_STEIGUNG_SCHWELLE = 0.05  # |50-Tage-Regressionssteigung| / Kurs, darunter gilt "kein klarer Trend"
SEITWAERTS_TREND_FENSTER = 50
SUPPORT_FENSTER = 10
COOLDOWN_TAGE = 3

# Derselbe Volatilitätsfilter-Mechanismus wie in mini_daily_gold.py: blockiert
# NEUE Einstiege (nicht bereits offene Positionen), wenn ATR(kurz) deutlich
# über ATR(lang) liegt. Standardmäßig AUS - siehe Docstring oben.
VOLA_FILTER_AKTIV = False
VOLA_SCHWELLE = 1.8
VOLA_FENSTER_KURZ = 14
VOLA_FENSTER_LANG = 100


def hole_api_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY nicht gesetzt. Gleichen Key wie im Mini-Daily-Gold-Projekt "
            "verwenden und als GitHub Secret hinterlegen."
        )
    return key


def hole_taeglich(max_versuche=4):
    """Holt die komplette Tages-Historie in EINER Anfrage (Twelve Data erlaubt
    bis zu outputsize=5000 - für Tagesdaten seit 2019 reicht das locker, ganz
    anders als bei den Stundenkerzen im Range-Ausbruch-Backtest, wo dafür
    mehrere Anfragen nötig waren). Bei HTTP 429 (Rate-Limit) wird gewartet und
    erneut versucht, statt abzubrechen."""
    api_key = hole_api_key()
    for versuch in range(1, max_versuche + 1):
        antwort = requests.get(
            TWELVEDATA_BASIS_URL,
            params={
                "symbol": SYMBOL, "interval": "1day", "apikey": api_key,
                "timezone": "UTC", "order": "ASC",
                "start_date": START_DATUM, "end_date": date.today().isoformat(),
                "outputsize": 5000,
            },
            timeout=20,
        )
        try:
            daten = antwort.json()
        except ValueError:
            antwort.raise_for_status()
            raise RuntimeError(f"Unerwartete Antwort ohne JSON-Body: {antwort.text[:300]}")

        if antwort.status_code == 429:
            wartezeit = 65
            print(f"  Rate-Limit (Versuch {versuch}/{max_versuche}) - warte {wartezeit}s und versuche es erneut...")
            time.sleep(wartezeit)
            continue

        if antwort.status_code != 200 or daten.get("status") == "error":
            raise RuntimeError(f"Twelve-Data-Fehler (HTTP {antwort.status_code}): {daten.get('message', daten)}")
        if "values" not in daten:
            raise RuntimeError(f"Keine Werte in der Antwort: {daten}")

        df = pd.DataFrame(daten["values"])
        df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        for spalte in ("Open", "High", "Low", "Close"):
            df[spalte] = df[spalte].astype(float)
        df = df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

        # Wochenend-Zeilen raus, aus denselben Datenqualitätsgründen wie bei
        # V1e (siehe mini_daily_gold.py, hole_zeitreihe_taeglich()).
        vor_filter = len(df)
        df = df[df.index.dayofweek < 5]
        entfernt = vor_filter - len(df)
        if entfernt:
            print(f"Wochenend-Zeilen entfernt: {entfernt} von {vor_filter}")
        return df

    raise RuntimeError(f"Twelve-Data-Rate-Limit nach {max_versuche} Versuchen nicht überwunden.")


def berechne_atr(daten, fenster):
    hoch, tief, schluss_vortag = daten["High"], daten["Low"], daten["Close"].shift(1)
    true_range = pd.concat([
        hoch - tief,
        (hoch - schluss_vortag).abs(),
        (tief - schluss_vortag).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(fenster).mean()


def berechne_vola_erlaubt(daten):
    atr_kurz = berechne_atr(daten, VOLA_FENSTER_KURZ)
    atr_lang = berechne_atr(daten, VOLA_FENSTER_LANG)
    return ((atr_kurz / atr_lang) <= VOLA_SCHWELLE).shift(1)


def backtest(daily):
    def steigung_normiert(fenster_werte):
        x = np.arange(len(fenster_werte))
        m, _ = np.polyfit(x, fenster_werte, 1)
        return m / fenster_werte[-1]  # auf den Kurs normiert, damit die Schwelle über Kursniveaus hinweg vergleichbar bleibt

    steigung = daily["Close"].rolling(SEITWAERTS_TREND_FENSTER).apply(steigung_normiert, raw=True).shift(1)
    seitwaerts = steigung.abs() < SEITWAERTS_STEIGUNG_SCHWELLE
    referenz_tief = daily["Low"].rolling(SUPPORT_FENSTER).min().shift(1)
    vola_erlaubt = berechne_vola_erlaubt(daily)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    stufe = 0
    entry_datum = None
    cooldown_bis = None

    for datum, bar in daily.iterrows():
        hoch, tief, schluss = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        ist_seitwaerts = seitwaerts.get(datum)
        ref_tief = referenz_tief.get(datum)
        vola_ok = (not VOLA_FILTER_AKTIV) or bool(vola_erlaubt.get(datum, False))

        if not in_position:
            if cooldown_bis is not None and datum < cooldown_bis:
                continue
            if pd.notna(ist_seitwaerts) and ist_seitwaerts and pd.notna(ref_tief) and vola_ok:
                ref_tief = float(ref_tief)
                if tief <= ref_tief and schluss > ref_tief:
                    entry = schluss
                    stop = ref_tief
                    if stop < entry:
                        r = entry - stop
                        tp1 = entry + 1.5 * r
                        tp2 = entry + 2.5 * r
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
    print(f"Volatilitätsfilter: {'AKTIV' if VOLA_FILTER_AKTIV else 'AUS'}")
    daily = hole_taeglich()
    print(f"\n{len(daily)} Tageskerzen geladen, {daily.index.min().date()} bis {daily.index.max().date()}")
    daily.to_csv("mean_reversion_tagesdaten_roh.csv")

    trades_df = backtest(daily)
    if trades_df.empty:
        print("Keine Trades im Backtest-Zeitraum gefunden.")
        return

    trades_df.to_csv("backtest_mean_reversion_trades.csv", index=False)

    n = len(trades_df)
    gewinner = trades_df[trades_df["ergebnis_pct"] > 0]
    verlierer = trades_df[trades_df["ergebnis_pct"] <= 0]
    trefferquote = len(gewinner) / n * 100
    avg_gewinn = gewinner["ergebnis_pct"].mean() if len(gewinner) else 0
    avg_verlust = verlierer["ergebnis_pct"].mean() if len(verlierer) else 0
    summe_pct = trades_df["ergebnis_pct"].sum()
    avg_haltedauer = trades_df["haltedauer_tage"].mean()

    print(f"\n=== Backtest Mean-Reversion (XAU/USD, Tagesbasis) ===")
    print(f"Zeitraum: {trades_df['einstieg_datum'].min()} bis {trades_df['ausstieg_datum'].max()}")
    print(f"Anzahl Trades: {n}")
    print(f"Ø Haltedauer: {avg_haltedauer:.1f} Tage")
    print(f"Trefferquote: {trefferquote:.1f}%")
    print(f"Ø Gewinn (Gewinner): {avg_gewinn:+.2f}%")
    print(f"Ø Verlust (Verlierer): {avg_verlust:+.2f}%")
    print(f"Summe aller Trades: {summe_pct:+.2f}%")
    print(f"\nZum Vergleich V1e (Spot, live in mini_daily_gold.py): 43 Trades seit 2019, "
          f"Trefferquote 26%, Summe +13,89% (Stand 05.08.2026)")
    print(f"Zum Vergleich Range-Ausbruch (Spot, 1h): 144 Trades 24.01.2020-05.08.2026, "
          f"Trefferquote 32,6%, Summe +110,82%")
    print(f"Trade-Log gespeichert: backtest_mean_reversion_trades.csv")


if __name__ == "__main__":
    main()
