"""Red-pen 'handwritten' marking overlay for scanned scripts.
Coordinates given in 100-dpi page units; rendered at 200 dpi (SCALE=2)."""
import numpy as np, cv2, re, os
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont
from skimage.morphology import skeletonize

SCALE = 2
SS = 4
INK = np.array([205, 28, 36], np.float32)   # red ball-pen (RGB)
_HERE = os.path.dirname(os.path.abspath(__file__))
F_MAIN = os.path.join(_HERE, 'fonts', 'Poppins-Light.ttf')
F_FALL = os.path.join(_HERE, 'fonts', 'DejaVuSans.ttf')
_cmap = set(TTFont(F_MAIN).getBestCmap().keys())
_fc = {}
def _font(path, px):
    k = (path, px)
    if k not in _fc: _fc[k] = ImageFont.truetype(path, px)
    return _fc[k]

def _elastic(m, alpha, sigma, rng):
    h, w = m.shape
    k = max(1, int(sigma / 4))
    hs, ws = max(2, h // k + 2), max(2, w // k + 2)
    def field():
        n = (rng.random((hs, ws)) * 2 - 1).astype(np.float32)
        n = cv2.GaussianBlur(n, (0, 0), max(0.5, sigma / k)) * (alpha / k)
        return cv2.resize(n, (w, h), interpolation=cv2.INTER_LINEAR)
    dx = field(); dy = field()
    x, y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(m, x + dx, y + dy, cv2.INTER_LINEAR)

def _tokens(text):
    """split into (char, level) where level 0 normal, 1 superscript, -1 subscript"""
    out = []; i = 0
    while i < len(text):
        if text[i] in '^_' and i + 1 < len(text):
            lvl = 1 if text[i] == '^' else -1
            if text[i + 1] == '{':
                j = text.index('}', i)
                for c in text[i + 2:j]: out.append((c, lvl))
                i = j + 1
            else:
                out.append((text[i + 1], lvl)); i += 2
        else:
            out.append((text[i], 0)); i += 1
    return out

def text_mask(text, size, pen, seed=0, slant=0.2):
    """size: cap height-ish in final px. returns (alpha float, baseline_y_px)"""
    rng = np.random.default_rng(seed)
    S = size * SS
    fpx = int(S * 1.3)
    f = _font(F_MAIN, fpx)
    asc, desc = f.getmetrics()
    top = int(S * 0.7)
    H = top + asc + desc + int(S * 0.3)
    base = top + asc
    toks = _tokens(text)
    W = int(S * 0.9 * len(toks) + S * 3)
    can = np.zeros((H, W), np.uint8)
    x = int(S * 0.4)
    for ch, lvl in toks:
        sc_l = 1.0 if lvl == 0 else 0.62
        if ch == ' ':
            x += int(S * 0.42 * (0.85 + 0.3 * rng.random())); continue
        path = F_MAIN if ord(ch) in _cmap else F_FALL
        ff = _font(path, max(8, int(fpx * sc_l)))
        a2, d2 = ff.getmetrics()
        gi = Image.new('L', (int(S * 2.5), H), 0); d = ImageDraw.Draw(gi)
        yoff = base - a2
        if lvl == 1: yoff -= int(S * 0.55)
        if lvl == -1: yoff += int(S * 0.28)
        d.text((int(S * 0.3), yoff), ch, font=ff, fill=255)
        g = np.array(gi)
        sc = 1 + rng.normal(0, 0.08); ang = rng.normal(0, 5.0); dy = rng.normal(0, S * 0.05)
        M = cv2.getRotationMatrix2D((S * 0.7, base - S * 0.4), ang, sc); M[1, 2] += dy
        M[0, :2] *= (1 + rng.normal(0, 0.08))
        g = cv2.warpAffine(g, M, (g.shape[1], H))
        cols = np.where(g.max(0) > 40)[0]
        if len(cols) == 0: x += int(S * 0.3); continue
        g = g[:, cols[0]:cols[-1] + 1]
        w = g.shape[1]
        if x + w >= W:
            can = np.pad(can, ((0, 0), (0, w + int(S * 2))))
            W = can.shape[1]
        can[:, x:x + w] = np.maximum(can[:, x:x + w], g)
        x += w + int(S * (0.09 if lvl == 0 else 0.05) * (0.6 + 0.8 * rng.random()))
    can = can[:, :x + int(S * 0.4)]
    h, w = can.shape
    sh = int(slant * h)
    M = np.float32([[1, -slant, slant * base], [0, 1, 0]])
    can = cv2.warpAffine(can, M, (w + sh, h))
    # gentle baseline drift over the word/line
    drift = cv2.GaussianBlur((rng.random((1, can.shape[1])) * 2 - 1).astype(np.float32), (0, 0), S * 4)[0]
    drift = drift / (np.abs(drift).max() + 1e-6) * S * 0.08
    xs, ys = np.meshgrid(np.arange(can.shape[1]), np.arange(h))
    can = cv2.remap(can.astype(np.float32), xs.astype(np.float32), (ys - drift[None, :]).astype(np.float32), cv2.INTER_LINEAR)
    can = _elastic(can, S * 0.9, S * 0.45, rng)
    can = _elastic(can, S * 0.35, S * 0.16, rng)
    b = can > 105
    sk = skeletonize(b).astype(np.uint8) * 255
    r = max(1, int(round(pen * SS / 2)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    st = cv2.dilate(sk, k)
    st = cv2.GaussianBlur(st, (0, 0), SS * 0.3)
    out = cv2.resize(st, (st.shape[1] // SS, st.shape[0] // SS), interpolation=cv2.INTER_AREA)
    return out.astype(np.float32) / 255.0, base / SS


class Page:
    def __init__(self, img_path, seed=1):
        self.img = np.array(Image.open(img_path).convert('RGB')).astype(np.float32)
        self.H, self.W = self.img.shape[:2]
        self.ink = np.zeros((self.H, self.W), np.float32)
        self.rng = np.random.default_rng(seed)
        self.pen = 2.6   # px at 200 dpi
        self.k = 0

    # ---------- low level ----------
    def _stamp(self, a, x, y):
        h, w = a.shape
        x0, y0 = int(x), int(y)
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(self.W, x0 + w), min(self.H, y0 + h)
        if xb <= xa or yb <= ya: return
        self.ink[ya:yb, xa:xb] = np.maximum(self.ink[ya:yb, xa:xb], a[ya - y0:yb - y0, xa - x0:xb - x0])

    def _poly(self, pts, wob=0.6, pen=None):
        """pts in 100dpi units; draws a smooth hand stroke through them"""
        pen = pen or self.pen
        P = np.array(pts, np.float32) * SCALE
        # densify via linear interp + smoothing
        seg = []
        for i in range(len(P) - 1):
            n = max(2, int(np.linalg.norm(P[i + 1] - P[i]) / 2))
            t = np.linspace(0, 1, n, endpoint=False)[:, None]
            seg.append(P[i] * (1 - t) + P[i + 1] * t)
        seg.append(P[-1:])
        Q = np.vstack(seg)
        if len(Q) > 4:
            n = len(Q)
            noise = self.rng.normal(0, 1, (n, 2)).astype(np.float32)
            noise = cv2.GaussianBlur(noise, (1, 0), sigmaX=0, sigmaY=max(2, n / 6)).reshape(n, 2) if False else noise
            # smooth noise along the curve
            from scipy.ndimage import gaussian_filter1d
            noise = gaussian_filter1d(noise, sigma=max(1.5, n / 8), axis=0, mode='nearest')
            noise = noise / (noise.std() + 1e-6)
            Q = Q + noise * wob * SCALE
        m = int(pen * 2) + 6
        x0 = max(0, int(np.floor(Q[:, 0].min())) - m); x1 = min(self.W, int(np.ceil(Q[:, 0].max())) + m)
        y0 = max(0, int(np.floor(Q[:, 1].min())) - m); y1 = min(self.H, int(np.ceil(Q[:, 1].max())) + m)
        if x1 <= x0 or y1 <= y0:
            return
        layer = np.zeros(((y1 - y0) * 2, (x1 - x0) * 2), np.uint8)
        Ql = (Q - np.array([x0, y0], np.float32)) * 2
        cv2.polylines(layer, [np.round(Ql).astype(np.int32)], False, 255, int(round(pen * 2)), cv2.LINE_AA)
        layer = cv2.resize(layer, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        self.ink[y0:y1, x0:x1] = np.maximum(self.ink[y0:y1, x0:x1], layer)

    # ---------- marks ----------
    def tick(self, x, y, s=1.0):
        """x,y = bottom of the tick (100dpi)"""
        r = self.rng.normal(0, 0.06, 4)
        a = 7 * s; b = 20 * s
        self._poly([(x - a * (1 + r[0]), y - a * (0.9 + r[1])), (x - a * 0.4, y - a * 0.35), (x, y),
                    (x + b * 0.35, y - b * 0.5), (x + b * (0.75 + r[2]), y - b * (1.05 + r[3]))], wob=0.25)

    def cross(self, x, y, s=1.0):
        d = 8 * s
        self._poly([(x - d, y - d), (x + d * 1.05, y + d * 0.95)], wob=0.3)
        self._poly([(x + d * 0.95, y - d * 1.05), (x - d, y + d)], wob=0.3)

    def write(self, x, y, text, size=11, pen=None, slant=0.2):
        """x = left, y = baseline (100dpi units)"""
        self.k += 1
        a, base = text_mask(text, size * SCALE, (pen or self.pen) * 0.95, seed=int(self.rng.integers(1e9)), slant=slant)
        self._stamp(a, x * SCALE, y * SCALE - base)
        return a.shape[1] / SCALE  # width in 100dpi units

    def underline(self, x1, x2, y, wavy=False):
        if wavy:
            xs = np.linspace(x1, x2, max(4, int((x2 - x1) / 6)))
            pts = [(xx, y + (2 if i % 2 else -1)) for i, xx in enumerate(xs)]
            self._poly(pts, wob=0.3)
        else:
            self._poly([(x1, y), ((x1 + x2) / 2, y + 0.8), (x2, y - 0.6)], wob=0.4)

    def ellipse(self, cx, cy, rx, ry, overshoot=0.18):
        t0 = self.rng.uniform(0, 2 * np.pi)
        t = np.linspace(t0, t0 + 2 * np.pi * (1 + overshoot), 60)
        rr = 1 + 0.04 * np.sin(3 * t + self.rng.uniform(0, 6))
        pts = [(cx + rx * rr[i] * np.cos(tt), cy + ry * rr[i] * np.sin(tt) + 0.6 * (tt - t0)) for i, tt in enumerate(t)]
        self._poly(pts, wob=0.3)

    def arrow(self, x1, y1, x2, y2, curve=0.0):
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        nx, ny = -(y2 - y1), (x2 - x1)
        cx, cy = mx + nx * curve, my + ny * curve
        t = np.linspace(0, 1, 14)
        pts = [((1 - u) ** 2 * x1 + 2 * (1 - u) * u * cx + u * u * x2, (1 - u) ** 2 * y1 + 2 * (1 - u) * u * cy + u * u * y2) for u in t]
        self._poly(pts, wob=0.3)
        dx, dy = x2 - pts[-3][0], y2 - pts[-3][1]
        L = np.hypot(dx, dy) + 1e-6; dx /= L; dy /= L
        h = 7
        for sgn in (1, -1):
            ang = 0.5 * sgn
            ex = x2 - h * (dx * np.cos(ang) - dy * np.sin(ang))
            ey = y2 - h * (dx * np.sin(ang) + dy * np.cos(ang))
            self._poly([(x2, y2), (ex, ey)], wob=0.1)

    def caret(self, x, y):
        self._poly([(x - 5, y + 2), (x, y - 7), (x + 5, y + 2)], wob=0.15)

    def strike(self, x1, x2, y):
        self._poly([(x1, y + 1), (x2, y - 1)], wob=0.3)

    def score(self, x, y, txt, size=15):
        """circled part mark, centred at x,y"""
        w = self.write(x - 100, -100, txt, size)  # measure only (off page)
        self.write(x - w / 2 - 1, y + size * 0.45, txt, size, pen=self.pen * 1.05)
        self.ellipse(x + 0.5, y - 1, max(w / 2 + 2, size * 0.75), size * 0.85)

    def dot(self, x, y, r=2.2):
        cv2.circle(self.ink_u8 if False else self.ink, (int(x * SCALE), int(y * SCALE)), int(r * SCALE), 1.0, -1, cv2.LINE_AA)

    def render(self):
        a = np.clip(self.ink, 0, 1)[..., None] * 0.93
        out = self.img * (1 - a) + np.minimum(getattr(self,'ink_rgb',INK), self.img) * a
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


_calib = {}
def _measure(text, size):
    """fast width estimate (100-dpi units) calibrated against the real renderer"""
    f = _font(F_MAIN, max(8, int(size * 1.3 * 4)))
    if size not in _calib:
        sample = 'The quick brown fox jumps over the lazy dog 0123456789 = x 10^{-3}'
        a, _ = text_mask(sample, size * SCALE, 2.4, seed=7)
        _calib[size] = (a.shape[1] / SCALE) / max(1.0, f.getlength(sample))
    return f.getlength(text) * _calib[size] * 1.07

def report(page, items, x0=70, y0=90, maxw=690, lh=22):
    """items: list of (kind, text). kinds: 'title','sub','head','para','bullet','gap'"""
    y = y0
    for kind, text in items:
        if kind == 'gap':
            y += lh * 0.6; continue
        size = {'title': 16, 'sub': 11, 'head': 13, 'para': 10.5, 'bullet': 10.5}[kind]
        llh = lh * size / 10.5 if kind in ('title', 'head') else lh
        indent = 22 if kind == 'bullet' else 0
        lead = text
        words = lead.split(' ')
        lines = []; cur = ''
        for w in words:
            cand = (cur + ' ' + w).strip()
            if cur and _measure(cand, size) > maxw - indent:
                lines.append(cur); cur = w
            else:
                cur = cand
        if cur: lines.append(cur)
        for i, ln in enumerate(lines):
            xx = x0 + indent
            if kind == 'bullet' and i == 0:
                page.write(x0 + 4, y, '-', size)
            w = page.write(xx, y, ln, size)
            if kind in ('title', 'head'):
                page.underline(xx, xx + w - 6, y + 5)
            y += llh
        if kind in ('title', 'head'): y += 4
    return y
