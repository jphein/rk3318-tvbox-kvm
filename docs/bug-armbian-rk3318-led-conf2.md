# 🐛 Armbian `rockchip-rk3318-box-led-conf2.dtbo` cannot work as shipped

**Status:** unreported upstream as of writing. This page is deliberately structured as a
bug report so it can be filed as-is.

**Affected:** `armbian/build`, the `rk3318-box` board. The overlay source lives at
`patch/kernel/archive/rockchip64-<ver>/overlay/rockchip-rk3318-box-led-conf2.dtso` and is
**identical across at least `rockchip64-6.12` through `rockchip64-7.2`**, so this has been
broken for the entire life of the overlay.

**Impact:** every RK3318 box that selects `led-conf2` has a permanently dead front-panel
display. It fails silently from a user's point of view — the panel simply never lights, and
nothing points at the overlay.

---

## Summary

The overlay declares an FD628 display on an `spi-gpio` bus. It has **two independent
defects**, either of which alone is fatal:

1. `reg = <0x24>` — an **I2C slave address** in an **SPI** child node, where `reg` means
   chip-select index. Rejected by the SPI core, so the device never registers.
2. `compatible = "fdhisi,fd628"` on its own — a form the `tm16xx` binding does not allow,
   and which matches **no driver**. Even with defect 1 fixed, nothing binds.

The node is also *named* `i2c-aux-display` while being an `spi-gpio` bus, which is the
fingerprint of an I2C example copy-pasted onto SPI and explains how both defects arrived
together.

## The defective source

```dts
	i2c_aux_display: i2c-aux-display {
		#address-cells = <1>;
		#size-cells = <0>;
		compatible = "spi-gpio";
		sck-gpios = <&gpio2 RK_PC3 GPIO_ACTIVE_HIGH>;
		mosi-gpios = <&gpio2 RK_PC6 GPIO_ACTIVE_HIGH>;
		cs-gpios = <&gpio2 RK_PC2 GPIO_ACTIVE_HIGH>;
		num-chipselects = <1>;

		display@24 {
			compatible = "fdhisi,fd628";
			reg = <0x24>;
			spi-3wire;
			…
```

## Defect 1 — invalid `reg` on an SPI child

For an SPI child, `reg` is the **chip-select index**, and the SPI core range-checks it
against the controller's `num-chipselect`. `0x24` is 36; the bus declares 1 chip-select.

Observed on a real box (kernel 6.18.45, `rockchip64`):

```
spi_gpio i2c-aux-display: cs36 >= max 1
spi_master spi0: spi_device register error /i2c-aux-display/display@24
spi_master spi0: Failed to create SPI device for /i2c-aux-display/display@24
```

That `cs%d >= max %d` string comes from `drivers/spi/spi.c`, in the validation loop that
runs over each chip-select parsed out of `reg`:

```c
	for (idx = 0; idx < spi->num_chipselect; idx++) {
		/* Chipselects are numbered 0..max; validate. */
		cs = spi_get_chipselect(spi, idx);
		if (cs >= ctlr->num_chipselect) {
			dev_err(dev, "cs%d >= max %d\n", spi_get_chipselect(spi, idx),
				ctlr->num_chipselect);
			return -EINVAL;
		}
	}
```

`-EINVAL` here means the SPI device is never created at all.

**Correct value:** `reg = <0>`.

For contrast, Armbian's own i2c-based variants (`led-conf5/6/7`) use `reg = <0x24>`
**correctly**, because on an `i2c-gpio` bus `0x24` genuinely is the slave address these
chips use. The defect is specific to the one SPI overlay.

## Defect 2 — `compatible` matches no driver

The `tm16xx` binding permits `fdhisi,fd628` only in a two-item form with a fallback:

```yaml
  compatible:
    oneOf:
      - items:
          - enum:
              - fdhisi,fd628
              - princeton,pt6964
              - wxicore,aip1628
          - const: titanmec,tm1628
```

The bare-enum branch lists `fd620, fd655, fd6551, tm1618, tm1620, tm1628, tm1638, hbs658` —
**not** `fd628`. It is `titanmec,tm1628` that carries the actual driver match.

Verifiable on any box with the module present, without touching hardware:

```console
$ grep -i fd628 /lib/modules/$(uname -r)/modules.alias
                                       # (no output -- nothing claims fd628)
$ modinfo tm16xx_spi | grep '^alias'
alias:          of:N*T*Cfdhisi,fd620C*
alias:          of:N*T*Ctitanmec,tm1628C*
alias:          of:N*T*Ctitanmec,tm1638C*
alias:          of:N*T*Ctitanmec,tm1620C*
alias:          of:N*T*Ctitanmec,tm1618C*
                                       # fd628 is absent
```

**Correct value:** `compatible = "fdhisi,fd628", "titanmec,tm1628";`

Armbian's `led-conf6` gets the analogous case right, with a comment that reads like someone
already hit this once:

```dts
compatible = "fdhisi,fd650" , "titanmec,tm1650"; /* do not remove tm1650 */
```

## Reproduction

1. Any RK3318 box on Armbian `rockchip64`, with `rk3318-box-led-conf2` in `overlays=`.
2. Boot. `dmesg | grep -i spi` shows the `cs36 >= max 1` triad above.
3. `ls /sys/bus/spi/devices/` is empty; `/sys/class/leds/` has no `display*` entries.
4. The front panel stays dark.

## Suggested patch

```diff
 		display@24 {
-			compatible = "fdhisi,fd628";
-			reg = <0x24>;
+			compatible = "fdhisi,fd628", "titanmec,tm1628";
+			reg = <0>;
 			spi-3wire;
```

Renaming the node to `display@0` (to match `reg`) and the bus to something not containing
"i2c" would both be cosmetic improvements, but only the two properties above are required.

## Verification that the patch is sufficient

Applied as a runtime corrective overlay on a live box (see
[07-front-panel-display.md](07-front-panel-display.md)), changing only those two properties:

```
before:  /proc/device-tree/.../display@24/reg  ->  00 00 00 24
         compatible                            ->  "fdhisi,fd628"
after:   reg                                   ->  00 00 00 00
         compatible                            ->  "fdhisi,fd628" "titanmec,tm1628"

then:  spi child registered: spi0.0
       driver bound: /sys/bus/spi/drivers/tm16xx-spi
       /sys/class/leds/: display  display::colon  display::lan  display::power
                         display::wlan-hi  display::wlan-lo
       echo 1234 > /sys/class/leds/display/message   -> panel shows 1234
```

Everything else in the overlay — the bus, the five LED definitions, and the four-digit
segment maps — is correct and works unchanged. Only those two properties are wrong.

## A related third issue (not a defect in the overlay)

Worth mentioning in any report because it will be the next question. Even with both
properties fixed, the module does **not** autoload: the SPI modalias is derived from the
first `compatible` string, giving `spi:fd628`, which no module claims.

```console
$ cat /sys/bus/spi/devices/spi0.0/modalias
spi:fd628
```

The OF match on `titanmec,tm1628` succeeds, but only once `tm16xx_spi` is already resident,
so `modprobe tm16xx_spi` (or a `modules-load.d` entry) is required. This is arguably working
as designed for SPI modalias generation, but it makes the failure mode silent, and any
documentation of the overlay should mention it.
