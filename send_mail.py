"""
Verschickt den Report per E-Mail (SMTP, z.B. Gmail mit App-Passwort).

Schickt sowohl eine HTML-Version als auch eine reine Klartext-Version mit
(als Fallback UND als Anhang) - manche Mail-Clients entfernen Style-Angaben
aus dem HTML, dann bleibt der Klartext lesbar.

Benötigte Secrets:
- SMTP_HOST (z.B. smtp.gmail.com)
- SMTP_PORT (z.B. 587)
- SMTP_USER (Absender-Adresse)
- SMTP_PASSWORD (App-Passwort, kein normales Kontopasswort)
- MAIL_EMPFAENGER (Ziel-Adresse)
"""

import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo


def main():
    with open("mini_daily_gold.html", "r", encoding="utf-8") as f:
        html = f.read()
    with open("mini_daily_gold.txt", "r", encoding="utf-8") as f:
        text = f.read()

    jetzt_berlin = datetime.now(ZoneInfo("Europe/Berlin"))

    msg = EmailMessage()
    msg["Subject"] = f"Mini Daily: Gold - {jetzt_berlin.strftime('%d.%m.%Y %H:%M')}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with open("chart.png", "rb") as img:
        msg.get_payload()[1].add_related(img.read(), maintype="image", subtype="png", cid="chart")
    with open("chart_tages.png", "rb") as img:
        msg.get_payload()[1].add_related(img.read(), maintype="image", subtype="png", cid="chart_tages")
    with open("chart_langfrist.png", "rb") as img:
        msg.get_payload()[1].add_related(img.read(), maintype="image", subtype="png", cid="chart_lang")

    # Klartext zusätzlich als Anhang, falls der Mail-Client trotzdem nur HTML anzeigt
    zeitstempel = jetzt_berlin.strftime("%Y-%m-%d_%H-%M")
    with open("mini_daily_gold.txt", "rb") as f:
        msg.add_attachment(f.read(), maintype="text", subtype="plain",
                            filename=f"{zeitstempel}_Briefing.txt")

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)

    print("Mail versendet an", os.environ["MAIL_EMPFAENGER"])


if __name__ == "__main__":
    main()
