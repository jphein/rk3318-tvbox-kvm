# 02 — Armbian base install

## Exact versions this was built and verified on

Everything in this repo was developed against:

```
Armbian 26.11.0-trunk.21 (nightly)   BOARD=rk3318-box   BOARDFAMILY=rockchip64
kernel 6.18.45-current-rockchip64    BOARD_TYPE=tvb     BRANCH=current
```

Nothing here is version-fragile in principle — it's device tree, configfs and systemd — but
the front-panel half in particular depends on which overlays your Armbian ships. Newer or
older builds may name them differently.

## Getting Armbian onto the box

Out of scope for this repo, because it is entirely box-specific and there is a large
existing community around it. The `rk3318-box` target is **community-maintained** and these
boxes lie about their hardware constantly (fake RAM sizes, fake eMMC, three different wifi
chips under one model name).

Start here:

- Armbian's `rk3318-box` board page and the long-running RK3318/RK3328 TV box forum thread —
  that thread is where the DRAM timings and eMMC quirks for specific board revisions live.
- The `multitool` image is the usual way to back up stock Android and write a new image.

**Back up your stock Android first.** It is the only copy of your box's vendor device tree,
and that vendor DTB is genuinely valuable — it is the ground truth for how *your* board is
wired (front panel pins, LED GPIOs, regulators). See
[07-front-panel-display.md](07-front-panel-display.md) for how to extract and read it.

## The load-bearing overlays — do not remove these

After a successful install, `/boot/armbianEnv.txt` will look something like:

```
overlay_prefix=rockchip
fdtfile=rockchip/rk3318-box.dtb
overlays=rk3318-box-cpu-hs rk3318-box-emmc-ddr rk3318-box-emmc-hs200 rk3318-box-led-conf2
```

⚠️ **Leave `overlays=` alone.** Those entries are load-bearing — the `emmc-*` ones are eMMC
timing, and getting them wrong can make the box unbootable. In particular:

- Everything this project adds goes in **`user_overlays=`**, never `overlays=`.
- `user_overlays=` loads `.dtbo` files from **`/boot/overlay-user/`** (create it if absent),
  and — unlike `overlays=` — the filenames do **not** get the `overlay_prefix` prepended.
- `user_overlays=` is applied **after** `overlays=`, which is what lets a user overlay
  patch a property that a kernel-provided overlay set.

### The failure mode worth knowing about

Armbian's boot script does roughly:

```
for overlay_file in ${overlays};      do fdt apply … || setenv overlay_error "true"; done
for overlay_file in ${user_overlays}; do fdt apply … || setenv overlay_error "true"; done
if overlay_error: restore the pristine DT and boot anyway
```

Two consequences:

1. **A bad overlay costs you the feature, not the box.** u-boot falls back to the untouched
   device tree. This makes overlay experimentation much safer than it sounds.
2. **But it is all-or-nothing.** If *any* overlay fails to apply, *every* overlay is
   discarded. So one broken entry in `user_overlays=` silently disables all the others —
   and the symptom presents as some *unrelated* feature mysteriously regressing.

If you have more than one user overlay, validate the **combination** offline, not just your
own file:

```bash
# apply them in boot order onto a copy of the real base DTB
fdtoverlay -i /boot/dtb/rockchip/rk3318-box.dtb -o /tmp/combined.dtb \
    /boot/overlay-user/first.dtbo /boot/overlay-user/second.dtbo && echo "combined merge OK"
dtc -I dtb -O dts /tmp/combined.dtb | grep -A5 'the-node-you-expect'
```

`fdtoverlay` uses the same libfdt path as u-boot's `fdt apply`, so this is a genuine
pre-flight check rather than an approximation.

## Sanity check before starting

```bash
ssh root@<box-ip> '
  uname -r
  cat /etc/armbian-release | grep -E "^(VERSION|BOARD|BRANCH)="
  grep -E "^overlays|^user_overlays|^fdtfile" /boot/armbianEnv.txt
  ls -d /boot/overlay-user 2>/dev/null || echo "(no /boot/overlay-user yet -- create it)"
'
```

Also back up `armbianEnv.txt` before you touch it. It is a tiny file and it is the one that
decides whether the box boots:

```bash
ssh root@<box-ip> 'cp -a /boot/armbianEnv.txt /root/armbianEnv.txt.bak-$(date +%F-%H%M%S)'
```
