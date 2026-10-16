"""Read events from every Google Calendar on your account (OAuth, read-only).

One-time setup: put the OAuth client file from Google Cloud in credentials.json,
then run `./briefing --login` on a machine with a browser. That saves token.json,
which can be copied to a headless server and refreshes itself from then on.
"""
import datetime as dt
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def login(credentials_path, token_path):
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    Path(token_path).write_text(creds.to_json())


def _creds(token_path):
    token_path = Path(token_path)
    if not token_path.exists():
        raise RuntimeError("not logged in; run ./briefing --login")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds


def _when(t, tz):
    if "date" in t:
        return dt.date.fromisoformat(t["date"])
    return dt.datetime.fromisoformat(t["dateTime"]).astimezone(tz)


def events(token_path, start, end, tz, exclude=()):
    """Events between start and end as ([(uid, start, end, title)], errors).

    Uses every calendar that's ticked in Google Calendar's sidebar, minus
    any whose name is in `exclude`. Declined events are skipped.
    """
    service = build("calendar", "v3", credentials=_creds(token_path), cache_discovery=False)

    calendars, page = [], None
    while True:
        resp = service.calendarList().list(pageToken=page).execute()
        calendars += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            break

    found, errors = [], []
    for cal in calendars:
        name = cal.get("summaryOverride") or cal.get("summary", "")
        if not cal.get("selected") or name in exclude:
            continue
        try:
            resp = service.events().list(
                calendarId=cal["id"], timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, maxResults=250,
            ).execute()
        except Exception:
            errors.append(f"(couldn't load {name})")
            continue
        for ev in resp.get("items", []):
            if ev.get("status") == "cancelled" or ev.get("eventType") == "workingLocation":
                continue
            me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
            if me and me.get("responseStatus") == "declined":
                continue
            found.append((
                ev.get("iCalUID", ev["id"]),
                _when(ev["start"], tz),
                _when(ev["end"], tz),
                ev.get("summary", "(no title)").strip(),
            ))
    return found, errors
