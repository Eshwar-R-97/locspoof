# Location Picker

A local Mac web app that sets **your own** USB/Wi-Fi-connected iPhone's simulated GPS location, using Apple's
developer location-simulation service (the same one Xcode's *Debug → Simulate Location* uses).

- **Teleport** anywhere (click the map, search, paste coordinates, or tap a saved spot)
- **Travel** along real roads (OSRM routing, walk/bike/drive/highway/custom speed, multi-stop, realistic speed with traffic stops)
- **Wander**, a smooth bounded random walk (standing around / indoors / walking around / GPS drift only)
- **Saved spots** with rename, move, reorder, delete, import and export
- **Reset to real location** at any time. Ctrl+C also restores it.

Everything runs on your Mac. Nothing runs on the phone.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

You need:
- Python 3.11+ on macOS 13+
- iPhone with **Developer Mode on** (Settings → Privacy & Security → Developer Mode), and this Mac trusted

### Do I need Xcode?

**Usually not.** pymobiledevice3 handles pairing, downloads and mounts the developer disk image, and talks to
the location service itself. Xcode only helps in two cases:

| Need | Without Xcode | With Xcode |
|---|---|---|
| Developer Mode toggle doesn't show on the phone | `.venv/bin/python -m pymobiledevice3 amfi reveal-developer-mode` (then toggle it on the phone and restart) | Plug in and open Xcode once and the toggle appears |
| Trust / pairing | Plug in, unlock, tap **Trust** when prompted | Same |
| Wi-Fi (cable-free) mode | `.venv/bin/python -m pymobiledevice3 lockdown wifi-connections on` while plugged in | Window → Devices and Simulators → **Connect via network** |

You never have to build or run anything in Xcode.

## Run

**iOS 17 and newer** needs the tunnel daemon running in a second terminal:

```bash
sudo .venv/bin/python -m pymobiledevice3 remote tunneld
```

Then:

```bash
.venv/bin/python run.py            # open http://127.0.0.1:8765/
.venv/bin/python run.py --dry-run  # whole UI with a fake phone, no device needed
```

iOS 16 and older doesn't need tunneld. The app falls back to the usbmux/lockdown backend automatically, or you can force it with `--legacy`.

```
python3 run.py [--legacy] [--udid UDID] [--port 8765] [--host 127.0.0.1]
               [--token TOKEN] [--tick 1.0] [--dry-run]
               [--auto-reset-after DURATION] [--data-dir ./data] [-v]
               [--tunneld HOST:PORT] [--no-mount]
```

| Flag | Meaning |
|---|---|
| `--udid` | Pick a device when several are connected (the error lists them) |
| `--host 0.0.0.0 --token SECRET` | Control from another device on your LAN. A token is required, and the banner prints a `?token=` URL |
| `--auto-reset-after 4h` | Restore real GPS automatically after 4h of simulated location (also `30m`, `1h30m`, `90s`) |
| `--tick` | Seconds between position updates while moving (default 1.0) |
| `--no-mount` | Skip the developer disk image auto-mount |

Smoke-test the device layer without the web app:

```bash
.venv/bin/python -m locpicker.backends --set -- 41.8781 -87.6298   # holds until Ctrl+C on iOS 17+
.venv/bin/python -m locpicker.backends --clear
```

## Using it

- **Keyboard:** `T` teleport to pin · `Space` pause/resume · `Esc` stop · `R` reset (asks first) · `/` search
- **Travel:** with no simulated position yet, choose a start (a spot, or *Pick on map*). Pick a destination, add stops with
  *Add pin as a stop*, and choose a speed. The panel previews the route as "4.2 km · 9 min by car". Press **Start**.
  If routing fails or takes longer than 8 s you can use a straight line instead.
  *Match route's estimated time* uses OSRM's own duration, which gives a realistic driving pace.
- **Resume:** the app saves its state every 5 s. After a restart it offers **Resume previous session?** It never moves
  the phone without your input.
- **Status dot:** green = connected, amber = reconnecting, orange = no device or server offline.

## When you're done

- Ctrl+C restores your real location (`Restored real location.`).
- **Restarting the phone always restores real GPS**, whatever state the app was in.
- Turn Developer Mode off when you no longer need it.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Can't reach tunneld` / `No device in tunneld` | `sudo python3 -m pymobiledevice3 remote tunneld` isn't running, the phone is locked, or the cable/Wi-Fi dropped |
| `iOS 17.x device found over USB, but it isn't in tunneld` | Start tunneld (above) and unlock the phone |
| `DeveloperDiskImage not mounted` / InvalidService | `python3 -m pymobiledevice3 mounter auto-mount --tunnel ""` and check Developer Mode is on |
| Location snaps back between updates | The connection is being reopened. The DVT backend keeps one connection open, so check the log for reconnect loops |
| Negative longitude errors in CLI testing | Put `--` before coordinates: `--set -- 41.8 -87.6` |
| Find My not updating | It refreshes on its own schedule (1–2 min). Check Apple Maps first |
| Works in Maps, not in some app | Some apps also use Wi-Fi/cell positioning. Out of scope |
| `401 unauthorized` in the browser | Open the exact URL the banner printed (with `?token=`) once. The page remembers it |

Logs: console plus `data/locpicker.log` (rotating, 1 MB × 3). Use `-v` for pymobiledevice3 debug output.

## Known limitations

- iOS 17+ needs the Mac running and connected (USB or Wi-Fi) the whole time. The location only holds while the
  connection is open.
- Apple can change or remove these developer services in any iOS update. Once it works, pin the version:
  this was built and tested against **pymobiledevice3 11.56.2**.
- Public OSRM (routing.openstreetmap.de), Nominatim and OpenStreetMap tiles have fair-use limits. Requests are
  occasional and user-triggered (search is throttled to 1/s).
- Routes crossing the ±180° meridian are rejected.

## Development

```bash
.venv/bin/python -m pytest -q
```

```
locpicker/
  geo.py       pure math: haversine, bearing, destination, interpolate, densify, simplify, offsets
  backends.py  DeviceBackend protocol + Dvt (iOS 17+) / Legacy (≤16) / Auto / Fake
  engine.py    async state machine + tick loop (static, travel, wander, paused; keepalive; reconnect backoff)
  spots.py     saved spots (JSON, atomic writes, corrupt-file backup)
  server.py    aiohttp API, SSE, Host/Origin/token checks, session persistence
  static/      index.html, app.js, app.css (Leaflet 1.9, no build step)
```

Security: binds to `127.0.0.1` by default. `/api/*` rejects cross-origin requests (CSRF) and a foreign `Host` header
(DNS rebinding). In LAN mode every request needs `Authorization: Bearer <token>`.
