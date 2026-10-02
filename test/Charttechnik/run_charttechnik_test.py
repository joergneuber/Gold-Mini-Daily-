"""Isolierter Charttechnik-Testlauf.

Diagnose fuer EIN konkretes Gold-Setup:
Chartstrukturen -> charttechnische Entry-Grundlage -> Stop -> TP1 -> TP2 -> CRV.

Wichtig:
- Produktionslogik wird nicht ausgefuehrt/veraendert.
- Kein Score.
- Keine 2R-/3R-Fallbacks.
- Das CRV ist ausschliesslich Zulassungsschranke.
- Der aktuell laufende Intraday-Kanal (seit dem letzten relevanten Wendepunkt)
  wird separat vom Gesamtkanal diagnostiziert und ueber mehrere Zeitpunkte
  verfolgt, damit die dynamische Veraenderung des Kanals sichtbar wird.
"""
import importlib.util
import json
import os
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "test" / "Charttechnik" / "mini_daily_gold.py"

spec = importlib.util.spec_from_file_location("mini_daily_gold_charttechnik_test", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _channel_at(reihe):
    """Liest den aktuell aktiven Wendepunkt-Kanal am letzten Bar aus."""
    info = mod.kanal_seit_wendepunkt(reihe)
    if info is None:
        return {"status": "kein_aktiver_wendepunkt_kanal"}

    last_ts = reihe.index[-1]
    x = mod.mdates.date2num(last_ts)
    out = {
        "status": "aktiv",
        "start": str(info["start"]),
        "ende": str(last_ts),
        "typ": info["typ"],
        "bars": int(len(info["reihe"])),
    }
    if info["typ"] == "kanal":
        k = info["daten"]
        obere = float(k["obere_linie"][0] * x + k["obere_linie"][1])
        untere = float(k["untere_linie"][0] * x + k["untere_linie"][1])
        out.update({
            "formation": k.get("formation"),
            "obere_grenze": obere,
            "untere_grenze": untere,
            "kanalbreite": obere - untere,
            "obere_steigung": float(k["obere_linie"][0]),
            "untere_steigung": float(k["untere_linie"][0]),
        })
    else:
        steigung, achse = info["daten"]
        out.update({
            "formation": "Aufwärtstrend" if steigung > 0 else "Abwärtstrend",
            "trendwert": float(steigung * x + achse),
            "steigung": float(steigung),
        })
    return out


def _channel_evolution(reihe, checkpoints=12):
    """Zeigt, wie sich der aktive Wendepunkt-Kanal mit neuen Bars veraendert."""
    n = len(reihe)
    if n < 30:
        return []
    start = max(30, n - checkpoints * 2)
    indices = list(range(start, n, max(1, (n - start) // max(1, checkpoints - 1))))
    indices = indices[-checkpoints:]
    out = []
    for i in indices:
        sub = reihe.iloc[:i + 1]
        c = _channel_at(sub)
        c["zeitpunkt"] = str(sub.index[-1])
        c["kurs_close"] = float(sub["Close"].iloc[-1])
        out.append(c)
    return out


def _kandidat_view(k, entry, stop):
    """Erzeugt die reine Kandidatenansicht ohne vorgezogene CRV-Berechnung.

    Das CRV darf erst berechnet werden, nachdem der Kandidat als
    charttechnisch gueltige Struktur bestaetigt wurde.
    """
    preis = float(k["preis"])
    rw = k.get("rollenwechsel") or {}
    return {
        "preis": preis,
        "typ": k.get("typ"),
        "ebene": k.get("ebene"),
        "seite": k.get("seite"),
        "quelle": k.get("quelle"),
        "treffer": k.get("treffer", 0),
        "crv": None,
        "rollenwechsel": rw or None,
    }


def _struktur_rang(k):
    """Echte if/elif-Hierarchie fuer charttechnische Relevanz.

    Kein Score und keine numerische Gewichtung: Die Rangfolge beschreibt nur,
    welche aktuelle Chartstruktur zuerst geprueft wird. Erst danach kommt CRV.
    """
    typ = str(k.get("typ", "")).lower()
    ebene = str(k.get("ebene", "")).lower()
    dynamisch = bool(k.get("dynamisch"))
    quelle = str(k.get("quelle", "")).lower()

    if ebene == "intraday" and typ == "umkehrzone":
        return 1, "aktuelle_intraday_umkehrzone"
    if ebene == "intraday" and dynamisch and k.get("seite") == "widerstand":
        return 2, "aktuelle_dynamische_kanalgrenze"
    if ebene == "intraday" and typ == "kanal":
        return 3, "intraday_kanalstruktur"
    if ebene == "intraday":
        return 3, "weitere_intraday_struktur"
    if ebene == "6m":
        return 4, "6m_struktur"
    if ebene == "tageschart":
        return 5, "tageschart_struktur"
    if "widerstand" in quelle or "support" in quelle:
        return 7, "sonstige_chartstruktur"
    return 8, "sonstige_struktur"


def _charttechnischer_grund(k):
    """Liefert nur dann einen gueltigen Chartgrund, wenn die Struktur selbst belegt ist.

    CRV ist hier absichtlich NICHT Bestandteil der Pruefung. Ein Kandidat darf
    nur wegen seiner charttechnischen Struktur in die CRV-Pruefung gelangen.
    """
    preis = k.get("preis")
    typ = str(k.get("typ") or "").strip()
    ebene = str(k.get("ebene") or "").strip()
    quelle = str(k.get("quelle") or "").strip()
    if preis is None or not typ or not ebene or not quelle:
        return False, "kein_vollstaendiger_charttechnischer_nachweis"
    if not k.get("aktuelle_rolle_gueltig", True):
        return False, "charttechnische_rolle_nicht_mehr_gueltig"
    return True, f"{typ}; {ebene}; {quelle}"


def _chart_hierarchie(kandidaten, entry):
    """Liefert ALLE Widerstandskandidaten preislich aufsteigend.

    Die Preisreihenfolge bestimmt die Pruefreihenfolge. Ein Kandidat mit
    ungueltiger aktueller Chartrolle bleibt fuer die Diagnose sichtbar, darf
    aber niemals als TP verwendet werden. CRV wird erst fuer charttechnisch
    gueltige Kandidaten als Zulassungsfilter ausgewertet.
    """
    oberhalb = []
    for k in kandidaten:
        if k.get("seite") != "widerstand" or float(k["preis"]) <= entry:
            continue
        gueltig, grund = _charttechnischer_grund(k)
        v = dict(k)
        v["charttechnisch_gueltig"] = bool(gueltig)
        v["charttechnischer_grund"] = grund
        oberhalb.append(v)
    return sorted(oberhalb, key=lambda k: float(k["preis"]))


def _konsolidiere_nahe_zielstrukturen(kandidaten, toleranz=3.0):
    """Fasst praktisch identische Preislevel zu einer Chartzone zusammen.

    Rohkandidaten bleiben unangetastet; nur die Entscheidungs-/TP-Kette erhält
    keine künstliche Doppelstruktur aus nahezu identischen Quellen.
    """
    sortiert = sorted(kandidaten, key=lambda k: float(k["preis"]))
    out = []
    for k in sortiert:
        if not out:
            out.append(dict(k))
            continue
        last = out[-1]
        if abs(float(k["preis"]) - float(last["preis"])) <= toleranz:
            quellen = list(last.get("zusammengefasste_quellen") or [])
            for q in (last.get("quelle"), k.get("quelle")):
                if q and q not in quellen:
                    quellen.append(q)
            last["zusammengefasste_quellen"] = quellen
            last["zusammengefasste_preise"] = sorted(set(
                [float(last["preis"]), float(k["preis"])]
            ))
            if last.get("untere_grenze") is not None or k.get("untere_grenze") is not None:
                grenzen_unten = [
                    float(v) for v in (last.get("untere_grenze"), k.get("untere_grenze"))
                    if v is not None
                ]
                if grenzen_unten:
                    last["untere_grenze"] = min(grenzen_unten)
            if last.get("obere_grenze") is not None or k.get("obere_grenze") is not None:
                grenzen_oben = [
                    float(v) for v in (last.get("obere_grenze"), k.get("obere_grenze"))
                    if v is not None
                ]
                if grenzen_oben:
                    last["obere_grenze"] = max(grenzen_oben)
        else:
            out.append(dict(k))
    return out


def _tp_kandidaten_diagnose(kandidaten, entry, stop, tp1_crv=1.0):
    """Prueft jeden TP-Kandidaten in Preisreihenfolge.

    Harte Regel: kein charttechnischer Grund -> kein TP, unabhaengig vom CRV.
    Erst bei gueltiger Chartstruktur wird das CRV als nachgelagerter Filter
    berechnet. Rollenwechsel mit aktueller Rolle SUPPORT sind daher niemals
    gueltige Long-TP-Widerstaende.
    """
    risiko = (entry - stop) if stop is not None and entry > stop else None
    rows = []
    for r in _chart_hierarchie(kandidaten, entry):
        preis = float(r["preis"])
        row = _kandidat_view(r, entry, stop)
        row["aktuelle_rolle_gueltig"] = bool(r.get("aktuelle_rolle_gueltig", True))
        row["charttechnisch_gueltig"] = bool(r.get("charttechnisch_gueltig"))
        row["charttechnischer_grund"] = r.get("charttechnischer_grund")
        row["chart_rang"] = _struktur_rang(r)[0]
        row["chart_rang_begruendung"] = _struktur_rang(r)[1]
        if not row["charttechnisch_gueltig"]:
            row["crv"] = None
            row["verworfen"] = True
            row["verwerfungsgrund"] = "kein_gueltiger_charttechnischer_grund"
            row["auswahlbegruendung"] = "charttechnische_voraussetzung_nicht_erfuellt; CRV_nicht_entscheidungsrelevant"
            row["auswahlstufe"] = "charttechnisch_verworfen"
        elif risiko is None or risiko <= 0:
            row["crv"] = None
            row["verworfen"] = True
            row["verwerfungsgrund"] = "kein_gueltiges_risiko_fuer_crv_berechenbar"
            row["auswahlbegruendung"] = "charttechnisch_gueltig; CRV_nicht_berechenbar"
            row["auswahlstufe"] = "crv_nicht_berechenbar"
        else:
            crv = (preis - entry) / risiko
            row["crv"] = crv
            row["verworfen"] = crv <= tp1_crv
            row["verwerfungsgrund"] = f"CRV <= {tp1_crv:g}" if crv <= tp1_crv else None
            row["auswahlbegruendung"] = (
                f"charttechnischer_grund_vorhanden; CRV > {tp1_crv:g}"
                if crv > tp1_crv else
                f"charttechnischer_grund_vorhanden; CRV <= {tp1_crv:g}"
            )
            row["auswahlstufe"] = "TP1-erster-gueltiger" if crv > tp1_crv else "vor_TP1_verworfen"
        rows.append(row)
    # Nur der erste charttechnisch gueltige Kandidat mit CRV > 1 ist TP1.
    erster = next((i for i, row in enumerate(rows)
                   if row.get("charttechnisch_gueltig") and row.get("crv") is not None and row["crv"] > tp1_crv), None)
    if erster is not None:
        for i, row in enumerate(rows):
            if i > erster and row.get("charttechnisch_gueltig") and row.get("crv") is not None and row["crv"] > tp1_crv:
                row["auswahlstufe"] = "nach_TP1_nicht_ausgewaehlt"
                row["auswahlbegruendung"] = "charttechnisch_gueltig; CRV > 1, aber TP1 bereits durch naehere gueltige Struktur bestimmt"
    return rows


def _ermittle_tp_kette(kandidaten, entry, stop, tp1_crv=1.0, tp2_crv=2.0):
    """Ermittelt die endgültige TP-Kette nach der verbindlichen Chartregel.

    - Rohstrukturen werden innerhalb von 3,0 Punkten zu einer Chartzone
      konsolidiert.
    - TP1 ist die erste aktuell gültige Struktur oberhalb des Entries mit
      CRV > 1.
    - TP2 ist ausschließlich die unmittelbar nächste aktuell gültige
      Struktur nach TP1 mit CRV >= 2.
    - Ist diese nächste Struktur unter 2R, bleibt TP2 leer; spätere
      Strukturen werden nicht geprüft/übersprungen.
    """
    kandidaten_zone = _konsolidiere_nahe_zielstrukturen(
        [k for k in kandidaten if k.get("seite") == "widerstand"],
        toleranz=3.0,
    )
    rows = _tp_kandidaten_diagnose(
        kandidaten_zone, float(entry), stop, tp1_crv=tp1_crv
    )
    gueltig_tp1 = [
        x for x in rows
        if x.get("charttechnisch_gueltig") and not x.get("verworfen")
    ]
    tp1 = gueltig_tp1[0] if gueltig_tp1 else None
    tp2 = None
    tp2_pruefung = None

    if tp1 is None:
        return {
            "tp1": None,
            "tp2": None,
            "tp2_pruefung": {
                "status": "kein_tp1",
                "grund": "keine_charttechnisch_gueltige_struktur_mit_crv_gt_1",
            },
            "alle_tp_kandidaten": rows,
        }

    naechster = next(
        (
            x for x in rows
            if x["preis"] > tp1["preis"] + 1e-6
            and x.get("charttechnisch_gueltig")
        ),
        None,
    )
    if naechster is None:
        tp2_pruefung = {
            "status": "nicht_vorhanden",
            "grund": "keine_hoeherliegende_gueltige_chartstruktur",
            "spaetere_strukturen_geprueft": False,
        }
    elif naechster.get("crv") is not None and naechster["crv"] >= tp2_crv:
        tp2 = naechster
        tp2["auswahlstufe"] = "TP2-naechste-gueltige-struktur"
        tp2["auswahlbegruendung"] = (
            "charttechnischer_grund_vorhanden; "
            "naechste_hoeherliegende_gueltige_chartstruktur; CRV >= 2"
        )
        tp2_pruefung = {
            "status": "zugelassen",
            "grund": "naechste_hoeherliegende_gueltige_chartstruktur; CRV >= 2",
            "kandidat": naechster,
            "spaetere_strukturen_geprueft": False,
        }
    else:
        naechster["verworfen"] = True
        naechster["verwerfungsgrund"] = (
            "naechste_hoeherliegende_gueltige_chartstruktur hat CRV < 2; "
            "keine spaetere Struktur wird uebersprungen"
        )
        naechster["auswahlstufe"] = "TP2-verworfen"
        naechster["auswahlbegruendung"] = (
            "charttechnischer_grund_vorhanden; "
            "naechste_hoeherliegende_gueltige_chartstruktur; CRV < 2"
        )
        tp2_pruefung = {
            "status": "nicht_vorhanden",
            "grund": (
                "TP2 nicht vorhanden – keine spaetere Struktur geprüft; "
                "die unmittelbar naechste gueltige Struktur hat CRV < 2"
            ),
            "kandidat": naechster,
            "spaetere_strukturen_geprueft": False,
        }

    return {
        "tp1": tp1,
        "tp2": tp2,
        "tp2_pruefung": tp2_pruefung,
        "alle_tp_kandidaten": rows,
    }


def _rollenwechsel_bestaetigung(rw):
    """Prueft, ob ein Rollenwechsel charttechnisch ausreichend bestaetigt ist.

    Ein kurzer Bruch allein reicht nicht. Fuer einen bestaetigten Wechsel werden
    Bruch, Retest und mindestens die im Datensatz hinterlegte Schluss-/Bestatigung
    (bestaetigungs_bars) verlangt. Fehlt die Retest-/Bestatigungsinformation,
    bleibt der Wechsel unbestaetigt und darf die aktuelle Rolle nicht umschalten.
    """
    if not rw or not rw.get("rollenwechsel"):
        return {
            "bestaetigt": False,
            "bruch_erkannt": False,
            "fakeout_vermutet": False,
            "retetst_erkannt": False,
            "retetst_bestaetigt": False,
            "grund": "kein_rollenwechselereignis",
        }
    bruch = rw.get("bruch_zeit")
    retest = rw.get("retest_zeit")
    bars = rw.get("bestaetigungs_bars")
    try:
        bars_ok = int(bars) >= 2
    except (TypeError, ValueError):
        bars_ok = False
    bruch_ok = bool(bruch)
    retest_ok = bool(retest)
    bestaetigt = bruch_ok and retest_ok and bars_ok
    fakeout = bruch_ok and not bestaetigt
    if bestaetigt:
        grund = "Bruch + Retest + mindestens 2 Bestaetigungsbars vorhanden"
    elif fakeout:
        grund = "Bruch vorhanden, aber Retest/Bestaetigung fehlt; Rollenwechsel nicht bestaetigt"
    else:
        grund = "Rollenwechsel nicht vollstaendig bestaetigt"
    return {
        "bestaetigt": bestaetigt,
        "bruch_erkannt": bruch_ok,
        "fakeout_vermutet": fakeout,
        "retetst_erkannt": retest_ok,
        "retetst_bestaetigt": bool(retest_ok and bars_ok),
        "bruch_zeit": bruch,
        "retest_zeit": retest,
        "bestaetigungs_bars": bars,
        "grund": grund,
    }


def _rollenwechsel_aktuell_gueltig(kandidat, aktueller_kurs, gruppen_events=None):
    """Bestimmt die aktuelle Rolle chronologisch aus bestaetigten Ereignissen.

    Der aktuelle Kurs allein darf KEINEN Rollenwechsel erzeugen oder aufheben.
    Ein bestaetigtes R->S bleibt Support, auch wenn der Kurs spaeter unter dem
    Level notiert. Erst ein spaeteres, separat bestaetigtes S->R darf die Rolle
    wieder umkehren. Spiegelbildlich gilt dasselbe fuer S->R -> R->S.
    """
    rw = kandidat.get("rollenwechsel") or {}
    events = list(gruppen_events or [])
    if rw and rw.get("rollenwechsel"):
        events.append(rw)

    bestaetigte = []
    diag_events = []
    for event in events:
        check = _rollenwechsel_bestaetigung(event)
        item = dict(check)
        item.update({
            "urspruengliche_seite": event.get("urspruengliche_seite"),
            "neue_seite": event.get("neue_seite"),
            "preis": event.get("preis", kandidat.get("preis")),
        })
        diag_events.append(item)
        if check["bestaetigt"] and event.get("neue_seite") in {"support", "widerstand"}:
            bestaetigte.append(event)

    if not bestaetigte:
        return kandidat.get("seite"), True, "keine_bestaetigte_rollenwechselhistorie; urspruengliche_chartrolle_bleibt", {
            "ereignisse": diag_events,
            "aktuelle_rolle": kandidat.get("seite"),
        }

    def event_time(event):
        value = event.get("retest_zeit") or event.get("bruch_zeit")
        if value is None:
            return pd.Timestamp.min
        try:
            return pd.Timestamp(value)
        except Exception:
            return pd.Timestamp.min

    latest = max(bestaetigte, key=event_time)
    aktuelle_seite = latest.get("neue_seite")
    latest_check = _rollenwechsel_bestaetigung(latest)
    return aktuelle_seite, True, (
        f"letzter_bestaetigter_rollenwechsel={latest.get('urspruengliche_seite')}"
        f"->{aktuelle_seite}; aktuelle_kurslage_aendert_die_rolle_nicht"
    ), {
        "ereignisse": diag_events,
        "aktuelle_rolle": aktuelle_seite,
        "letztes_bestaetigtes_ereignis": latest,
        "bestaetigung": latest_check,
    }


def _aktive_kandidaten(kandidaten, aktueller_kurs):
    """Fasst gleiche Preis-/Ebenen-Strukturen zusammen und bestimmt ihre Rolle chronologisch."""
    gruppen = {}
    for k in kandidaten:
        key = (round(float(k["preis"]), 4), str(k.get("typ", "")), str(k.get("ebene", "")))
        gruppen.setdefault(key, []).append(k)

    out = []
    diagnostik = []
    for key, gruppe in gruppen.items():
        rollen_events = [k.get("rollenwechsel") for k in gruppe if k.get("rollenwechsel")]
        # Die unveränderte Basisrolle kommt aus der Gruppe. Ein bestätigter Rollenwechsel
        # darf sie nur chronologisch überschreiben; der aktuelle Kurs ist kein Kriterium.
        basis = next((k for k in gruppe if not k.get("rollenwechsel")), gruppe[0])
        aktuelle_seite, gueltig, grund, rollen_info = _rollenwechsel_aktuell_gueltig(
            basis, aktueller_kurs, rollen_events
        )

        for original in gruppe:
            v = dict(original)
            v["urspruengliche_seite"] = original.get("seite")
            v["aktuelle_seite"] = aktuelle_seite
            v["aktuelle_rolle_gueltig"] = gueltig
            v["rollenwechsel_pruefung"] = grund
            v["rollenwechsel_bestaetigung"] = rollen_info
            # Eine aktuell bestaetigte Rolle ist die einzige Seite, die fuer Entry/Stop/TP
            # verwendet werden darf. Die Rohobjekte bleiben separat diagnostisch sichtbar.
            v["seite"] = aktuelle_seite
            out.append(v)

        diagnostik.append({
            "preis": float(basis["preis"]),
            "typ": basis.get("typ"),
            "ebene": basis.get("ebene"),
            "urspruengliche_seiten": sorted(set(str(k.get("seite")) for k in gruppe)),
            "aktuelle_seite": aktuelle_seite,
            "aktuelle_rolle_gueltig": gueltig,
            "pruefung": grund,
            "rollenwechsel_bestaetigung": rollen_info,
        })
    return out, diagnostik


def _aktive_wendepunkt_kandidaten(reihe):
    """Erzeugt aus dem *aktuellen* Wendepunkt-Kanal zwei testweise Chartkandidaten."""
    info = mod.kanal_seit_wendepunkt(reihe)
    if info is None or info.get("typ") != "kanal":
        return [], {"status": "kein_aktiver_kanal"}
    x = mod.mdates.date2num(reihe.index[-1])
    k = info["daten"]
    oben = float(k["obere_linie"][0] * x + k["obere_linie"][1])
    unten = float(k["untere_linie"][0] * x + k["untere_linie"][1])
    ebene = "Intraday"
    formation = k.get("formation", info.get("typ"))
    kandidaten = [
        {
            "preis": oben, "typ": "aktiver_kanal", "seite": "widerstand",
            "ebene": ebene, "quelle": f"Aktiver Wendepunkt-Kanal ({formation})",
            "treffer": 0, "dynamisch": True,
        },
        {
            "preis": unten, "typ": "aktiver_kanal", "seite": "support",
            "ebene": ebene, "quelle": f"Aktiver Wendepunkt-Kanal ({formation})",
            "treffer": 0, "dynamisch": True,
        },
    ]
    return kandidaten, {
        "status": "aktiv",
        "start": str(info["start"]),
        "ende": str(reihe.index[-1]),
        "formation": formation,
        "obere_grenze": oben,
        "untere_grenze": unten,
    }


def _bounce_confirmation(reihe, support_price, lookback=5):
    """Prueft einen abgeschlossenen Support-Bounce ohne Look-ahead."""
    if reihe is None or len(reihe) < 3:
        return {"bestaetigt": False, "grund": "zu_wenig_bars"}
    start = max(0, len(reihe) - lookback)
    teil = reihe.iloc[start:]
    touched = []
    for i in range(len(teil) - 1):
        if float(teil["Low"].iloc[i]) <= float(support_price):
            touched.append(i)
    if not touched:
        return {"bestaetigt": False, "grund": "kein_abgeschlossener_touch_des_supports"}
    touch_i = touched[-1]
    for j in range(touch_i + 1, len(teil)):
        if float(teil["Close"].iloc[j]) > float(support_price):
            return {
                "bestaetigt": True,
                "touch_zeit": str(teil.index[touch_i]),
                "bestaetigungs_zeit": str(teil.index[j]),
                "bestaetigungs_close": float(teil["Close"].iloc[j]),
                "regel": "Low beruehrt/unterschreitet Support; spaeterer abgeschlossener Close schliesst wieder darueber.",
            }
    return {"bestaetigt": False, "grund": "touch_ohne_bestaetigungs_close_darueber"}


def _richtung_aus_kanal(reihe, ebene):
    """Leitet nur aus der vorhandenen Kanal-/Trendstruktur eine Richtung ab."""
    if reihe is None or len(reihe) < 30:
        return {"ebene": ebene, "richtung": "NEUTRAL", "grund": "zu_wenig_daten"}
    info = mod.kanal_seit_wendepunkt(reihe)
    if info is None:
        return {"ebene": ebene, "richtung": "NEUTRAL", "grund": "keine_aktive_kanalstruktur"}
    daten = info.get("daten")
    if info.get("typ") == "kanal":
        obere = float(daten["obere_linie"][0])
        untere = float(daten["untere_linie"][0])
        mittel = (obere + untere) / 2.0
        if mittel < -1e-10:
            richtung = "ABWAERTS"
        elif mittel > 1e-10:
            richtung = "AUFWAERTS"
        else:
            richtung = "NEUTRAL"
        return {
            "ebene": ebene,
            "richtung": richtung,
            "grund": f"aktive {daten.get('formation', 'Kanal')}struktur; Mittelliniensteigung {mittel:.8g}",
            "formation": daten.get("formation"),
            "obere_steigung": obere,
            "untere_steigung": untere,
        }
    steigung = float(daten[0])
    return {
        "ebene": ebene,
        "richtung": "AUFWAERTS" if steigung > 1e-10 else "ABWAERTS" if steigung < -1e-10 else "NEUTRAL",
        "grund": f"aktive Trendstruktur; Steigung {steigung:.8g}",
    }


def _uebergeordnete_richtung(intraday, daily):
    sechs_m = daily.loc[daily.index >= (daily.index[-1] - mod.pd.DateOffset(months=mod.LANGFRIST_MONATE))]
    result = {
        "6M": _richtung_aus_kanal(sechs_m, "6M"),
        "Tageschart": _richtung_aus_kanal(daily, "Tageschart"),
        "Intraday": _richtung_aus_kanal(intraday, "Intraday"),
    }
    gegen = [result[x]["richtung"] == "ABWAERTS" for x in ("6M", "Tageschart")]
    warnung = all(gegen)
    result["long_warnung"] = warnung
    result["long_setup"] = "Gegenbewegung / Counter-Trend" if warnung else "normales Setup"
    result["grund"] = (
        "Long laeuft gegen die uebergeordnete Struktur."
        if warnung
        else "6M und Tageschart sind nicht gemeinsam abwaertsgerichtet; keine uebergeordnete Long-Warnung."
    )
    return result



def _stop_aus_support(support):
    """Ermittelt einen Stop nur aus einer explizit belegten Support-Unterkante."""
    if support is None:
        return None, "keine_supportstruktur"
    raw_unterkante = support.get("untere_grenze")
    try:
        unterkante = float(raw_unterkante) if raw_unterkante is not None else None
    except (TypeError, ValueError):
        unterkante = None
    if unterkante is not None and unterkante < float(support["preis"]):
        return unterkante, "bestimmbar"
    return None, "Unterkante nicht bestimmbar"

def _build_structure_chain(setup, intraday, daily, kurs, kanal_aktuell=None):
    """Baut die Test-Variante der vollstaendigen Chartkette.

    Wichtig: Diese Funktion aendert NICHT die Produktionsfunktion bestimme_chart_setup.
    Sie ersetzt im Test lediglich die bisherige statische/alte Kanalbetrachtung durch
    die aktuell gueltigen Strukturen und prueft Entry/Stop/TP1/TP2 als zusammenhaengende
    Chartkette. CRV ist nur Zulassungsschranke, niemals Score oder Auswahlgewicht.
    """
    basis = list(setup.get("kandidaten", []))
    aktive, rollen_diag = _aktive_kandidaten(basis, kurs)
    dynamisch, kanal_diag = _aktive_wendepunkt_kandidaten(intraday)
    kandidaten = aktive + dynamisch

    # Keine doppelte Struktur, wenn der dynamische Kanal bereits als identisches Level
    # in der Kandidatenliste vorhanden ist.
    dedup = {}
    for k in kandidaten:
        key = (round(float(k["preis"]), 4), k.get("seite"), k.get("ebene"))
        dedup[key] = k
    kandidaten = list(dedup.values())

    supports = _konsolidiere_nahe_zielstrukturen(
        [k for k in kandidaten
         if k.get("seite") == "support"
         and k.get("aktuelle_rolle_gueltig", True)
         and float(k["preis"]) < kurs],
        toleranz=3.0,
    )
    supports = sorted(supports, key=lambda k: float(k["preis"]), reverse=True)
    resistances = _konsolidiere_nahe_zielstrukturen(
        [k for k in kandidaten
         if k.get("seite") == "widerstand"
         and k.get("aktuelle_rolle_gueltig", True)
         and float(k["preis"]) > kurs],
        toleranz=3.0,
    )
    resistances = sorted(resistances, key=lambda k: float(k["preis"]))

    # ENTRY: kein willkuerlicher Preis. Der aktuelle abgeschlossene Close ist nur dann
    # ein Test-Entry, wenn eine darunterliegende Chartstruktur in den letzten 5 Bars
    # beruehrt und danach per Close zurueckerobert wurde.
    entry_candidates = []
    for support in supports:
        bounce = _bounce_confirmation(intraday, float(support["preis"]), lookback=5)
        entry_candidates.append({
            "preis": float(support["preis"]),
            "quelle": support.get("quelle"),
            "ebene": support.get("ebene"),
            "typ": support.get("typ"),
            "rollenwechsel": support.get("rollenwechsel"),
            "bounce": bounce,
        })
    bestaetigte_entries = [x for x in entry_candidates if x["bounce"].get("bestaetigt")]
    entry_basis = bestaetigte_entries[0] if bestaetigte_entries else None
    test_entry = float(entry_basis["bounce"]["bestaetigungs_close"]) if entry_basis else None

    stop_candidates = []
    if test_entry is not None:
        for support in sorted(
            _konsolidiere_nahe_zielstrukturen(
                [k for k in kandidaten
                 if k.get("seite") == "support"
                 and k.get("aktuelle_rolle_gueltig", True)
                 and float(k["preis"]) < test_entry],
                toleranz=3.0,
            ),
            key=lambda k: float(k["preis"]), reverse=True,
        ):
            stop_candidates.append({
                "preis": float(support["preis"]),
                "quelle": support.get("quelle"),
                "ebene": support.get("ebene"),
                "typ": support.get("typ"),
                "rollenwechsel": support.get("rollenwechsel"),
                "untere_grenze": support.get("untere_grenze"),
                "obere_grenze": support.get("obere_grenze"),
                "begruendung": "naechste aktuell gueltige Supportstruktur unter dem bestaetigten Entry",
            })

    stop_basis = stop_candidates[0] if stop_candidates else None
    stop_unterkante, stop_unterkante_status = _stop_aus_support(stop_basis)
    stop = stop_unterkante

    # Zusaetzliche Diagnose: neben dem naechsten Support wird auch der
    # uebernaechste Support als moegliche strukturelle Stopbasis betrachtet.
    # Wir erzwingen keinen kuenstlichen Dollar-/ATR-Puffer. Ein "minimal unter"
    # dem Support ist nur dann numerisch bestimmbar, wenn die Quelle eine
    # explizite Unterkante liefert; andernfalls bleibt der Level selbst die
    # diagnostische Referenz.
    stop_alternativen = []
    if test_entry is not None:
        for idx, support in enumerate(sorted(
            _konsolidiere_nahe_zielstrukturen(
                [k for k in kandidaten
                 if k.get("seite") == "support"
                 and k.get("aktuelle_rolle_gueltig", True)
                 and float(k["preis"]) < test_entry],
                toleranz=3.0,
            ),
            key=lambda k: float(k["preis"]), reverse=True,
        )[:2], start=1):
            stop_alternativen.append({
                "rang_unter_entry": idx,
                "support": support,
                "stop_level": float(support["preis"]),
                "stop_unterkante": support.get("untere_grenze"),
                "minimal_unter_berechenbar": (
                    support.get("untere_grenze") is not None
                    and float(support.get("untere_grenze")) < float(support["preis"])
                ) if support.get("untere_grenze") is not None else False,
                "status": (
                    "Unterkante bestimmbar"
                    if support.get("untere_grenze") is not None and float(support.get("untere_grenze")) < float(support["preis"])
                    else "Unterkante nicht bestimmbar"
                ),
                "begruendung": (
                    "explizite Support-Unterkante vorhanden; Stop wird an der Unterkante gesetzt"
                    if support.get("untere_grenze") is not None and float(support.get("untere_grenze")) < float(support["preis"])
                    else "keine belastbare Support-Unterkante in der gelieferten Quelle; Stop bleibt unbestimmbar und darf nicht direkt auf die Supportlinie gesetzt werden"
                ),
            })
    risiko = (test_entry - stop) if test_entry is not None and stop is not None else None

    tp_kette = {"tp1": None, "tp2": None, "tp2_pruefung": None, "alle_tp_kandidaten": []}
    if test_entry is not None and risiko and risiko > 0:
        tp_kette = _ermittle_tp_kette(kandidaten, test_entry, stop)
    tp_candidates = tp_kette["alle_tp_kandidaten"]
    tp1 = tp_kette["tp1"]
    tp2 = tp_kette["tp2"]
    tp2_pruefung = tp_kette["tp2_pruefung"]

    # HYPOTHETISCHE TP-KETTE: reine Diagnose, niemals Trade-Auswahl.
    # Sie verwendet den aktuellen Kurs nur als hypothetischen Entry, damit TP1/TP2
    # auch dann vollständig geprüft werden können, wenn aktuell kein bestätigter Entry
    # vorliegt. Es wird dadurch KEIN Trade erzeugt.
    hypothetischer_entry = float(kurs)
    hypo_stop_candidates = sorted(
        _konsolidiere_nahe_zielstrukturen(
            [k for k in kandidaten
             if k.get("seite") == "support"
             and k.get("aktuelle_rolle_gueltig", True)
             and float(k["preis"]) < hypothetischer_entry],
            toleranz=3.0,
        ),
        key=lambda k: float(k["preis"]), reverse=True,
    )
    hypothetischer_stop_basis = hypo_stop_candidates[0] if hypo_stop_candidates else None
    hypothetischer_stop, hypothetischer_stop_status = _stop_aus_support(hypothetischer_stop_basis)
    hypothetischer_stop_diagnose = False
    if hypothetischer_stop is None and hypothetischer_stop_basis is not None:
        # Nur fuer die hypothetische Diagnose: der belegte Supportpreis darf als
        # Risikoreferenz dienen. Das ist ausdruecklich KEIN Produktions-Stop.
        hypothetischer_stop = float(hypothetischer_stop_basis["preis"])
        hypothetischer_stop_status = "Unterkante nicht bestimmbar; Supportpreis nur diagnostische Risikoreferenz"
        hypothetischer_stop_diagnose = True
    hypothetischer_stop_regel = (
        "explizite Support-Unterkante vorhanden; hypothetischer Stop wird an der Unterkante gesetzt"
        if not hypothetischer_stop_diagnose and hypothetischer_stop_status == "bestimmbar"
        else hypothetischer_stop_status
    )
    hypothetisches_risiko = (
        hypothetischer_entry - hypothetischer_stop
        if hypothetischer_stop is not None and hypothetischer_stop < hypothetischer_entry
        else None
    )

    hypothetische_tp_kette = {
        "diagnostisch_nur": True,
        "trade_ausloesen": False,
        "hypothetischer_entry": hypothetischer_entry,
        "entry_grundlage": "aktueller Kurs nur als Diagnosewert; kein bestaetigter Entry",
        "hypothetischer_stop": hypothetischer_stop,
        "hypothetischer_stop_basis": hypothetischer_stop_basis,
        "hypothetischer_stop_unterkante_status": hypothetischer_stop_status,
        "hypothetisches_risiko": hypothetisches_risiko,
        "hypothetischer_stop_regel": hypothetischer_stop_regel,
        "hypothetischer_stop_diagnose": hypothetischer_stop_diagnose,
        "tp1": None,
        "tp2": None,
        "tp2_pruefung": None,
        "alle_tp_kandidaten": [],
        "chart_hierarchie": [
            {"chart_rang": _struktur_rang(k)[0], "begruendung": _struktur_rang(k)[1], "preis": float(k["preis"]), "typ": k.get("typ"), "ebene": k.get("ebene"), "quelle": k.get("quelle")}
            for k in _chart_hierarchie(kandidaten, hypothetischer_entry)
        ],
    }
    if hypothetisches_risiko and hypothetisches_risiko > 0:
        hypo_kette = _ermittle_tp_kette(kandidaten, hypothetischer_entry, hypothetischer_stop)
        hypothetische_tp_kette.update(hypo_kette)

    hypothetische_tp_kette["regel"] = (
        "Charttechnischer Grund ist zwingend. Aktuelle Chartrolle und Preisnaehe "
        "bestimmen die Kandidaten; erst danach wird CRV berechnet. CRV > 1 ist "
        "nur Zulassung fuer TP1, kein Score und keine Auswahlbegruendung. TP1 "
        "ist Pflicht fuer einen zulaessigen Trade. TP2 ist optional: Nur die "
        "unmittelbar naechste hoehere gueltige Chartstruktur wird geprueft; "
        "CRV >= 2 ist nur deren Zulassungsfilter. Erfuellt diese Struktur CRV < 2, "
        "ist TP2 nicht vorhanden und es werden keine spaeteren Strukturen geprueft "
        "oder uebersprungen. Die aktuelle Rolle bestimmt die Funktion."
    )

    richtung = _uebergeordnete_richtung(intraday, daily)
    return {
        "kurs": kurs,
        "uebergeordnete_richtung": richtung,
        "long_warnung": richtung.get("long_warnung"),
        "long_setup": richtung.get("long_setup"),
        "entry": {
            "test_entry": test_entry,
            "status": "charttechnisch_bestaetigt" if entry_basis else "kein_bestaetigter_entry",
            "basis_support": entry_basis,
            "alle_entry_support_kandidaten": entry_candidates,
            "regel": "Entry nur nach bestaetigtem Bounce an einer aktuell gueltigen Supportstruktur; sonst kein kuenstlicher Entry.",
        },
        "stop": {
            "test_stop": stop,
            "basis": stop_basis,
            "unterkante": stop_unterkante,
            "unterkante_status": stop_unterkante_status,
            "alle_stop_support_kandidaten": stop_candidates,
            "stop_alternativen_naechster_und_uebernaechster_support": stop_alternativen,
            "risiko": risiko,
            "regel": "Stop wird nicht direkt auf die Supportlinie gesetzt. Verwendet wird nur eine explizit belegte Support-Unterkante; andernfalls Status 'Unterkante nicht bestimmbar' und kein kuenstlicher ATR-/Dollar-Puffer.",
        },
        "tp": {
            "zone_toleranz_punkte": 3.0,
        "tp1": tp1,
            "tp2": tp2,
            "tp2_pruefung": tp2_pruefung,
            "alle_tp_kandidaten": tp_candidates,
            "regel": "Jeder Entry/Stop/TP benoetigt zuerst einen charttechnischen Grund und eine aktuell gueltige Chartrolle. Widerstaende werden nach Preisnaehe geprueft; Strukturen innerhalb von 3,0 Punkten bilden eine gemeinsame Zone; erst danach CRV. CRV > 1 ist nur Zulassung fuer TP1. TP1 ist Pflicht; TP2 ist optional. TP2 = unmittelbar naechste hoehere gueltige Chartstruktur; CRV >= 2 ist nur deren Zulassungsfilter. Wenn die naechste Struktur CRV < 2 hat, bleibt TP2 leer und keine spaetere Struktur wird geprueft. Kein Score, kein 2R/3R-Fallback, kein Ueberspringen. Die aktuelle bestaetigte Rolle ist massgeblich; ein historischer R->S-Wechsel bleibt Support, bis ein spaeterer bestaetigter S->R-Wechsel vorliegt.",
        },
        "hypothetische_tp_kette": hypothetische_tp_kette,
        "rollenwechsel": {
            "kandidaten": rollen_diag,
            "regel": "Aktuelle Rolle wird ausschliesslich aus der Chronologie bestaetigter Rollenwechsel bestimmt; der aktuelle Kurs allein hebt die Rolle nicht auf.",
        },
        "aktiver_kanal": kanal_diag,
        "uebergeordnete_richtung": richtung,
        "trade": {
            "status": "trade_zulaessig" if tp1 is not None else "verwerfen",
            "tp2_optional": True,
            "crv_tp1": tp1["crv"] if tp1 else None,
            "crv_tp2": tp2["crv"] if tp2 else None,
        },
    }


def main():
    if not os.environ.get("TWELVEDATA_API_KEY"):
        raise SystemExit("TWELVEDATA_API_KEY fehlt – echter Gold-Test kann nicht ausgefuehrt werden.")

    daten = mod.hole_kursdaten()
    intraday = daten["intraday_reihe"]
    daily = mod.hole_langfrist_daten(monate=36)
    if daily is None or daily.empty:
        raise SystemExit("Keine ausreichenden Tagesdaten fuer den Test erhalten.")

    kurs = float(daten["realtime"])
    zeitpunkt = intraday.index[-1]
    setup = mod.bestimme_chart_setup(
        zeitpunkt,
        intraday_reihe=intraday,
        daily_reihe=daily,
        aktueller_kurs=kurs,
    )

    print("=== CHARTTECHNIK-TEST: EIN GOLD-SETUP ===")
    print(f"Kurs:     {kurs:.4f}")
    print(f"Intraday: {intraday.index[-1]} | Bars: {len(intraday)}")
    print(f"Daily:    {daily.index[-1]} | Bars: {len(daily)}")

    # Die neue Strukturkette ist ab hier die EINZIGE Entscheidungsquelle.
    # bestimme_chart_setup() liefert nur noch die Roh-/Basisstrukturen; dessen
    # bereits berechnete Entry/Stop/TP-Werte werden bewusst NICHT verwendet.
    structure_chain = _build_structure_chain(setup, intraday, daily, kurs)

    kanal_gesamt = mod.diagnostiziere_kanalvarianten(
        intraday, mod.INTRADAY_KANAL_FENSTER, mod.INTRADAY_KANAL_MIN_PUNKTE
    )
    kanal_aktuell = _channel_at(intraday)
    kanal_verlauf = _channel_evolution(intraday, checkpoints=12)

    rollenwechsel = []
    for k in setup.get("kandidaten", []):
        rw = k.get("rollenwechsel")
        if rw:
            rollenwechsel.append({
                "preis": k.get("preis"),
                "ebene": k.get("ebene"),
                "typ": k.get("typ"),
                "urspruengliche_seite": rw.get("urspruengliche_seite"),
                "neue_seite": rw.get("neue_seite"),
                "quelle": k.get("quelle"),
                "support_oder_widerstand_zeit": rw.get("support_zeit", rw.get("widerstand_zeit")),
                "bruch_zeit": rw.get("bruch_zeit"),
                "retest_zeit": rw.get("retest_zeit"),
                "bestaetigungs_bars": rw.get("bestaetigungs_bars"),
            })

    print("\n=== UEBERGEORDNETE RICHTUNG / LONG-WARNUNG ===")
    print(json.dumps(_safe(structure_chain["uebergeordnete_richtung"]), ensure_ascii=False, indent=2, default=str))
    print("\n=== FINALES CHART-SETUP (NEUE STRUKTURKETTE) ===")
    print(json.dumps(_safe(structure_chain), ensure_ascii=False, indent=2, default=str))
    print("\n=== BASIS-CHARTANALYSE (NUR DIAGNOSTIK, NICHT ENTSCHEIDUNGSRELEVANT) ===")
    print(json.dumps(_safe(setup), ensure_ascii=False, indent=2, default=str))
    print("\n=== AKTUELLER WENDEPUNKT-KANAL ===")
    print(json.dumps(_safe(kanal_aktuell), ensure_ascii=False, indent=2, default=str))
    print("\n=== KANALVERLAUF: LETZTE ENTWICKLUNG ===")
    print(json.dumps(_safe(kanal_verlauf), ensure_ascii=False, indent=2, default=str))
    print("\n=== GESAMTKANAL CLOSE vs HIGH/LOW ===")
    print(json.dumps(_safe(kanal_gesamt), ensure_ascii=False, indent=2, default=str))
    print("\n=== SUPPORT/WIDERSTAND-ROLLENWECHSEL ===")
    print(json.dumps(_safe(rollenwechsel), ensure_ascii=False, indent=2, default=str))

    out = {
        "realtime": kurs,
        "intraday_end": str(intraday.index[-1]),
        "daily_end": str(daily.index[-1]),
        "uebergeordnete_richtung": _safe(structure_chain["uebergeordnete_richtung"]),
        "long_warnung": structure_chain.get("long_warnung"),
        "long_setup": structure_chain.get("long_setup"),
        # EINZIGE Entscheidungsquelle fuer das Test-Setup.
        "chart_setup": _safe(structure_chain),
        # Alte Funktion nur als Roh-/Diagnosequelle; ihre Auswahlwerte werden
        # nicht als Entry/Stop/TP uebernommen.
        "basis_chartanalyse_diagnose": _safe(setup),
        "strukturkette": _safe(structure_chain),
        "aktiver_wendepunkt_kanal": _safe(kanal_aktuell),
        "kanal_verlauf": _safe(kanal_verlauf),
        "kanal_gesamt_diagnose": _safe(kanal_gesamt),
        "rollenwechsel_diagnose": _safe(rollenwechsel),
    }
    Path("charttechnik_test_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\nDiagnose gespeichert: charttechnik_test_result.json")


if __name__ == "__main__":
    main()
