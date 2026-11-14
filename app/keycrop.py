"""Find each question's region of the marking key from the key PDF's own text.

The model's coordinates for these regions proved unreliable, so when the key has "Question N" headings
(the college's own key format) the regions are measured from the PDF text instead. All values are in
100-dpi page units (A4 = 827 x 1169), the same units the renderer uses.
"""
import re, subprocess, html, os
import numpy as np
from PIL import Image

K = 100 / 72.0          # PDF points -> 100-dpi units


def words(pdf):
    """[(page, x0, y0, x1, y1, text)] for every word, pages numbered from 1; [] if the PDF has no text."""
    try:
        out = subprocess.run(['pdftotext', '-bbox', pdf, '-'], capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return []
    res = []; pg = 0
    for m in re.finditer(r'<page |<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', out):
        if m.group(0) == '<page ': pg += 1; continue
        res.append((pg, float(m.group(1)) * K, float(m.group(2)) * K, float(m.group(3)) * K, float(m.group(4)) * K, html.unescape(m.group(5))))
    return res


def from_text(pdf, qnums, key_dir=None):
    """{q: [[page, y0, y1], ...]} when a "Question N" heading is found for every question, else None."""
    ws = words(pdf)
    if not ws: return None
    heads = {}
    # a heading is the word "Question" with a bare number just to its right on the same line. Word order in the
    # PDF is not reliable (keys built from pasted mark-scheme pages carry hidden text), so match by position.
    nums = [w for w in ws if re.fullmatch(r'\d{1,2}', w[5])]
    for a in ws:
        if a[5].lower() != 'question' or a[1] > 300: continue
        for b in nums:
            if b[0] == a[0] and abs(b[2] - a[2]) < 6 and 0 <= b[1] - a[3] < 45:
                n = int(b[5])
                if n in qnums and n not in heads: heads[n] = (a[0], min(a[2], b[2]))
                break
    order = sorted(qnums)
    if any(q not in heads for q in order): return None
    if [heads[q] for q in order] != sorted(heads[q] for q in order): return None     # headings must appear in order
    npages = max(w[0] for w in ws)
    body = {}                                                                        # page -> (top, bottom) of its content
    for p in range(1, npages + 1):
        # a running footer ("... Page 1 of 4") is not part of any answer
        foot = [w[2] for w in ws if w[0] == p and w[2] > 1040 and re.fullmatch(r'page', w[5], re.I)]
        limit = (min(foot) - 6) if foot else 1125
        ys = [(w[2], w[4]) for w in ws if w[0] == p and w[2] > 40 and w[4] < limit]
        top = min((y[0] for y in ys), default=None); bot = max((y[1] for y in ys), default=None)
        # answers are often pasted in as pictures, which the text does not see: measure the ink on the page image too
        img = os.path.join(key_dir, f'ms-{p:02d}.png') if key_dir else None
        if img and os.path.exists(img):
            g = np.array(Image.open(img).convert('L').resize((827, 1169)))
            rows = np.where((g[:, 40:787] < 190).sum(axis=1) > 2)[0]
            rows = rows[(rows > 40) & (rows < limit)]
            if len(rows): top, bot = float(rows[0]), float(rows[-1])     # what is visible wins over (possibly hidden) text
        if top is not None: body[p] = (top, bot)
    out = {}
    for i, q in enumerate(order):
        p0, y0 = heads[q]
        p1, y1 = heads[order[i + 1]] if i + 1 < len(order) else (npages, 1e9)
        crops = []
        for p in range(p0, p1 + 1):
            if p not in body: continue
            top = y0 - 8 if p == p0 else body[p][0] - 8
            bot = min(y1 - 8, body[p][1] + 10) if p == p1 else body[p][1] + 10
            if p == p0: bot = max(bot, top) if p != p1 else bot
            if bot - top >= 18: crops.append([p, int(max(0, top)), int(min(1169, bot))])
        if not crops: return None
        out[q] = crops
    return out


def sanitise(qs, npages):
    """Model-given regions: undo a wrong scale and clamp to the page. Returns problems found."""
    ys = [c[2] for x in qs for c in x.get('key_crop') or [] if len(c) == 3]
    scale = 1.0
    if ys and max(ys) > 1200: scale = 1169.0 / max(1754.0, max(ys))      # given in image pixels, not grid units
    bad = []
    for x in qs:
        crops = []
        for c in x.get('key_crop') or []:
            try:
                p, y0, y1 = int(c[0]), max(0, int(c[1] * scale)), min(1169, int(c[2] * scale))
            except Exception:
                continue
            if 1 <= p <= npages and y1 - y0 >= 18: crops.append([p, y0, y1])
        x['key_crop'] = crops
        if not crops: bad.append(x.get('q'))
    return bad
