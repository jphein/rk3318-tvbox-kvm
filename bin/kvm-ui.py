#!/usr/bin/env python3
"""
kvm-ui -- thin web KVM shim for the rk3318 box.

Joins the two halves that already exist:
  * video : ustreamer MJPEG on :8080 (kvm-stream.service) -- referenced directly
  * input : USB HID gadget /dev/hidg0 + /dev/hidg1 (hid-gadget.service)

Serves ONE page on :8081 with a WebSocket at /ws carrying input events.
Python 3 stdlib only -- no websockets package, no pip, no kvmd.

kvmd remains the upgrade path; this is deliberately a shim.
"""
import base64, errno, hashlib, json, os, socket, struct, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8081
STREAM_PORT = 8080
KBD, PTR = "/dev/hidg0", "/dev/hidg1"
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ---------------------------------------------------------------- HID layer
# Ported from hid-send (not shelled out -- one write() per event, no fork).
MODS = {0xE0: 0x01, 0xE1: 0x02, 0xE2: 0x04, 0xE3: 0x08,
        0xE4: 0x10, 0xE5: 0x20, 0xE6: 0x40, 0xE7: 0x80}


class Hid:
    """Serialises access to the two hidg endpoints and tracks host presence."""

    def __init__(self):
        self.lock = threading.Lock()
        self.host = True          # optimistic; first failed write corrects it
        self.keys = []            # live pressed non-modifier usages (max 6)
        self.mods = 0
        self.x = self.y = 16384
        self.buttons = 0

    # Expected "nobody is listening" errnos. ESHUTDOWN/EAGAIN/EPIPE = gadget is
    # bound but no host enumerated it. ENOENT/ENXIO = hid-gadget.service isn't
    # up yet (or was stopped). All are normal states, not faults -- they must
    # drive the UI banner, never spam the journal at event rate.
    QUIET = (errno.EAGAIN, errno.EPIPE, errno.ESHUTDOWN, errno.ENODEV,
             errno.EINVAL, errno.ENOENT, errno.ENXIO)

    def _write(self, path, data):
        """Returns True if the target host accepted the report."""
        fd = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, data)
            self._moaned = False
            return True
        except OSError as e:
            if e.errno not in self.QUIET and not getattr(self, "_moaned", False):
                # log genuinely unexpected errors ONCE, not once per keystroke
                print(f"kvm-ui: {path}: {e}", file=sys.stderr)
                self._moaned = True
            return False
        finally:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass

    # -- keyboard ---------------------------------------------------------
    def key(self, usage, down):
        with self.lock:
            if usage in MODS:
                if down: self.mods |= MODS[usage]
                else:    self.mods &= ~MODS[usage]
            else:
                if down:
                    if usage not in self.keys and len(self.keys) < 6:
                        self.keys.append(usage)
                elif usage in self.keys:
                    self.keys.remove(usage)
            # Report the true live state: holding a key keeps it in the report,
            # so the TARGET's own auto-repeat drives repetition. That is what
            # real keyboards do -- we must not synthesise repeats ourselves.
            k = self.keys + [0] * (6 - len(self.keys))
            return self._send(KBD, bytes([self.mods & 0xFF, 0] + k))

    def chord(self, usages, mods):
        """One-shot combo (e.g. ctrl+alt+del) that ignores live state."""
        with self.lock:
            k = list(usages)[:6] + [0] * (6 - len(usages[:6]))
            ok = self._send(KBD, bytes([mods & 0xFF, 0] + k))
            self._send(KBD, bytes(8))          # all-up
            return ok

    def release_all(self):
        with self.lock:
            self.keys, self.mods, self.buttons = [], 0, 0
            self._send(KBD, bytes(8))
            self._send(PTR, self._ptr())

    # -- pointer ----------------------------------------------------------
    def _ptr(self, wheel=0):
        return bytes([self.buttons & 0x1F,
                      self.x & 0xFF, (self.x >> 8) & 0xFF,
                      self.y & 0xFF, (self.y >> 8) & 0xFF,
                      wheel & 0xFF])

    def move(self, x, y):
        with self.lock:
            self.x = max(0, min(32767, int(x)))
            self.y = max(0, min(32767, int(y)))
            return self._send(PTR, self._ptr())

    def button(self, mask, down):
        with self.lock:
            if down: self.buttons |= mask
            else:    self.buttons &= ~mask
            return self._send(PTR, self._ptr())

    def wheel(self, d):
        with self.lock:
            d = max(-127, min(127, int(d)))
            ok = self._send(PTR, self._ptr(d))
            self._send(PTR, self._ptr(0))
            return ok

    def _send(self, path, data):
        ok = self._write(path, data)
        self.host = ok
        return ok


HID = Hid()

# ---------------------------------------------------------------- WebSocket
def ws_frame(payload: bytes, opcode=0x1) -> bytes:
    """Server->client frame (never masked)."""
    n = len(payload)
    h = bytes([0x80 | opcode])
    if n < 126:      h += bytes([n])
    elif n < 65536:  h += b"\x7e" + struct.pack(">H", n)
    else:            h += b"\x7f" + struct.pack(">Q", n)
    return h + payload


def ws_read(rfile):
    """Yield (opcode, payload) from a client. Client frames are always masked.

    Reads through the handler's BUFFERED rfile, not the raw socket: the HTTP
    layer may already have pulled the first WS frame into that buffer, and a
    raw recv() would silently skip it.
    """
    def need(n):
        d = rfile.read(n)
        if not d or len(d) < n:
            raise ConnectionError
        return d

    while True:
        b0, b1 = need(2)
        opcode, masked, ln = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if ln == 126:   ln = struct.unpack(">H", need(2))[0]
        elif ln == 127: ln = struct.unpack(">Q", need(8))[0]
        if ln > 1 << 20:
            raise ConnectionError("frame too large")
        key = need(4) if masked else b"\0\0\0\0"
        data = bytearray(need(ln))
        for i in range(ln):
            data[i] ^= key[i % 4]
        yield opcode, bytes(data)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kvm-ui/1.0"

    def log_message(self, *a):
        pass                                   # keep the journal quiet

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws":
            return self._ws()
        if path in ("/", "/index.html"):
            body = PAGE.replace("{{STREAM_PORT}}", str(STREAM_PORT)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    # GET-only by design: no POST/PUT handlers exist at all.

    def _ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self.send_error(400, "not a websocket request")
        acc = base64.b64encode(
            hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.close_connection = True   # we own the socket from here on
        sock = self.connection
        sock.settimeout(None)

        def push(attached):
            sock.sendall(ws_frame(json.dumps(
                {"t": "host", "attached": attached}).encode()))

        # Probe once so the banner is correct on page load rather than after
        # the first keystroke. An all-keys-up report is a no-op on the target.
        HID.release_all()
        last_host = HID.host
        try:
            push(last_host)
            for opcode, data in ws_read(self.rfile):
                if opcode == 0x8:                       # close
                    break
                if opcode == 0x9:                       # ping -> pong
                    sock.sendall(ws_frame(data, 0xA)); continue
                if opcode != 0x1:
                    continue
                try:
                    self._event(json.loads(data))
                except Exception as e:
                    print(f"kvm-ui: bad event: {e}", file=sys.stderr)
                if HID.host != last_host:                # push state changes
                    last_host = HID.host
                    push(last_host)
        except (ConnectionError, OSError):
            pass
        finally:
            HID.release_all()      # never leave keys stuck down on disconnect

    @staticmethod
    def _event(m):
        t = m.get("t")
        if t == "k":     HID.key(int(m["u"]), bool(m["d"]))
        elif t == "m":   HID.move(m["x"], m["y"])
        elif t == "b":   HID.button(int(m["m"]), bool(m["d"]))
        elif t == "w":   HID.wheel(m["d"])
        elif t == "cad": HID.chord([0x4C], 0x01 | 0x04)   # ctrl+alt+del
        elif t == "rel": HID.release_all()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>rk3318 KVM</title>
<style>
:root{--bg:#101216;--fg:#e6e8ec;--dim:#8a90a0;--acc:#4a9eff;--warn:#f5a623}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:8px 12px;background:#181b21;
       border-bottom:1px solid #262a33;position:sticky;top:0;z-index:2;flex-wrap:wrap}
h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.02em}
button{background:#22262f;color:var(--fg);border:1px solid #333844;border-radius:5px;
       padding:5px 10px;cursor:pointer;font:inherit}
button:hover{border-color:var(--acc)}
#wrap{position:relative;width:100%;max-width:1920px;margin:0 auto}
#v{display:block;width:100%;background:#000;cursor:crosshair}
#banner{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
        background:rgba(16,18,22,.82);color:var(--warn);font-size:16px;text-align:center;padding:20px}
#banner.on{display:flex}
.pill{font-size:12px;color:var(--dim);border:1px solid #2c313b;border-radius:99px;padding:3px 9px}
.pill.live{color:#5bd68a;border-color:#2c5a3e}
footer{padding:10px 12px;color:var(--dim);font-size:12px;border-top:1px solid #262a33}
kbd{background:#22262f;border:1px solid #333844;border-radius:3px;padding:0 4px;font-size:11px}
@media (prefers-color-scheme: light){
 :root{--bg:#f6f7f9;--fg:#14171c;--dim:#5c6270;--acc:#0b66d0}
 header{background:#fff;border-color:#dfe3ea} button{background:#fff;border-color:#cfd5df}
 footer{border-color:#dfe3ea} #banner{background:rgba(246,247,249,.86)} kbd{background:#eef1f5;border-color:#cfd5df}
}
</style></head><body>
<header>
  <h1>rk3318 KVM</h1>
  <span class="pill" id="ws">connecting</span>
  <span class="pill" id="grab">click image to capture keyboard</span>
  <button id="cad">Ctrl+Alt+Del</button>
  <button id="rel">Release all keys</button>
</header>
<div id="wrap">
  <img id="v" src="" alt="capture">
  <div id="banner"><div><b>No target attached.</b><br>
    Cable the gadget port (the dwc2 socket, see docs/01-hardware.md) to a PC.<br>
    Video may still be live &mdash; only input needs the USB link.</div></div>
</div>
<footer>
  Input goes to <code>/dev/hidg0</code> (keyboard) and <code>/dev/hidg1</code>
  (absolute pointer). Pointer is absolute &mdash; no acceleration drift.
  <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd> then click elsewhere to drop capture.
  <br><b>No authentication</b> &mdash; this page trusts the network. Keep it on trusted VLANs only.
</footer>
<script>
const V=document.getElementById('v'), WSP=document.getElementById('ws'),
      GRAB=document.getElementById('grab'), BAN=document.getElementById('banner');
V.src = location.protocol+'//'+location.hostname+':{{STREAM_PORT}}/stream';

let ws, captured=false, sendQ=null;
function connect(){
  ws=new WebSocket('ws://'+location.host+'/ws');
  ws.onopen =()=>{WSP.textContent='connected';WSP.classList.add('live');};
  ws.onclose=()=>{WSP.textContent='disconnected';WSP.classList.remove('live');
                  setTimeout(connect,1500);};
  ws.onmessage=e=>{const m=JSON.parse(e.data);
    if(m.t==='host') BAN.classList.toggle('on', !m.attached);};
}
connect();
const S=o=>{ if(ws&&ws.readyState===1) ws.send(JSON.stringify(o)); };

// --- browser event.code -> HID usage (US layout, physical keys) ------------
const M={Escape:0x29,Backspace:0x2a,Tab:0x2b,Space:0x2c,Minus:0x2d,Equal:0x2e,
 BracketLeft:0x2f,BracketRight:0x30,Backslash:0x31,Semicolon:0x33,Quote:0x34,
 Backquote:0x35,Comma:0x36,Period:0x37,Slash:0x38,CapsLock:0x39,Enter:0x28,
 PrintScreen:0x46,ScrollLock:0x47,Pause:0x48,Insert:0x49,Home:0x4a,PageUp:0x4b,
 Delete:0x4c,End:0x4d,PageDown:0x4e,ArrowRight:0x4f,ArrowLeft:0x50,ArrowDown:0x51,
 ArrowUp:0x52,NumLock:0x53,NumpadDivide:0x54,NumpadMultiply:0x55,NumpadSubtract:0x56,
 NumpadAdd:0x57,NumpadEnter:0x58,NumpadDecimal:0x63,ContextMenu:0x65,
 ControlLeft:0xE0,ShiftLeft:0xE1,AltLeft:0xE2,MetaLeft:0xE3,
 ControlRight:0xE4,ShiftRight:0xE5,AltRight:0xE6,MetaRight:0xE7};
for(let i=0;i<26;i++) M['Key'+String.fromCharCode(65+i)]=0x04+i;
for(let i=1;i<=9;i++)  M['Digit'+i]=0x1d+i;      M.Digit0=0x27;
for(let i=1;i<=12;i++) M['F'+i]=0x39+i;
for(let i=1;i<=9;i++)  M['Numpad'+i]=0x58+i;     M.Numpad0=0x62;

addEventListener('keydown',e=>{ if(!captured) return;
  const u=M[e.code]; if(u===undefined) return;
  e.preventDefault(); if(e.repeat) return;   // target OS does the repeating
  S({t:'k',u:u,d:1}); },{capture:true});
addEventListener('keyup',e=>{ if(!captured) return;
  const u=M[e.code]; if(u===undefined) return;
  e.preventDefault(); S({t:'k',u:u,d:0}); },{capture:true});

// --- pointer: absolute, throttled to ~60/s -------------------------------
let pend=null, timer=null;
V.addEventListener('mousemove',e=>{
  const r=V.getBoundingClientRect(); if(!r.width||!r.height) return;
  pend=[Math.round((e.clientX-r.left)/r.width*32767),
        Math.round((e.clientY-r.top )/r.height*32767)];
  if(!timer) timer=setTimeout(()=>{timer=null;
    if(pend){S({t:'m',x:pend[0],y:pend[1]});pend=null;}},16);
});
const BM={0:1,1:4,2:2};   // browser button -> HID bit (left,middle,right)
V.addEventListener('mousedown',e=>{e.preventDefault();
  if(!captured){captured=true;GRAB.textContent='keyboard captured';GRAB.classList.add('live');}
  S({t:'b',m:BM[e.button]||1,d:1});});
addEventListener('mouseup',e=>{ if(BM[e.button]!==undefined) S({t:'b',m:BM[e.button],d:0});});
V.addEventListener('contextmenu',e=>e.preventDefault());
V.addEventListener('wheel',e=>{e.preventDefault();
  S({t:'w',d: e.deltaY>0?-1:1});},{passive:false});

document.getElementById('cad').onclick=()=>S({t:'cad'});
document.getElementById('rel').onclick=()=>{S({t:'rel'});
  captured=false;GRAB.textContent='click image to capture keyboard';GRAB.classList.remove('live');};
addEventListener('blur',()=>S({t:'rel'}));   // never leave keys stuck
</script></body></html>
"""

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"kvm-ui: listening on 0.0.0.0:{PORT} (stream :{STREAM_PORT})",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
