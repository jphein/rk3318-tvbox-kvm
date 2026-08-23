# 03 — HDMI capture (MS2109 + ustreamer)

The video half. This is the easy half — the MS2109 is a UVC device, so the kernel already
has a driver and nothing needs building.

## Hardware

An **MS2109** HDMI→USB capture stick (`534d:2109`, "MacroSilicon"). ~$10, sold under a
hundred names. Known characteristics, so you're not surprised:

- **1080p at 30 fps max**, and 1080p is often *interpolated* — many of these sticks capture
  720p internally. Fine for a KVM, not fine for video work.
- Presents MJPEG **and** raw YUYV. Use MJPEG: the stick compresses in hardware, so
  ustreamer just forwards frames instead of the box's weak CPU encoding them.
- It also exposes an **audio** interface (it appears as a sound card). Unused here.

Plug it into the **xHCI** socket, not dwc2 — see [01-hardware.md](01-hardware.md). It
should appear as a `/dev/videoN` node:

```bash
ssh root@<box-ip> 'v4l2-ctl --list-devices'
ssh root@<box-ip> 'ls -l /dev/v4l/by-id/'
```

## Install ustreamer

[ustreamer](https://github.com/pikvm/ustreamer) is the MJPEG server from the PiKVM project.
It may be packaged for your distro; otherwise it builds in a minute with no exotic deps.

```bash
ssh root@<box-ip> 'apt-get install -y ustreamer || {
    apt-get install -y build-essential libevent-dev libjpeg-dev libbsd-dev libudev-dev &&
    git clone --depth1 https://github.com/pikvm/ustreamer /tmp/ustreamer &&
    make -C /tmp/ustreamer -j$(nproc) && install -m0755 /tmp/ustreamer/ustreamer /usr/bin/
}'
```

## The unit

[`systemd/kvm-stream.service`](../systemd/kvm-stream.service):

```ini
ExecStart=/usr/bin/ustreamer \
  --device=/dev/v4l/by-id/usb-MACROSILICON_USB_Video-video-index0 \
  --format=mjpeg \
  --resolution=1920x1080 \
  --host=0.0.0.0 \
  --port=8080 \
  --drop-same-frames=30
```

Why each flag matters:

- **`--device=/dev/v4l/by-id/…`** rather than `/dev/video0`. The box has *six* `/dev/videoN`
  nodes (the Rockchip VPU, RGA and rkvdec all claim some), so the capture stick's index is
  not stable — and applying the OTG overlay renumbers the USB buses, which can shuffle it
  again. The `by-id` path is stable across both. Check yours with
  `ls /dev/v4l/by-id/`; the exact string varies with the stick's reported name.
- **`--format=mjpeg`** — hardware-compressed frames pass straight through. Choosing YUYV
  here would make the box's CPU do the JPEG encoding and it does not have the headroom.
- **`--drop-same-frames=30`** — a KVM view is mostly static. This stops retransmitting
  identical frames, up to 30 in a row, which cuts idle bandwidth and CPU dramatically.
- **`--host=0.0.0.0`** — see the security warning in the [README](../README.md). This is
  wide open, exactly like the UI.

```bash
ssh root@<box-ip> 'systemctl daemon-reload && systemctl enable --now kvm-stream'
ssh root@<box-ip> 'systemctl is-active kvm-stream; ss -lntp | grep 8080'
```

Then `http://<box-ip>:8080/stream` is a raw `multipart/x-mixed-replace` MJPEG stream, and
`http://<box-ip>:8080/` is ustreamer's own built-in page. `kvm-ui.py` references
`:8080/stream` **directly** in an `<img>` rather than proxying it — same host, so no CORS
problem, and zero added latency or extra copies through Python.

## Troubleshooting

**No `/dev/video*` for the stick:** it is probably on the dwc2 port after you applied the
OTG overlay, where nothing enumerates any more. Move it to the xHCI socket.

**Black or frozen stream:** the MS2109 does not like resolution changes on the fly. Restart
`kvm-stream` after the target changes mode. Also check the target is actually outputting —
many machines drop HDMI output entirely when no *display* EDID is detected, and some MS2109
clones present a poor EDID. An HDMI EDID emulator ("dummy plug") fixes that class of
problem, and is the usual fix for "works on my monitor, black on the capture stick".

**Verify without a browser:**

```bash
curl -sI http://<box-ip>:8080/stream | head -3     # want: 200, multipart/x-mixed-replace
```
