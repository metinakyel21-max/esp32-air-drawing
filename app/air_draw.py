"""Air Drawing 3D - draw in the air with an ESP32-S3 + MPU6050 and turn the sketch into a 3D solid.

How it works
  * The board streams one line per sample over USB serial:  AX:..,AY:..,AZ:..,GX:..,GY:..,GZ:..
  * Tilting the board moves the pen. Tilt is measured from gravity (accelerometer), which is an
    absolute reference - so unlike gyro integration the cursor never drifts away.
  * An MPU6050 cannot measure position in space (double-integrating acceleration drifts by metres
    within seconds), so depth comes from the W/S keys, and E extrudes a finished sketch into a
    shaded, auto-rotating 3D solid.

Keys
  SPACE pen up/down | W/S depth | E 3D solid | mouse drag / arrows rotate view | R reset view
  C clear | Ctrl+S save PNG + JSON | O open last save | K recalibrate | X/Y invert | D swap axes
  +/- sensitivity | 1-5 colour | ESC quit

Usage:  python air_draw.py            (auto-detects the board)
        python air_draw.py --port COM5
"""
import argparse
import glob
import json
import locale
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk

import serial
import serial.tools.list_ports
from PIL import Image, ImageDraw

W, H = 1000, 650
COLORS = ["#111111", "#d62828", "#1d4ed8", "#15803d", "#f59e0b"]
HALF_X = 1.5                       # world: X -1.5..1.5, Y -1..1 (down +), Z -1..1 (far +)
SCALE, EYE = 150.0, 5.0            # screen scale and perspective distance
DEPTH_SPEED = 0.5                  # world units per second while W/S is held
THICKNESS = 0.18                   # half thickness of the extruded solid
CALIB_SAMPLES = 150                # ~1.7 s at 90 Hz
SMOOTHING = 0.06                   # exponential smoothing factor for tilt (lower = calmer)

# ----------------------------------------------------------------- UI text (EN / TR)
TEXT = {
    "en": {
        "title": "Air Drawing 3D", "calib": "Calibrating: hold the board STILL for 2 seconds...",
        "ready": "Ready. Tilt = move · W/S = depth · SPACE = pen · drag to rotate",
        "solid": "3D SOLID: auto-rotating · E = back to sketch · Ctrl+S save", "sketch": "Sketch mode",
        "saved": "Saved: Desktop/{name}.png (+ .json 3D data)",
        "no_save": "No saved drawing on the Desktop (press Ctrl+S first)",
        "opened": "Opened: {name} · E = 3D solid", "lost": "Board disconnected! Check the cable and restart.",
        "busy": "Could not open the port: another program (an old window?) is using the board.",
        "pen": "Pen", "down": "DOWN", "up": "up", "depth": "Depth", "edge": "Tilt to edge",
        "help": "E 3D solid · O open · W/S depth · drag/arrows rotate · R reset · C clear · Ctrl+S save"
                " · K calibrate · X/Y invert · D swap · +/- speed · 1-5 colour",
        "not_found": "No ESP32 + MPU6050 board found.\nPlug in the USB cable and try again\n"
                     "(or pass --port COMx).",
    },
    "tr": {
        "title": "Havada Çizim 3B", "calib": "Kalibrasyon: kartı 2 saniye SABİT tutun...",
        "ready": "Hazır. Eğ = hareket · W/S = derinlik · BOŞLUK = kalem · sürükle = çevir",
        "solid": "3B CİSİM: kendiliğinden dönüyor · E = çizime dön · Ctrl+S kaydet", "sketch": "Çizim modu",
        "saved": "Kaydedildi: Masaüstü/{name}.png (+ .json 3B veri)",
        "no_save": "Masaüstünde kayıtlı çizim yok (önce Ctrl+S ile kaydedin)",
        "opened": "Açıldı: {name} · E = 3B cisim", "lost": "Kart bağlantısı koptu! Kabloyu kontrol edip yeniden açın.",
        "busy": "Port açılamadı: başka bir program (eski pencere?) kartı kullanıyor.",
        "pen": "Kalem", "down": "AŞAĞI", "up": "yukarı", "depth": "Derinlik", "edge": "Kenara eğim",
        "help": "E 3B cisim · O son kaydı aç · W/S derinlik · sürükle/oklar çevir · R düz bakış · C temizle"
                " · Ctrl+S kaydet · K kalibre · X/Y ters · D takas · +/- hız · 1-5 renk",
        "not_found": "MPU6050'li ESP32 kartı bulunamadı.\nUSB kablosunu takıp tekrar deneyin\n"
                     "(ya da --port COMx verin).",
    },
}


def pick_language():
    for value in (os.environ.get("AIR_DRAW_LANG"), locale.getlocale()[0], os.environ.get("LANG")):
        if value and value.lower().startswith(("tr", "turkish")):
            return "tr"
    return "en"


T = TEXT[pick_language()]


# ----------------------------------------------------------------- math helpers
def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def rotate(p, yaw, pitch):
    x, y, z = p
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    x1, z1 = x * cy - z * sy, x * sy + z * cy
    return x1, y * cp - z1 * sp, y * sp + z1 * cp


def project(p, yaw, pitch):
    """World point -> (screen_x, screen_y, nearness k). Larger k = closer to the eye."""
    x, y, z = rotate(p, yaw, pitch)
    k = EYE / max(0.5, EYE + z)
    return W / 2 + x * k * SCALE, H / 2 + y * k * SCALE, k


def shade(color, f):
    """Darken (f<1) or lighten (f>1) a '#rrggbb' colour."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    if f <= 1:
        r, g, b = r * f, g * f, b * f
    else:
        t = min(1.0, f - 1)
        r, g, b = r + (255 - r) * t, g + (255 - g) * t, b + (255 - b) * t
    return "#%02x%02x%02x" % (int(r), int(g), int(b))


def simplify(pts, step=0.03, max_pts=250):
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(out[-1], p) >= step:
            out.append(p)
    if len(out) > max_pts:
        out = out[::math.ceil(len(out) / max_pts)]
    return out


def box_edges():
    c = [(x, y, z) for x in (-HALF_X, HALF_X) for y in (-1, 1) for z in (-1, 1)]
    return [(a, b) for i, a in enumerate(c) for b in c[i + 1:] if sum(u != v for u, v in zip(a, b)) == 1]


def solid_polygons(strokes, yaw, pitch):
    """Extrude strokes into a 3D solid -> [(screen_points, fill, outline)] sorted far-to-near.
    A stroke that ends near its start is treated as closed (filled front/back faces);
    an open stroke becomes a ribbon."""
    polys = []
    for s in strokes:
        if len(s["pts"]) < 2:
            continue
        pts = simplify(s["pts"])
        n = len(pts)
        if n < 2:
            continue
        closed = n > 3 and math.dist(pts[0], pts[-1]) < 0.25
        front = [(x, y, z - THICKNESS) for x, y, z in pts]
        back = [(x, y, z + THICKNESS) for x, y, z in pts]
        faces = [[front[i], front[(i + 1) % n], back[(i + 1) % n], back[i]] for i in range(n - 1 + closed)]
        if closed:
            faces += [front, back[::-1]]
        base = "#505050" if s["color"] == "#111111" else s["color"]   # pure black shows no shading
        for face in faces:
            r = [rotate(p, yaw, pitch) for p in face]
            # Newell's method: robust normal even with duplicate / collinear vertices
            nx = ny = nz = 0.0
            for (x0, y0, z0), (x1, y1, z1) in zip(r, r[1:] + r[:1]):
                nx += (y0 - y1) * (z0 + z1)
                ny += (z0 - z1) * (x0 + x1)
                nz += (x0 - x1) * (y0 + y1)
            light = 0.55 + 0.75 * abs(nz) / (math.sqrt(nx * nx + ny * ny + nz * nz) or 1)
            screen = []
            for x, y, z in r:
                k = EYE / max(0.5, EYE + z)
                screen += [W / 2 + x * k * SCALE, H / 2 + y * k * SCALE]
            fill = shade(base, light)
            polys.append((sum(q[2] for q in r) / len(r), screen, fill, shade(fill, 0.8) if len(face) > 4 else fill))
    polys.sort(key=lambda t: -t[0])          # painter's algorithm: farthest first
    return [p[1:] for p in polys]


# ----------------------------------------------------------------- serial
def parse_line(line):
    """'AX:1.0,AY:...,GZ:0.01' -> dict, or None for anything else / implausible values."""
    if not line.startswith("AX:"):
        return None
    try:
        v = {k: float(x) for k, x in (p.split(":") for p in line.split(","))}
    except ValueError:
        return None
    if not all(k in v for k in ("AX", "AY", "AZ", "GX", "GY", "GZ")):
        return None
    # A loose wire gives all-zero or absurd readings; gravity alone is ~9.8 m/s^2.
    if abs(v["AX"]) + abs(v["AY"]) + abs(v["AZ"]) < 0.5 or \
       max(abs(v[k]) for k in ("AX", "AY", "AZ")) > 30 or max(abs(v[k]) for k in ("GX", "GY", "GZ")) > 10:
        return None
    return v


def open_serial(port):
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.5
    s.dtr = False                     # do not reset the board when opening
    s.rts = False
    s.open()
    return s


def find_port(listen_s=1.5):
    """Return the first serial port that streams sensor lines (boards without a sensor are skipped)."""
    for p in serial.tools.list_ports.comports():
        try:
            s = open_serial(p.device)
        except (serial.SerialException, OSError):
            continue
        try:
            end = time.time() + listen_s
            while time.time() < end:
                if parse_line(s.readline().decode(errors="ignore").strip()):
                    return p.device
        except (serial.SerialException, OSError):
            pass
        finally:
            s.close()
    return None


def reader(port, q, stop):
    try:
        s = open_serial(port)
    except serial.SerialException:
        q.put("BUSY")
        return
    while not stop.is_set():
        try:
            v = parse_line(s.readline().decode(errors="ignore").strip())
        except serial.SerialException:
            q.put("LOST")
            break
        if v:
            q.put(v)
    s.close()


# ----------------------------------------------------------------- app
class App:
    def __init__(self, root, port, save_dir=None):
        self.root, self.q, self.stop = root, queue.Queue(), threading.Event()
        self.save_dir = save_dir or os.path.join(os.path.expanduser("~"), "Desktop")
        root.title(f"{T['title']} - {port}")
        self.canvas = tk.Canvas(root, width=W, height=H, bg="white", highlightthickness=0)
        self.canvas.pack()
        self.info = tk.Label(root, anchor="w", font=("Segoe UI", 10))
        self.info.pack(fill="x")
        self.pen, self.color, self.edge_deg = False, 0, 75.0   # edge_deg: tilt needed to reach the edge
        self.sign_x, self.sign_y, self.swap = 1, 1, False
        self.p = [0.0, 0.0, 0.0]                              # cursor (world)
        self.strokes = []                                     # [{"color":..,"pts":[(x,y,z),..]}]
        self.yaw, self.pitch = 0.0, 0.0
        self.keys = {"w": False, "s": False}
        self.dirty, self.solid = True, False
        self.start_calibration()
        b = root.bind
        for key, fn in {"<space>": self.toggle_pen, "c": self.clear, "e": self.toggle_solid, "o": self.open_last,
                        "k": lambda e: self.start_calibration(), "x": lambda e: self.invert("sign_x"),
                        "y": lambda e: self.invert("sign_y"), "d": lambda e: self.toggle_swap(),
                        "+": lambda e: self.speed(0.8), "-": lambda e: self.speed(1.25),
                        "r": lambda e: self.view(0, 0, reset=True),
                        "<Left>": lambda e: self.view(-0.15, 0), "<Right>": lambda e: self.view(0.15, 0),
                        "<Up>": lambda e: self.view(0, 0.15), "<Down>": lambda e: self.view(0, -0.15),
                        "<Control-s>": self.save, "<Escape>": lambda e: self.close()}.items():
            b(key, fn)
        for k in ("w", "s"):
            b(f"<KeyPress-{k}>", lambda e, k=k: self.keys.__setitem__(k, True))
            b(f"<KeyRelease-{k}>", lambda e, k=k: self.keys.__setitem__(k, False))
        for i in range(len(COLORS)):
            b(str(i + 1), lambda e, i=i: setattr(self, "color", i))
        self.canvas.bind("<ButtonPress-1>", lambda e: setattr(self, "mouse", (e.x, e.y)))
        self.canvas.bind("<B1-Motion>", self.drag)
        root.protocol("WM_DELETE_WINDOW", self.close)
        if port:
            threading.Thread(target=reader, args=(port, self.q, self.stop), daemon=True).start()
        self.last = time.time()
        self.tick()

    # -- settings
    def start_calibration(self):
        self.calib = [None]
        self.status = T["calib"]

    def toggle_pen(self, e=None):
        self.pen = not self.pen
        if self.pen:
            self.strokes.append({"color": COLORS[self.color], "pts": [tuple(self.p)]})

    def invert(self, name):
        setattr(self, name, -getattr(self, name))

    def toggle_swap(self):
        self.swap = not self.swap

    def speed(self, k):
        self.edge_deg = max(8.0, min(90.0, self.edge_deg * k))

    def view(self, dyaw, dpitch, reset=False):
        if reset:
            self.yaw, self.pitch = 0.0, 0.0
        else:
            self.yaw += dyaw
            self.pitch = max(-1.4, min(1.4, self.pitch + dpitch))
        self.dirty = True

    def drag(self, e):
        mx, my = getattr(self, "mouse", (e.x, e.y))
        self.mouse = (e.x, e.y)
        self.view((e.x - mx) * 0.01, -(e.y - my) * 0.01)

    def toggle_solid(self, e=None):
        self.solid = not self.solid
        if self.solid:
            self.pitch = 0.35
            self.status = T["solid"]
        else:
            self.yaw, self.pitch = 0.0, 0.0
            self.status = T["sketch"]
        self.dirty = True

    def clear(self, e=None):
        self.strokes = []
        if self.pen:
            self.strokes.append({"color": COLORS[self.color], "pts": [tuple(self.p)]})
        self.dirty = True

    def render_image(self):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        for a, b in box_edges():
            d.line(project(a, self.yaw, self.pitch)[:2] + project(b, self.yaw, self.pitch)[:2], fill="#dddddd")
        if self.solid:
            for pts, fill, outline in solid_polygons(self.strokes, self.yaw, self.pitch):
                d.polygon(pts, fill=fill, outline=outline)
        else:
            for s in self.strokes:
                for a, b in zip(s["pts"], s["pts"][1:]):
                    pa, pb = project(a, self.yaw, self.pitch), project(b, self.yaw, self.pitch)
                    d.line(pa[:2] + pb[:2], fill=s["color"], width=max(1, round(1 + 3 * pa[2])))
        return img

    def save(self, e=None):
        name = time.strftime("air_drawing_%Y%m%d_%H%M%S")
        os.makedirs(self.save_dir, exist_ok=True)
        self.render_image().save(os.path.join(self.save_dir, name + ".png"))
        with open(os.path.join(self.save_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(self.strokes, f)
        self.status = T["saved"].format(name=name)

    def open_last(self, e=None):
        files = sorted(glob.glob(os.path.join(self.save_dir, "air_drawing_*.json")))
        if not files:
            self.status = T["no_save"]
            return
        with open(files[-1], encoding="utf-8") as f:
            self.strokes = [{"color": s["color"], "pts": [tuple(p) for p in s["pts"]]} for s in json.load(f)]
        self.pen, self.dirty = False, True
        self.status = T["opened"].format(name=os.path.basename(files[-1]))

    # -- sensor
    def feed(self, v):
        """Tilt -> cursor X/Y. Gravity is an absolute reference, so no drift accumulates."""
        g = (v["AX"], v["AY"], v["AZ"])
        if self.calib and self.calib[0] is None:
            self.calib.append(g)
            if len(self.calib) > CALIB_SAMPLES:
                avg = [sum(o[i] for o in self.calib[1:]) / (len(self.calib) - 1) for i in range(3)]
                self.n = unit(avg)                        # "neutral" gravity direction
                axis = min(range(3), key=lambda i: abs(self.n[i]))
                e = [0.0, 0.0, 0.0]
                e[axis] = 1.0
                self.u = unit(cross(self.n, e))           # two tilt directions perpendicular to it
                self.w = cross(self.n, self.u)
                self.smooth = None
                self.calib = []
                self.status = T["ready"]
            return
        if not hasattr(self, "u"):
            return
        gu = unit(g)
        target = (sum(a * b for a, b in zip(gu, self.u)), sum(a * b for a, b in zip(gu, self.w)))
        self.smooth = target if self.smooth is None else \
            tuple(s + SMOOTHING * (t - s) for s, t in zip(self.smooth, target))
        a, b = self.smooth
        if self.swap:
            a, b = b, a
        a = math.degrees(math.asin(max(-1.0, min(1.0, a))))   # linear in degrees: every degree = same distance
        b = math.degrees(math.asin(max(-1.0, min(1.0, b))))
        self.p[0] = max(-1.0, min(1.0, self.sign_x * a / self.edge_deg)) * HALF_X
        self.p[1] = max(-1.0, min(1.0, self.sign_y * b / self.edge_deg))

    # -- drawing loop
    def add_point(self):
        if not (self.pen and self.strokes):
            return
        pts = self.strokes[-1]["pts"]
        if math.dist(pts[-1], self.p) < 0.004:
            return
        a, b = pts[-1], tuple(self.p)
        pts.append(b)
        if self.solid:
            self.dirty = True
        elif not self.dirty:                  # view unchanged: draw only the new segment
            self.segment(a, b, self.strokes[-1]["color"])

    def segment(self, a, b, color):
        pa, pb = project(a, self.yaw, self.pitch), project(b, self.yaw, self.pitch)
        self.canvas.create_line(pa[0], pa[1], pb[0], pb[1], fill=color, width=1 + 3 * pa[2],
                                capstyle="round", tags="ink")

    def redraw(self):
        c = self.canvas
        c.delete("all")
        for a, b in box_edges():
            pa, pb = project(a, self.yaw, self.pitch), project(b, self.yaw, self.pitch)
            c.create_line(pa[0], pa[1], pb[0], pb[1], fill="#dddddd")
        for z in (-0.5, 0, 0.5):              # floor grid helps depth perception
            pa, pb = project((-HALF_X, 1, z), self.yaw, self.pitch), project((HALF_X, 1, z), self.yaw, self.pitch)
            c.create_line(pa[0], pa[1], pb[0], pb[1], fill="#eeeeee")
        if self.solid:
            for pts, fill, outline in solid_polygons(self.strokes, self.yaw, self.pitch):
                c.create_polygon(pts, fill=fill, outline=outline, tags="ink")
        else:
            for s in self.strokes:
                for a, b in zip(s["pts"], s["pts"][1:]):
                    self.segment(a, b, s["color"])
        self.shadow = c.create_line(0, 0, 0, 0, fill="#bbbbbb", dash=(3, 3))
        self.cursor = c.create_oval(0, 0, 0, 0)
        self.dirty = False

    def tick(self):
        now = time.time()
        dt, self.last = now - self.last, now
        while not self.q.empty():
            v = self.q.get_nowait()
            if v == "LOST":
                self.status = T["lost"]
            elif v == "BUSY":
                self.status = T["busy"]
            else:
                self.feed(v)
        self.p[2] = max(-1.0, min(1.0, self.p[2] + (self.keys["w"] - self.keys["s"]) * DEPTH_SPEED * dt))
        if self.solid:
            self.yaw += 0.6 * dt              # auto-rotate
            self.dirty = True
        if self.dirty:
            self.redraw()
        self.add_point()
        sx, sy, k = project(self.p, self.yaw, self.pitch)
        fx, fy, _ = project((self.p[0], 1, self.p[2]), self.yaw, self.pitch)
        r = (9 if self.pen else 6) * k
        self.canvas.coords(self.shadow, sx, sy, fx, fy)
        self.canvas.coords(self.cursor, sx - r, sy - r, sx + r, sy + r)
        self.canvas.itemconfig(self.cursor, outline=COLORS[self.color] if self.pen else "#999",
                               width=3 if self.pen else 1)
        self.canvas.tag_raise(self.cursor)
        self.info.config(text=f"  {self.status}   |   {T['pen']}: {T['down'] if self.pen else T['up']}"
                              f"   {T['depth']}={self.p[2]:+.2f}   {T['edge']}={self.edge_deg:.0f}°   |  {T['help']}")
        self.root.after(16, self.tick)

    def close(self):
        self.stop.set()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="Draw in the air with an ESP32 + MPU6050.")
    ap.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyACM0 (default: auto-detect)")
    ap.add_argument("--save-dir", help="where Ctrl+S saves (default: Desktop)")
    args = ap.parse_args()
    port = args.port or find_port()
    if not port:
        import tkinter.messagebox as mb
        tk.Tk().withdraw()
        mb.showerror(T["title"], T["not_found"])
        sys.exit(1)
    root = tk.Tk()
    root.resizable(False, False)
    App(root, port, args.save_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
