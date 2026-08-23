# 06 — DisplayLink lapdock as a crash-cart console (and the invisible-cursor fix)

A DisplayLink lapdock (a screen + keyboard + trackpad in a laptop shell, e.g. a Sentio
Superbook) makes the TV box into a self-contained crash cart: plug in one USB cable and you
have a physical console for the box itself.

The interesting part is the bug at the end. If you only came here for that, jump to
[the invisible cursor](#the-invisible-cursor).

## Install DisplayLink + evdi

DisplayLink hardware needs the vendor's userspace daemon (`DisplayLinkManager`) plus the
`evdi` kernel module. `evdi` is DKMS and builds fine on aarch64.

```bash
ssh root@<box-ip> 'apt-get install -y dkms libdrm-dev'
# fetch DisplayLink's .run installer for your architecture from their site, then:
ssh root@<box-ip> 'chmod +x displaylink-driver-*.run && ./displaylink-driver-*.run'
ssh root@<box-ip> 'dkms status; systemctl is-active displaylink-driver'
```

Verified here with **evdi 1.15.0** against kernel **6.18.45**, driver package 6.3.0-48.
Newer kernels regularly break evdi; if DKMS fails to build, that is the first thing to
check.

Once up, the dock appears as an extra DRM device:

```bash
ssh root@<box-ip> 'ls -l /dev/dri/; for c in /sys/class/drm/card*-*/; do
    echo "$c $(cat $c/status) $(cat $c/enabled)"; done'
```

Expect the dock as `card2-DVI-I-1  connected  enabled` (card numbering varies).

## Getting a session onto it

Simplest path is a normal desktop session with autologin — GDM + GNOME on Wayland works,
and on these boxes the dock is often the *only* connected output (the SoC's own HDMI is
`disconnected` unless you plug a TV in).

That gives you a real desktop with a real pointer, which is what you want from a crash
cart. It is also heavier than the alternatives; a `cage`/`wlroots` kiosk or plain fbcon are
lighter, but see the notes at the bottom for why those were harder here than they look.

---

## The invisible cursor

**Symptom:** the dock's screen works, the keyboard works, the trackpad moves the pointer —
but **there is no visible cursor**. Nothing is drawn at all.

### The wrong diagnosis (worth documenting, because it is the natural one)

The obvious read is "it's the framebuffer console, so install `gpm`". On this box that is
wrong twice over:

```bash
cat /proc/fb                                    # EMPTY
ls /sys/class/graphics/                         # only 'fbcon' -- no fb0
cat /sys/class/vtconsole/vtcon0/name            # "(S) dummy device"
grep DRM_FBDEV /boot/config-$(uname -r)         # nothing; only CONFIG_FB_CORE=y
```

There is **no fbdev at all**, and the only vtconsole is the **dummy** driver — so fbcon
renders nowhere. `gpm` draws its pointer by inverting cells in VT screen memory, so on a box
in this state it can never produce a visible cursor no matter how it is configured.
`/dev/input/mice` + `exps2` versus `-t evdev` is a red herring; the whole avenue is dead by
construction.

What misleads you is `fgconsole` returning `2` — tty2 is where the **Wayland session**
lives, not a text console.

### The actual cause

The dock is on a **secondary GPU** (evdi), while the primary is the SoC's own display
controller with nothing connected:

```
gnome-shell: Failed to initialize accelerated iGPU/dGPU framebuffer sharing: No matching EGL configs
gnome-shell: Integrated GPU /dev/dri/card0 selected as primary
gnome-shell: Added device '/dev/dri/card2' (evdi) using atomic mode setting.
```

So the compositor renders on the render node and **CPU-copies** the result to evdi for
scanout. Now the key fact:

```bash
modetest -M evdi -p        # from libdrm-tests
```

```
Planes:
33  type: Primary (value 1)   formats: XR24 AR24 XB24 AB24
35  type: Cursor  (value 2)   formats: XR24 AR24 XB24 AB24
```

**evdi advertises a DRM plane of type `Cursor`.** So the compositor takes the *hardware*
cursor path — and a compositor that believes it has a hardware cursor deliberately does
**not** composite a software cursor into the scene. But DisplayLink's userspace only
transports the **primary** plane to the dock. The cursor-plane commits are accepted by the
kernel and silently dropped.

Confirmed directly with mutter's own cursor debug topic, while injecting pointer motion:

```
KMS: Realizing HW cursor for cursor sprite for CRTC 37 (/dev/dri/card2)
KMS: [atomic] Assigning cursor plane (35, /dev/dri/card2) to 46, 64x64+0+0 -> 64x64+42+27
KMS: [atomic] Assigning cursor plane (35, /dev/dri/card2) to 46, 64x64+0+0 -> 64x64+999+625
   ... 30+ assignments, tracking the pointer perfectly across the screen
```

The compositor was doing everything right, into a plane nothing reads.

### The fix

Inhibit the hardware cursor so the sprite is composited into the framebuffer that
DisplayLink *does* ship:

`/etc/environment.d/90-displaylink-cursor.conf`
```
MUTTER_DEBUG_DISABLE_HW_CURSORS=1
```

and, belt-and-braces, a drop-in on the unit itself so it applies even if `environment.d`
isn't re-read — `/etc/systemd/user/org.gnome.Shell@wayland.service.d/10-cursor.conf`:
```ini
[Service]
Environment=MUTTER_DEBUG_DISABLE_HW_CURSORS=1
```

Then restart the display manager (or reboot).

> **The variable is `MUTTER_DEBUG_DISABLE_HW_CURSORS` — plural `CURSORS`.** The singular
> form is wrong and silently does nothing. Verify against your own build rather than
> trusting any writeup, including this one:
> `strings /usr/lib/*/libmutter-*.so.0 | grep -i HW_CURSOR`

**It is self-verifying.** Mutter logs an unconditional line at startup — no debug topic
needed — which makes a permanent canary:

```bash
journalctl -b _COMM=gnome-shell | grep -i "Disabling hardware cursors"
# -> "Disabling hardware cursors because MUTTER_DEBUG_DISABLE_HW_CURSORS is set"
```

A/B on cursor-plane commits under identical injected pointer motion:

| state | `Assigning cursor plane` events |
|---|---|
| broken (hw cursor active) | **30+** |
| fixed (inhibited) | **0** |

Zero means the sprite is no longer routed to the dead plane.

### Caveats

- **Expect a soft/laggy pointer.** A software cursor means a full-frame CPU copy to the dock
  on every pointer move, on a box with no accelerated GPU↔GPU sharing. That is inherent to
  this hardware combination, not to the fix — and the broken state was already paying the
  identical cost while drawing nothing.
- `MUTTER_DEBUG_DISABLE_HW_CURSORS` is nominally a debug knob. It is the accepted workaround
  for DisplayLink under mutter, but it is not a stability contract; a future mutter could
  rename it. The journal line above is the canary.
- **The real upstream bug is evdi advertising a Cursor plane it cannot transport.** If evdi
  simply did not expose plane 35, the compositor would have chosen a software cursor
  automatically and none of this would happen.
- `wlroots` compositors have their own equivalent: `WLR_NO_HARDWARE_CURSORS=1`.

### Measurements that look convincing and are not

Recorded so nobody repeats them:

- **`DisplayLinkManager` CPU rises ~30× on pointer motion in *both* states** (broken and
  fixed). It does not discriminate: in the broken state each cursor-plane commit still wakes
  evdi, so the daemon re-reads and re-transmits the primary plane — full frame cost,
  cursor-free result. Pure wasted work.
- `/proc/<pid>/io` `wchar` on DisplayLinkManager is meaningless here; it pushes frames via
  USB ioctls, not `write()`.
- `org.gnome.Shell.Screenshot` is **AccessDenied** on GNOME 46+ (restricted to the shell and
  the portal), so screenshot-based verification isn't available.

### On the lighter alternatives

A `cage`/wlroots kiosk fails on this box with `Failed to create allocator`, which has the
same root cause as the mutter EGL warning above: no working GBM/EGL config pairing between
the render node and evdi. Plain fbcon is a non-starter because, as shown at the top, there
is no fbdev to render to. If you want either of those, that EGL/GBM pairing is the thing to
solve first.
