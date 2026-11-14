"""Find each question's region of the marking key from the key PDF's own text.

The model's coordinates for these regions proved unreliable, so when the key has "Question N" headings
(the college's own key format) the regions are measured from the PDF text instead. All values are in
100-dpi page units (A4 = 827 x 1169), the same units the renderer uses.
"""
import re, subprocess, html

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


def from_text(pdf, qnums):
    """{q: [[page, y0, y1], ...]} when a "Question N" heading is found for every question, else None."""
    ws = words(pdf)
    if not ws: return None
    heads = {}
    for a, b in zip(ws, ws[1:]):
        if a[5].lower() == 'question' and a[1] < 300 and a[0] == b[0] and abs(a[2] - b[2]) < 6:
            n = re.sub(r'\D', '', b[5])
            if n and int(n) in qnums and int(n) not in heads: heads[int(n)] = (a[0], a[2])
    order = sorted(qnums)
    if any(q not in heads for q in order): return None
    if [heads[q] for q in order] != sorted(heads[q] for q in order): return None     # headings must appear in order
    npages = max(w[0] for w in ws)
    body = {}                                                                        # page -> (top, bottom) of its content
    for p in range(1, npages + 1):
        ys = [(w[2], w[4]) for w in ws if w[0] == p and w[2] > 50 and w[4] < 1110]
        if ys: body[p] = (min(y[0] for y in ys), max(y[1] for y in ys))
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
