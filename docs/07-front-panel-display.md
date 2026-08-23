# 07 — The front-panel display and LEDs (FD628 + tm16xx)

These boxes have a real 4-digit 7-segment display and a few status LEDs behind the front
panel, driven by an FD628-class controller. Armbian already ships everything needed to use
it — and ships it **broken**. This page gets it working; the defect itself is written up
separately in [bug-armbian-rk3318-led-conf2.md](bug-armbian-rk3318-led-conf2.md).

End state: the panel shows a clock, the power LED is solid, and the LAN/WLAN segments track
link and traffic.

## First: figure out which panel *you* have

⚠️ **RK3318 boxes vary wildly here.** Armbian ships **seven** different
`rockchip-rk3318-box-led-conf*.dtbo` variants because the front panel is one of the least
standardised parts of these boards:

| overlay | bus | controller | notes |
|---|---|---|---|
| conf1, conf4 | — | — | GPIO LEDs only, no display |
| **conf2** | spi-gpio (3-wire) | FD628 / TM1628 | what this doc fixes |
| conf3 | — | — | regulators, unrelated to LEDs |
| conf5 | i2c-gpio (2-wire) | FD6551 | |
| conf6 | i2c-gpio (2-wire) | FD650 / TM1650 | |
| conf7 | i2c-gpio (2-wire) | FD6551, different pins | |

Check which one your install selected:

```bash
ssh root@<box-ip> 'grep ^overlays /boot/armbianEnv.txt'
```

**The authoritative source for your board is its own stock Android device tree.** If you
backed up stock Android before installing Armbian (you were told to in
[02-armbian-base.md](02-armbian-base.md)), the vendor DTB tells you the chip and the exact
pins. Extraction recipe, which works on Allwinner and Rockchip Android images alike:

```bash
# find FDT magic (d00dfeed) inside a boot/vendor_boot/dtbo partition image
grep -abo $'\xd0\x0d\xfe\xed' vendor_boot.img | head
# carve from that offset (totalsize is bytes 4..8, big-endian) and decompile
dtc -I dtb -O dts carved.dtb | grep -iE 'vfd|fd628|fd650|fd655|tm16|leds_clk|leds_dat'
```

Vendor nodes to look for: `fd628_dev` with `fd628_gpio_clk`/`_dat`, an `openvfd` node, or a
`leds { compatible = "allwinner,fd655_dev"; leds_clk = …; }`-style node. Those properties
are the ground truth for your wiring.

**Wire count narrows the chip family** on its own:

- **3 wires** (clk + data + strobe) → FD628, TM1628, FD620, TM1618, TM1620, TM1638 → SPI-like
- **2 wires** (clk + data only) → FD650, TM1650, FD655, FD6551, HBS658 → I2C-like

## The driver is already present

`tm16xx` is **not** in mainline — Armbian carries it as an out-of-tree patch — but it *is*
patched into the `rockchip64` family, so on an rk3318 box the modules already exist:

```bash
ssh root@<box-ip> 'ls /lib/modules/$(uname -r)/kernel/drivers/auxdisplay/tm16xx*'
# tm16xx.ko  tm16xx_i2c.ko  tm16xx_spi.ko
```

(If you are on a kernel family Armbian does *not* patch, you have to build it out of tree —
and that has its own two traps, documented in
[bug-tm16xx-out-of-tree-build.md](bug-tm16xx-out-of-tree-build.md).)

## Why it doesn't work out of the box

With `conf2` selected, the display never appears. `dmesg` says exactly why:

```
spi_gpio i2c-aux-display: cs36 >= max 1
spi_master spi0: spi_device register error /i2c-aux-display/display@24
spi_master spi0: Failed to create SPI device for /i2c-aux-display/display@24
```

Two independent defects in the shipped overlay:

1. **`reg = <0x24>` is an I2C slave address**, but the parent bus is `spi-gpio` with
   `num-chipselects = <1>`. The SPI core reads `reg` as the chip-select index and
   range-checks it — `0x24` = 36, so it is rejected and the device never registers. The only
   legal value on a single-CS bus is `0`.
2. **`compatible = "fdhisi,fd628"` alone matches no driver.** Per the tm16xx binding, `fd628`
   is only valid in the two-item form with `titanmec,tm1628`, and it is the *fallback* that
   carries the driver match. Confirm on your own box rather than taking this on faith:
   ```bash
   ssh root@<box-ip> 'grep -i fd628 /lib/modules/$(uname -r)/modules.alias'   # -> nothing
   ssh root@<box-ip> 'modinfo tm16xx_spi | grep alias'                        # -> tm1628, fd620, …
   ```

Everything *else* in that overlay is correct and valuable — the bus, the 5 LEDs, and the
full 4-digit segment maps. So the fix patches two properties and keeps the rest.

## The fix

[`overlays/rk3318-fd628-fix.dts`](../overlays/rk3318-fd628-fix.dts) targets the existing
node by path and overrides just those two properties.

Because `CONFIG_OF_CONFIGFS=y` on this kernel, it can be applied to the **live** device tree
— **no reboot, and no `armbianEnv.txt` edit at all.** That matters if the box is doing
something you'd rather not interrupt, and it keeps this change from colliding with the
`user_overlays=` entry the OTG gadget needs.

```bash
dtc -@ -I dts -O dtb -o rk3318-fd628-fix.dtbo overlays/rk3318-fd628-fix.dts
# (dtc warns about reg_format/unit_address -- harmless, it cannot see the target's cell
#  sizes through a target-path overlay)

scp rk3318-fd628-fix.dtbo root@<box-ip>:/usr/local/share/panel-display/
scp bin/panel-display-setup bin/panel-clock root@<box-ip>:/usr/local/sbin/
scp systemd/panel-display.service systemd/panel-clock.service root@<box-ip>:/etc/systemd/system/
ssh root@<box-ip> 'chmod 0755 /usr/local/sbin/panel-display-setup /usr/local/sbin/panel-clock &&
                   systemctl daemon-reload && systemctl enable --now panel-display panel-clock'
```

[`bin/panel-display-setup`](../bin/panel-display-setup) does the whole bring-up and is
**idempotent** — it checks whether the live DT is already correct and skips the overlay, so
moving the fix into u-boot `user_overlays=` later needs no change to it. It also handles
`spi_gpio` being unloaded, unbound, or bound-with-a-failed-child, each of which needs a
different action.

### The third trap: autoload silently does nothing

Even with the correct two-item `compatible`, the device's SPI modalias is derived from the
**first** compatible string:

```bash
ssh root@<box-ip> 'cat /sys/bus/spi/devices/spi0.0/modalias'   # -> spi:fd628
```

No module claims `spi:fd628`. The OF match on `titanmec,tm1628` *does* succeed — but only
once the module is already resident. So `modprobe tm16xx_spi` is **mandatory**, not a
convenience; without it the device sits on the bus with no driver and **no error message at
all**. `panel-display-setup` does this explicitly.

## Result

```bash
ssh root@<box-ip> 'ls /sys/class/leds/'
```

```
display              num_chars=4  max_brightness=8   message=HHMM
display::power       trigger=default-on
display::colon       trigger=timer   delay_on=500 delay_off=500
display::lan         trigger=netdev  device_name=eth0   link=1
display::wlan-lo     trigger=netdev  device_name=wlan0  link=1
display::wlan-hi     trigger=netdev  device_name=wlan0  tx=1 rx=1 interval=50
```

Write text to the display yourself:

```bash
ssh root@<box-ip> 'echo 1234 > /sys/class/leds/display/message'
ssh root@<box-ip> 'cat /sys/class/leds/display/max_brightness > /sys/class/leds/display/brightness'
```

> The attribute is **`message`**, not `value`. Some tm16xx documentation says `value`; that
> attribute does not exist. `message` comes from the kernel's `line-display` class, which
> tm16xx attaches to the same LED device.

**The colon is not a character.** The panel has only 4 digit positions, so `13:38` is `1338`
plus a separately-driven colon icon LED. Driving it from the kernel's own `timer` trigger
means it keeps blinking even if the clock service is stopped.

Note also that the overlay's own `linux,default-trigger` properties do **not** take effect
if the trigger modules aren't resident at probe time — you get `[none]` on everything.
`panel-display-setup` modprobes `ledtrig-timer` and `ledtrig-netdev` and sets the triggers
explicitly for that reason.

## Adapting to a different panel

If your box uses one of the i2c variants (conf5/6/7), you do **not** need this fix — `reg =
<0x24>` is a legitimate I2C slave address there, and those overlays are correct as shipped.
Just select the right one in `overlays=` and make sure `tm16xx_i2c` is loaded.

If the digits light but render as nonsense, the panel's segment/grid mapping differs from the
overlay's. The mapping lives in the `digits { digit@N { segments = <grid seg>, …; } }`
subnodes — seven `<grid segment>` pairs per digit, in the fixed order **a, b, c, d, e, f, g**.
The fastest way to re-derive it is to light one segment bit at a time and write down what
appears.
