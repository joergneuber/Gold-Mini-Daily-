#!/usr/bin/env python3
"""Historischer Signal-Backtest fuer die neue Charttechnik C.

WICHTIG:
- C wird als Signal- UND Positionsmanagement-Backtest bewertet.
- Der Backtest prueft die vollständige Kette:
  bestaetigter Entry -> struktureller Stop -> TP1 -> flexibles Positionsmanagement -> TP2/Trailing-Stop.
- Es werden keine 2R-/3R-Fallbacks erzeugt.
- TP2 wird nur als unmittelbar naechste gueltige Struktur zugelassen.
- Die spaetere Struktur darf bei CRV < 2 nicht uebersprungen werden.
- Fuer jeden historischen Entscheidungszeitpunkt werden nur Daten bis zu
  diesem Zeitpunkt verwendet. Tagesdaten werden waehrend des Intraday-Tages
  bewusst NICHT vorzeitig verwendet.
- Teilverkaeufe sind ausgeschlossen: C verwaltet immer 100 % der Position.
- TP1 ist ein Managementpunkt, kein automatischer Verkauf: bei Fortsetzung wird
  die Position gehalten; der Stop wird mindestens auf Break-even und danach
  unter bestaetigte Higher-Lows/Swing-Lows nachgezogen. Bei Schwaeche wird die
  Position ueber den nachgezogenen Stop vollstaendig geschlossen.
- TP2 erreicht -> 100 % Exit. Ohne TP2 wird die Position weiter strukturbasiert
  ueber den Trailing-Stop verwaltet.
- Es wird kein Look-ahead verwendet: Stop-Nachzuege duerfen nur auf bereits
  bestaetigten, abgeschlossenen Bars/Strukturen beruhen.
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
DAILY_WARMUP_TAGE = int(os.getenv("C_DAILY_WARMUP_DAYS", "270"))
EVAL_EVERY_N_BARS = max(1, int(os.getenv("C_EVAL_EVERY_N_BARS", "1")))
MAX_EVAL_BARS = int(os.getenv("C_MAX_EVAL_BARS", "0"))
HORIZON_BARS = max(24, int(os.getenv("C_HORIZON_BARS", "240")))
API_URL = "https://api.twelvedata.com/time_series"
EARLIEST_URL = "https://api.twelvedata.com/earliest_timestamp"


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
    return key


def _parse_earliest_timestamp(payload):
    """Liest die von Twelve Data gelieferte frueheste Zeit robust aus."""
    wert = payload.get("datetime")
    if wert is None:
        wert = payload.get("earliest_timestamp")
    if wert is None:
        wert = payload.get("timestamp")

    if wert is None:
        raise RuntimeError(
            "Twelve-Data-Antwort fuer /earliest_timestamp enthaelt kein "
            f"Datums-/Timestamp-Feld: {payload}"
        )

    if isinstance(wert, (int, float)):
        return pd.to_datetime(wert, unit="s", utc=True)

    text = str(wert).strip()
    if text.isdigit():
        return pd.to_datetime(int(text), unit="s", utc=True)
    return pd.to_datetime(text, utc=True)


def hole_fruehestes_datum(max_versuche=4):
    """Ermittelt das tatsaechlich verfuegbare frueheste 1h-Datum bei Twelve Data."""
    key = hole_api_key()
    letzter_fehler = None

    for versuch in range(1, max_versuche + 1):
        try:
            antwort = requests.get(
                EARLIEST_URL,
                params={
                    "symbol": SYMBOL,
                    "interval": INTERVALL,
                    "apikey": key,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            letzter_fehler = exc
            if versuch < max_versuche:
                warte = 10 * versuch
                print(f"Netzwerkfehler bei /earliest_timestamp: {exc}; warte {warte}s")
                time.sleep(warte)
                continue
            raise RuntimeError(
                f"Twelve Data /earliest_timestamp nach {max_versuche} Versuchen nicht erreichbar: {exc}"
            ) from exc

        if antwort.status_code == 429:
            if versuch < max_versuche:
                print(f"Rate-Limit bei /earliest_timestamp; warte 65s")
                time.sleep(65)
                continue
            raise RuntimeError("Twelve-Data-Rate-Limit bei /earliest_timestamp konnte nicht aufgeloest werden.")

        try:
            payload = antwort.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ungueltige JSON-Antwort von Twelve Data /earliest_timestamp "
                f"(HTTP {antwort.status_code}): {antwort.text[:500]}"
            ) from exc

        if antwort.status_code >= 400 or payload.get("status") == "error":
            raise RuntimeError(
                f"Twelve-Data-Fehler bei /earliest_timestamp "
                f"(HTTP {antwort.status_code}): {payload}"
            )

        try:
            frueheste = _parse_earliest_timestamp(payload)
        except Exception as exc:
            raise RuntimeError(
                f"Twelve-Data-Antwort fuer /earliest_timestamp konnte nicht ausgewertet werden: {payload}"
            ) from exc

        return frueheste.date()

    raise RuntimeError(f"Fruehestes 1h-Datum konnte nicht ermittelt werden: {letzter_fehler}")


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

        try:
            daten = antwort.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ungueltige JSON-Antwort von Twelve Data fuer {start} bis {ende} "
                f"(HTTP {antwort.status_code}): {antwort.text[:500]}"
            ) from exc
        if antwort.status_code >= 400 or daten.get("status") == "error" or "values" not in daten:
            raise RuntimeError(
                f"Twelve-Data-Fehler fuer {start} bis {ende} "
                f"(HTTP {antwort.status_code}): {daten}"
            )

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
    fruehestes_1h_datum = hole_fruehestes_datum()
    # Fuer kurze Backtest-Zeitraeume wird die notwendige Warmup-Historie
    # separat vor dem eigentlichen Bewertungsbeginn geladen. Dadurch bleibt
    # START_DATUM der echte erste Bewertungszeitpunkt.
    warmup_start = START_DATUM - timedelta(days=WARMUP_TAGE)
    start = max(warmup_start, fruehestes_1h_datum)

    print(f"Angeforderter 1h-Backtestbeginn: {START_DATUM}")
    print(f"Fruehestes verfuegbares XAU/USD-1h-Datum: {fruehestes_1h_datum}")
    print(f"1h-Warmup-Beginn: {start}")
    print(f"Effektiver Bewertungsbeginn: {START_DATUM}")

    if start > heute:
        raise RuntimeError(
            f"Das frueheste verfuegbare 1h-Datum {start} liegt in der Zukunft; "
            "historischer Backtest kann nicht gestartet werden."
        )

    teile = []
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
    # Die 6M-Strukturanalyse benoetigt rund 180 Handelstage.
    # 270 Kalendertage liefern dafuer auch an Feiertags-/Wochenendgrenzen
    # ausreichend Werktage. Die Bewertung selbst beginnt erst bei START_DATUM.
    start = START_DATUM - timedelta(days=DAILY_WARMUP_TAGE)
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
        try:
            daten = antwort.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ungueltige JSON-Antwort von Twelve Data fuer Tagesdaten "
                f"(HTTP {antwort.status_code}): {antwort.text[:500]}"
            ) from exc
        if antwort.status_code >= 400 or daten.get("status") == "error" or "values" not in daten:
            raise RuntimeError(
                f"Twelve-Data-Tagesfehler (HTTP {antwort.status_code}): {daten}"
            )
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


def _bestaetigtes_swing_low_am_bar(intraday_hist, lookback=2):
    """Prueft nur den zuletzt bestaetigbaren Swing-Low-Punkt.

    Der Pivot liegt ``lookback`` Bars zurueck. Erst nachdem die rechten
    Bestaetigungs-Bars abgeschlossen sind, darf er fuer den Stop verwendet
    werden. Dadurch bleibt die Pruefung schnell und look-ahead-frei.
    """
    if len(intraday_hist) < 2 * lookback + 1:
        return None

    pivot_index = len(intraday_hist) - lookback - 1
    lows = pd.to_numeric(intraday_hist["Low"], errors="coerce")
    value = lows.iloc[pivot_index]
    if pd.isna(value):
        return None

    links = lows.iloc[pivot_index - lookback:pivot_index]
    rechts = lows.iloc[pivot_index + 1:pivot_index + lookback + 1]
    if value <= links.min() and value <= rechts.min():
        return {
            "index": int(pivot_index),
            "zeit": str(lows.index[pivot_index]),
            "preis": float(value),
        }
    return None


def _management_snapshot(stunden, i, position):
    """Liefert den neu bestaetigten Swing-Low/Higher-Low-Punkt.

    Die C-Regel benoetigt nach TP1 keinen erneuten kompletten Entry/TP-Aufbau.
    TP2 bleibt das beim Entry bestimmte naechste Strukturziel; fuer das
    Positionsmanagement wird nur geprueft, ob inzwischen ein neues, hoeheres
    und bereits bestaetigtes Swing-Low entstanden ist.
    """
    intraday_hist = stunden.iloc[: i + 1]
    swing_low = _bestaetigtes_swing_low_am_bar(intraday_hist)
    higher_low = None
    if swing_low is not None:
        if position.get("letztes_swing_low") is None:
            if swing_low["preis"] > position["entry"]:
                higher_low = swing_low
        elif swing_low["preis"] > float(position["letztes_swing_low"]):
            higher_low = swing_low

    return {
        "swing_low": higher_low,
        "higher_low_confirmed": higher_low is not None,
        "next_target": position.get("tp2"),
        "next_target_crv": position.get("tp2_crv"),
    }


def _record_stop_change(position, ts, old_stop, new_stop, basis):
    """Protokolliert jede echte Stop-Aenderung chronologisch fuer das Audit."""
    if abs(float(new_stop) - float(old_stop)) < 1e-12:
        return
    position.setdefault("stop_history", []).append({
        "zeit": str(ts),
        "alter_stop": float(old_stop),
        "neuer_stop": float(new_stop),
        "basis": str(basis),
    })


def _update_post_tp1_management(stunden, i, ts, close, position):
    """Aktualisiert den Stop nach TP1 ohne Teilverkauf.

    Regel: TP1 ist nur ein Managementpunkt. Die Position bleibt vollstaendig
    offen. Der Stop wird zunaechst mindestens auf Break-even angehoben und
    danach nur auf bereits bestaetigte Higher-Lows/Swing-Lows angehoben.
    """
    snapshot = _management_snapshot(stunden, i, position)
    old_stop = float(position["stop"])
    new_stop = old_stop
    stop_basis = position.get("stop_basis_aktuell") or "break_even_nach_TP1"

    # Break-even wird nur dann als aktuelle Basis dokumentiert, wenn der
    # Stop tatsaechlich von unterhalb Entry auf Entry angehoben wurde. Ein
    # bereits hoeherer struktureller Stop darf nicht bei jeder Folgepruefung
    # wieder als "break_even_nach_TP1" etikettiert werden.
    entry = float(position["entry"])
    if old_stop < entry:
        new_stop = entry
        stop_basis = "break_even_nach_TP1"

    swing_low = snapshot.get("swing_low")
    if swing_low is not None:
        candidate = float(swing_low["preis"])
        if candidate > new_stop and candidate < close:
            new_stop = candidate
            stop_basis = "bestaetigtes_Higher-Low_Swing-Low"
            position["letztes_swing_low"] = candidate
            position["letztes_swing_low_zeit"] = swing_low["zeit"]

    _record_stop_change(position, ts, old_stop, new_stop, stop_basis)
    position["stop"] = new_stop
    position["stop_basis_aktuell"] = stop_basis
    position["stop_nach_tp1"] = True
    position["management_letzte_pruefung"] = str(ts)
    position["management_naechstes_ziel"] = (
        float(snapshot["next_target"]) if snapshot.get("next_target") is not None else None
    )
    position["management_naechstes_ziel_crv"] = snapshot.get("next_target_crv")
    position["management_swing_low"] = (
        float(swing_low["preis"]) if swing_low is not None else position.get("management_swing_low")
    )
    position["management_entscheidung"] = (
        "POSITION_HALTEN_BIS_TP2_ODER_TRAILING_STOP"
        if position.get("tp2") is not None
        else "POSITION_WEITER_STRUKTURBASIERT_TRAILEN"
    )
    return position


def historische_signale(stunden):
    daily = hole_tagesdaten(stunden)
    if len(daily) < 180:
        raise RuntimeError("Zu wenig Tageshistorie fuer die 6M-Struktur.")

    start_index = max(1, int(len(stunden) * 0))
    # Die Historie vor START_DATUM dient ausschliesslich als Warmup fuer
    # Indikatoren/Strukturen. Bewertet werden erst Bars ab START_DATUM.
    eligible = [
        i for i, ts in enumerate(stunden.index)
        if ts >= pd.Timestamp(START_DATUM, tz="UTC")
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

        # Bereits laufendes Signal: erst den zu Beginn dieser Kerze gueltigen
        # Stop pruefen. Neue Strukturen werden erst fuer die Folgekerze wirksam.
        if in_observation is not None:
            if i <= in_observation["entry_index"]:
                continue

            high = float(bar["High"])
            low = float(bar["Low"])
            stop_zu_beginn = float(in_observation["stop"]) if in_observation["stop"] is not None else None

            if stop_zu_beginn is not None and low <= stop_zu_beginn:
                if in_observation["tp1_erreicht"]:
                    in_observation["zweites_ereignis"] = "STOP_NACH_TP1"
                else:
                    in_observation["erstes_ereignis"] = "STOP_VOR_TP1"
                in_observation["ausstieg_index"] = i
                in_observation["ausstieg_zeit"] = str(ts)
                in_observation["ausstieg_preis"] = stop_zu_beginn
                in_observation["rendite_pct"] = round((stop_zu_beginn / in_observation["entry"] - 1.0) * 100.0, 4)
                in_observation["positionsstatus"] = "GESCHLOSSEN"
                in_observation["pnl_zaehlt"] = True
                trades.append(dict(in_observation))
                in_observation = None
                continue

            if not in_observation["tp1_erreicht"] and high >= in_observation["tp1"]:
                in_observation["tp1_erreicht"] = True
                in_observation["tp1_zeit"] = str(ts)
                in_observation["tp1_bars_nach_entry"] = i - in_observation["entry_index"]

                # TP1 = Managementpunkt, kein Verkauf. Der Break-even-Stop
                # wird ab der Folgekerze wirksam; kein nachtraegliches Repricing
                # innerhalb der TP1-Kerze.
                alter_stop = float(in_observation["stop"])
                neuer_stop = max(alter_stop, float(in_observation["entry"]))
                _record_stop_change(in_observation, ts, alter_stop, neuer_stop, "break_even_nach_TP1")
                in_observation["stop"] = neuer_stop
                in_observation["stop_basis_aktuell"] = "break_even_nach_TP1"
                in_observation["stop_nach_tp1"] = True
                in_observation["management_letzte_pruefung"] = str(ts)
                in_observation["management_entscheidung"] = (
                    "POSITION_HALTEN_BIS_TP2_ODER_TRAILING_STOP"
                    if in_observation.get("tp2") is not None
                    else "POSITION_WEITER_STRUKTURBASIERT_TRAILEN"
                )

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
                in_observation["ausstieg_preis"] = float(in_observation["tp2"])
                in_observation["rendite_pct"] = round(
                    (float(in_observation["tp2"]) / in_observation["entry"] - 1.0) * 100.0,
                    4,
                )
                in_observation["management_entscheidung"] = "TP2_100_PROZENT_EXIT"
                in_observation["positionsstatus"] = "GESCHLOSSEN"
                in_observation["pnl_zaehlt"] = True
                trades.append(dict(in_observation))
                in_observation = None
                continue

            # Neue bestaetigte Swing-/Higher-Low-Strukturen werden erst nach
            # der aktuellen Kerze als Stop fuer die Folgekerze aktiviert.
            if in_observation["tp1_erreicht"]:
                _update_post_tp1_management(stunden, i, ts, close, in_observation)

            if i - in_observation["entry_index"] >= HORIZON_BARS:
                in_observation["ausstieg_index"] = i
                in_observation["ausstieg_zeit"] = str(ts)
                in_observation["ausstieg_preis"] = None
                in_observation["mark_to_market_preis"] = close
                in_observation["mark_to_market_rendite_pct"] = round((close / in_observation["entry"] - 1.0) * 100.0, 4)
                in_observation["zweites_ereignis"] = (
                    "HORIZONT_NACH_TP1_OFFEN" if in_observation["tp1_erreicht"]
                    else "HORIZONT_OHNE_TP1_OFFEN"
                )
                in_observation["positionsstatus"] = "OFFEN_AM_HORIZONT"
                in_observation["pnl_zaehlt"] = False
                in_observation["rendite_pct"] = None
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

        tp = chain.get("tp", {})
        tp2_pruefung = tp.get("tp2_pruefung") or {}
        tp2_kandidat = tp2_pruefung.get("kandidat") or {}
        tp_kandidaten = tp.get("alle_tp_kandidaten") or []
        kandidat_preis = tp2_kandidat.get("preis")
        spaetere_kandidaten = []
        if kandidat_preis is not None:
            for kandidat in tp_kandidaten:
                preis = kandidat.get("preis")
                if preis is None or float(preis) <= float(kandidat_preis) + 1e-6:
                    continue
                if kandidat.get("charttechnisch_gueltig"):
                    spaetere_kandidaten.append({
                        "preis": float(preis),
                        "crv": kandidat.get("crv"),
                        "verworfen": bool(kandidat.get("verworfen", False)),
                        "verwerfungsgrund": kandidat.get("verwerfungsgrund"),
                    })
        tp2_spaetere_vorhanden = bool(spaetere_kandidaten)
        # Spaetere Strukturen duerfen natuerlich existieren. "Keine Struktur
        # uebersprungen" bedeutet hier deshalb nicht "keine spaetere Struktur
        # vorhanden", sondern: Der gepruefte TP2-Kandidat ist der erste
        # charttechnisch gueltige Kandidat nach TP1. Genau das ist die
        # verbindliche TP2-Kette; spaetere Kandidaten werden nicht zur Auswahl
        # herangezogen.
        tp2_kein_ueberspringen = bool(
            tp2_pruefung.get("status") in {"zugelassen", "verworfen"}
            and kandidat_preis is not None
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
            "stop_nach_tp1": False,
            "stop_basis_aktuell": "initiale_chartstruktur",
            "management_entscheidung": None,
            "management_letzte_pruefung": None,
            "management_naechstes_ziel": None,
            "management_naechstes_ziel_crv": None,
            "management_swing_low": None,
            "letztes_swing_low": None,
            "letztes_swing_low_zeit": None,
            "ausstieg_preis": None,
            "rendite_pct": None,
            "mark_to_market_preis": None,
            "mark_to_market_rendite_pct": None,
            "positionsstatus": "OFFEN",
            "pnl_zaehlt": True,
            "initial_stop": float(stop),
            "stop_history": [],
            "tp2_pruefstatus": tp2_pruefung.get("status"),
            "tp2_pruefkandidat_preis": tp2_kandidat.get("preis"),
            "tp2_pruefkandidat_crv": tp2_kandidat.get("crv"),
            "tp2_pruefkandidat_grund": tp2_pruefung.get("grund"),
            "tp2_spaetere_kandidaten_vorhanden": tp2_spaetere_vorhanden,
            "tp2_spaetere_kandidaten": spaetere_kandidaten,
            "tp2_keine_struktur_uebersprungen": tp2_kein_ueberspringen,
        }

    if in_observation is not None:
        letzter_close = float(stunden.iloc[-1]["Close"])
        trades.append({
            **in_observation,
            "ausstieg_zeit": None,
            "ausstieg_preis": None,
            "mark_to_market_preis": letzter_close,
            "mark_to_market_rendite_pct": round((letzter_close / in_observation["entry"] - 1.0) * 100.0, 4),
            "rendite_pct": None,
            "zweites_ereignis": "DATENENDE_OFFEN",
            "positionsstatus": "OFFEN_AM_DATENENDE",
            "pnl_zaehlt": False,
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
            "TP1_management_ohne_teilverkauf_%": 0.0,
            "Ø_Rendite_%": 0.0,
            "Median_Rendite_%": 0.0,
            "Hinweis": "Keine zulaessigen C-Signale im Backtest.",
        }

    n = len(trades)
    tp1 = int(trades["tp1_erreicht"].sum())
    stop1 = int((trades["erstes_ereignis"] == "STOP_VOR_TP1").sum())
    tp2_vorhanden = int((trades["tp2_status"] == "vorhanden").sum())
    tp2_hit = int(trades["tp2_erreicht"].sum())
    management_count = int(
        trades.loc[trades["tp1_erreicht"], "stop_nach_tp1"].fillna(False).sum()
    )
    renditen = pd.to_numeric(trades.loc[trades["pnl_zaehlt"].fillna(False), "rendite_pct"], errors="coerce").dropna()
    offene = int((~trades["pnl_zaehlt"].fillna(False)).sum())
    stop_hist = int(trades.loc[trades["tp1_erreicht"], "stop_history"].apply(lambda x: isinstance(x, list) and len(x) > 0).sum())
    return {
        "Signale": n,
        "Geschlossene_Positionen": n - offene,
        "Offene_Positionen_am_Horizont_oder_Datenende": offene,
        "Offene_PnL_nicht_eingerechnet": True,
        "TP1_Stop_Historie_vorhanden": stop_hist,
        "TP1_erreicht_%": round(tp1 / n * 100, 1),
        "Stop_vor_TP1_%": round(stop1 / n * 100, 1),
        "TP2_vorhanden_%": round(tp2_vorhanden / n * 100, 1),
        "TP2_erreicht_von_vorhanden_%": round(tp2_hit / tp2_vorhanden * 100, 1) if tp2_vorhanden else 0.0,
        "TP2_Spaetere_Strukturen_trotz_Kandidat_%": round(
            float(trades["tp2_spaetere_kandidaten_vorhanden"].fillna(False).astype(bool).mean()) * 100, 1
        ) if "tp2_spaetere_kandidaten_vorhanden" in trades.columns else 0.0,
        "TP2_Keine_Struktur_uebersprungen_%": round(
            float(trades["tp2_keine_struktur_uebersprungen"].dropna().astype(bool).mean()) * 100, 1
        ) if "tp2_keine_struktur_uebersprungen" in trades.columns and trades["tp2_keine_struktur_uebersprungen"].notna().any() else 0.0,
        "TP1_management_ohne_teilverkauf_%": round(management_count / tp1 * 100, 1) if tp1 else 0.0,
        "Ø_Rendite_%": round(float(renditen.mean()), 4) if not renditen.empty else 0.0,
        "Median_Rendite_%": round(float(renditen.median()), 4) if not renditen.empty else 0.0,
        "Ø_TP1_CRV": round(float(trades["tp1_crv"].mean()), 3),
        "Ø_TP2_CRV_vorhanden": round(float(trades.loc[trades["tp2_status"] == "vorhanden", "tp2_crv"].mean()), 3)
        if tp2_vorhanden else None,
        "Hinweis": (
            "C-Positionsmanagement aktiv: kein Teilverkauf. TP1 ist Managementpunkt; "
            "bei Fortsetzung wird gehalten, Stop mindestens auf Break-even und danach "
            "nur ueber bestaetigte Higher-Lows/Swing-Lows nachgezogen. "
            "TP2 fuehrt zum 100%-Exit; ohne TP2 wird strukturbasiert weiter getrailt."
        ),
    }


def main():
    stunden = hole_daten()
    trades, daily = historische_signale(stunden)

    stunden.to_csv("charttechnik_c_1h_daten.csv")
    daily.to_csv("charttechnik_c_daily_daten.csv")
    export_trades = trades.copy()
    if "stop_history" in export_trades.columns:
        export_trades["stop_history"] = export_trades["stop_history"].apply(
            lambda value: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        )
    export_trades.to_csv("backtest_charttechnik_c_signale.csv", index=False)

    stats = kennzahlen(trades)
    stats.update({
        "Angeforderter_Start_1h": str(START_DATUM),
        "Tatsaechlich_verfuegbarer_Start_1h": str(stunden.index.min().date()),
        "Effektiver_Start_1h": str(stunden.index.min().date()),
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
