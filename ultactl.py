#!/usr/bin/env python3
"""ultactl — управление мышью Ardor Gaming Ulta (RGB, DPI, заряд) из Linux.

Запуск без аргументов — интерактивное меню. Примеры:
  ultactl status
  ultactl steady red -b 80
  ultactl stream -l 5
  ultactl dpi set 2 1600
  ultactl poll 500
"""
import argparse, glob, os, re, select, sys, time

VID = 0x25A7
PID_DONGLE = 0xFA7C      # донгл 2.4G
PID_WIRED = 0xFA7B       # мышь по кабелю
CK_TARGET = 0x55

# карта EEPROM (сверена с дампом и проектом ardor-mouse)
A_POLLING = 0x00         # [code][ck]: 1=1000 2=500 4=250 8=125 Гц
A_DPI_ACTIVE = 0x04      # активная ступень (с нуля)
A_LOD = 0x0A             # высота отрыва: 1|2 мм
A_DPI_TABLE = 0x0C       # 8 слотов по 4 байта [code][code][mul|mul<<4][ck]
A_LED = 0xA0             # [mode][R][G][B][speed][bright][ck]
A_LED_OFF_MOVING = 0xB3  # [v][ck]: 1 — подсветка гаснет при движении

EFFECT_HW = {"steady": 2, "breath": 1, "stream": 0, "neon": 3,
             "scroll": 5, "colorbreath": 7, "off": 4}
EFFECT_NAMES = {v: k for k, v in EFFECT_HW.items()}
COLOR_MODES = ("steady", "breath", "scroll")
MODE_LABELS = {"steady": "статичный", "breath": "дыхание", "stream": "поток",
               "neon": "неон (мигание)", "scroll": "бег",
               "colorbreath": "радужное дыхание", "off": "выключена"}
MODES_ORDER = ["steady", "breath", "stream", "neon", "colorbreath", "scroll", "off"]

SPEED_STD = [0xFF, 0xE6, 0xD2, 0xBE, 0xAA, 0x96, 0x82, 0x6E, 0x46, 0x28]
BRI_STD = [0x2D, 0x32, 0x4B, 0x64, 0x7D, 0x96, 0xAF, 0xC8, 0xE1, 0xFF]
SPEED_STREAM = [0x0A, 0x09, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01]
BRI_STREAM = [0x48, 0x5C, 0x70, 0x84, 0x98, 0xAC, 0xC0, 0xD4, 0xE8, 0xFC]

POLL_HZ = {1000: 0x01, 500: 0x02, 250: 0x04, 125: 0x08}
HZ_CODE = {v: k for k, v in POLL_HZ.items()}
LOD_MM = {1: "1 мм", 2: "2 мм"}

NAMED = {"red": 0xFF0000, "green": 0x00FF00, "blue": 0x0000FF, "cyan": 0x00FFFF,
         "magenta": 0xFF00FF, "yellow": 0xFFFF00, "orange": 0xFF7F00,
         "white": 0xFFFFFF, "purple": 0x8000FF, "pink": 0xFF69B4,
         "gold": 0xFFD700, "lime": 0x00FF7F, "ice": 0xADD8E6}


class MouseError(Exception):
    pass


# ------------------------------------------------------------- протокол ----

def cksum(vals):
    return (CK_TARGET - sum(vals)) & 0xFF


def record(vals):
    vals = [v & 0xFF for v in vals]
    return vals + [cksum(vals)]


def ckpack(payload):
    p = list(payload) + [0x00] * (16 - len(payload))
    return bytes(p + [cksum(p)])


def build_write(addr, data):
    p = [0x08, 0x07, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, len(data)]
    p += [b & 0xFF for b in data]
    return ckpack(p)


def build_read(addr, length=10):
    p = [0x08, 0x08, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, length] + [0x00] * 10
    return ckpack(p)


def build_battery():
    return ckpack([0x08, 0x04])


# сессия вендор-утилиты: первый пакет — «тычок» (одновременно заряд-запрос)
SESSION = [
    build_battery(),
    ckpack([0x08, 0x03]),
    ckpack([0x08, 0x01, 0x00, 0x00, 0x00, 0x04, 0x34, 0x2E, 0x4C, 0x57]),
    ckpack([0x08, 0x08, 0x00, 0x00, 0x04, 0x02]),
    ckpack([0x08, 0x02, 0x00, 0x00, 0x00, 0x01, 0x01]),
]


def has_feature_report(desc, rid):
    """Есть ли в report descriptor FEATURE-элемент (0xB1) под Report ID `rid`."""
    i, cur = 0, 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:                       # long item
            i += 3 + desc[i + 1]
            continue
        size = 4 if (prefix & 3) == 3 else prefix & 3
        tag = prefix & 0xFC
        if tag == 0x84:                          # Report ID
            cur = desc[i + 1] if i + 1 < len(desc) else 0
        elif tag == 0xB0 and cur == rid:         # Feature
            return True
        i += 1 + size
    return False


def find_hidraw():
    """Все подходящие config-интерфейсы; кабель имеет приоритет над донглом."""
    cands = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.join(node, "device")
        try:
            with open(os.path.join(dev, "uevent")) as f:
                uevent = f.read()
            with open(os.path.join(dev, "report_descriptor"), "rb") as f:
                desc = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=0003:([0-9a-fA-F]+):([0-9a-fA-F]+)", uevent)
        if not m:
            continue
        vid, pid = int(m.group(1), 16), int(m.group(2), 16)
        if vid != VID or pid not in (PID_DONGLE, PID_WIRED):
            continue
        if not has_feature_report(desc, 0x08):   # config-интерфейс отдельный
            continue
        cands.append((0 if pid == PID_WIRED else 1,
                      "/dev/" + os.path.basename(node), pid, node))
    cands.sort(key=lambda c: c[0])
    if not cands:
        return None
    prio, path, pid, node = cands[0]
    return path, pid, node


def wake_interfaces(node):
    """hidraw-узлы соседних интерфейсов того же USB-устройства (для wake)."""
    dev = os.path.realpath(os.path.join(node, "device")).rsplit(":", 1)[0]
    fds = []
    for sub in sorted(glob.glob(os.path.join(dev, ":1.*", "hidraw", "hidraw*"))):
        name = os.path.basename(sub)
        try:
            fds.append(os.open("/dev/" + name, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    return fds


# ------------------------------------------------------------------ мышь ----

class Mouse:
    def __init__(self, awake_timeout=180.0):
        found = find_hidraw()
        if not found:
            raise MouseError("мышь не найдена — вставьте донгл или подключите кабель")
        self.path, self.pid, self.node = found
        self.link = "кабель (wired)" if self.pid == PID_WIRED else "донгл (2.4G)"
        self.awake_timeout = awake_timeout
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise MouseError(f"не удалось открыть {self.path}: {e}")
        self.image = {}          # кэш блоков EEPROM по 10 байт

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _drain(self, seconds=0.25):
        out = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [],
                                    max(0, end - time.monotonic()))
            if not r:
                break
            try:
                out.append(os.read(self.fd, 64))
            except BlockingIOError:
                continue
        return out

    def send(self, pkt, tries=30, wait=0.25):
        for _ in range(tries):
            try:
                os.write(self.fd, pkt)
            except OSError as e:
                raise MouseError(f"устройство недоступно ({e.strerror or e})")
            for r in self._drain(wait):
                if len(r) >= 17 and r[0] == 0x09 and r[1] == pkt[1] \
                        and r[3:5] == pkt[3:5]:
                    return r
        raise MouseError("устройство не ответило — разбудите мышь движением")

    def _wait_awake(self, prompt=True):
        fds = wake_interfaces(self.node)
        if not fds:
            return True
        if prompt:
            print("  жду пробуждения мыши — подвигайте её или нажмите кнопку",
                  file=sys.stderr, flush=True)
        end = time.monotonic() + self.awake_timeout
        try:
            while time.monotonic() < end:
                r, _, _ = select.select(fds, [], [], 0.25)
                if r:
                    return True
            return False
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def open(self):
        """Разбудить мышь и открыть сессию настройки."""
        if not self._wait_awake():
            raise MouseError("мышь спит — подвигайте ею и повторите")
        last = None
        for i, s in enumerate(SESSION):
            last = self.send(s, tries=12)
        self.image = {}
        return last

    # ---- EEPROM -----------------------------------------------------------
    def read_block(self, addr, length=10):
        r = self.send(build_read(addr, length), tries=12)
        if (0x4C - sum(r[1:16])) & 0xFF != r[16]:
            print("warning: контрольная сумма ответа не сошлась", file=sys.stderr)
        return list(r[6:6 + length])

    def write_block(self, addr, data, label="write"):
        r = self.send(build_write(addr, data), tries=15)
        if (r[2] if len(r) > 2 else 0) != 0:
            raise MouseError(f"{label}: устройство сообщило ошибку записи")
        self.image.pop((addr // 10) * 10, None)

    def mem(self, addr):
        base = (addr // 10) * 10
        if base not in self.image:
            self.image[base] = self.read_block(base)
        blk = self.image[base]
        return blk[addr - base] if blk else None

    def read_profile(self):
        for base in (0x00, 0x0A, 0x0C, 0x10, 0x14,
                     0x1C, 0x20, 0x24, 0x28, 0x2C, 0xA0):
            self.mem(base)

    def battery(self):
        r = self.send(build_battery(), tries=12)
        return int(r[6]), bool(r[7])


# ------------------------------------------------------- параметры / цвет ----

def level_of_pct(pct):
    pct = max(0, min(100, int(pct)))
    return max(1, min(10, (pct + 9) // 10))


def tables_for(mode):
    if mode == "stream":
        return SPEED_STREAM, BRI_STREAM
    return SPEED_STD, BRI_STD


def parse_color(s):
    s = s.strip().lower().lstrip("#")
    if s in NAMED:
        v = NAMED[s]
        return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
    if "," in s:
        try:
            parts = [int(x) for x in s.split(",")]
        except ValueError:
            raise ValueError(f"не разобрал цвет: {s!r}")
        if len(parts) != 3 or any(not 0 <= v <= 255 for v in parts):
            raise ValueError("RGB — три числа 0-255")
        return tuple(parts)
    if len(s) == 6 and all(c in "0123456789abcdef" for c in s):
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    raise ValueError("цвет: имя (red/orange/…), hex (ff8800) или 255,128,0")


# --------------------------------------------------------------- DPI код ----

def dpi_decode(code, mul):
    base = 100 if mul & 2 else 50
    v = base * (code + 1)
    return v * 2 if mul & 1 else v


def dpi_snap(dpi):
    dpi = max(50, min(19000, int(dpi)))
    if dpi < 10050:
        return min(10000, (dpi + 25) // 50 * 50)
    return (dpi + 50) // 100 * 100


def dpi_encode(dpi):
    dpi = dpi_snap(dpi)
    if dpi <= 10000:
        code, mul = dpi // 50 - 1, 0
    elif dpi <= 19000:
        code, mul = dpi // 100 - 1, 2
    else:
        code, mul = dpi // 100 - 1, 1
    return code & 0xFF, mul


def dpi_slot_record(code, mul):
    flags = (mul & 0x0F) | (mul << 4)
    return record([code, code, flags])[:3]      # [c][c][flags][ck]


# ------------------------------------------------------------- применение ----

def led_set(m, mode, rgb=(0, 0, 0), brightness_pct=100, level=None,
            speed_raw=None):
    speeds, bris = tables_for(mode)
    lv = max(1, min(10, int(level))) if level is not None else 6
    speed = speeds[lv - 1] if speed_raw is None else (speed_raw & 0xFF)
    bright = bris[level_of_pct(brightness_pct) - 1]
    r, g, b = rgb
    m.write_block(A_LED, record([EFFECT_HW[mode], r, g, b, speed, bright]),
                  label=f"led {mode}")
    return mode


def polling_set(m, hz):
    code = POLL_HZ.get(int(hz))
    if code is None:
        raise ValueError("частота: 125 / 250 / 500 / 1000 Гц")
    m.write_block(A_POLLING, record([code]), label="polling")
    return int(hz)


def dpi_set_stage(m, stage_idx, dpi):
    if not 1 <= int(stage_idx) <= 8:
        raise ValueError("ступень 1-8")
    code, mul = dpi_encode(dpi)
    m.write_block(A_DPI_TABLE + 4 * (int(stage_idx) - 1),
                  record([code, code, (mul & 0x0F) | (mul << 4)])[:4],
                  label=f"dpi{stage_idx}")


def dpi_set_active(m, stage_idx):
    if not 1 <= int(stage_idx) <= 8:
        raise ValueError("ступень 1-8")
    m.write_block(A_DPI_ACTIVE, record([int(stage_idx) - 1]), label="dpi-active")


def motion_led_get(m):
    """Полная правка: подсветка горит при движении?"""
    return not (m.mem(A_LED_OFF_MOVING) or 0)


def motion_led_set(m, led_on_moving):
    """led_on_moving=True — подсветка горит при движении; False — гаснет."""
    m.write_block(A_LED_OFF_MOVING, record([0 if led_on_moving else 1]),
                  label="motion-led")
    return bool(led_on_moving)


# ------------------------------------------------------------ статус/вывод ----

def status(m, bat=None):
    m.read_profile()
    lines = [f"подключение : {m.link}  ({m.path})",
             "заряд       : "
             + (f"{bat[0]}%" + ("  ⟳ зарядка" if bat[1] else "")
                if bat else "—")]
    mode = EFFECT_NAMES.get(m.mem(A_LED))
    if mode:
        rgb = (m.mem(A_LED + 1) or 0, m.mem(A_LED + 2) or 0, m.mem(A_LED + 3) or 0)
        speed, bright = m.mem(A_LED + 4), m.mem(A_LED + 5)
        speeds, bris = tables_for(mode)
        lv_s = speeds.index(speed) + 1 if speed in speeds else None
        lv_b = bris.index(bright) + 1 if bright in bris else None
        stxt = f"ур. {lv_s}" if lv_s else f"raw {speed}"
        btxt = "100%" if bright == 0xFF else (f"ур. {lv_b}" if lv_b else f"raw {bright}")
        lines.append(f"подсветка   : {MODE_LABELS.get(mode, mode)}"
                     f"  цвет #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                     f"  скорость {stxt}  яркость {btxt}")
    pol = m.mem(A_POLLING)
    if pol is not None:
        lines.append(f"опрос USB   : {HZ_CODE.get(pol, pol)} Гц")
    lod = m.mem(A_LOD)
    if lod in LOD_MM:
        lines.append(f"LOD         : {LOD_MM[lod]}")
    lines.append("движение    : "
                 + ("подсветка горит" if motion_led_get(m)
                    else "подсветка гаснет"))
    act = m.mem(A_DPI_ACTIVE)
    stages = []
    for i in range(8):
        code, mul = m.mem(A_DPI_TABLE + 4 * i), m.mem(A_DPI_TABLE + 4 * i + 2)
        if code is None:
            break
        stages.append(dpi_decode(code, mul & 0x03) if mul is not None else None)
    dsp = [f"[{i + 1}] {v}{(' ←' if act == i else '')}"
           if v is not None else f"[{i + 1}] —" for i, v in enumerate(stages)]
    if dsp:
        lines.append("DPI         : " + "   ".join(dsp))
    return "\n".join(lines)


def run_with_link(fn):
    m = Mouse()
    try:
        m.open()
        return fn(m)
    finally:
        m.close()


# ------------------------------------------------------------------ CLI ----

def list_colors():
    for name, v in sorted(NAMED.items()):
        print(f"{name:8s} #{v:06x}")


def add_mode_args(p, has_color):
    if has_color:
        p.add_argument("color", nargs="?", default=None,
                       help="цвет: имя | hex | R,G,B (по умолчанию текущий)")
    p.add_argument("--brightness", "-b", type=int, default=100, metavar="0-100")
    p.add_argument("--level", "-l", type=int, default=None, metavar="1-10")
    return p


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="ultactl",
        description="Управление подсветкой, DPI и зарядом мыши Ardor Gaming Ulta.",
        epilog="Без аргументов — интерактивное меню. Настройки сохраняются "
               "в памяти мыши.")
    ap.add_argument("--device", metavar="HIDRAW",
                    help="путь hidraw (по умолчанию авто)")
    sub = ap.add_subparsers(dest="cmd", metavar="команда")
    sub.add_parser("status", help="состояние: заряд, подсветка, опрос, DPI")
    sub.add_parser("battery", help="заряд и зарядка")
    sub.add_parser("colors", help="именованные цвета")
    sub.add_parser("ui", help="интерактивное меню")
    add_mode_args(sub.add_parser("steady", help="статичный цвет"), True)
    add_mode_args(sub.add_parser("breath", help="дыхание цветом"), True)
    add_mode_args(sub.add_parser("scroll", help="«бег» цветом"), True)
    add_mode_args(sub.add_parser("stream", help="«поток» (перелив)"), False)
    add_mode_args(sub.add_parser("neon", help="«неон» (мигание)"), False)
    add_mode_args(sub.add_parser("colorbreath", help="радужное дыхание"), False)
    sub.add_parser("off", help="подсветка выключить")
    p = sub.add_parser("dpi", help="таблица DPI")
    pd = p.add_subparsers(dest="sub")
    e = pd.add_parser("set", help="ступень S = DPI")
    e.add_argument("stage", type=int)
    e.add_argument("dpi", type=int)
    e = pd.add_parser("active", help="активная ступень S")
    e.add_argument("stage", type=int)
    p = sub.add_parser("poll", help="частота опроса USB")
    p.add_argument("hz", type=int, choices=sorted(POLL_HZ))
    p = sub.add_parser("motion", help="подсветка при движении мыши")
    p.add_argument("state", nargs="?", choices=("on", "off"),
                   help="on — горит при движении, off — гаснет при движении")
    return ap.parse_args(argv)


def apply_colors(m, args, mode):
    cur = [m.mem(A_LED + 1) or 0, m.mem(A_LED + 2) or 0, m.mem(A_LED + 3) or 0]
    if getattr(args, "color", None):
        cur = list(parse_color(args.color))
    led_set(m, mode, tuple(cur), brightness_pct=args.brightness, level=args.level)


def run_cmd(ns):
    if ns.cmd == "colors":
        list_colors()
        return 0
    if ns.cmd == "status":
        return run_with_link(lambda m: print(status(m, m.battery())))
    if ns.cmd == "battery":
        def battery(m):
            b = m.battery()
            print(f"заряд: {b[0]}%  зарядка: {'да' if b[1] else 'нет'}")
            return 0
        return run_with_link(battery)
    if ns.cmd == "off":
        def off(m):
            led_set(m, "off")
            print("OK: подсветка выключена")
            return 0
        return run_with_link(off)
    if ns.cmd == "dpi":
        if ns.sub == "set":
            def dset(m):
                dpi_set_stage(m, ns.stage, ns.dpi)
                print(f"OK: ступень {ns.stage} = {dpi_snap(ns.dpi)} DPI")
                print(status(m, None))
                return 0
            return run_with_link(dset)
        if ns.sub == "active":
            def dact(m):
                dpi_set_active(m, ns.stage)
                print(f"OK: активная ступень {ns.stage}")
                print(status(m, None))
                return 0
            return run_with_link(dact)

        def dshow(m):
            print(status(m, None))
            return 0
        return run_with_link(dshow)
    if ns.cmd == "poll":
        def poll(m):
            polling_set(m, ns.hz)
            print(f"OK: опрос USB {ns.hz} Гц")
            return 0
        return run_with_link(poll)
    if ns.cmd == "motion":
        def motion(m):
            if ns.state is None:
                cur = motion_led_get(m)
                print("подсветка при движении: "
                      + ("горит" if cur else "гаснет"))
                return 0
            motion_led_set(m, ns.state == "on")
            print("OK: подсветка при движении "
                  + ("горит" if ns.state == "on" else "гаснет"))
            return 0
        return run_with_link(motion)
    if ns.cmd in EFFECT_HW and ns.cmd != "off":
        def fx(m):
            apply_colors(m, ns, ns.cmd)
            print(f"OK: {MODE_LABELS.get(ns.cmd, ns.cmd)}")
            return 0
        return run_with_link(fx)
    # ns.cmd == "ui" или None
    return interactive()


# --------------------------------------------------------------- меню ----

def interactive():
    state = {"m": None}
    bat = [None]

    def close_old():
        old = state["m"]
        if old:
            old.close()
        state["m"] = None

    def reconnect():
        print("  переподключаюсь...")
        close_old()
        try:
            nm = Mouse(awake_timeout=30)
        except MouseError as e:
            print("  ошибка:", e)
            return False
        state["m"] = nm
        try:
            nm.open()
            return True
        except MouseError as e:
            print("  ошибка:", e)
            return False

    def refresh():
        m = state["m"]
        m.image = {}
        bat[0] = m.battery() or None
        print()
        print("=" * 70)
        print(status(m, bat[0]))
        print("=" * 70)

    state["m"] = Mouse()
    try:
        state["m"].open()
        refresh()
        while True:
            print()
            print("  [1] режим   [2] цвет   [3] яркость   [4] скорость")
            print("  [5] DPI     [6] частота опроса    [7] заряд    "
                  "[8] подсветка при движении  [r] обновить  [q] выход")
            try:
                ch = input("ultactl> ").strip().lower()
            except EOFError:
                break
            if ch in ("q", "quit", "exit", "выход"):
                break
            try:
                m = state["m"]
                if not ch or ch == "r":
                    refresh()
                elif ch in ("1", "режим"):
                    for i, n in enumerate(MODES_ORDER, 1):
                        print(f"  {i}) {MODE_LABELS[n]} ({n})")
                    name = input("  режим: ").strip().lower()
                    if not name:
                        continue
                    if name.isdigit() and 1 <= int(name) <= len(MODES_ORDER):
                        name = MODES_ORDER[int(name) - 1]
                    if name not in EFFECT_HW:
                        print("  нет такого режима")
                        continue
                    rgb = (0, 0, 0)
                    if name in COLOR_MODES:
                        cur = (m.mem(A_LED + 1) or 0, m.mem(A_LED + 2) or 0,
                               m.mem(A_LED + 3) or 0)
                        c = input(f"  цвет [{f'#{cur[0]:02x}{cur[1]:02x}{cur[2]:02x}'}]: ").strip()
                        rgb = parse_color(c) if c else cur
                    led_set(m, name, rgb)
                    refresh()
                elif ch in ("2", "цвет"):
                    c = input("  цвет (имя/hex/R,G,B): ").strip()
                    if c:
                        mode = EFFECT_NAMES.get(m.mem(A_LED), "steady")
                        if mode not in COLOR_MODES:
                            mode = "steady"
                        led_set(m, mode, parse_color(c))
                        refresh()
                elif ch in ("3", "яркость"):
                    pct = int(input("  яркость 0-100: ").strip())
                    mode = EFFECT_NAMES.get(m.mem(A_LED), "steady")
                    rgb = (m.mem(A_LED + 1) or 0, m.mem(A_LED + 2) or 0,
                           m.mem(A_LED + 3) or 0)
                    led_set(m, mode, rgb, brightness_pct=pct)
                    refresh()
                elif ch in ("4", "скорость"):
                    lv = int(input("  скорость 1-10 (1 — медленно): ").strip())
                    mode = EFFECT_NAMES.get(m.mem(A_LED), "steady")
                    rgb = (m.mem(A_LED + 1) or 0, m.mem(A_LED + 2) or 0,
                           m.mem(A_LED + 3) or 0)
                    led_set(m, mode, rgb, level=lv)
                    refresh()
                elif ch in ("5", "dpi"):
                    print("  формат: «2 1600» — ступень 2 → 1600 DPI; "
                          "«a 1» — активная ступень 1")
                    cmd = input("  dpi> ").strip().lower()
                    if not cmd:
                        continue
                    if cmd.startswith("a"):
                        dpi_set_active(m, int(cmd.split()[1]))
                    else:
                        s, d = cmd.split()
                        dpi_set_stage(m, int(s), int(d))
                    m.image = {}
                    refresh()
                elif ch in ("6", "poll"):
                    hz = input("  частота 1000/500/250/125: ").strip()
                    if hz:
                        polling_set(m, hz)
                        m.image = {}
                    refresh()
                elif ch in ("7", "заряд"):
                    bat[0] = m.battery()
                    print(f"  заряд: {bat[0][0]}%  зарядка: "
                          f"{'да' if bat[0][1] else 'нет'}")
                elif ch in ("8", "motion", "движение"):
                    cur = motion_led_get(m)
                    print(f"  сейчас: подсветка при движении "
                          f"{'горит' if cur else 'гаснет'}")
                    what = input("  [on = горит / off = гаснет; "
                                 "enter = отмена]: ").strip().lower()
                    if what in ("on", "off"):
                        motion_led_set(m, what == "on")
                        refresh()
            except (ValueError, IndexError) as e:
                print("  ошибка:", e)
            except MouseError as e:
                print("  ошибка связи:", e)
                if reconnect():
                    refresh()
            except OSError as e:
                print("  ошибка устройства:", e)
                if reconnect():
                    refresh()
    finally:
        close_old()
    print("выход.")
    return 0


def main(argv=None):
    ns = parse_args(argv)
    try:
        return run_cmd(ns)
    except (MouseError, ValueError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
