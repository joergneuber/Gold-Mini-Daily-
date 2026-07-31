"""
Lädt mini_daily_gold.html und chart.png in einen festen Google-Drive-Ordner hoch.
Erwartet ein Service-Account-JSON als Umgebungsvariable GOOGLE_SERVICE_ACCOUNT_JSON
und die Ziel-Ordner-ID als GOOGLE_DRIVE_FOLDER_ID (Secrets im Repo hinterlegen).

Hinweis: Der Ordner muss vorher in Drive angelegt und mit der Service-Account-
E-Mail-Adresse geteilt werden (Rolle: Bearbeiter), sonst schlägt der Upload fehl.
"""

import os
import json
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def hole_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def hochladen(pfad, mime_type, ordner_id, service):
    zeitstempel = datetime.now().strftime("%Y-%m-%d_%H-%M")
    dateiname = f"{zeitstempel}_{os.path.basename(pfad)}"
    metadata = {"name": dateiname, "parents": [ordner_id]}
    media = MediaFileUpload(pfad, mimetype=mime_type)
    service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Hochgeladen: {dateiname}")


def main():
    ordner_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
    service = hole_service()
    hochladen("mini_daily_gold.html", "text/html", ordner_id, service)
    hochladen("chart.png", "image/png", ordner_id, service)


if __name__ == "__main__":
    main()
