#!/usr/bin/env python3
# MINI DAILY GOLD – Range-Ausbruch 1h
# TP-Backtest: A / C1 / C2 / C3
#
# A  = TP1 2R, TP2 3R
# C1 = TP1 nächster bestätigter 1h-Widerstand >= 1R,
#      TP2 max(nächster Widerstand, 3R)
# C2 = TP1 nächster bestätigter 1h-Widerstand >= 1.5R,
#      TP2 max(nächster Widerstand, 3R)
# C3 = TP1 nächster bestätigter 1h-Widerstand >= 2R,
#      TP2 max(nächster Widerstand, 3R)
#
# Entry/Stop-Regeln:
# - Long only
# - bestätigter Close über rollierendem 24h-Hoch
# - Stop = 24h-Tief beim Entry
# - Stop-Abstand > 0.60% => Trade wird abgelehnt
# - Nach TP1: Stop auf Break-even
# - Nach TP2: Stop auf TP1; danach 24h-Tief-Trailing
# - 12h Cooldown nach Stop
#
# Das Skript erwartet range_ausbruch_stundendaten_roh.csv im selben
# Verzeichnis bzw. im aktuellen Arbeitsverzeichnis.

from pathlib import Path
import math
import pandas as pd
import numpy as np

MAX_STOP_PCT = 0.006
COOLDOWN_HOURS = 12
CSV_CANDIDATES = [
    Path("range_ausbruch_stundendaten_roh.csv"),
    Path("backtest_results/range_ausbruch_stundendaten_roh.csv"),
]

def load_data():
    path = next((p for p in CSV_CANDIDATES if p.exists()), None)
    if path is None:
        raise FileNotFoundError(
            "range_ausbruch_stundendaten_roh.csv nicht gefunden. "
            "Bitte die vorhandene Rohdaten-Datei ins Repo legen."
        )
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    aliases = {
        "datetime": ["datetime", "date", "timestamp", "time"],
        "open": ["open"],
        "high": ["high"],
        "low": ["low"],
        "close": ["close"],
    }
    rename = {}
    for target, opts in aliases.items():
        for opt in opts:
            if opt in df.columns:
                rename[opt] = target
                break
    df = df.rename(columns=rename)
    required = ["datetime", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Fehlende Spalten: {missing}")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=required).sort_values("datetime").drop_duplicates("datetime")
    return df.reset_index(drop=True)

def confirmed_swing_highs(df, left=2, right=2):
    h = df["high"].to_numpy(float)
    out = []
    for i in range(left, len(df)-right):
        v = h[i]
        if v >= np.max(h[i-left:i]) and v > np.max(h[i+1:i+right+1]):
            out.append((i, v))
    return out

def build_resistance_list(df):
    swings = confirmed_swing_highs(df)
    # A swing becomes usable only after right confirmation candles.
    return [(i, float(v)) for i, v in swings]

def next_resistance(resistances, start_idx, min_price):
    for idx, price in resistances:
        if idx <= start_idx:
            continue
        if price >= min_price:
            return float(price), idx
    return None, None

def simulate(df, variant, resistances):
    trades = []
    i = 24
    cooldown_until = None

    while i < len(df)-2:
        if cooldown_until is not None and df.loc[i, "datetime"] < cooldown_until:
            i += 1
            continue

        # Entry: close breaks above rolling 24h high (previous 24 completed bars).
        prior_high = df.loc[i-24:i-1, "high"].max()
        if not (df.loc[i, "close"] > prior_high):
            i += 1
            continue

        entry = float(df.loc[i, "close"])
        stop = float(df.loc[i-24:i, "low"].min())
        risk = entry - stop
        if risk <= 0:
            i += 1
            continue

        stop_pct = risk / entry
        if stop_pct > MAX_STOP_PCT:
            i += 1
            continue

        r = risk
        if variant == "A":
            tp1 = entry + 2*r
            tp2 = entry + 3*r
        else:
            threshold = {"C1": 1.0, "C2": 1.5, "C3": 2.0}[variant]
            first_min = entry + threshold*r
            r1, _ = next_resistance(resistances, i, first_min)
            if r1 is None:
                # No chart target => fall back to 2R for TP1.
                r1 = entry + 2*r
            tp1 = r1

            second_min = max(tp1, entry + 3*r)
            r2, _ = next_resistance(resistances, i, second_min)
            if r2 is None:
                r2 = entry + 3*r
            tp2 = r2

            # Ensure strict ordering.
            if tp2 <= tp1:
                tp2 = max(entry + 3*r, tp1)

        state = "open"
        current_stop = stop
        tp1_hit = False
        tp2_hit = False
        exit_price = None
        exit_reason = None
        exit_idx = None

        j = i + 1
        while j < len(df):
            row = df.loc[j]
            high = float(row["high"])
            low = float(row["low"])

            # Conservative intrabar ordering: stop first if it was already active,
            # then targets. This avoids assuming favorable ordering inside a bar.
            if low <= current_stop:
                exit_price = current_stop
                exit_reason = "STOP" if not tp1_hit else "BE/STOP"
                exit_idx = j
                break

            if not tp1_hit and high >= tp1:
                tp1_hit = True
                current_stop = entry

            if tp1_hit and not tp2_hit and high >= tp2:
                tp2_hit = True
                current_stop = tp1
                # Continue to allow trailing after TP2.

            if tp2_hit:
                # Trailing at the current 24h low, never below TP1.
                trail = float(df.loc[max(0, j-23):j, "low"].min())
                current_stop = max(tp1, trail)

            j += 1

        if exit_price is None:
            exit_idx = len(df)-1
            exit_price = float(df.loc[exit_idx, "close"])
            exit_reason = "END"

        ret = (exit_price / entry - 1.0) * 100.0

        trades.append({
            "variant": variant,
            "entry_time": df.loc[i, "datetime"],
            "entry": entry,
            "stop_initial": stop,
            "stop_pct": stop_pct * 100,
            "tp1": tp1,
            "tp2": tp2,
            "exit_time": df.loc[exit_idx, "datetime"],
            "exit": exit_price,
            "exit_reason": exit_reason,
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "return_pct": ret,
        })

        if exit_reason in ("STOP", "BE/STOP"):
            cooldown_until = df.loc[exit_idx, "datetime"] + pd.Timedelta(hours=COOLDOWN_HOURS)
        else:
            cooldown_until = None

        i = max(i + 1, exit_idx + 1)

    return pd.DataFrame(trades)

def summarize(t):
    if t.empty:
        return {
            "Trades": 0, "Trefferquote": 0.0, "Summe_%": 0.0,
            "Ø_Trade_%": 0.0, "Ø_Gewinner_%": 0.0, "Ø_Verlierer_%": 0.0,
            "Stop_BE": 0, "TP1": 0, "TP2": 0
        }
    wins = t[t["return_pct"] > 1e-12]
    losses = t[t["return_pct"] < -1e-12]
    return {
        "Trades": len(t),
        "Trefferquote": round(len(wins)/len(t)*100, 1),
        "Summe_%": round(t["return_pct"].sum(), 2),
        "Ø_Trade_%": round(t["return_pct"].mean(), 2),
        "Ø_Gewinner_%": round(wins["return_pct"].mean(), 2) if len(wins) else 0.0,
        "Ø_Verlierer_%": round(losses["return_pct"].mean(), 2) if len(losses) else 0.0,
        "Stop_BE": int(t["exit_reason"].isin(["STOP","BE/STOP"]).sum()),
        "TP1": int(t["tp1_hit"].sum()),
        "TP2": int(t["tp2_hit"].sum()),
    }

def main():
    df = load_data()
    print(f"Stundenkerzen geladen: {len(df)}, {df['datetime'].min()} bis {df['datetime'].max()}")
    resistances = build_resistance_list(df)
    print(f"Bestätigte Swing-Highs für charttechnische TPs: {len(resistances)}")

    all_trades = []
    rows = []

    for variant, label in [
        ("A", "A – 2R/3R (Referenz)"),
        ("C1", "C1 – TP1 Widerstand >= 1R, TP2 max(Widerstand, 3R)"),
        ("C2", "C2 – TP1 Widerstand >= 1.5R, TP2 max(Widerstand, 3R)"),
        ("C3", "C3 – TP1 Widerstand >= 2R, TP2 max(Widerstand, 3R)"),
    ]:
        t = simulate(df, variant, resistances)
        s = summarize(t)
        rows.append({"Variante": label, **s})
        all_trades.append(t)
        print(f"=== {label} ===")
        for k, v in s.items():
            print(f"{k}: {v}")

    out = pd.DataFrame(rows)
    out.to_csv("backtest_range_ausbruch_C1C2C3_vergleich.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_trades, ignore_index=True).to_csv(
        "backtest_range_ausbruch_C1C2C3_trades.csv",
        index=False, encoding="utf-8-sig"
    )
    print("Dateien gespeichert:")
    print("- backtest_range_ausbruch_C1C2C3_vergleich.csv")
    print("- backtest_range_ausbruch_C1C2C3_trades.csv")

if __name__ == "__main__":
    main()
