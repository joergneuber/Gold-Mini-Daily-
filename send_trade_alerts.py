"""Versendet separate Trade-Event-Mails für Gold.

Die Datei trade_alerts.json wird von mini_daily_gold.py erzeugt. Die Mail
enthält ausschließlich Gold-Level; WKN, Optionsschein und Hebel bleiben bewusst
außen vor, weil diese Auswahl beim Nutzer liegt.

Die Datei trade_alert_state.json wird im GitHub-Workflow per Actions-Cache
zwischen Läufen erhalten, damit dasselbe Ereignis nur einmal gemeldet wird.
"""
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

EVENT_FILE = Path("trade_alerts.json")
STATE_FILE = Path("trade_alert_state.json")


def de_zahl(n):
    return f"{float(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hole_neue_events():
    if not EVENT_FILE.exists():
        return []
    with EVENT_FILE.open("r", encoding="utf-8") as f:
        daten = json.load(f)
    events = daten.get("events", [])
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
            gesehen = set(state.get("sent_event_ids", []))
        except (json.JSONDecodeError, OSError):
            gesehen = set()
    else:
        gesehen = set()
    neue = [e for e in events if e.get("event_id") not in gesehen]
    return neue


def markiere_gesendet(events):
    ids = set()
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                ids = set(json.load(f).get("sent_event_ids", []))
        except (json.JSONDecodeError, OSError):
            ids = set()
    ids.update(e["event_id"] for e in events)
    # Nur die letzten 200 IDs behalten, damit der Cache klein bleibt.
    ids = list(sorted(ids))[-200:]
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump({"sent_event_ids": ids}, f, ensure_ascii=False, indent=2)


def mailtext(event):
    system = event["system"]
    event_name = event["event"]
    zeit = datetime.fromisoformat(event["zeit"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin"))
    titel = {
        "ENTRY": "🟢 GOLD LONG – ENTRY",
        "TP1": "🟢 GOLD LONG – TP1 ERREICHT",
        "TP2": "🟢 GOLD LONG – TP2 ERREICHT",
        "STOP": "🔴 GOLD LONG – STOP ERREICHT",
    }[event_name]
    neue_stop = event["stop"]
    return (f"{titel}\n\n"
            f"System:     {system}\n"
            f"Zeit:       {zeit.strftime('%d.%m.%Y %H:%M')} Uhr\n\n"
            f"Einstieg:   {de_zahl(event['einstieg'])} USD\n"
            f"Stop:       {de_zahl(event['stop'])} USD\n"
            f"TP1:        {de_zahl(event['tp1'])} USD\n"
            f"TP2:        {de_zahl(event['tp2'])} USD\n\n"
            f"Ereignis:   {event_name}\n"
            f"Neuer Stop nach Ereignis: {de_zahl(neue_stop)} USD\n\n"
            f"WKN/Optionsschein/Hebel: vom Nutzer selbst gewählt.\n")


def sende(event):
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    msg = EmailMessage()
    msg["Subject"] = f"Gold {event['event']} – {event['system']} – {jetzt.strftime('%d.%m.%Y %H:%M')}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]
    msg.set_content(mailtext(event))
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
    print(f"Trade-Alert-Mail versendet: {event['event_id']}")


def main():
    neue = hole_neue_events()
    if not neue:
        print("Keine neuen Gold-Trade-Events – keine separate Alert-Mail.")
        return
    for event in neue:
        sende(event)
    markiere_gesendet(neue)


if __name__ == "__main__":
    main()
