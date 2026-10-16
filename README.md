# receipt-printer

Scripts for an Epson TM-T88V on USB.

- `./receipt "text"` prints text, images (`--image`) or QR codes (`--qr`). See `./receipt --help`.
- `./briefing` prints today's weather, Google Calendar events and Things Today list.

Our print head has dead dots around 183–192, so all text is laid out to skip them
(`DEAD_DOTS` in `receipt.py`).

## Setup (Linux server)

Needs Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then add your calendar URLs
```

Let non-root users talk to the printer over USB:

```sh
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="04b8", ATTRS{idProduct}=="0202", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-tm-t88v.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the printer.

## Briefing

`config.toml` holds the timezone, the weather location, Things login and calendars.

**Google Calendar (OAuth):** with a `[google]` section, every calendar ticked in Google
Calendar's sidebar is included (skip some with `exclude = [...]`), and declined events are
hidden. One-time setup: create a Desktop OAuth client with the `calendar.readonly` scope in
Google Cloud, save it as `credentials.json`, then run `./briefing --login` on a machine with
a browser. Copy the resulting `token.json` to the server; it refreshes itself.

**iCal feeds:** you can also (or instead) add `[[calendars]]` blocks with any .ics URL,
such as a Google calendar's "Secret address in iCal format".

Todos come from your Things 3 Today list via Things Cloud. Add a `[things]` section
with your Things account email, and put the password in `config.toml` or the
`THINGS_PASSWORD` environment variable. Things Cloud has no official API: this uses the
protocol reverse-engineered by [things-cloud-sdk](https://github.com/arthursoares/things-cloud-sdk),
only reads, and could break if Cultured Code changes their sync. Synced data is cached in
`.things_cache.json` so each run only downloads new changes.

Try it without printing:

```sh
./briefing --dry-run
./briefing --dry-run --date 2026-11-26
```

To print it every day, run `./briefing` from cron or a systemd timer.
