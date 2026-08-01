"""
Lädt mini_daily_gold.html, mini_daily_gold.txt und chart.png in einen festen
Google-Drive-Ordner hoch, mit lesbaren Dateinamen (Zeitstempel_Briefing.* / Grafik.png).

Nutzt OAuth mit einem privaten Google-Konto (kein Service-Account - Service-Accounts
haben kein eigenes Speicherkontingent auf normalem Drive). Die Datei landet dadurch
in deinem eigenen Speicherplatz.

Benötigte Secrets:
- GOOGLE_OAUTH_TOKEN_JSON (kompletter JSON-String aus get_refresh_token.py)
- GOOGLE_DRIVE_FOLDER_ID
"""

import os
import json
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def hole_service():
    token_data = json.loads(os.environ["GOOGLE_OAUTH_TOKEN_JSON"])
    creds = Credentials(
        token=None,
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds)


def hochladen(pfad, mime_type, ordner_id, service, anzeige_name):
    zeitstempel = datetime.now().strftime("%Y-%m-%d_%H-%M")
    dateiname = f"{zeitstempel}_{anzeige_name}"
    metadata = {"name": dateiname, "parents": [ordner_id]}
    media = MediaFileUpload(pfad, mimetype=mime_type)
    service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Hochgeladen: {dateiname}")


def main():
    ordner_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"].strip()
    if ordner_id.startswith("http"):
        raise RuntimeError(
            "GOOGLE_DRIVE_FOLDER_ID scheint eine komplette URL zu sein, nicht nur die ID. "
            "Nur den Teil nach '/folders/' verwenden."
        )
    service = hole_service()
    hochladen("mini_daily_gold.html", "text/html", ordner_id, service, "Briefing.html")
    hochladen("mini_daily_gold.txt", "text/plain", ordner_id, service, "Briefing.txt")
    hochladen("chart.png", "image/png", ordner_id, service, "Grafik.png")


if __name__ == "__main__":
    main()
