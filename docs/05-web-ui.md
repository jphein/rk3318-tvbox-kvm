# 05 — The web UI (`kvm-ui.py`)

One page that shows the capture and drives the HID gadget. **Python 3 stdlib only** — no
pip, no `websockets` package, no kvmd.

> ⚠️ **No authentication.** See the security section of the [README](../README.md) before
> deploying this anywhere. Anyone who can reach `:8081` has keyboard and mouse on whatever
> is cabled to the gadget port.

## Install

```bash
scp bin/kvm-ui.py root@<box-ip>:/usr/local/bin/
scp systemd/kvm-ui.service root@<box-ip>:/etc/systemd/system/
ssh root@<box-ip> 'chmod 0755 /usr/local/bin/kvm-ui.py &&
                   systemctl daemon-reload && systemctl enable --now kvm-ui'
```

Then open `http://<box-ip>:8081/`.

## Shape

~370 lines total: ~250 of backend, ~120 of embedded HTML/CSS/JS, in one file. **~10 MB
RSS**, which is the whole point — kvmd would be an order of magnitude more alongside a
GNOME session and DisplayLinkManager on a 2 GB box.

- `GET /` → the page. `GET /ws` → WebSocket carrying input events.
- **GET-only by construction**: there is no `do_POST` method at all, so POST returns 501.
  That is a stronger statement than a rejection branch, since there is no handler to get
  the logic wrong in.
- Video is `<img src="http://<host>:8080/stream">` — referenced **directly, not proxied**.
  Same host, so no CORS issue, and it keeps ustreamer's MJPEG path untouched: zero added
  latency, zero extra copies through Python.
- Input goes straight to `/dev/hidg0`/`hidg1` — one `write()` per event. `hid-send`'s
  encoding logic was **ported, not shelled out**, so there is no fork per keystroke.

## Input model

**Keyboard.** Browser `event.code` → HID usage, US layout, *physical* keys — so it is
layout-independent at the browser end. The backend keeps the live pressed-key set plus the
modifier byte and emits the true 8-byte state.

Key repeat is **pass-through by construction**: a held key stays in the report and the
*target machine's* own auto-repeat drives repetition, which is what a real keyboard does.
`e.repeat` keydowns are dropped client-side so repeats are never synthesised here.

**Mouse.** Absolute. `mousemove` over the image → the image's bounding rect normalised to
`0..32767`, throttled to ~60/s (16 ms). Left/middle/right buttons, and wheel
(`deltaY` → ±1).

**Chords.** A Ctrl+Alt+Del button, sent as a one-shot that ignores live state.

**Stuck-key prevention.** A "Release all keys" button, *plus* an automatic release on window
`blur` and on WebSocket disconnect. A dropped tab leaving a key held down on the target is a
nasty enough failure mode to be worth the three lines.

Click the image to capture the keyboard; "Release all keys" drops capture.

## The "no target attached" path

This is the case you will hit first, and it is handled deliberately rather than as an error.

Writes to `/dev/hidg*` fail with `ESHUTDOWN`/`EAGAIN`/`EPIPE`/`ENODEV` when the gadget is
bound but no host has enumerated it, and with `ENOENT`/`ENXIO` when `hid-gadget.service`
isn't running. The backend classifies **all** of those as *no host* and pushes a state
message over the WebSocket; the page shows a **banner over the video**, not an error dialog.

Genuinely unexpected errnos are logged **once**, not once per keystroke. An event-rate
journal flood was a real risk here — a held key at 30/s writing an unexpected errno would
fill the journal — and it is specifically prevented with a latch.

The banner clears by itself within about a second of cabling a real target.

## Service relationships

`After=network-online.target hid-gadget.service` and **`Wants=`, not `Requires=`**. If the
gadget is down, kvm-ui still serves the page with the banner showing: degraded, not dead.
And nothing about kvm-ui can drag `hid-gadget`, `kvm-stream` or `displaylink-driver` down
with it.

It runs as root because `/dev/hidg*` are `0600 root:root`. Hardening is modest and
deliberately **not** `PrivateDevices` (it needs those nodes): `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome`, `ProtectKernelTunables`, `ProtectControlGroups`,
`RestrictRealtime`.

## Verified

```
GET  /              -> 200 text/html
GET  /nope          -> 404
POST /              -> 501                                (no POST handler exists)
GET  :8080/stream   -> 200 multipart/x-mixed-replace      (untouched by the UI)
GET  /ws            -> 101 Switching Protocols, Sec-WebSocket-Accept correct
   initial push     -> {"t":"host","attached":false}      (correct with no target PC)
   all event types  -> accepted (k / m / b / w / cad / rel); malformed input survived
   ping             -> pong echoed
journalctl -u kvm-ui after a 40-event burst -> zero lines beyond the start banner
```

Not verified: whether input actually lands on a target PC — see
[04-usb-hid-gadget.md](04-usb-hid-gadget.md).

## One implementation detail worth stealing

The WebSocket reader reads through the handler's **buffered `rfile`**, not the raw socket.
`BaseHTTPRequestHandler` may already have pulled the first WebSocket frame into that buffer
while parsing headers, and a raw `recv()` would silently skip it — costing you exactly one
frame, intermittently, in a way that looks like a client bug. This is a classic trap when
bolting WebSockets onto `http.server`.

## The upgrade path

This is deliberately a shim. If you want authentication, TLS, mass-storage emulation, ATX
power control, audio, or a maintained UI, install **[PiKVM's kvmd](https://pikvm.org/)** and
point it at the same `/dev/hidg*` nodes and capture device. Nothing in this repo's gadget or
capture layer is kvmd-incompatible — `hid-gadget-up` builds a standard PiKVM-shaped
composite gadget on purpose.

## Revert

```bash
ssh root@<box-ip> 'systemctl disable --now kvm-ui
  rm -f /usr/local/bin/kvm-ui.py /etc/systemd/system/kvm-ui.service
  systemctl daemon-reload'
```
