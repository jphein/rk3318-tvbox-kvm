# 04 — USB HID gadget (the input half)

Makes the box present itself to a target PC as a **composite USB keyboard + absolute
pointer**, driverless on Linux, Windows and macOS. This is the same approach PiKVM uses.

**Prerequisite:** read [01-hardware.md](01-hardware.md) first — you must free the dwc2
socket, and there is a VBUS hazard to understand before cabling to a PC.

## Kernel support: already there

Nothing needs compiling. On the verified Armbian build:

```
CONFIG_USB_DWC2=y                 CONFIG_USB_DWC2_DUAL_ROLE=y
CONFIG_USB_GADGET=y               CONFIG_USB_LIBCOMPOSITE=m
CONFIG_USB_CONFIGFS=m             CONFIG_USB_CONFIGFS_F_HID=y
CONFIG_USB_F_HID=m
```

Check yours:

```bash
ssh root@<box-ip> 'zgrep -E "CONFIG_USB_(DWC2|GADGET|CONFIGFS|F_HID)" /proc/config.gz \
  || grep -E "CONFIG_USB_(DWC2|GADGET|CONFIGFS|F_HID)" /boot/config-$(uname -r)'
```

## Step 1 — flip dwc2 to peripheral

[`overlays/rk3318-otg-peripheral.dts`](../overlays/rk3318-otg-peripheral.dts) does two
things: sets `dr_mode = "peripheral"` on `usb@ff580000`, and disables the `vcc_otg_vbus`
regulator (see the VBUS section of [01-hardware.md](01-hardware.md)).

Armbian ships **no** rk3318 OTG overlay — only `rk3308-otg-host.dtbo` and a RockPi one —
which is why this is custom.

```bash
dtc -@ -I dts -O dtb -o rk3318-otg-peripheral.dtbo overlays/rk3318-otg-peripheral.dts
scp rk3318-otg-peripheral.dtbo root@<box-ip>:/boot/overlay-user/
ssh root@<box-ip> '
  grep -q "^user_overlays=" /boot/armbianEnv.txt \
    && sed -i "s|^user_overlays=.*|& rk3318-otg-peripheral|" /boot/armbianEnv.txt \
    || echo "user_overlays=rk3318-otg-peripheral" >> /boot/armbianEnv.txt
  sync'
```

Validate offline before rebooting — this uses the same libfdt path u-boot will:

```bash
fdtoverlay -i /boot/dtb/rockchip/rk3318-box.dtb -o /tmp/m.dtb \
           /boot/overlay-user/rk3318-otg-peripheral.dtbo && echo "merge OK"
dtc -I dtb -O dts /tmp/m.dtb | grep -A2 'usb@ff580000' | grep dr_mode   # want: "peripheral"
```

Then reboot. Afterwards:

```bash
ssh root@<box-ip> 'ls /sys/class/udc/'    # want: ff580000.usb  (was empty before)
ssh root@<box-ip> 'tr -d "\0" < /proc/device-tree/usb@ff580000/dr_mode'   # want: peripheral
```

## Step 2 — the gadget itself

[`bin/hid-gadget-up`](../bin/hid-gadget-up) builds a configfs composite gadget. It is
**idempotent** and has a `down` path, so the unit is restartable rather than
one-shot-and-pray.

```bash
scp bin/hid-gadget-up root@<box-ip>:/usr/local/sbin/
scp systemd/hid-gadget.service root@<box-ip>:/etc/systemd/system/
ssh root@<box-ip> 'chmod 0755 /usr/local/sbin/hid-gadget-up &&
                   systemctl daemon-reload && systemctl enable --now hid-gadget'
ssh root@<box-ip> 'ls -l /dev/hidg0 /dev/hidg1'
```

What it creates, and why:

- **`1d6b:0104`** — Linux Foundation / Multifunction Composite Gadget. Generic and
  driverless for plain boot-protocol HID on all three major OSes.
- **`/dev/hidg0` — keyboard**, protocol 1, subclass 1 (boot interface), 8-byte reports:
  `modifiers(1) reserved(1) keycode[6]`. The canonical boot-protocol descriptor.
- **`/dev/hidg1` — absolute pointer**, protocol 2, subclass 0, 6-byte reports:
  `buttons(1: 5 bits + 3 pad) absX(2) absY(2) wheel(1)`, axes `0..32767` little-endian.
- **Serial number derived from `/etc/machine-id`**, so the target PC sees one stable device
  across reboots rather than a new one each time.

  > 🔒 **Privacy note.** systemd's own documentation says `/etc/machine-id` should be
  > treated as confidential and not exposed on untrusted networks, because it is a stable
  > unique identifier for the host. Using the first 16 hex characters of it as a USB serial
  > means **every machine you plug the gadget into can read it** and could correlate visits.
  > For a KVM on your own bench that is almost certainly fine. If you would rather not,
  > replace that line in `hid-gadget-up` with any fixed or randomly-generated string — the
  > only requirement is that it be stable, so the target doesn't re-enumerate a new device
  > on every reboot:
  >
  > ```sh
  > echo "0123456789abcdef" > strings/0x409/serialnumber
  > ```
- **Link order sets the numbering** — `hid.kbd` is linked into the config first, so it
  becomes `hidg0` and the mouse becomes `hidg1`. Nothing else guarantees that mapping.

### Why absolute and not relative pointing

Relative pointing over a network is miserable: the target applies its own pointer
acceleration to deltas that arrive in bursts, so the cursor drifts away from where your
real mouse is and never comes back. Absolute reports (`0..32767` mapped across the target's
screen) have no such drift — you point, it goes there. This is what PiKVM does too.

The tradeoff is that the target maps `0..32767` across its *whole* screen, so multi-monitor
targets will feel wrong (the range spans the entire desktop).

### The unit's ordering is deliberately weak

`hid-gadget.service` is ordered `After=sys-kernel-config.mount local-fs.target` and has
**no relationship at all** to `kvm-stream` or `displaylink-driver`. That is intentional:
this unit must never be able to take the capture or the console down with it. It also has
`Restart=on-failure`, because dwc2 can be slow to register the UDC on a cold boot — the
script polls for 10 s, but if it loses the race, let systemd retry rather than leaving you
with no gadget.

## Step 3 — drive it from the CLI

[`bin/hid-send`](../bin/hid-send) is a small stdlib-only Python seam. Deliberately dumb — a
later kvmd layer can replace it entirely.

```bash
scp bin/hid-send root@<box-ip>:/usr/local/bin/ && ssh root@<box-ip> 'chmod 0755 /usr/local/bin/hid-send'

ssh root@<box-ip> 'hid-send type "hello world"'
ssh root@<box-ip> 'hid-send key enter'
ssh root@<box-ip> 'hid-send key ctrl+alt+delete'
ssh root@<box-ip> 'hid-send move 50% 50%; hid-send click'
ssh root@<box-ip> 'hid-send scroll -3'
```

## Testing with NO target attached — do this first

```bash
ssh root@<box-ip> '
  ls /sys/class/udc/            # expect: ff580000.usb
  ls -l /dev/hidg0 /dev/hidg1   # expect both present
  hid-send type hello           # expect: "no USB host attached", exit 3
'
```

**Both nodes existing while `hid-send` exits 3 is the correct, healthy result.** A gadget
has nowhere to send reports until a host enumerates it, so `write()` returning
`EAGAIN`(11), `EPIPE`(32) or `ESHUTDOWN`(108) is the normal no-host signature — not a
fault. `hid-send` distinguishes that case from a real error specifically so this test is
unambiguous, and `kvm-ui.py` classifies the same errnos to drive its UI banner.

## Testing with a real target

1. A-to-A cable from the **dwc2 socket** to the PC. (Check VBUS first —
   [01-hardware.md](01-hardware.md).)
2. The PC should enumerate `1d6b:0104` "rk3318 KVM HID" as a composite keyboard + pointer.
3. `hid-send type "hello"` should appear in whatever has focus; `hid-send move 50% 50%`
   should jump the pointer to screen centre.

> ⚠️ **This is the step that has not been verified here.** Everything above was tested
> headless. Whether keystrokes actually land, and whether the absolute pointer lands where
> you expect at your target's resolution, needs a cabled PC. If you try it, please report
> back.

## Troubleshooting

**`/sys/class/udc/` still empty:** the overlay didn't apply. Check
`tr -d '\0' < /proc/device-tree/usb@ff580000/dr_mode` — if it still says `host`, look for
`Error applying DT overlays` on the boot console, and remember that *one* bad entry in
`user_overlays=` discards *all* of them ([02-armbian-base.md](02-armbian-base.md)).

**`/dev/hidg*` missing but a UDC exists:** `journalctl -u hid-gadget`. The script logs each
step and warns explicitly if the nodes don't appear after binding.

**The gadget port no longer enumerates USB sticks:** correct and expected. With
`dr_mode = "peripheral"` dwc2 registers only a UDC and no host controller, so there is no
`usbN` under `/sys/devices/platform/ff580000.usb` at all.

**Revert everything:**

```bash
ssh root@<box-ip> 'systemctl disable --now hid-gadget
  sed -i "s/ *rk3318-otg-peripheral//" /boot/armbianEnv.txt && sync && reboot'
```
