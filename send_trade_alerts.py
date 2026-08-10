"""
Separate Trade-Event-Mails fuer das Gold-Signal.

Die WKN/der Optionsschein ist absichtlich NICHT Bestandteil dieser Datei:
der Nutzer waehlt den Hebel/das Instrument selbst. Gemeldet werden nur die
Spot-Gold-Marken des jeweiligen Signals.

Input: trade_alerts.json (vom Mini-Daily-Lauf erzeugt)
Secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_EMPFAENGER
"""

import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo


TYPEN = {
    "ENTRY": ("🟢", "ENTRY / KAUFSIGNAL"),
    "STOP": ("🔴", "STOP / AUSSTIEG"),
    "TP1": ("🟢", "TP1 ERREICHT"),
    "TP2": ("🟢", "TP2 ERREICHT"),
}

SYSTEM_LABEL = {
    "RANGE_AUSBRUCH_1H": "Range-Ausbruch 1h",
    "POSITIONSTRADING_TAGESBASIS": "Positionstrading Tagesbasis",
}


def fmt(value):
    if value is None:
        return "-"
    return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def lade_events():
    try:
        with open("trade_alerts.json", "r", encoding="utf-8") as f:
            events = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(events, list):
        return []
    return [e for e in events if e.get("typ") in TYPEN]


def baue_mail(event):
    typ = event["typ"]
    emoji, titel = TYPEN[typ]
    system = SYSTEM_LABEL.get(event.get("system"), event.get("system", "Gold-Signal"))
    zeit = datetime.fromisoformat(event["zeit"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin"))

    entry = fmt(event.get("einstieg"))
    stop = fmt(event.get("stop"))
    tp1 = fmt(event.get("tp1"))
    tp2 = fmt(event.get("tp2"))

    if typ == "ENTRY":
        aktion = "Neues Long-Setup ausgeloest. Der Optionsschein/Hebel wird manuell gewaehlt."
    elif typ == "STOP":
        aktion = f"Stop erreicht bei {stop} USD. Das Spot-Gold-Setup ist beendet."
    elif typ == "TP1":
        aktion = f"TP1 erreicht. Neuer Stop laut Regelwerk: {stop} USD (Breakeven)."
    else:
        aktion = f"TP2 erreicht. Neuer Stop laut Regelwerk: {stop} USD (TP1 / anschliessendes Nachziehen)."

    text = f"""NEUBER PRECIOUS METALS
GOLD – {titel}

Signaltyp: {system}
Zeit:       {zeit.strftime('%d.%m.%Y %H:%M')} Uhr

Einstieg:   {entry} USD
Stop:       {stop} USD
TP1:        {tp1} USD
TP2:        {tp2} USD

{aktion}

Hinweis: Keine WKN / kein Optionsschein hinterlegt. Das Instrument und der Hebel werden manuell gewaehlt.
"""

    html = f"""
    <html><body style="background:#14110d;color:#ece6d9;font-family:Arial,sans-serif;padding:22px;">
    <div style="color:#a89d87;font-size:12px;letter-spacing:2px;">NEUBER PRECIOUS METALS</div>
    <h1 style="color:#e8b95c;margin-bottom:6px;">{emoji} GOLD – {titel}</h1>
    <p style="color:#a89d87;">{system} · {zeit.strftime('%d.%m.%Y %H:%M')} Uhr</p>
    <table style="border-collapse:collapse;font-family:monospace;font-size:15px;">
      <tr><td style="padding:5px 20px 5px 0;">Einstieg</td><td><b>{entry} USD</b></td></tr>
      <tr><td style="padding:5px 20px 5px 0;">Stop</td><td><b>{stop} USD</b></td></tr>
      <tr><td style="padding:5px 20px 5px 0;">TP1</td><td><b>{tp1} USD</b></td></tr>
      <tr><td style="padding:5px 20px 5px 0;">TP2</td><td><b>{tp2} USD</b></td></tr>
    </table>
    <p style="margin-top:22px;">{aktion}</p>
    <p style="color:#a89d87;font-size:11px;margin-top:24px;">Keine WKN / kein Optionsschein hinterlegt. Instrument und Hebel werden manuell gewaehlt.</p>
    </body></html>
    """
    return text, html, f"Gold {system} – {titel}"


def sende(event):
    text, html, subject_tail = baue_mail(event)
    msg = EmailMessage()
    msg["Subject"] = f"{TYPEN[event['typ']][0]} Gold: {subject_tail}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def main():
    events = lade_events()
    if not events:
        print("Keine neuen Gold-Trade-Events - keine Alert-Mail.")
        return

    for event in events:
        sende(event)
        print(f"Trade-Alert versendet: {event['system']} {event['typ']} {event['zeit']}")


if __name__ == "__main__":
    main()
