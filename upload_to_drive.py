"""
Lädt mini_daily_gold.html und chart.png in einen festen Google-Drive-Ordner hoch.

Nutzt OAuth mit einem PRIVATEN Google-Konto (kein Service-Account - Service-Accounts
haben kein eigenes Speicherkontingent auf normalem Drive). Die Datei landet dadurch
in deinem eigenen Speicherplatz.

Benötigte Secrets:
- GOOGLE_OAUTH_CLIENT_ID
- GOOGLE_OAUTH_CLIENT_SECRET
- GOOGLE_OAUTH_REFRESH_TOKEN
- GOOGLE_DRIVE_FOLDER_ID

Wie du diese drei OAuth-Werte einmalig bekommst: siehe README.md, Abschnitt
"Einmalige OAuth-Einrichtung".
"""

import os
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def hole_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds)


def hochladen(pfad, mime_type, ordner_id, service):
    zeitstempel = datetime.now().strftime("%Y-%m-%d_%H-%M")
    dateiname = f"{zeitstempel}_{os.path.basename(pfad)}"
    metadata = {"name": dateiname, "parents": [ordner_id]}
    media = MediaFileUpload(pfad, mimetype=mime_type)
    service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Hochgeladen: {dateiname}")


def main():
    ordner_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"].strip()
    print(f"Ordner-ID Länge: {len(ordner_id)} Zeichen (zur Kontrolle, ohne Wert preiszugeben)")
    if ordner_id.startswith("http"):
        raise RuntimeError(
            "GOOGLE_DRIVE_FOLDER_ID scheint eine komplette URL zu sein, nicht nur die ID. "
            "Nur den Teil nach '/folders/' verwenden."
        )
    service = hole_service()
    hochladen("mini_daily_gold.html", "text/html", ordner_id, service)
    hochladen("chart.png", "image/png", ordner_id, service)


if __name__ == "__main__":
    main()
