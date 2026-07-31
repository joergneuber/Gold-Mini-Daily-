"""
Verschickt den Report per E-Mail (SMTP, z.B. Gmail mit App-Passwort).

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


def main():
    with open("mini_daily_gold.html", "r", encoding="utf-8") as f:
        html = f.read()

    msg = EmailMessage()
    msg["Subject"] = f"Mini Daily: Gold - {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]
    msg.set_content("Dieser Report benötigt einen HTML-fähigen E-Mail-Client.")
    msg.add_alternative(html, subtype="html")

    with open("chart.png", "rb") as img:
        msg.get_payload()[1].add_related(img.read(), maintype="image", subtype="png", cid="chart")

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)

    print("Mail versendet an", os.environ["MAIL_EMPFAENGER"])


if __name__ == "__main__":
    main()
