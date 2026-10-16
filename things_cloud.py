"""Read the Things 3 Today list from Things Cloud.

Things Cloud has no official API. This follows the protocol as reverse
engineered by https://github.com/arthursoares/things-cloud-sdk and only ever
reads (GET requests), so it can't change anything in your Things.

The cloud stores a log of changes ("items"). We replay that log to rebuild
the current tasks, and cache the result so later runs only fetch new changes.
"""
import base64
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://cloud.culturedcode.com/version/1"
USER_AGENT = "ThingsMac/32209501"
CLIENT_INFO = base64.b64encode(json.dumps({
    "dm": "MacBookPro18,3", "lr": "US", "nf": True, "nk": True,
    "nn": "ThingsMac", "nv": "32209501", "on": "macOS", "ov": "15.7.3",
    "pl": "en-US", "ul": "en-Latn-US",
}).encode()).decode()

# task fields (see types.go in the SDK above)
TITLE, STATUS, SCHEDULE, START_DATE, DEADLINE = "tt", "ss", "st", "sr", "dd"
TYPE, TRASHED, PROJECTS, TODAY_INDEX, EVENING, REPEATER = "tp", "tr", "pr", "ti", "sb", "rr"
OPEN = 0  # status
INBOX, ANYTIME = 0, 1  # schedule
TODO, PROJECT = 0, 1  # type (2 = heading)
INDEX = "ix"


def _get(path, password=None, **params):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Things-Client-Info": CLIENT_INFO,
    })
    if password:
        req.add_header("Authorization", f"Password {password}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _apply(objects, items):
    """Apply a page of change-log items to {uuid: {"kind", "p"}}."""
    for entry in items:
        for uuid, item in entry.items():
            action, kind, props = item.get("t"), item.get("e", ""), item.get("p") or {}
            if kind.startswith("Tombstone"):
                objects.pop(props.get("dloid"), None)
            elif action == 0:  # create
                objects[uuid] = {"kind": kind, "p": props}
            elif action == 1:  # edit
                objects.setdefault(uuid, {"kind": kind, "p": {}})["p"].update(props)
            elif action == 2:  # delete
                objects.pop(uuid, None)


def sync(email, password, cache_path):
    """Bring the cached copy of all Things objects up to date."""
    cache_path = Path(cache_path)
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    account = _get(f"/account/{urllib.parse.quote(email)}", password)
    key = account["history-key"]
    if cache.get("history") != key:
        cache = {"history": key, "index": 0, "objects": {}}

    while True:
        page = _get(f"/history/{key}/items", **{"start-index": cache["index"]})
        _apply(cache["objects"], page["items"])
        cache["index"] += len(page["items"])
        if not page["items"] or cache["index"] >= page["current-item-index"]:
            break

    cache_path.write_text(json.dumps(cache))
    return cache["objects"]


def _date(ts):
    """Things stores dates as midnight UTC timestamps."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date() if ts else None


def today_list(objects, day):
    """The to-dos Things shows in Today, in Today's order (evening last)."""
    tasks = {u: o["p"] for u, o in objects.items() if o["kind"].startswith("Task")}
    out = []
    for uuid, t in tasks.items():
        if t.get(STATUS) != OPEN or t.get(TRASHED) or t.get(REPEATER):
            continue
        if t.get(TYPE) not in (TODO, PROJECT):
            continue
        start, deadline = _date(t.get(START_DATE)), _date(t.get(DEADLINE))
        started = t.get(SCHEDULE) == ANYTIME and start and start <= day
        due = deadline and deadline <= day
        if not (started or due):
            continue
        project = next((tasks[p].get(TITLE) for p in t.get(PROJECTS) or [] if p in tasks), None)
        out.append({
            "title": (t.get(TITLE) or "(untitled)").strip(),
            "project": project,
            "deadline": deadline,
            "evening": bool(t.get(EVENING)),
            "order": (bool(t.get(EVENING)), t.get(TODAY_INDEX) or 0),
        })
    out.sort(key=lambda x: x["order"])
    for x in out:
        del x["order"]
    return out


def inbox_list(objects):
    """Open to-dos in the Inbox, in Inbox order."""
    tasks = [o["p"] for o in objects.values() if o["kind"].startswith("Task")]
    inbox = [
        t for t in tasks
        if t.get(SCHEDULE) == INBOX and t.get(TYPE) == TODO
        and t.get(STATUS) == OPEN and not t.get(TRASHED) and not t.get(REPEATER)
    ]
    inbox.sort(key=lambda t: t.get(INDEX) or 0)
    return [
        {"title": (t.get(TITLE) or "(untitled)").strip(), "deadline": _date(t.get(DEADLINE))}
        for t in inbox
    ]
