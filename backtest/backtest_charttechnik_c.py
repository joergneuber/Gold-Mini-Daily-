#!/usr/bin/env python3
"""Historischer Signal-Backtest fuer die neue Charttechnik C.

WICHTIG:
- C wird hier noch NICHT als fertiges Profitabilitaetssystem bewertet.
- Der Backtest prueft die Signal-/Zielqualitaet der neuen Chartkette:
  bestaetigter Entry -> struktureller Stop -> TP1 -> optionales TP2.
- Es werden keine 2R-/3R-Fallbacks erzeugt.
- TP2 wird nur als unmittelbar naechste gueltige Struktur zugelassen.
- Die spaetere Struktur darf bei CRV < 2 nicht uebersprungen werden.
- Fuer jeden historischen Entscheidungszeitpunkt werden nur Daten bis zu
  diesem Zeitpunkt verwendet. Tagesdaten werden waehrend des Intraday-Tages
  bewusst NICHT vorzeitig verwendet.
- Eine wirtschaftliche C-P&L-Auswertung erfolgt erst, wenn die finale
  Positionsverwaltung fuer C festgelegt ist.
"""

import importlib.util
import json
import os
import sys
import time
import types
from datetime import date, timedelta
from pathlib import Path

try:
    from google import genai  # noqa: F401
except Exception:
    google_pkg = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    google_pkg.genai = genai_mod
    sys.modules.setdefault("google", google_pkg)
    sys.modules["google.genai"] = genai_mod

if "economic_events" not in sys.modules:
    economic_events_mod = types.ModuleType("economic_events")
    economic_events_mod.briefing_block = lambda days_ahead=7: ("", [])
    sys.modules["economic_events"] = economic_events_mod

import numpy as np
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNNER_PATH = ROOT / "test" / "Charttechnik" / "run_charttechnik_test.py"
TEST_MODULE_PATH = ROOT / "test" / "Charttechnik" / "mini_daily_gold.py"

SYMBOL = "XAU/USD"
INTERVALL = "1h"
START_DATUM = date.fromisoformat(os.getenv("C_START_DATE", "2019-01-01"))
CHUNK_TAGE = int(os.getenv("C_CHUNK_DAYS", "180"))
WARMUP_TAGE = int(os.getenv("C_WARMUP_DAYS", "180"))
EVAL_EVERY_N_BARS = max(1, int(os.getenv("C_EVAL_EVERY_N_BARS", "1")))
MAX_EVAL_BARS = int(os.getenv("C_MAX_EVAL_BARS", "0"))
HORIZON_BARS = max(24, int(os.getenv("C_HORIZON_BARS", "240")))
API_URL = "https://api.twelvedata.com/time_series"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module("mini_daily_gold_charttechnik_backtest", TEST_MODULE_PATH)
runner = _load_module("charttechnik_runner_backtest", TEST_RUNNER_PATH)


def hole_api_key():
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError("TWELVEDATA_API_KEY nicht gesetzt.")


def hole_ausschnitt(start, ende, max_versuche=4):
    key = os.getenv("TWELVEDATA_API_KEY")
    for versuch in range(1, max_versuche + 1):
        try:
            antwort = requests.get(
                API_URL,
                params={
                    "symbol": SYMBOL,
                    "interval": INTERVALL,
                    "apikey": key,
                    "timezone": "UTC",
                    "order": "ASC",
                    "start_date": start.isoformat(),
                    "end_date": ende.isoformat(),
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            if versuch < max_versuche:
                warte = 10 * versuch
                print(f"Netzwerkfehler {start} bis {ende}: {exc}; warte {warte}s")
                time.sleep(warte)
                continue
            raise

        if antwort.status_code == 429:
            if versuch < max_versuche:
                print(f"Rate-Limit {start} bis {ende}; warte 65s")
                time.sleep(65)
                continue
            raise RuntimeError("Twelve-Data-Rate-Limit konnte nicht aufgeloest werden.")

        antwort.raise_for_status()
        daten = antwort.json()
        if daten.get("status") == "error" or "values" not in daten:
            raise RuntimeError(f"Twelve-Data-Fehler: {daten}")

        df = pd.DataFrame(daten["values"])
        df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.rename(
            columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}
        )
        for spalte in ("Open", "High", "Low", "Close"):
            df[spalte] = pd.to_numeric(df[spalte], errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

    raise RuntimeError("Keine Daten erhalten.")


def hole_daten():
    csv_pfad = os.getenv("C_1H_CSV")
    if csv_pfad:
        df = pd.read_csv(csv_pfad, parse_dates=["datetime"])
        df = df.rename(
            columns={"datetime": "Datum", "open": "Open", "high": "High", "low": "Low", "close": "Close"}
        )
        return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

    hole_api_key()
    heute = date.today()
    teile = []
    start = START_DATUM
    while start < heute:
        ende = min(start + timedelta(days=CHUNK_TAGE - 1), heute)
        print(f"Hole XAU/USD 1h: {start} bis {ende}")
        teil = hole_ausschnitt(start, ende)
        if not teil.empty:
            teile.append(teil)
        start = ende + timedelta(days=1)
        time.sleep(8)

    if not teile:
        raise RuntimeError("Keine XAU/USD-1h-Daten erhalten.")
    df = pd.concat(teile)
    return df[~df.index.duplicated()].sort_index()


def hole_tagesdaten(stunden):
    csv_pfad = os.getenv("C_DAILY_CSV")
    if csv_pfad:
        df = pd.read_csv(csv_pfad, parse_dates=["datetime"])
        df = df.rename(
            columns={"datetime": "Datum", "open": "Open", "high": "High", "low": "Low", "close": "Close"}
        )
        return df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]

    # Produktionsnahe Tagesbasis: gleiche XAU/USD-Quelle, nicht aus 1h
    # resampeln. Dadurch bleiben Tagesgrenzen und OHLC-Definitionen der
    # Produktionsdatenquelle erhalten.
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        raise EnvironmentError("TWELVEDATA_API_KEY nicht gesetzt.")
    start = START_DATUM
    ende = date.today()
    for versuch in range(1, 5):
        try:
            antwort = requests.get(
                API_URL,
                params={
                    "symbol": SYMBOL,
                    "interval": "1day",
                    "apikey": key,
                    "timezone": "UTC",
                    "order": "ASC",
                    "start_date": start.isoformat(),
                    "end_date": ende.isoformat(),
                    "outputsize": 5000,
                },
                timeout=60,
            )
        except requests.RequestException:
            if versuch < 4:
                time.sleep(10 * versuch)
                continue
            raise
        if antwort.status_code == 429:
            if versuch < 4:
                time.sleep(65)
                continue
            raise RuntimeError("Twelve-Data-Rate-Limit bei Tagesdaten.")
        antwort.raise_for_status()
        daten = antwort.json()
        if daten.get("status") == "error" or "values" not in daten:
            raise RuntimeError(f"Twelve-Data-Tagesfehler: {daten}")
        df = pd.DataFrame(daten["values"])
        df["Datum"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.rename(
            columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}
        )
        for spalte in ("Open", "High", "Low", "Close"):
            df[spalte] = pd.to_numeric(df[spalte], errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df.set_index("Datum").sort_index()[["Open", "High", "Low", "Close"]]
        return df[df.index.dayofweek < 5]

    raise RuntimeError("Keine Tagesdaten erhalten.")


def historische_signale(stunden):
    daily = hole_tagesdaten(stunden)
    if len(daily) < 180:
        raise RuntimeError("Zu wenig Tageshistorie fuer die 6M-Struktur.")

    start_index = max(1, int(len(stunden) * 0))
    warmup_ts = stunden.index[0] + pd.Timedelta(days=WARMUP_TAGE)
    eligible = [
        i for i, ts in enumerate(stunden.index)
        if ts >= warmup_ts
    ]
    if MAX_EVAL_BARS > 0:
        eligible = eligible[-MAX_EVAL_BARS:]

    trades = []
    in_observation = None
    letzte_pruefung = None

    for pos, i in enumerate(eligible):
        if pos % EVAL_EVERY_N_BARS:
            continue

        ts = stunden.index[i]
        bar = stunden.iloc[i]
        close = float(bar["Close"])

        # Bereits laufendes Signal: nur Ziel-/Stop-Erreichung beobachten.
        if in_observation is not None:
            if i <= in_observation["entry_index"]:
                continue

            high = float(bar["High"])
            low = float(bar["Low"])

            # Konservativ: Stop vor Ziel, falls beides in derselben Kerze liegt.
            if in_observation["stop"] is not None and low <= in_observation["stop"]:
                if in_observation["tp1_erreicht"]:
                    in_observation["zweites_ereignis"] = "STOP_NACH_TP1"
                else:
                    in_observation["erstes_ereignis"] = "STOP_VOR_TP1"
                    in_observation["ausstieg_index"] = i
                    in_observation["ausstieg_zeit"] = str(ts)
                trades.append(dict(in_observation))
                in_observation = None
                continue

            if not in_observation["tp1_erreicht"] and high >= in_observation["tp1"]:
                in_observation["tp1_erreicht"] = True
                in_observation["tp1_zeit"] = str(ts)
                in_observation["tp1_bars_nach_entry"] = i - in_observation["entry_index"]

            if (
                in_observation["tp1_erreicht"]
                and in_observation["tp2"] is not None
                and high >= in_observation["tp2"]
            ):
                in_observation["tp2_erreicht"] = True
                in_observation["tp2_zeit"] = str(ts)
                in_observation["zweites_ereignis"] = "TP2"
                in_observation["ausstieg_index"] = i
                in_observation["ausstieg_zeit"] = str(ts)
                trades.append(dict(in_observation))
                in_observation = None
                continue

            if i - in_observation["entry_index"] >= HORIZON_BARS:
                in_observation["ausstieg_index"] = i
                in_observation["ausstieg_zeit"] = str(ts)
                in_observation["zweites_ereignis"] = (
                    "HORIZONT_NACH_TP1" if in_observation["tp1_erreicht"]
                    else "HORIZONT_OHNE_TP1"
                )
                trades.append(dict(in_observation))
                in_observation = None
                continue

            continue

        # Nur abgeschlossene Tagesdaten verwenden: die aktuelle Tageskerze
        # ist waehrend eines Intraday-Checks noch nicht vollstaendig bekannt.
        daily_hist = daily[daily.index < pd.Timestamp(ts).normalize()]
        if len(daily_hist) < 60:
            continue

        intraday_hist = stunden.iloc[: i + 1]
        basis_kandidaten = mod._chartkandidaten_fuer_setup(
            ts,
            intraday_reihe=intraday_hist,
            daily_reihe=daily_hist,
        )
        setup = {"kandidaten": basis_kandidaten}
        chain = runner._build_structure_chain(
            setup,
            intraday_hist,
            daily_hist,
            close,
        )

        trade = chain.get("trade", {})
        if trade.get("status") != "trade_zulaessig":
            continue

        entry = chain.get("entry", {}).get("test_entry")
        stop = chain.get("stop", {}).get("test_stop")
        tp1 = chain.get("tp", {}).get("tp1")
        tp2 = chain.get("tp", {}).get("tp2")

        if entry is None or stop is None or tp1 is None:
            continue

        if letzte_pruefung == (str(ts), float(entry)):
            continue
        letzte_pruefung = (str(ts), float(entry))

        in_observation = {
            "strategie": "C",
            "entry_zeit": str(ts),
            "entry_index": i,
            "entry": float(entry),
            "stop": float(stop),
            "tp1": float(tp1["preis"]),
            "tp1_crv": float(tp1["crv"]),
            "tp2": float(tp2["preis"]) if tp2 is not None else None,
            "tp2_crv": float(tp2["crv"]) if tp2 is not None else None,
            "tp2_status": "vorhanden" if tp2 is not None else "nicht_vorhanden",
            "tp2_grund": (chain.get("tp", {}).get("tp2_pruefung") or {}).get("grund"),
            "tp1_erreicht": False,
            "tp2_erreicht": False,
            "erstes_ereignis": None,
            "zweites_ereignis": None,
        }

    if in_observation is not None:
        trades.append({
            **in_observation,
            "ausstieg_zeit": str(stunden.index[-1]),
            "zweites_ereignis": "DATENENDE",
        })

    return pd.DataFrame(trades), daily


def kennzahlen(trades):
    if trades.empty:
        return {
            "Signale": 0,
            "TP1_erreicht_%": 0.0,
            "Stop_vor_TP1_%": 0.0,
            "TP2_vorhanden_%": 0.0,
            "TP2_erreicht_von_vorhanden_%": 0.0,
            "Hinweis": "Keine zulaessigen C-Signale im Backtest.",
        }

    n = len(trades)
    tp1 = int(trades["tp1_erreicht"].sum())
    stop1 = int((trades["erstes_ereignis"] == "STOP_VOR_TP1").sum())
    tp2_vorhanden = int((trades["tp2_status"] == "vorhanden").sum())
    tp2_hit = int(trades["tp2_erreicht"].sum())
    return {
        "Signale": n,
        "TP1_erreicht_%": round(tp1 / n * 100, 1),
        "Stop_vor_TP1_%": round(stop1 / n * 100, 1),
        "TP2_vorhanden_%": round(tp2_vorhanden / n * 100, 1),
        "TP2_erreicht_von_vorhanden_%": round(tp2_hit / tp2_vorhanden * 100, 1) if tp2_vorhanden else 0.0,
        "Ø_TP1_CRV": round(float(trades["tp1_crv"].mean()), 3),
        "Ø_TP2_CRV_vorhanden": round(float(trades.loc[trades["tp2_status"] == "vorhanden", "tp2_crv"].mean()), 3)
        if tp2_vorhanden else None,
        "Hinweis": (
            "Signal-/Zielqualitaet, noch keine C-P&L. "
            "Positionsmanagement nach TP1/bei fehlendem TP2 ist noch separat festzulegen."
        ),
    }


def main():
    stunden = hole_daten()
    trades, daily = historische_signale(stunden)

    stunden.to_csv("charttechnik_c_1h_daten.csv")
    daily.to_csv("charttechnik_c_daily_daten.csv")
    trades.to_csv("backtest_charttechnik_c_signale.csv", index=False)

    stats = kennzahlen(trades)
    stats.update({
        "Zeitraum_1h": f"{stunden.index.min()} bis {stunden.index.max()}",
        "Tagesdaten": f"{daily.index.min()} bis {daily.index.max()}",
        "EVAL_EVERY_N_BARS": EVAL_EVERY_N_BARS,
        "HORIZON_BARS": HORIZON_BARS,
    })
    Path("backtest_charttechnik_c_kennzahlen.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=== CHARTTECHNIK C – HISTORISCHER SIGNAL-BACKTEST ===")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("Ausgaben:")
    print("- backtest_charttechnik_c_signale.csv")
    print("- backtest_charttechnik_c_kennzahlen.json")
    print("- charttechnik_c_1h_daten.csv")
    print("- charttechnik_c_daily_daten.csv")


if __name__ == "__main__":
    main()
