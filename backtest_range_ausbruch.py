"""
Backtest Range-Ausbruch 1h – drei TP-Varianten für MINI DAILY GOLD

Gemeinsame Entry-/Stop-Regeln:
- Long-only
- Entry: bestätigter 1h-Schlusskurs über dem rollierenden 24h-Hoch
- Stop: rollierendes 24h-Tief zum Entry
- Stop-Abstand > 0,60 %: Trade wird abgelehnt (Stop wird nicht verschoben)
- Cooldown nach Stop: 12 Stunden
- TP1 erreicht -> Stop auf Breakeven
- TP2 erreicht -> Stop auf TP1
- danach Stop am aktuellen 24h-Tief nachziehen

TP-Varianten:
A = Referenz: TP1=2R, TP2=3R
B = Charttechnisch: TP1=erste bestätigte 1h-Widerstandszone >= 1R,
    TP2=darauffolgende bestätigte 1h-Widerstandszone
C = Hybrid: TP1=erste bestätigte 1h-Widerstandszone >= 1R,
    TP2=max(darauffolgende bestätigte 1h-Widerstandszone, 3R)

WICHTIG: Widerstände werden ohne Lookahead bestimmt. Ein Swing-High wird erst
fenster Stunden nach seiner Bildung als bestätigt betrachtet. Für einen Entry
werden nur bis zum Entry-Zeitpunkt bestätigte Swing-Highs verwendet.
"""

import os
import time
from datetime import date, timedelta
import requests
import pandas as pd
import numpy as np

TWELVEDATA_BASIS_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
INTERVALL = "1h"
START_DATUM = date(2019, 1, 1)
CHUNK_TAGE = 180
RANGE_FENSTER = 24
COOLDOWN_STUNDEN = 12
MAX_STOP_ABSTAND_PCT = 0.60

# Charttechnische TP-Parameter
SWING_FENSTER = 3              # 3 links + 3 rechts; erst danach bestätigt
WIDERSTAND_BUCKET_USD = 5.0
WIDERSTAND_MIN_TREFFER = 2
MIN_TP1_R = 1.0


def hole_api_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError("TWELVEDATA_API_KEY nicht gesetzt.")
    return key


def hole_ausschnitt(api_key, start, ende, max_versuche=4):
    for versuch in range(1, max_versuche + 1):
        try:
            antwort = requests.get(
                TWELVEDATA_BASIS_URL,
                params={
                    "symbol": SYMBOL, "interval": INTERVALL, "apikey": api_key,
                    "timezone": "UTC", "order": "ASC",
                    "start_date": start.isoformat(), "end_date": ende.isoformat(),
                    "outputsize": 5000,
                },
                timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            wartezeit = min(30 * versuch, 90)
            print(f"  Netzwerkfehler {start} bis {ende} (Versuch {versuch}/{max_versuche}): {exc}")
            if versuch < max_versuche:
                print(f"  Warte {wartezeit}s und versuche erneut...")
                time.sleep(wartezeit)
                continue
            return pd.DataFrame()

        try:
            daten = antwort.json()
        except ValueError:
            antwort.raise_for_status()
            raise RuntimeError(f"Unerwartete Antwort ohne JSON-Body: {antwort.text[:300]}")

        if antwort.status_code == 429:
            wartezeit = 65
            print(f"  Rate-Limit bei {start} bis {ende} (Versuch {versuch}/{max_versuche}) - warte {wartezeit}s...")
            time.sleep(wartezeit)
            continue
        if antwort.status_code != 200 or daten.get("status") == "error":
            print(f"  Kein Ausschnitt {start} bis {ende} (HTTP {antwort.status_code}): {daten.get('message', daten)}")
            return pd.DataFrame()
        if "values" not in daten:
            return pd.DataFrame()

        df = pd.DataFrame(daten["values"])
        df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        for spalte in ("Open", "High", "Low", "Close"):
            df[spalte] = df[spalte].astype(float)
        return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

    return pd.DataFrame()


def hole_daten():
    api_key = hole_api_key()
    heute = date.today()
    teile = []
    fenster_start = START_DATUM
    while fenster_start < heute:
        fenster_ende = min(fenster_start + timedelta(days=CHUNK_TAGE - 1), heute)
        print(f"Hole {fenster_start} bis {fenster_ende}...")
        teil = hole_ausschnitt(api_key, fenster_start, fenster_ende)
        if not teil.empty:
            teile.append(teil)
        fenster_start = fenster_ende + timedelta(days=1)
        time.sleep(8)
    if not teile:
        raise RuntimeError("Keine Daten von Twelve Data erhalten - Tarif/Key prüfen.")
    stunden = pd.concat(teile)
    return stunden[~stunden.index.duplicated()].sort_index()


def bestaetigte_swing_highs(stunden):
    """DataFrame der bestätigten Swing-Highs.

    Für einen Pivot an Position i braucht es SWING_FENSTER Bars rechts davon.
    Der Pivot ist daher erst ab i+SWING_FENSTER bekannt – kein Lookahead.
    """
    high = stunden["High"].to_numpy()
    idx = stunden.index
    bestaetigt = []
    f = SWING_FENSTER
    for i in range(f, len(stunden) - f):
        if high[i] >= np.max(high[i-f:i+f+1]):
            bestaetigungs_index = i + f
            bestaetigt.append((idx[bestaetigungs_index], float(high[i])))
    return pd.DataFrame(bestaetigt, columns=["bekannt_ab", "preis"]).set_index("bekannt_ab") if bestaetigt else pd.DataFrame(columns=["preis"])


def widerstaende_bis_zeitpunkt(swing_highs, zeit, entry, min_preis=None):
    """Clustert nur bestätigte Swing-Highs, die zum Zeitpunkt `zeit` bekannt waren.
    Eine Zone braucht mindestens WIDERSTAND_MIN_TREFFER Berührungen im 5-USD-Bucket.
    """
    if swing_highs.empty:
        return []
    bekannte = swing_highs.loc[swing_highs.index <= zeit, "preis"]
    bekannte = bekannte[bekannte > (min_preis if min_preis is not None else entry)]
    if bekannte.empty:
        return []

    buckets = {}
    for preis in bekannte:
        key = round(float(preis) / WIDERSTAND_BUCKET_USD) * WIDERSTAND_BUCKET_USD
        buckets.setdefault(key, []).append(float(preis))
    zonen = []
    for werte in buckets.values():
        if len(werte) >= WIDERSTAND_MIN_TREFFER:
            zonen.append((float(np.mean(werte)), len(werte)))
    zonen.sort(key=lambda x: x[0])
    return zonen


def bestimme_tps(variant, entry, stop, widerstaende):
    r = entry - stop
    tp1_2r = entry + 2 * r
    tp2_3r = entry + 3 * r

    if variant == "A":
        return tp1_2r, tp2_3r, "2R/3R"

    # Erste Widerstandszone mindestens 1R oberhalb Entry.
    min_tp1 = entry + MIN_TP1_R * r
    kandidaten = [(p, t) for p, t in widerstaende if p >= min_tp1]
    if not kandidaten:
        return None, None, "kein_geeigneter_widerstand"

    tp1 = kandidaten[0][0]
    rest = [(p, t) for p, t in kandidaten[1:] if p > tp1]
    if not rest:
        return None, None, "kein_2_widerstand"

    naechster = rest[0][0]
    if variant == "B":
        tp2 = naechster
    elif variant == "C":
        tp2 = max(naechster, tp2_3r)
    else:
        raise ValueError(variant)

    if tp2 <= tp1:
        return None, None, "tp2_nicht_oberhalb_tp1"
    return tp1, tp2, "charttechnisch"


def backtest(stunden, variant, swing_highs):
    range_hoch_referenz = stunden["High"].rolling(RANGE_FENSTER).max().shift(1)
    range_tief_referenz = stunden["Low"].rolling(RANGE_FENSTER).min().shift(1)

    trades = []
    in_position = False
    entry = stop = tp1 = tp2 = None
    initial_stop = None
    stufe = 0
    entry_zeit = None
    cooldown_bis = None
    tp_typ = None

    for zeit, bar in stunden.iterrows():
        hoch, tief, schluss = map(float, (bar["High"], bar["Low"], bar["Close"]))
        ref_hoch = range_hoch_referenz.get(zeit)
        ref_tief = range_tief_referenz.get(zeit)

        if not in_position:
            if cooldown_bis is not None and zeit < cooldown_bis:
                continue
            if pd.notna(ref_hoch) and pd.notna(ref_tief) and schluss > float(ref_hoch):
                entry = schluss
                stop = float(ref_tief)
                initial_stop = stop
                if stop >= entry:
                    continue
                risiko_pct = (entry - stop) / entry * 100
                if risiko_pct > MAX_STOP_ABSTAND_PCT:
                    continue

                if variant == "A":
                    tp1, tp2, tp_typ = bestimme_tps("A", entry, stop, [])
                else:
                    wz = widerstaende_bis_zeitpunkt(swing_highs, zeit, entry)
                    tp1, tp2, tp_typ = bestimme_tps(variant, entry, stop, wz)
                    if tp1 is None or tp2 is None:
                        # Setup wird verworfen, wenn charttechnisch kein sauberer
                        # TP1/TP2-Aufbau vorhanden ist. Kein künstliches 2R-Ersatz-Ziel.
                        continue

                in_position = True
                stufe = 0
                entry_zeit = zeit
        else:
            # Nach TP2: Stop laufend am bestätigten 24h-Tief nachziehen.
            if stufe == 2 and pd.notna(ref_tief):
                stop = max(stop, float(ref_tief))

            # Konservative Intrabar-Reihenfolge wie im bisherigen Backtest:
            # Stop wird zuerst geprüft, wenn Stop und Ziel in derselben Kerze liegen.
            if tief <= stop:
                ausstieg = stop
                trades.append({
                    "variant": variant,
                    "tp_typ": tp_typ,
                    "einstieg_zeit": entry_zeit,
                    "ausstieg_zeit": zeit,
                    "haltedauer_stunden": (zeit-entry_zeit).total_seconds()/3600,
                    "einstieg": entry, "stop_initial": initial_stop,
                    "tp1": tp1, "tp2": tp2,
                    "ausstieg": ausstieg,
                    "ergebnis_pct": (ausstieg-entry)/entry*100,
                    "stufe_bei_ausstieg": stufe,
                    "ergebnis_R": ((ausstieg-entry)/(entry-(float(range_tief_referenz.loc[entry_zeit])))) if pd.notna(range_tief_referenz.get(entry_zeit)) else np.nan,
                })
                in_position = False
                cooldown_bis = zeit + pd.Timedelta(hours=COOLDOWN_STUNDEN)
            elif stufe < 2 and hoch >= tp2:
                stufe = 2
                stop = max(stop, tp1)
            elif stufe < 1 and hoch >= tp1:
                stufe = 1
                stop = max(stop, entry)

    if in_position:
        letzter_preis = float(stunden["Close"].iloc[-1])
        letzte_zeit = stunden.index[-1]
        trades.append({
            "variant": variant, "tp_typ": tp_typ,
            "einstieg_zeit": entry_zeit, "ausstieg_zeit": letzte_zeit,
            "haltedauer_stunden": (letzte_zeit-entry_zeit).total_seconds()/3600,
            "einstieg": entry, "stop_initial": initial_stop,
            "tp1": tp1, "tp2": tp2, "ausstieg": letzter_preis,
            "ergebnis_pct": (letzter_preis-entry)/entry*100,
            "stufe_bei_ausstieg": stufe,
            "hinweis": "Backtest endete waehrend offener Position",
        })

    return pd.DataFrame(trades)


def statistik(df):
    if df.empty:
        return {"trades": 0, "trefferquote": 0, "summe": 0, "avg": 0, "avg_gew": 0, "avg_verl": 0,
                "tp1": 0, "tp2": 0, "stop": 0, "breakeven": 0}
    gew = df[df.ergebnis_pct > 0]
    verl = df[df.ergebnis_pct < 0]
    return {
        "trades": len(df),
        "trefferquote": len(gew)/len(df)*100,
        "summe": df.ergebnis_pct.sum(),
        "avg": df.ergebnis_pct.mean(),
        "avg_gew": gew.ergebnis_pct.mean() if len(gew) else 0,
        "avg_verl": verl.ergebnis_pct.mean() if len(verl) else 0,
        "tp1": int((df.stufe_bei_ausstieg == 1).sum()),
        "tp2": int((df.stufe_bei_ausstieg == 2).sum()),
        "stop": int((df.stufe_bei_ausstieg == 0).sum()),
        "breakeven": int((df.ergebnis_pct.abs() < 0.01).sum()),
    }


def main():
    stunden = hole_daten()
    print(f"\n{len(stunden)} Stundenkerzen geladen, {stunden.index.min()} bis {stunden.index.max()}")
    stunden.to_csv("range_ausbruch_stundendaten_roh.csv")
    swing_highs = bestaetigte_swing_highs(stunden)
    print(f"Bestätigte Swing-Highs für charttechnische TPs: {len(swing_highs)}")

    alle = []
    ergebnisse = {}
    labels = {"A": "A – 2R/3R (Referenz)", "B": "B – Charttechnisch", "C": "C – Hybrid"}
    for variant in ("A", "B", "C"):
        df = backtest(stunden, variant, swing_highs)
        ergebnisse[variant] = df
        if not df.empty:
            df.to_csv(f"backtest_range_ausbruch_{variant}.csv", index=False)
            alle.append(df)
        s = statistik(df)
        print(f"\n=== {labels[variant]} ===")
        print(f"Trades: {s['trades']}")
        print(f"Trefferquote: {s['trefferquote']:.1f}%")
        print(f"Summe: {s['summe']:+.2f}%")
        print(f"Ø Trade: {s['avg']:+.2f}%")
        print(f"Ø Gewinner: {s['avg_gew']:+.2f}% | Ø Verlierer: {s['avg_verl']:+.2f}%")
        print(f"Stop/BE: {s['stop']}/{s['breakeven']} | TP1: {s['tp1']} | TP2: {s['tp2']}")

    if alle:
        vergleich = []
        for variant in ("A", "B", "C"):
            df = ergebnisse.get(variant, pd.DataFrame())
            s = statistik(df)
            s["variant"] = labels[variant]
            vergleich.append(s)
        pd.DataFrame(vergleich).to_csv("backtest_range_ausbruch_3varianten_vergleich.csv", index=False)
        # Kompatibilitätsdatei für den bisherigen Workflow/Artifact-Namen: Referenz A.
        ergebnisse["A"].to_csv("backtest_range_ausbruch_trades.csv", index=False)

    print("\nDateien gespeichert:")
    print("- backtest_range_ausbruch_A.csv")
    print("- backtest_range_ausbruch_B.csv")
    print("- backtest_range_ausbruch_C.csv")
    print("- backtest_range_ausbruch_3varianten_vergleich.csv")


if __name__ == "__main__":
    main()
