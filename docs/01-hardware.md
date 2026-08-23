# 01 — Hardware, the port map, and the VBUS hazard

## Bill of materials

| item | approx | notes |
|---|---|---|
| RK3318/RK3328 Android TV box | $15–25 used | The generic H96 / X88 / MXQ-class boxes. 2 GB RAM is plenty; 4 GB is nicer if you want the GNOME console too. |
| MS2109 HDMI→USB capture stick | ~$10 | USB ID `534d:2109`. 1080p30 MJPEG, UVC, driverless. |
| Powered USB hub | ~$10 | Needed because everything has to share the one xHCI socket. |
| USB A-to-A cable | ~$5 | Box gadget port → target PC. **Read the VBUS section below before using one.** |
| *(optional)* DisplayLink lapdock | varies | For the crash-cart console. A Sentio Superbook was used here. |

## The USB controller map — this is the whole trick

The RK3328-family SoC has four USB controllers. **Only one of them can be a USB gadget**,
and on a stock box it is invariably the socket that already has everything plugged into it.

| controller | DT node | gadget capable? | typical state on a stock box |
|---|---|---|---|
| **dwc2** | `usb@ff580000` | ✅ **the only candidate** | everything is plugged in here |
| dwc3 / xHCI | `usb@ff600000` | ✗ (host-only as wired) | usually empty |
| EHCI | `usb@ff5c0000` | ✗ | usually empty |
| OHCI | `usb@ff5d0000` | ✗ | usually empty |

`/sys/class/udc/` is **empty** out of the box because the TV-box DTB sets
`dr_mode = "host"` on `usb@ff580000`. dwc2 is built in and dual-role capable, so this is a
one-property device-tree change — **not** a kernel rebuild:

```bash
tr -d '\0' < /proc/device-tree/usb@ff580000/dr_mode     # -> "host" on a stock box
```

Note `CONFIG_USB_DWC2_HOST` and `CONFIG_USB_DWC2_PERIPHERAL` are both *unset* while
`CONFIG_USB_DWC2_DUAL_ROLE=y` — meaning dwc2's role comes entirely from the DT `dr_mode`,
which is exactly the knob [the overlay](../overlays/rk3318-otg-peripheral.dts) turns.

### Identifying which physical socket is which, without a datasheet

A physical connector is hard-wired to one controller and PHY (dwc2 uses `u2phy_otg`, xHCI
uses `u2phy_host` + `usb3phy`), so these really are distinct sockets — not one socket being
re-routed. To find out which is which, plug a USB stick into one socket and watch:

```bash
ssh root@<box-ip> 'dmesg -w | grep -E "usb [0-9]-"'
```

- `usb 1-…` → **dwc2**. This is the socket that becomes your gadget port.
- `usb 2-…` or `usb 3-…` → **xHCI**. This is where your hub needs to move to.

Do this **before** applying the overlay, because afterwards dwc2 registers only a UDC and
no host controller — a stick plugged into it will be simply dead, and that's expected, not
a fault.

### The migration

1. Move the hub — with capture stick, dock and any HIDs still attached — from the **dwc2**
   socket to the **xHCI** socket. Everything keeps working; a DisplayLink dock arguably
   improves, since xHCI can offer USB3 instead of shared 480M.
2. Confirm with `lsusb -t` that the hub now hangs off `xhci-hcd`, and that your capture and
   console are still healthy.
3. Install the overlay and reboot. The now-empty dwc2 socket is the gadget port.

If the hub genuinely cannot live on xHCI, this box cannot do capture + console + gadget
simultaneously with two usable sockets, and a separate ~$5 **ESP32-S3 HID device** is a
better input half — it keeps the two halves on separate hardware.

### Side effect: USB bus numbers shift

Because dwc2 stops registering a host bus, everything renumbers:

| | before | after |
|---|---|---|
| dwc2 | bus 1 | **no bus** (UDC only) |
| xHCI | bus 2 / 3 | **bus 1 / 2** |
| EHCI | bus 4 | **bus 3** |
| OHCI | bus 5 | **bus 4** |

Harmless in itself — v4l2 nodes and device paths are what ustreamer and DisplayLink key
off, and both survive. But if you have a udev rule or script that hardcodes `Bus 002`,
this is why it moved. Prefer `/dev/v4l/by-id/…` paths (as
[kvm-stream.service](../systemd/kvm-stream.service) does) precisely for this reason.

---

## ⚠️ VBUS back-feed — read before cabling to a PC

On these boxes `vcc_otg_vbus` is a `regulator-fixed` marked **`regulator-always-on`**,
which parks a GPIO high and makes the box **actively source 5 V onto the OTG socket's VBUS
pin**:

```
gpio-27  ( |vcc-otg-vbus ) out hi
```

Cable that socket to a target PC with an A-to-A cable and you have **two 5 V sources
fighting on one rail** — back-powering the PC's port, and possibly confusing session
detection.

[The overlay therefore also disables that regulator.](../overlays/rk3318-otg-peripheral.dts)
This is safe: nothing in the device tree references the `vcc_otg_vbus` phandle — zero
consumers — so the node exists only to park the pin high.

This is deliberately **not** solved by cutting the VBUS wire in the cable: dwc2 needs to
*sense* VBUS from the PC to detect the session. The correct configuration is **sense but
don't source** — VBUS wire intact, box-side regulator off.

After rebooting, confirm the socket is not live *before* plugging it into anything you care
about:

```bash
ssh root@<box-ip> 'tr -d "\0" < /proc/device-tree/vcc-otg-vbus/status'   # want: disabled
ssh root@<box-ip> 'grep -i otg /sys/kernel/debug/gpio || echo "otg vbus gpio released (good)"'
```

A-to-A cabling is electrically unusual at the best of times. The overlay removes the main
hazard (the box-side source), but do the check above rather than trusting it.

## Why `peripheral` and not `otg`

The overlay sets `dr_mode = "peripheral"`, not `"otg"`. On a plain USB-A socket there is no
ID pin, so OTG role detection is unreliable and can leave you with an intermittent gadget
that works only sometimes — a genuinely annoying thing to debug. `peripheral` is
unambiguous. The cost is that the socket becomes device-only until you remove the overlay.

**Revert:** delete the `user_overlays=` entry and reboot. A failed `fdt apply` makes u-boot
restore the pristine device tree and boot on anyway, so a bad overlay costs you the gadget,
not the box.
