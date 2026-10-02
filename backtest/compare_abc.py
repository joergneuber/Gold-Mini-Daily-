#!/usr/bin/env python3
"""Vergleich der vorhandenen A/B-Backtests mit C.

A/B werden aus ihren vorhandenen Trade-Logs ausgewertet.
C wird bewusst als Signal-/Zielqualitaet ausgewiesen, solange fuer C noch
keine finale Positionsverwaltung beschlossen wurde. Dadurch werden keine
P&L-Zahlen von C erfunden oder mit A/B verwechselt.
"""

import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def stats_pnl(path, name):
    if not Path(path).exists():
        return {"Strategie": name, "Status": "Trade-Log fehlt"}

    df = pd.read_csv(path)
    if df.empty or "ergebnis_pct" not in df:
        return {"Strategie": name, "Status": "kein auswertbarer Trade-Log"}

    winners = df[df["ergebnis_pct"] > 0]
    losers = df[df["ergebnis_pct"] < 0]
    return {
        "Strategie": name,
        "Status": "P&L-Backtest",
        "Trades": len(df),
        "Trefferquote_%": round(len(winners) / len(df) * 100, 1),
        "Summe_Trade_%": round(df["ergebnis_pct"].sum(), 2),
        "Ø_Trade_%": round(df["ergebnis_pct"].mean(), 2),
        "Ø_Gewinner_%": round(winners["ergebnis_pct"].mean(), 2) if len(winners) else 0.0,
        "Ø_Verlierer_%": round(losers["ergebnis_pct"].mean(), 2) if len(losers) else 0.0,
        "TP1_%": round((df["stufe_bei_ausstieg"] >= 1).mean() * 100, 1)
        if "stufe_bei_ausstieg" in df.columns else None,
        "TP2_%": round((df["stufe_bei_ausstieg"] >= 2).mean() * 100, 1)
        if "stufe_bei_ausstieg" in df.columns else None,
    }


def stats_c(path):
    if not Path(path).exists():
        return {"Strategie": "C", "Status": "C-Kennzahlen fehlen"}

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "Strategie": "C",
        "Status": "Signal-/Zielqualitaet (keine P&L)",
        "Trades": data.get("Signale", 0),
        "Trefferquote_%": data.get("TP1_erreicht_%"),
        "Summe_Trade_%": None,
        "Ø_Trade_%": None,
        "Ø_Gewinner_%": None,
        "Ø_Verlierer_%": None,
        "TP1_%": data.get("TP1_erreicht_%"),
        "TP2_%": data.get("TP2_erreicht_von_vorhanden_%"),
    }


def main():
    rows = [
        stats_pnl(ROOT / "backtest_range_ausbruch_trades.csv", "A – Range-Ausbruch 1h"),
        stats_pnl(ROOT / "backtest_v1e_trades.csv", "B – Positionstrading"),
        stats_c(ROOT / "backtest_charttechnik_c_kennzahlen.json"),
    ]
    df = pd.DataFrame(rows)
    df.to_csv("vergleich_A_B_C.csv", index=False, encoding="utf-8-sig")
    print(df.to_string(index=False))
    print(
        "\nWichtig: C ist absichtlich noch kein P&L-System. "
        "Eine direkte Rendite-Rangfolge A/B/C ist erst nach Festlegung "
        "der finalen C-Positionsverwaltung zulaessig."
    )


if __name__ == "__main__":
    main()
