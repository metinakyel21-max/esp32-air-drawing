"""Hardware-free tests:  python -m pytest tests   (or: python tests/test_air_draw.py)"""
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import air_draw as ad  # noqa: E402


def sample(g):
    return {"AX": g[0], "AY": g[1], "AZ": g[2], "GX": 0.0, "GY": 0.0, "GZ": 0.0}


class FakeApp:
    """Just the state App.feed() needs - no window, no serial port."""
    def __init__(self):
        self.p = [0.0, 0.0, 0.0]
        self.edge_deg, self.sign_x, self.sign_y, self.swap = 75.0, 1, 1, False
        self.calib, self.status = [None], ""

    feed = ad.App.feed


def calibrated(g0):
    app = FakeApp()
    for _ in range(ad.CALIB_SAMPLES + 5):
        app.feed(sample(g0))
    return app


def tilted(app, g0, deg, direction):
    n, t = ad.unit(g0), math.radians(deg)
    return tuple(9.81 * (math.cos(t) * a + math.sin(t) * b) for a, b in zip(n, direction))


def test_parse_line():
    v = ad.parse_line("AX:-7.07,AY:6.03,AZ:-0.36,GX:0.04,GY:-0.04,GZ:0.01")
    assert v["AX"] == -7.07 and v["GZ"] == 0.01


def test_parse_rejects_noise_and_loose_wire_values():
    assert ad.parse_line("ERROR:MPU6050_NOT_FOUND") is None
    assert ad.parse_line("AX:broken") is None
    assert ad.parse_line("AX:0.00,AY:0.00,AZ:0.00,GX:0.00,GY:0.00,GZ:0.00") is None       # bus dropped
    assert ad.parse_line("AX:1.22,AY:-135.93,AZ:0.62,GX:-13.56,GY:-34.78,GZ:-14.92") is None  # garbage


def test_cursor_stays_centered_when_still():
    g0 = (-7.0, 6.0, -0.4)                     # sensor mounted sideways: any orientation works
    app = calibrated(g0)
    for _ in range(300):
        app.feed(sample(g0))
    assert abs(app.p[0]) < 1e-6 and abs(app.p[1]) < 1e-6


def test_tilt_to_edge_angle_reaches_screen_edge_on_both_axes():
    g0 = (0.3, 0.2, 9.8)
    for direction, axis, limit in (("u", 0, ad.HALF_X), ("w", 1, 1.0)):
        app = calibrated(g0)
        g = tilted(app, g0, app.edge_deg, getattr(app, direction))
        for _ in range(400):
            app.feed(sample(g))
        assert abs(app.p[axis] - limit) < 0.01, (direction, app.p)
        assert abs(app.p[1 - axis]) < 0.01


def test_half_tilt_is_half_way_linear_in_degrees():
    g0 = (0.0, 0.0, 9.81)
    app = calibrated(g0)
    g = tilted(app, g0, app.edge_deg / 2, app.u)
    for _ in range(400):
        app.feed(sample(g))
    assert abs(app.p[0] - ad.HALF_X / 2) < 0.01


def test_projection_rotates_depth_into_view():
    sx, _, _ = ad.project((0, 0, 1), math.pi / 2, 0)
    assert abs(sx - ad.W / 2) > 50
    assert len(ad.box_edges()) == 12


def test_box_fits_window_at_moderate_angles():
    for yaw in (-0.6, -0.3, 0, 0.3, 0.6):
        for pitch in (-0.4, 0, 0.4):
            for corner in [(x, y, z) for x in (-ad.HALF_X, ad.HALF_X) for y in (-1, 1) for z in (-1, 1)]:
                sx, sy, _ = ad.project(corner, yaw, pitch)
                assert 0 <= sx <= ad.W and 0 <= sy <= ad.H


def star():
    pts = []
    for i in range(11):
        r, a = (0.8 if i % 2 == 0 else 0.35), -math.pi / 2 + i * math.pi / 5
        pts.append((r * math.cos(a), r * math.sin(a), 0.0))
    return [tuple(p + (q - p) * t / 20 for p, q in zip(pts[j], pts[j + 1])) for j in range(10) for t in range(20)] + [pts[0]]


def test_closed_stroke_becomes_solid_with_front_and_back_faces():
    polys = ad.solid_polygons([{"color": "#d62828", "pts": star()}], 0.7, 0.35)
    big = [p for p in polys if len(p[0]) > 8]          # faces with more than 4 corners
    assert len(big) == 2                                # front + back
    assert len(polys) > 50                              # side walls


def test_open_stroke_becomes_ribbon_without_caps():
    wave = [(i / 100, 0.3 * math.sin(i / 12), 0.0) for i in range(100)]
    polys = ad.solid_polygons([{"color": "#111111", "pts": wave}], 0.5, 0.2)
    assert polys and all(len(p[0]) == 8 for p in polys)


def test_front_face_is_lit_when_facing_viewer():
    polys = ad.solid_polygons([{"color": "#d62828", "pts": star()}], 0.0, 0.0)
    face_fill = [p[1] for p in polys if len(p[0]) > 8][0]
    r = int(face_fill[1:3], 16)
    assert r > 0xd6, face_fill                          # brighter than the base colour


def test_language_override():
    os.environ["AIR_DRAW_LANG"] = "tr"
    assert ad.pick_language() == "tr"
    os.environ["AIR_DRAW_LANG"] = "en"
    assert ad.pick_language() in ("en", "tr")           # falls through to the OS locale
    del os.environ["AIR_DRAW_LANG"]


def test_gui_draw_save_and_reopen():
    import tkinter as tk
    with tempfile.TemporaryDirectory() as d:
        root = tk.Tk()
        app = ad.App(root, None, save_dir=d)            # no port: GUI only
        app.strokes = [{"color": "#d62828", "pts": star()}]
        app.toggle_solid()
        result = {}

        def finish():
            result["yaw"] = app.yaw
            result["items"] = len(app.canvas.find_withtag("ink"))
            app.save()
            app.strokes = []
            app.open_last()
            result["reopened"] = len(app.strokes[0]["pts"]) if app.strokes else 0
            app.close()

        root.after(1500, finish)
        root.mainloop()
        assert result["yaw"] > 0.3                      # auto-rotation ran
        assert result["items"] > 50
        assert result["reopened"] == len(star())
        assert any(f.endswith(".png") for f in os.listdir(d))
        saved = [f for f in os.listdir(d) if f.endswith(".json")][0]
        assert json.load(open(os.path.join(d, saved)))[0]["color"] == "#d62828"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as ex:          # noqa: BLE001
                failed += 1
                print("FAIL", name, repr(ex))
    sys.exit(1 if failed else 0)
