#!/usr/bin/env python3
"""Ardor Gaming Ulta (Compx 25a7:fa7c) — config memory dump.

Wakes a sleeping mouse: keeps sending the session-open packet while also
watching the mouse's regular input interface (hidraw4); the first ACK is
usually what proves the mouse is awake.
"""
import os, select, sys, time

CK_TARGET = 0x55
def cksum(data): return (CK_TARGET - sum(data)) & 0xFF

def ckpack(payload):
    p = list(payload) + [0x00] * (16 - len(payload))
    return bytes(p + [cksum(p)])

def build_write(addr, data):
    p = [0x08, 0x07, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, len(data)] + [b & 0xFF for b in data]
    return ckpack(p + [0x00] * (16 - len(p)))

def build_read(addr, length=10):
    p = [0x08, 0x08, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, length] + [0x00] * 10
    return ckpack(p)

def build_commit():
    return ckpack([0x08, 0x04])

SESSION = [
    build_commit ,           # poke first: close a stray session if any
    ckpack([0x08, 0x03]),
    ckpack([0x08, 0x01, 0x00, 0x00, 0x00, 0x04, 0x34, 0x2e, 0x4c, 0x57]),
    ckpack([0x08, 0x08, 0x00, 0x00, 0x04, 0x02]),
    ckpack([0x08, 0x02, 0x00, 0x00, 0x00, 0x01, 0x01]),
]

CFG = '/dev/hidraw5'
MSE = '/dev/hidraw4'
fd  = os.open(CFG, os.O_RDWR | os.O_NONBLOCK)

def drain(seconds=0.25, list_fd_only=False):
    got = []
    end = time.monotonic() + seconds
    base_fd = fd
    while True:
        left = end - time.monotonic()
        if left <= 0: break
        r, *_ = select.select([base_fd], [], [], left)
        if not r: break
        try: d = os.read(base_fd, 64)
        except BlockingIOError: continue
        got.append(d)
        end = max(end, time.monotonic() + 0.15)
    return got

# --- wait for the mouse to wake: any input report on the mouse interface ----
def wait_mouse_awake(timeout=125.0):
    mse = None
    try: mse = os.open(MSE, os.O_RDONLY | os.O_NONBLOCK)
    except OSError: return False
    print("waiting for mouse input (MOVE THE MOUSE)...", flush=True)
    end = time.monotonic() + timeout
    ok = False
    while time.monotonic() < end:
        r, *_ = select.select([mse], [], [], 0.3)
        if r:
            os.read(mse, 64)
            ok = True
            break
    os.close(mse)
    return ok

def send(packet, label, retries=40, delay=0.25):
    end_total = time.monotonic() + retries * delay
    while time.monotonic() < end_total:
        os.write(fd, packet)
        got = drain(delay)
        for r in got:
            if len(r) >= 17 and r[0] == 0x09 and r[1] == packet[1] and r[3:5] == packet[3:5]:
                if time.monotonic() > end_total - retries * delay + delay:
                    print(f"    [{label}] ack (after retries)")
                return r
    print(f"  [{label}] no ack", flush=True)
    return None

def wake_and_handshake():
    wait_mouse_awake()
    for i, s in enumerate(SESSION):
        pkt = s if callable(s) else s
        label = f"poke{i}" if callable(s) else f"sess{i-1}"
        if callable(s) and s is build_commit:
            r = send(s(), label + " (not required)")
            continue
        if not send(s, label, retries=20):
            return False
    return True

if not wake_and_handshake():
    print("handshake failed", file=sys.stderr); sys.exit(1)
print("== session open, dumping config ==", flush=True)

image = {}
pending = list(range(0x0000, 0x0100, 0x0a))
for a in pending:
    pkt = build_read(a, 10)
    r = send(pkt, f"read {a:#06x}", retries=15)
    if r:
        if (0x4C - sum(r[1:16])) & 0xFF != r[16]:
            print(f"    [read {a:#06x}] BAD reply cksum: {r.hex()}")
            image[a] = None
        else:
            image[a] = list(r[6:16])
    else:
        print(f"    {a:#06x}: failed -> reopening session")
        if wake_and_handshake():
            r = send(pkt, f"read {a:#06x} retry", retries=15)
            image[a] = list(r[6:16]) if r else None
        else:
            image[a] = None

os.close(fd)

print("\n=== config image ===")
bad = 0
line = []
for i in range(0x0000, 0x0100):
    blk = (i // 0x0a) * 0x0a
    off = i % 0x0a
    v = image.get(blk)
    if v is None or off >= 10:
        line.append("??"); bad += 1
    else:
        line.append(f"{v[off]:02x}")
    if i % 8 == 7:
        print(f"{i-7:04x} | " + " ".join(line)); line = []
if line: print(f"{(len(line))}: " + " ".join(line))
print(f"\nunread/failed bytes: {bad}")
