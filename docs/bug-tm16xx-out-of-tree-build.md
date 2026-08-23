# 🐛 `tm16xx-display`: out-of-tree build is broken on any kernel it isn't patched into

**Status:** unreported upstream as of writing. Structured as a bug report so it can be filed
as-is.

**Project:** [jefflessard/tm16xx-display](https://github.com/jefflessard/tm16xx-display)

**Context:** `tm16xx` is **not** in mainline Linux. Armbian carries it as an out-of-tree
patch, but only for the `rockchip64` and `meson64` kernel families. On any other family
(`sunxi64`, for instance) `CONFIG_TM16XX` does not exist and the modules are absent, so
building from this repo is the only option — and `make module` fails for two unrelated
reasons.

Neither failure is in the driver code itself. Both are in the build system, and both produce
error messages that point somewhere other than the cause.

---

## Defect 1 — the repo ships the full mainline `auxdisplay` Makefile

`drivers/auxdisplay/Makefile` in the repo is a copy of the complete mainline file:

```make
obj-$(CONFIG_ARM_CHARLCD)	+= arm-charlcd.o
obj-$(CONFIG_CFAG12864B)	+= cfag12864b.o cfag12864bfb.o
obj-$(CONFIG_CHARLCD)		+= charlcd.o
obj-$(CONFIG_HD44780_COMMON)	+= hd44780_common.o
obj-$(CONFIG_HD44780)		+= hd44780.o
…
obj-$(CONFIG_TM16XX)		+= tm16xx.o
```

But the repo only ships `tm16xx_*.c`, `line-display.c` and their headers. So on any kernel
whose config enables the *other* auxdisplay drivers — which is common; the verified box had
`CONFIG_CHARLCD=m`, `CONFIG_HD44780=m`, `CONFIG_LCD2S=m`, `CONFIG_IMG_ASCII_LCD=m` — an
out-of-tree `M=` build tries to build objects with no sources:

```
make[4]: *** No rule to make target 'charlcd.o', needed by './'.  Stop.
make[3]: *** [/usr/src/linux-headers-.../Makefile:2052: .] Error 2
make: *** [Makefile:50: module] Error 2
```

The message names `charlcd.o`, a driver the user is not trying to build and has no reason to
think about.

**Fix:** the out-of-tree Makefile should list only the targets the repo actually ships.

## Defect 2 — `EXTRA_CFLAGS` is ignored by modern kbuild

The repo's top-level Makefile passes its feature defines like this:

```make
CONFIG  += CONFIG_TM16XX=m
CCFLAGS += -DCONFIG_TM16XX
CONFIG  += CONFIG_TM16XX_KEYPAD=y
CCFLAGS += -DCONFIG_TM16XX_KEYPAD
…
module:
	make EXTRA_CFLAGS="$(CCFLAGS)" -C $(KDIR) M=$(PWD)/$(MDIR) $(CONFIG) modules
```

`EXTRA_CFLAGS` is a legacy kbuild variable that modern kbuild **no longer honours** —
`ccflags-y` is the supported mechanism. The result is a *split-brain* build, which is what
makes this confusing rather than merely broken:

- The `CONFIG_*=y/m` assignments **do** reach make, so `tm16xx_keypad.c` is added to the
  object list and compiled.
- The matching `-DCONFIG_TM16XX_KEYPAD` **does not** reach the compiler.

So while compiling `tm16xx_keypad.c`, this guard in `tm16xx.h` evaluates false:

```c
#if IS_ENABLED(CONFIG_TM16XX_KEYPAD)
int tm16xx_keypad_probe(struct tm16xx_display *display);
…
#else
static inline int tm16xx_keypad_probe(struct tm16xx_display *display)
{
	return 0;
}
```

…so the header emits its stubs *in the same translation unit* as the real definitions:

```
tm16xx_keypad.c:135:5: error: redefinition of 'tm16xx_keypad_probe'
tm16xx.h:184:19: note: previous definition of 'tm16xx_keypad_probe' …
make[4]: *** [.../Makefile.build:287: tm16xx_keypad.o] Error 1
```

A redefinition error inside the driver's own header reads like a source bug. It is not — it
is a build-variable that silently stopped working. (Note the same silence applies to the
`-include tm16xx_compat.h` and `-I include/` flags, which also never arrive; they simply
happen not to be needed on recent kernels, so the failure surfaces only via the keypad
guard.)

**Fix:** set the defines as `ccflags-y` in the module Makefile, so they are honoured, e.g.:

```make
ccflags-y += -DCONFIG_TM16XX -DCONFIG_TM16XX_I2C -DCONFIG_TM16XX_SPI
ccflags-y += -I$(src)/../../include
ccflags-y += -include $(src)/tm16xx_compat.h
```

## Working out-of-tree recipe

Both defects fixed, keypad disabled at both levels (no keys needed for a plain display).
Replace `drivers/auxdisplay/Makefile` with:

```make
ccflags-y += -DCONFIG_TM16XX
ccflags-y += -DCONFIG_TM16XX_I2C
ccflags-y += -DCONFIG_TM16XX_SPI
ccflags-y += -I$(src)/../../include
ccflags-y += -include $(src)/tm16xx_compat.h

obj-$(CONFIG_LINEDISP)		+= line-display.o
obj-$(CONFIG_TM16XX)		+= tm16xx.o
tm16xx-y			+= tm16xx_core.o
obj-$(CONFIG_TM16XX_I2C)	+= tm16xx_i2c.o
obj-$(CONFIG_TM16XX_SPI)	+= tm16xx_spi.o
```

then invoke the inner make directly (bypassing the top-level `EXTRA_CFLAGS` path):

```bash
make -C /lib/modules/<target-kernel>/build \
     M=$PWD/drivers/auxdisplay \
     CONFIG_TM16XX=m CONFIG_TM16XX_I2C=m CONFIG_TM16XX_SPI=m CONFIG_LINEDISP=m \
     modules

make -C /lib/modules/<target-kernel>/build \
     M=$PWD/drivers/auxdisplay \
     CONFIG_TM16XX=m CONFIG_TM16XX_I2C=m CONFIG_TM16XX_SPI=m CONFIG_LINEDISP=m \
     modules_install
```

Note you can build for a kernel other than the running one — pass the target kernel's build
directory and check the result before rebooting into it:

```bash
modinfo drivers/auxdisplay/tm16xx_i2c.ko | grep vermagic
```

## Third issue: `modules_install` silently skips `depmod`

Not a repo defect, but it bites immediately after a successful build and the symptom is
"the driver just doesn't load":

```
  INSTALL /lib/modules/<ver>/updates/tm16xx_i2c.ko
  DEPMOD  /lib/modules/<ver>
Warning: modules_install: missing 'System.map' file. Skipping depmod.
```

`depmod` is skipped, so `modules.alias` gets **no** entry for the chip and autoload quietly
does nothing. The `System.map` is usually in `/boot`, not in the build directory, so running
depmod by hand works:

```bash
depmod -a <target-kernel-version>
grep -i fd655 /lib/modules/<ver>/modules.alias        # should now resolve to tm16xx_i2c
grep '^updates/tm16xx_i2c' /lib/modules/<ver>/modules.dep
```

Mentioning this in the repo's README would save people a confusing hour.

## Documentation nit

The README instructs writing to `/sys/class/leds/<display>/value`. **That attribute does not
exist** — the writable text attribute is `message`, provided by the kernel's `line-display`
class which `tm16xx` attaches to the same LED device. The repo's own shipped
`display-service` script uses `message` correctly, so only the README is stale.
