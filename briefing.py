#!/usr/bin/env python3
"""Print a daily briefing: weather, today's calendar events, and todos.

  ./briefing              print today's briefing
  ./briefing --dry-run    show it in the terminal instead of printing
  ./briefing --date 2026-10-20 --dry-run

Settings live in config.toml (see config.example.toml).
"""
import argparse
import datetime as dt
import json
import os
import sys
import tomllib
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

import gcal
import things_cloud
from receipt import find_printer, flow_rows, layout, print_lines, print_text, two_columns

HERE = Path(__file__).resolve().parent
# config.toml, token.json and the Things cache; systemd sets STATE_DIRECTORY for services
STATE = Path(os.environ.get("STATE_DIRECTORY", HERE))

# https://open-meteo.com/en/docs (WMO weather interpretation codes)
WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Thunderstorms w/ hail", 99: "Thunderstorms w/ hail",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "receipt-briefing"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fmt_time(t):
    """09:00, 18:30"""
    return t.strftime("%H:%M")


# --- weather -------------------------------------------------------------

def weather_section(cfg, day, tz):
    loc = cfg["location"]
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['latitude']}&longitude={loc['longitude']}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "apparent_temperature_max,apparent_temperature_min,"
        "precipitation_probability_max,sunrise,sunset"
        "&hourly=temperature_2m"
        f"&timezone={tz.key}&start_date={day}&end_date={day + dt.timedelta(days=1)}"
    )
    try:
        data = json.loads(fetch(url))
    except Exception as e:
        return [("", f"(couldn't load weather: {e})")]

    d = {k: v[0] for k, v in data["daily"].items()}
    hourly = dict(zip(data["hourly"]["time"], data["hourly"]["temperature_2m"]))
    sunrise = dt.datetime.fromisoformat(d["sunrise"])
    sunset = dt.datetime.fromisoformat(d["sunset"])

    # every 3 hours from 09:00 until midnight (00:00 is the next day's first hour)
    snapshots = []
    for hour in range(9, 25, 3):
        t = dt.datetime.combine(day, dt.time()) + dt.timedelta(hours=hour)
        temp = hourly.get(t.strftime("%Y-%m-%dT%H:%M"))
        if temp is not None:
            snapshots.append(f"{t:%H}h\u00a0{round(temp)}°")

    return [
        ("Sky", WEATHER_CODES.get(d["weather_code"], "?")),
        ("High / low", f"{round(d['temperature_2m_max'])}°C / {round(d['temperature_2m_min'])}°C"),
        ("Feels like", f"{round(d['apparent_temperature_max'])}°C / {round(d['apparent_temperature_min'])}°C"),
        ("Rain chance", f"{d['precipitation_probability_max']}%"),
        ("Temps", "  ".join(snapshots)),
        ("Sun", f"{fmt_time(sunrise)} - {fmt_time(sunset)}"),
    ]


# --- calendar ------------------------------------------------------------

def ical_events(cfg, start, end, tz):
    """Events from [[calendars]] iCal URLs as ([(uid, start, end, title)], errors)."""
    found, errors = [], []
    for cal in cfg.get("calendars", []):
        try:
            ics = icalendar.Calendar.from_ical(fetch(cal["url"]))
        except Exception:
            errors.append(f"(couldn't load {cal.get('name', 'a calendar')})")
            continue
        for ev in recurring_ical_events.of(ics).between(start, end):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            ev_start = ev["DTSTART"].dt
            ev_end = ev["DTEND"].dt if "DTEND" in ev else ev_start
            if isinstance(ev_start, dt.datetime):
                if ev_start.tzinfo is None:  # "floating" time: treat as local
                    ev_start, ev_end = ev_start.replace(tzinfo=tz), ev_end.replace(tzinfo=tz)
                ev_start, ev_end = ev_start.astimezone(tz), ev_end.astimezone(tz)
            title = str(ev.get("SUMMARY", "(no title)")).strip()
            found.append((str(ev.get("UID")), ev_start, ev_end, title))
    return found, errors


def calendar_events(cfg, day, tz):
    """Return (all_day_titles, timed_events, errors) for the given day."""
    start = dt.datetime.combine(day, dt.time(), tz)
    end = start + dt.timedelta(days=1)

    found, errors = ical_events(cfg, start, end, tz)
    if "google" in cfg:  # an empty [google] section still means "on"
        google = cfg["google"]
        try:
            g_found, g_errors = gcal.events(
                STATE / google.get("token", "token.json"), start, end, tz,
                exclude=google.get("exclude", []),
            )
            found += g_found
            errors += g_errors
        except Exception as e:
            errors.append(f"(couldn't load Google Calendar: {e})")

    all_day, timed, seen = set(), [], set()
    for uid, ev_start, ev_end, title in found:
        # the same event often shows up on several shared calendars
        key = (uid, str(ev_start), title)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(ev_start, dt.datetime):
            timed.append((ev_start, ev_end, title))
        else:
            all_day.add(title)

    timed.sort()
    return sorted(all_day), timed, errors


def calendar_section(cfg, day, tz):
    all_day, timed, errors = calendar_events(cfg, day, tz)
    rows = [("all day", title) for title in all_day]
    day_start = dt.datetime.combine(day, dt.time(), tz)
    for ev_start, ev_end, title in timed:
        if ev_start < day_start:
            when = f"until {fmt_time(ev_end)}"
        else:
            when = f"{fmt_time(ev_start)}-{fmt_time(ev_end)}"
        rows.append((when, title))
    if not rows:
        rows.append(("", "Nothing scheduled"))
    return rows + [("", err) for err in errors]


# --- todos ---------------------------------------------------------------

def due_label(deadline, day):
    """today, 2 days, 3 late"""
    days = (deadline - day).days
    if days == 0:
        return "today"
    if days < 0:
        return f"{-days} late"
    return f"{days} day{'s' if days != 1 else ''}"


def todo_row(todo, day):
    """Checkbox and time left on the left of the dead dots, the to-do on the right."""
    title = todo["title"]
    if todo.get("project"):
        title += f" ({todo['project']})"
    left = "[ ]"
    if todo["deadline"]:
        left += f" {due_label(todo['deadline'], day)}"
    return (left, title)


# Made-up to-dos for --demo, so a receipt can be shown off without the real list.
# (title, project, days until deadline or None, evening)
DEMO_TODAY = [
    ("Problem set 6: Laplace transforms", "Signals & Systems", 0, False),
    ("Finish Lab 4 prelab: active filters", "ECE 2000", 2, False),
    ("Solder headers onto the ESP32 breakout", None, None, False),
    ("Order 10k resistors and 0.1uF caps from DigiKey", None, None, False),
    ("Reflow the motor driver board", "Rover", 5, True),
    ("Read Horowitz & Hill ch. 4 (feedback)", None, None, True),
]
DEMO_INBOX = [
    ("Email TA about scope probe compensation", None, None, False),
    ("Try differential pair routing in KiCad", None, None, False),
    ("Analog Devices internship application", None, 10, False),
]


def demo_todos(items, day):
    return [
        {"title": title, "project": project, "evening": evening,
         "deadline": day + dt.timedelta(days=due) if due is not None else None}
        for title, project, due, evening in items
    ]


def things_sections(cfg, day, demo=False):
    """[(title, rows)] for the Things Today list and Inbox, if configured."""
    if demo:
        today_list, inbox_list = demo_todos(DEMO_TODAY, day), demo_todos(DEMO_INBOX, day)
    else:
        things = cfg.get("things")
        if not things:
            return []
        password = os.environ.get("THINGS_PASSWORD") or things.get("password")
        try:
            objects = things_cloud.sync(things["email"], password, STATE / ".things_cache.json")
        except Exception as e:
            return [("Today", [("", f"(couldn't load Things: {e})")])]
        today_list, inbox_list = things_cloud.today_list(objects, day), things_cloud.inbox_list(objects)

    today, evening = [], False
    for todo in today_list:
        if todo["evening"] and not evening:
            today.append(("This Evening", ""))
            evening = True
        today.append(todo_row(todo, day))

    inbox = [todo_row(todo, day) for todo in inbox_list]
    return [
        ("Today", today or [("", "Nothing to do!")]),
        ("Inbox", inbox or [("", "Empty")]),
    ]


# --- output --------------------------------------------------------------

def build(cfg, day, tz, demo=False):
    """The briefing as a list of (kind, content, size, bold) blocks.

    kind "text" is centered with receipt.layout; "lines" is already laid out.
    """
    blocks = [
        ("text", day.strftime("%A"), 2, True),
        ("text", day.isoformat(), 2, False),
        ("lines", [""], 1, False),
    ]

    def section(title, rows, columns=False):
        blocks.append(("text", title, 2, True))
        lines = two_columns(rows) if columns else flow_rows(rows)
        blocks.append(("lines", lines + [""], 1, False))

    section("Weather", weather_section(cfg, day, tz), columns=True)
    section("Schedule", calendar_section(cfg, day, tz))
    for title, rows in things_sections(cfg, day, demo):
        section(title, rows, columns=True)
    return blocks


def main():
    p = argparse.ArgumentParser(description="Print the daily briefing.")
    p.add_argument("--dry-run", action="store_true", help="show it instead of printing")
    p.add_argument("--date", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: today)")
    p.add_argument("--config", default=STATE / "config.toml", type=Path)
    p.add_argument("--demo", action="store_true",
                   help="use made-up to-dos instead of your real Things lists")
    p.add_argument("--login", action="store_true",
                   help="sign in to Google Calendar (opens a browser) and save token.json")
    args = p.parse_args()

    if args.login:
        gcal.login(HERE / "credentials.json", STATE / "token.json")
        print("Saved token.json")
        return

    if not args.config.exists():
        sys.exit(f"Missing {args.config}. Copy config.example.toml to config.toml and fill it in.")
    cfg = tomllib.loads(args.config.read_text())
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    day = args.date or dt.datetime.now(tz).date()

    blocks = build(cfg, day, tz, demo=args.demo)

    if args.dry_run:
        for kind, content, size, bold in blocks:
            lines = layout(content, size, center=True)[0] if kind == "text" else content
            for line in lines:
                print(f"[{size}{'b' if bold else ' '}] {line}")
        return

    printer = find_printer()
    try:
        for kind, content, size, bold in blocks:
            if kind == "text":
                print_text(printer, content, size, center=True, bold=bold)
            else:
                print_lines(printer, content, size, bold)
        printer.cut()
    finally:
        printer.close()


if __name__ == "__main__":
    main()
