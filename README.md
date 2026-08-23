# rk3318-tvbox-kvm

Turn a ~$20 **RK3318 Android TV box** into a **browser-based DIY IP-KVM** (PiKVM-style
HDMI capture + USB HID gadget), a **DisplayLink lapdock crash-cart console**, and a working
**front-panel 7-segment clock** — all on mainline-ish Armbian, with no kernel rebuild.

These cheap RK3318/RK3328 boxes are unusually well suited to it: a dual-role dwc2 USB
controller (so it can *pretend to be* a keyboard), a second xHCI port to keep the capture
stick and dock on, HDMI **in** via a $10 MS2109 stick, and a front panel with a real
FD628 display. Total cost is well under a Raspberry Pi you can't buy.

**📖 Build guide, rendered: <https://jphein.github.io/rk3318-tvbox-kvm/>**

> **Search terms, so this is findable:** rk3318 pikvm · rk3318 ip-kvm · rk3328 usb gadget ·
> tv box kvm · armbian dwc2 peripheral · ms2109 ustreamer · rk3318 front panel led ·
> fd628 tm16xx armbian · displaylink evdi invisible cursor

---

## ⚠️ SECURITY: the web UI has NO AUTHENTICATION

**Read this before deploying anything here.**

`kvm-ui.py` binds `0.0.0.0:8081` and has **no login, no TLS, no access control of any
kind**. Anyone who can reach that port has a **keyboard and mouse on whatever machine is
cabled to the gadget port**. The MJPEG video stream on `:8080` is equally open.

That is a deliberate scope decision — this is a ~370-line stdlib-only shim, not a security
product — but it means:

- **Trusted LAN / management VLAN only.** Never port-forward it. Never put it on a
  network you don't control.
- If you need it remotely, put it behind something that *does* authentication: a VPN
  (WireGuard/Tailscale), an SSH tunnel, or an authenticating reverse proxy.
- If you want a hardened, authenticated, feature-complete stack instead, use
  **[PiKVM](https://pikvm.org/) / kvmd**. This project deliberately does not compete with
  it; kvmd is the documented upgrade path (see [docs/05-web-ui.md](docs/05-web-ui.md)).

There are no credentials anywhere in this repo, because there is no auth to have
credentials for. Don't mistake that for safety.

---

## What it does

Three independent capabilities. **Take only the parts you want** — the HID gadget, the
capture, the console and the front panel are separate and none requires the others.

| | what | how |
|---|---|---|
| **IP-KVM** | See and control a target PC from a browser | MS2109 HDMI capture → ustreamer MJPEG `:8080`; USB HID gadget → `/dev/hidg0/1`; `kvm-ui.py` joins them on `:8081` |
| **Crash-cart console** | A DisplayLink lapdock as a physical local console for the box itself | `evdi` + DisplayLink driver + a GNOME session, plus the fix that makes the mouse cursor actually *visible* |
| **Front panel** | The box's own 4-digit display shows a clock, and its LEDs track link/activity | kernel `tm16xx` driver + a corrective DT overlay + two tiny services |

```
                      ┌──────────────── RK3318 TV box ────────────────┐
   target PC          │                                               │
   ┌────────┐  HDMI   │  MS2109 capture ──► ustreamer ──► :8080 MJPEG │      browser
   │        ├────────►│  (on xHCI port)                          │    │   ┌───────────┐
   │        │         │                                          ├────┼──►│  kvm-ui   │
   │        │◄────────┤  dwc2 UDC ◄── /dev/hidg0,1 ◄── kvm-ui ────┘    │   │   :8081   │
   └────────┘  USB    │  (gadget port, A-to-A)                        │   └───────────┘
             HID      │                                               │
                      │  DisplayLink dock ──► evdi ──► GNOME console  │
                      │  FD628 front panel ──► tm16xx ──► clock + LEDs│
                      └───────────────────────────────────────────────┘
```

## Hardware

| item | notes |
|---|---|
| RK3318/RK3328 Android TV box | The generic "H96/X88/MXQ"-class boxes. Must be Armbian-bootable — see [docs/02-armbian-base.md](docs/02-armbian-base.md) |
| MS2109 HDMI→USB capture stick | ~$10. 1080p30 MJPEG. `534d:2109` |
| USB hub | To keep capture + dock on the single xHCI port |
| USB A-to-A cable | Box gadget port → target PC. **Read the VBUS warning in [docs/01-hardware.md](docs/01-hardware.md) first** |
| *(optional)* DisplayLink lapdock | e.g. a Sentio Superbook, for the crash-cart console |

**The critical constraint:** only the **dwc2** controller can be a USB gadget, and on these
boxes it is usually the socket everything is already plugged into. You must move your hub
to the **xHCI** socket first, which frees dwc2 to become the gadget port. Full port map and
how to identify each socket without a datasheet: [docs/01-hardware.md](docs/01-hardware.md).

## Quickstart

Replace `<box-ip>` with your box's address throughout.

```bash
git clone https://github.com/jphein/rk3318-tvbox-kvm && cd rk3318-tvbox-kvm

# 1. Move your USB hub to the xHCI socket (see docs/01-hardware.md). This frees dwc2.
# 2. Build and install the OTG overlay, then reboot:
dtc -@ -I dts -O dtb -o rk3318-otg-peripheral.dtbo overlays/rk3318-otg-peripheral.dts
scp rk3318-otg-peripheral.dtbo root@<box-ip>:/boot/overlay-user/
ssh root@<box-ip> 'grep -q ^user_overlays= /boot/armbianEnv.txt \
    && sed -i "s|^user_overlays=.*|& rk3318-otg-peripheral|" /boot/armbianEnv.txt \
    || echo "user_overlays=rk3318-otg-peripheral" >> /boot/armbianEnv.txt'

# 3. Install the gadget + capture + UI
scp bin/hid-gadget-up root@<box-ip>:/usr/local/sbin/
scp bin/hid-send bin/kvm-ui.py root@<box-ip>:/usr/local/bin/
scp systemd/hid-gadget.service systemd/kvm-stream.service systemd/kvm-ui.service \
    root@<box-ip>:/etc/systemd/system/
ssh root@<box-ip> 'systemctl daemon-reload &&
    systemctl enable hid-gadget kvm-stream kvm-ui && reboot'

# 4. Optional: the units' Documentation= fields point here, so put the docs
#    where they say. Purely a convenience for `systemctl status`.
ssh root@<box-ip> 'mkdir -p /usr/local/share/doc/rk3318-tvbox-kvm'
scp docs/*.md root@<box-ip>:/usr/local/share/doc/rk3318-tvbox-kvm/

# 5. Verify, then open http://<box-ip>:8081/
ssh root@<box-ip> 'ls /sys/class/udc/ && ls -l /dev/hidg0 /dev/hidg1'
```

`hid-send` gives you a CLI seam independent of the browser:

```bash
ssh root@<box-ip> 'hid-send type "hello"; hid-send key enter'
ssh root@<box-ip> 'hid-send move 50% 50%; hid-send click'
ssh root@<box-ip> 'hid-send key ctrl+alt+delete'
```

## Docs

| | |
|---|---|
| [01-hardware.md](docs/01-hardware.md) | Port map, which socket is dwc2, **the VBUS back-feed hazard**, BOM |
| [02-armbian-base.md](docs/02-armbian-base.md) | Getting Armbian onto the box; the load-bearing overlays you must not remove |
| [03-hdmi-capture.md](docs/03-hdmi-capture.md) | MS2109 + ustreamer |
| [04-usb-hid-gadget.md](docs/04-usb-hid-gadget.md) | dwc2 → peripheral, the configfs composite gadget, HID descriptors |
| [05-web-ui.md](docs/05-web-ui.md) | `kvm-ui.py` design, input model, the kvmd upgrade path |
| [06-displaylink-console.md](docs/06-displaylink-console.md) | evdi + DisplayLink, and **the invisible-cursor fix** |
| [07-front-panel-display.md](docs/07-front-panel-display.md) | FD628 + tm16xx, LED triggers, the clock |
| [bug-armbian-rk3318-led-conf2.md](docs/bug-armbian-rk3318-led-conf2.md) | 🐛 Two defects in Armbian's shipped overlay |
| [bug-tm16xx-out-of-tree-build.md](docs/bug-tm16xx-out-of-tree-build.md) | 🐛 Two defects in tm16xx's out-of-tree build |

## Two upstream bugs found along the way

Both are written up report-ready, with the exact evidence, in `docs/`:

1. **Armbian's `rockchip-rk3318-box-led-conf2.dtbo` cannot work as shipped** — it puts an
   I2C slave address in an SPI child's `reg`, *and* uses a `compatible` no driver matches.
   Present through at least the `rockchip64-7.2` patch series. The front panel is dead on
   every box using it. → [docs/bug-armbian-rk3318-led-conf2.md](docs/bug-armbian-rk3318-led-conf2.md)
2. **tm16xx's out-of-tree build is broken on any kernel Armbian doesn't patch** — it ships
   the full mainline `auxdisplay` Makefile (so `M=` builds demand sources it lacks), and
   passes its `-D` flags via `EXTRA_CFLAGS`, which modern kbuild ignores (producing a
   confusing redefinition error rather than a clean miss).
   → [docs/bug-tm16xx-out-of-tree-build.md](docs/bug-tm16xx-out-of-tree-build.md)

## Status / what is actually verified

Honest accounting, because "it works" claims in DIY hardware writeups are usually softer
than they look:

**Verified on real hardware**, surviving reboots:
- HID gadget enumerates: `/sys/class/udc` populated, `/dev/hidg0` + `/dev/hidg1` present,
  gadget presents as `1d6b:0104` "rk3318 KVM HID" (composite keyboard + absolute pointer).
- ustreamer serving MJPEG on `:8080`; capture survives the USB renumbering the overlay causes.
- `kvm-ui.py`: page, WebSocket handshake, every event type, malformed-input survival, and
  the no-host path (writes → `ESHUTDOWN` → UI banner, logged once not once-per-keystroke).
- DisplayLink console with a **visible** cursor; fix confirmed by mutter's own log line and
  by an A/B on cursor-plane commits (30+ → 0).
- Front panel: 4-digit clock tracking wall time, 5 LEDs with triggers, across a real reboot.

**Not verified / known gaps:**
- **Typing into a real target PC.** Everything gadget-side is verified headless, but whether
  keystrokes land and whether the absolute pointer maps where you expect on a given target
  resolution needs a cabled PC. Report back if you try it.
- No authentication (see above). No TLS. No audio, no mass-storage emulation, no ATX power
  control — all of which PiKVM has and this does not.
- Tested on one box, one Armbian build (see [docs/02-armbian-base.md](docs/02-armbian-base.md)
  for exact versions). RK3318 boxes vary *wildly* in wiring — especially the front panel,
  where Armbian ships **seven** different LED configurations. Expect to pick a different one.

## Related / prior art

- **[PiKVM](https://pikvm.org/)** — the real thing. Use it if you want a supported stack.
- **[ustreamer](https://github.com/pikvm/ustreamer)** — the MJPEG server used here (from the PiKVM project).
- **[tm16xx-display](https://github.com/jefflessard/tm16xx-display)** — the front-panel display driver.
- **[Armbian](https://www.armbian.com/)** — the base OS; `rk3318-box` is a community-maintained board.

## License

MIT — see [LICENSE](LICENSE). The bundled `hid-send`, `hid-gadget-up`, `kvm-ui.py`,
`panel-*` scripts and DT overlays are all original to this project.
