"""海戰棋 繪圖工具：棋盤幾何、艦艇外形、砲台、砲彈與特效（純 tkinter Canvas）"""
import math

from protocol import COLORS, FLEET, SIZE

PLAYER_COLORS = COLORS                 # 玩家可選的板子顏色（定義在 protocol.py）
INK = "#0b0f14"          # 深色描邊
FIRE_O = "#ff7a18"
FIRE_Y = "#ffd23f"
CHAR = "#262a33"         # 擊沉的焦黑船身
SMOKE = "#9aa3ad"


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Painter:
    """在 Canvas 上畫圖，所有物件自動加上同一個 tag，方便整批刪除"""

    def __init__(self, cv, tag):
        self.cv, self.tag = cv, tag

    def line(self, *a, **k):
        return self.cv.create_line(*a, tags=self.tag, **k)

    def poly(self, pts, **k):
        return self.cv.create_polygon(pts, tags=self.tag, **k)

    def oval(self, *a, **k):
        return self.cv.create_oval(*a, tags=self.tag, **k)

    def rect(self, *a, **k):
        return self.cv.create_rectangle(*a, tags=self.tag, **k)

    def text(self, *a, **k):
        return self.cv.create_text(*a, tags=self.tag, **k)

    def arc(self, *a, **k):
        return self.cv.create_arc(*a, tags=self.tag, **k)

    def rrect(self, x1, y1, x2, y2, r, **k):
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.poly(pts, smooth=True, **k)


class Geo:
    """棋盤幾何：左上角 (x0, y0)、格子大小 cs、格子間隙 gap"""

    def __init__(self, x0, y0, cs, gap=3):
        self.x0, self.y0, self.cs, self.gap = x0, y0, cs, gap

    @property
    def size(self):
        return self.cs * SIZE

    def box(self, x, y, inset=0):
        g = self.gap / 2 + inset
        return (self.x0 + x * self.cs + g, self.y0 + y * self.cs + g,
                self.x0 + (x + 1) * self.cs - g, self.y0 + (y + 1) * self.cs - g)

    def center(self, x, y):
        return self.x0 + (x + 0.5) * self.cs, self.y0 + (y + 0.5) * self.cs

    def cell_at(self, px, py):
        x, y = int((px - self.x0) // self.cs), int((py - self.y0) // self.cs)
        if px >= self.x0 and py >= self.y0 and 0 <= x < SIZE and 0 <= y < SIZE:
            return (x, y)
        return None


# ---------------------------------------------------------------- 艦艇外形
def _hull(sid, L, W):
    """回傳 (外形點列 [(u,v)], 是否平滑)。u：船尾→船頭，v：橫向"""
    h = W / 2
    if sid == "carrier":
        return [(0, -h), (L * 0.88, -h), (L, -h * 0.35), (L, h * 0.5), (L * 0.93, h), (0, h)], False
    if sid == "submarine":
        return [(0, 0), (L * 0.08, -h * 0.8), (L * 0.8, -h * 0.8), (L, 0), (L * 0.8, h * 0.8), (L * 0.08, h * 0.8)], True
    return [(L * 0.04, -h), (L * 0.72, -h), (L, 0), (L * 0.72, h), (L * 0.04, h), (0, h * 0.55), (0, -h * 0.55)], False


def ship_shape(P, geo, sid, cells, fill, outline=INK, width=2, stipple=None, detail=True):
    """依格子位置畫出一艘船（橫放船頭朝右，直放船頭朝上）"""
    cells = [tuple(c) for c in cells]
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    horiz = len(set(ys)) == 1 and len(cells) > 1
    pad = geo.cs * 0.12
    x1, y1, _, _ = geo.box(min(xs), min(ys))
    _, _, x2, y2 = geo.box(max(xs), max(ys))
    L = (x2 - x1 if horiz else y2 - y1) - 2 * pad
    W = geo.cs * 0.62
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

    def tr(u, v):
        return (x1 + pad + u, cy + v) if horiz else (cx + v, y2 - pad - u)

    pts, smooth = _hull(sid, L, W)
    flat = [c for u, v in pts for c in tr(u, v)]
    kw = {"stipple": stipple} if stipple else {}
    P.poly(flat, fill=fill, outline=outline, width=width, smooth=smooth, **kw)
    if not detail or stipple:
        return
    dk = blend(fill, "#000000", 0.35)
    lt = blend(fill, "#ffffff", 0.35)

    def circ(u, v, r, **k):
        x, y = tr(u, v)
        P.oval(x - r, y - r, x + r, y + r, **k)

    def box(u1, u2, v1, v2, **k):
        a, b = tr(u1, v1), tr(u2, v2)
        P.rect(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]), **k)

    if sid == "carrier":
        a, b = tr(L * 0.06, 0), tr(L * 0.84, 0)
        P.line(*a, *b, fill=lt, width=max(1, int(W * 0.08)), dash=(6, 5))
        box(L * 0.55, L * 0.7, W * 0.12, W * 0.45, fill=dk, outline="")
        for k in range(3):                                   # 甲板上的飛機
            x, y = tr(L * (0.16 + k * 0.1), -W * 0.18)
            s = W * 0.14
            P.poly([x - s, y + s, x + s, y + s, x, y - s] if not horiz else [x - s, y - s, x - s, y + s, x + s, y],
                   fill=dk, outline="")
    elif sid == "submarine":
        box(L * 0.45, L * 0.62, -W * 0.2, W * 0.2, fill=dk, outline="")
        circ(L * 0.53, 0, W * 0.09, fill=lt, outline="")
    else:
        turrets = {"battleship": [0.17, 0.33, 0.82], "cruiser": [0.2, 0.78], "destroyer": [0.75]}[sid]
        box(L * 0.45, L * 0.62, -W * 0.28, W * 0.28, fill=dk, outline="")
        for t in turrets:
            circ(L * t, 0, W * 0.2, fill=dk, outline="")
            a, b = tr(L * t, 0), tr(L * t + W * (0.4 if t > 0.5 else -0.4), 0)
            P.line(*a, *b, fill=dk, width=max(2, int(W * 0.1)))


def tray_ships(P, x, y, sunk_ids, color, cs=16, gap=6, dim=False):
    """一排艦艇圖示：還在的上色、擊沉的變焦黑。回傳每艘的範圍 {id: (x1,y1,x2,y2)}"""
    boxes = {}
    for sid, (name, size) in FLEET.items():
        g = Geo(x, y, cs, gap=0)
        cells = [(i, 0) for i in range(size)]
        dead = sid in sunk_ids
        fill = CHAR if dead else (blend(color, "#0b1a24", 0.55) if dim else color)
        ship_shape(P, g, sid, cells, fill, INK, width=2, detail=not dim)
        boxes[sid] = (x, y, x + size * cs, y + cs)
        x += size * cs + gap
    return boxes


def tray_width(cs=16, gap=6):
    return sum(s for _, s in FLEET.values()) * cs + gap * (len(FLEET) - 1)


# ---------------------------------------------------------------- 特效
def diamond(P, x, y, s, **k):
    P.poly([x, y - s, x + s, y, x, y + s, x - s, y], **k)


def fire(P, geo, x, y, t, seed=0):
    """命中後持續燃燒的火焰"""
    cx, cy = geo.center(x, y)
    cs = geo.cs
    for k, (dx, dy) in enumerate(((-0.16, 0.06), (0.14, -0.06))):
        f = math.sin(t * 11 + seed * 1.7 + k * 2.1)
        s = cs * 0.2 * (1 + 0.15 * f)
        px, py = cx + dx * cs, cy + dy * cs - f * cs * 0.03
        diamond(P, px, py, s, fill=FIRE_O, outline="")
        diamond(P, px + s * 0.15, py + s * 0.1, s * 0.6, fill=FIRE_Y, outline="")


def smoke(P, geo, cells, t, seed=0):
    """擊沉船隻冒出的煙"""
    x, y = cells[len(cells) // 2]
    cx, cy = geo.center(x, y)
    for k in range(3):
        ph = ((t * 0.6 + k / 3 + seed * 0.13) % 1.0)
        r = geo.cs * (0.14 + 0.22 * ph)
        px = cx + math.sin(ph * 6 + k) * geo.cs * 0.15
        py = cy - ph * geo.cs * 1.1
        P.oval(px - r, py - r, px + r, py + r, fill=SMOKE, outline="", stipple="gray50")


def miss_mark(P, geo, x, y, ink):
    cx, cy = geo.center(x, y)
    k = geo.cs * 0.2
    w = max(2, int(geo.cs * 0.09))
    P.line(cx - k, cy - k, cx + k, cy + k, fill=ink, width=w, capstyle="round")
    P.line(cx - k, cy + k, cx + k, cy - k, fill=ink, width=w, capstyle="round")


def cannon(P, cx, cy, ang, color, recoil=0.0, scale=1.0, flash=0.0):
    """俯視角砲台：底座＋朝 ang（弧度，0 = 向右，-π/2 = 向上）的砲管"""
    R = 30 * scale
    P.oval(cx - R - 3 + 6, cy - R - 3 + 8, cx + R + 3 + 6, cy + R + 3 + 8, fill="#000000", outline="", stipple="gray25")
    P.oval(cx - R - 4, cy - R - 4, cx + R + 4, cy + R + 4, fill=INK, outline="")
    P.oval(cx - R, cy - R, cx + R, cy + R, fill=color, outline="")
    P.oval(cx - R * 0.62, cy - R * 0.62, cx + R * 0.62, cy + R * 0.62, fill=blend(color, "#000000", 0.3), outline="")
    length = (52 - 12 * recoil) * scale
    w = 15 * scale
    ux, uy = math.cos(ang), math.sin(ang)
    nx, ny = -uy, ux
    bx, by = cx - ux * 6 * scale, cy - uy * 6 * scale
    tx, ty = cx + ux * length, cy + uy * length
    P.poly([bx + nx * (w + 4), by + ny * (w + 4), tx + nx * (w + 4), ty + ny * (w + 4),
            tx - nx * (w + 4), ty - ny * (w + 4), bx - nx * (w + 4), by - ny * (w + 4)], fill=INK, outline="")
    P.poly([bx + nx * w, by + ny * w, tx + nx * w, ty + ny * w,
            tx - nx * w, ty - ny * w, bx - nx * w, by - ny * w], fill="#4b5058", outline="")
    hx1, hy1 = bx + ux * length * 0.3 + nx * w * 0.45, by + uy * length * 0.3 + ny * w * 0.45
    hx2, hy2 = bx + ux * length * 0.65 + nx * w * 0.45, by + uy * length * 0.65 + ny * w * 0.45
    P.line(hx1, hy1, hx2, hy2, fill="#8a9099", width=max(2, int(4 * scale)), capstyle="round")
    mr = w + 5
    P.oval(tx - mr, ty - mr, tx + mr, ty + mr, fill=INK, outline="")
    P.oval(tx - w * 0.75, ty - w * 0.75, tx + w * 0.75, ty + w * 0.75, fill="#1b1e24", outline="")
    if flash > 0:
        fr = (14 + 18 * flash) * scale
        fx, fy = tx + ux * fr * 0.6, ty + uy * fr * 0.6
        diamond(P, fx, fy, fr, fill=FIRE_Y, outline="")
        diamond(P, fx, fy, fr * 0.55, fill="#ffffff", outline="")
    return tx, ty


def shell(P, sx, sy, ex, ey, t):
    """砲彈拋物線：t = 0~1"""
    x = sx + (ex - sx) * t
    y = sy + (ey - sy) * t
    h = 150 * math.sin(math.pi * t)
    r = 7 + 6 * math.sin(math.pi * t)
    P.oval(x - r * 0.9, y - r * 0.45, x + r * 0.9, y + r * 0.45, fill="#000000", outline="", stipple="gray50")
    P.oval(x - r, y - h - r, x + r, y - h + r, fill="#2b2f36", outline=INK, width=2)
    P.oval(x - r * 0.45, y - h - r * 0.55, x - r * 0.05, y - h - r * 0.15, fill="#8a9099", outline="")


def splash(P, cx, cy, t, cs, color):
    """沒打中：水花"""
    for k in range(2):
        tt = t - k * 0.12
        if 0 <= tt <= 1:
            r = cs * (0.2 + 0.7 * tt)
            P.oval(cx - r, cy - r * 0.8, cx + r, cy + r * 0.8, outline=blend("#ffffff", color, tt), width=3)
    if t < 0.6:
        for k in range(5):
            a = k * 1.256 + 0.4
            d = cs * 0.5 * t / 0.6
            r = cs * 0.07 * (1 - t / 0.6)
            x, y = cx + math.cos(a) * d, cy + math.sin(a) * d - cs * 0.4 * math.sin(math.pi * t / 0.6)
            P.oval(x - r, y - r, x + r, y + r, fill="#e6f6ff", outline="")


def boom(P, cx, cy, t, cs, big=False):
    """命中：爆炸"""
    k = 1.5 if big else 1.0
    if t < 0.5:
        r = cs * k * (0.3 + 0.9 * t / 0.5)
        P.oval(cx - r, cy - r, cx + r, cy + r, fill=blend(FIRE_Y, FIRE_O, t / 0.5), outline="")
        r2 = r * 0.55
        P.oval(cx - r2, cy - r2, cx + r2, cy + r2, fill="#fff7d6", outline="")
    for i in range(8 if big else 6):
        a = i * (6.283 / (8 if big else 6)) + 0.3
        d = cs * k * (0.3 + 1.1 * t)
        s = cs * 0.12 * (1 - t)
        if s > 0:
            diamond(P, cx + math.cos(a) * d, cy + math.sin(a) * d, s, fill=FIRE_O if i % 2 else FIRE_Y, outline="")


# ---------------------------------------------------------------- 表情（自己畫的卡通臉）
def emote_face(P, kind, cx, cy, r, font=None):
    """畫一個表情，r = 半徑"""
    w = max(2, int(r * 0.09))
    if kind == "boom":
        diamond(P, cx, cy, r * 1.1, fill=FIRE_O, outline=INK, width=w)
        diamond(P, cx, cy, r * 0.65, fill=FIRE_Y, outline="")
        for a in range(4):
            ang = a * 1.5708 + 0.785
            diamond(P, cx + math.cos(ang) * r * 0.85, cy + math.sin(ang) * r * 0.85, r * 0.28, fill=FIRE_Y, outline=INK, width=1)
        diamond(P, cx, cy, r * 0.25, fill="#ffffff", outline="")
        return
    if kind == "gg":
        P.rrect(cx - r * 1.1, cy - r * 0.8, cx + r * 1.1, cy + r * 0.7, r * 0.4, fill="#ffffff", outline=INK, width=w)
        P.poly([cx - r * 0.4, cy + r * 0.6, cx - r * 0.1, cy + r * 0.6, cx - r * 0.55, cy + r * 1.05], fill="#ffffff", outline=INK, width=w)
        P.rect(cx - r * 0.38, cy + r * 0.5, cx - r * 0.12, cy + r * 0.66, fill="#ffffff", outline="")
        if font:
            P.text(cx, cy - r * 0.05, text="GG", fill=INK, font=(font[0], max(8, int(r * 0.75)), "bold"))
        return
    face = "#ff6b5b" if kind == "angry" else "#ffd23f"
    P.oval(cx - r, cy - r, cx + r, cy + r, fill=face, outline=INK, width=w)
    ex, ey, er = r * 0.36, cy - r * 0.18, r * 0.13

    def eye_dot(x):
        P.oval(x - er, ey - er, x + er, ey + er, fill=INK, outline="")

    def eye_arc(x, up=True):
        P.arc(x - er * 2, ey - er * 1.4, x + er * 2, ey + er * 2.2, start=20 if up else 200, extent=140,
              style="arc", outline=INK, width=w)

    if kind == "laugh":
        eye_arc(cx - ex)
        eye_arc(cx + ex)
        P.arc(cx - r * 0.55, cy - r * 0.35, cx + r * 0.55, cy + r * 0.7, start=180, extent=180, style="pieslice", fill=INK, outline=INK)
        P.oval(cx - r * 0.25, cy + r * 0.38, cx + r * 0.25, cy + r * 0.6, fill="#ff6b5b", outline="")
        for sx in (-1, 1):
            P.oval(cx + sx * r * 0.82 - r * 0.13, ey + r * 0.05, cx + sx * r * 0.82 + r * 0.13, ey + r * 0.35, fill="#38bdf8", outline="")
    elif kind == "cool":
        for sx in (-1, 1):
            P.rrect(cx + sx * ex - r * 0.3, ey - r * 0.17, cx + sx * ex + r * 0.3, ey + r * 0.2, r * 0.1, fill=INK, outline="")
        P.line(cx - ex, ey - r * 0.1, cx + ex, ey - r * 0.1, fill=INK, width=w)
        P.line(cx - ex - r * 0.3, ey - r * 0.1, cx - r * 0.95, ey - r * 0.2, fill=INK, width=w)
        P.line(cx + ex + r * 0.3, ey - r * 0.1, cx + r * 0.95, ey - r * 0.2, fill=INK, width=w)
        P.arc(cx - r * 0.2, cy + r * 0.05, cx + r * 0.55, cy + r * 0.55, start=200, extent=110, style="arc", outline=INK, width=w)
    elif kind == "angry":
        eye_dot(cx - ex)
        eye_dot(cx + ex)
        P.line(cx - ex - r * 0.25, ey - r * 0.35, cx - ex + r * 0.2, ey - r * 0.15, fill=INK, width=w + 1)
        P.line(cx + ex + r * 0.25, ey - r * 0.35, cx + ex - r * 0.2, ey - r * 0.15, fill=INK, width=w + 1)
        P.arc(cx - r * 0.4, cy + r * 0.3, cx + r * 0.4, cy + r * 0.85, start=20, extent=140, style="arc", outline=INK, width=w)
    elif kind == "cry":
        for sx in (-1, 1):
            P.line(cx + sx * ex - r * 0.18, ey, cx + sx * ex + r * 0.18, ey, fill=INK, width=w)
            P.rect(cx + sx * ex - r * 0.08, ey + r * 0.05, cx + sx * ex + r * 0.08, cy + r * 0.85, fill="#38bdf8", outline="")
        P.arc(cx - r * 0.3, cy + r * 0.3, cx + r * 0.3, cy + r * 0.75, start=20, extent=140, style="arc", outline=INK, width=w)
    elif kind == "tongue":
        eye_arc(cx - ex)
        eye_dot(cx + ex)
        P.arc(cx - r * 0.5, cy - r * 0.15, cx + r * 0.5, cy + r * 0.5, start=200, extent=140, style="arc", outline=INK, width=w)
        P.rrect(cx - r * 0.12, cy + r * 0.3, cx + r * 0.34, cy + r * 0.78, r * 0.2, fill="#ff6b8a", outline=INK, width=max(1, w - 1))
    elif kind == "sleep":
        for sx in (-1, 1):
            P.line(cx + sx * ex - r * 0.18, ey + r * 0.05, cx + sx * ex + r * 0.18, ey + r * 0.05, fill=INK, width=w)
        P.oval(cx - r * 0.12, cy + r * 0.35, cx + r * 0.12, cy + r * 0.6, fill=INK, outline="")
        if font:
            P.text(cx + r * 0.85, cy - r * 0.85, text="Z", fill="#ffffff", font=(font[0], max(7, int(r * 0.5)), "bold"))
            P.text(cx + r * 1.2, cy - r * 1.25, text="z", fill="#ffffff", font=(font[0], max(6, int(r * 0.38)), "bold"))
