"""Deterministic renderer: mark spec + scanned script -> marked PDF, previews and QA warnings.

All coordinates are 100-dpi page units (A4 = 827 x 1169); pages are rendered at 200 dpi.
"""
import os, subprocess, tempfile, shutil, glob, threading
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .redpen import Page, report, _measure

BLUE = np.array([40, 130, 225], np.float32)
W, H = 1654, 2339
RENDER_SLOTS = threading.Semaphore(int(os.environ.get('MARKER_RENDERS', '1')))   # one render at a time keeps memory low
_DJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts') + os.sep


def rasterise(pdf, out_dir, dpi, prefix='p'):
    """PDF -> PNG pages; returns sorted list of paths."""
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run(['pdftoppm', '-r', str(dpi), '-png', pdf, os.path.join(out_dir, prefix)], check=True)
    return sorted(glob.glob(os.path.join(out_dir, prefix + '-*.png')))


def grid_image(path, dpi):
    """Page image with a light-blue coordinate grid labelled in 100-dpi units (for the marking model)."""
    im = Image.open(path).convert('RGB'); d = ImageDraw.Draw(im)
    k = dpi / 100.0
    f = ImageFont.truetype(_DJ + 'DejaVuSans.ttf', int(9 * k))
    for x in range(50, 827, 50):
        d.line([(x * k, 0), (x * k, im.height)], fill=(150, 190, 255), width=1)
        d.text((x * k + 2, 2), str(x), font=f, fill=(30, 90, 220))
    for y in range(50, 1169, 50):
        d.line([(0, y * k), (im.width, y * k)], fill=(150, 190, 255), width=1)
        d.text((2, y * k + 1), str(y), font=f, fill=(30, 90, 220))
    return im


# ---------------------------------------------------------------- marking-key page
_KEY_LOCK = threading.Lock()

def keypage(cfg, qn, key_dir):
    """A4 page showing question qn's region of the marking key, with watermark and footer note."""
    with _KEY_LOCK:       # two scripts rendering at once must not build the same cached page together
        return _keypage(cfg, qn, key_dir)

def _keypage(cfg, qn, key_dir):
    cache = os.path.join(key_dir, f'keypage_{qn}.png')
    if os.path.exists(cache):
        return Image.open(cache).convert('RGB')
    qc = next(x for x in cfg['questions'] if x['q'] == qn)
    parts = []
    for pg, y0, y1 in qc.get('key_crop', []):
        src = Image.open(os.path.join(key_dir, f'ms-{int(pg):02d}.png')).convert('RGB')
        parts.append(src.crop((60, int(y0 * 2), src.width - 60, int(y1 * 2))))
    fb = ImageFont.truetype(_DJ + 'DejaVuSans-Bold.ttf', 46); fs = ImageFont.truetype(_DJ + 'DejaVuSans.ttf', 27)
    red = (200, 30, 40)
    im = Image.new('RGB', (W, H), 'white'); d = ImageDraw.Draw(im)
    d.text((97, 150), f'Marking Key  -  Question {qn}', font=fb, fill=red)
    d.text((97, 215), 'Check your answer against the marking key below to see where you scored and where you lost marks.', font=fs, fill=(90, 90, 90))
    d.line([(97, 270), (1557, 270)], fill=red, width=3)
    if parts:
        c = Image.new('RGB', (max(p.width for p in parts), sum(p.height for p in parts)), 'white'); yy = 0
        for p in parts: c.paste(p, (0, yy)); yy += p.height
        s = min(1460 / c.width, 1760 / c.height)
        c = c.resize((int(c.width * s), int(c.height * s)), Image.LANCZOS)
        im.paste(c, (97, 310))
        d.rectangle([(92, 305), (97 + c.width + 5, 310 + c.height + 5)], outline=(160, 160, 160), width=2)
    f1 = ImageFont.truetype(_DJ + 'DejaVuSans-Bold.ttf', 50); f2 = ImageFont.truetype(_DJ + 'DejaVuSans-Bold.ttf', 33)
    big = Image.new('RGBA', (2600, 700), (0, 0, 0, 0)); bd = ImageDraw.Draw(big); y = 40
    for t, f in [('MARKING KEY - NOT PART OF THE QUESTION PAPER', f1),
                 ('Attached after checking for student reference only:', f2),
                 ('cross-check your corrections and areas of improvement', f2)]:
        w = bd.textlength(t, font=f); bd.text(((2600 - w) / 2, y), t, font=f, fill=(200, 30, 40, 52)); y += f.size + 40
    big = big.rotate(40, expand=True, resample=Image.BICUBIC)
    wm = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    wm.alpha_composite(big, (int(W / 2 - big.width / 2), int(1150 - big.height / 2)))
    im = Image.alpha_composite(im.convert('RGBA'), wm).convert('RGB'); d = ImageDraw.Draw(im)
    note = ('Note: This marking key was not included in the question paper. It has been attached to your checked script '
            'so you can compare your answers, understand where marks were lost, and correct your mistakes.')
    fn = ImageFont.truetype(_DJ + 'DejaVuSans.ttf', 27); ln = ''; ys = 2120
    d.rectangle([(97, 2095), (1557, 2250)], outline=red, width=2)
    for w_ in note.split():
        if d.textlength(ln + ' ' + w_, font=fn) > 1400:
            d.text((120, ys), ln.strip(), font=fn, fill=(120, 30, 35)); ys += 38; ln = ''
        ln += ' ' + w_
    d.text((120, ys), ln.strip(), font=fn, fill=(120, 30, 35))
    im.save(cache + '.tmp.png'); os.replace(cache + '.tmp.png', cache)
    return im


def clear_keypage_cache(key_dir):
    for f in glob.glob(os.path.join(key_dir, 'keypage_*.png')): os.remove(f)


# ---------------------------------------------------------------- spec helpers
def q_totals(spec, cfg):
    """Marks per question, summed from the part scores (the single source of truth)."""
    by = {s['part']: int(s['mark']) for s in spec.get('scores', [])}
    return [sum(by.get(p['id'], 0) for p in qc['parts']) for qc in cfg['questions']]


def validate(spec, cfg):
    """Structural problems that make a spec unusable. Returns a list of strings (empty = fine)."""
    errs = []
    maxes = {p['id']: p['max'] for qc in cfg['questions'] for p in qc['parts']}
    seen = set()
    for s in spec.get('scores', []):
        pid = s.get('part')
        if pid not in maxes: errs.append(f'unknown part {pid!r}'); continue
        if pid in seen: errs.append(f'part {pid} scored twice')
        seen.add(pid)
        if not (0 <= int(s.get('mark', -1)) <= maxes[pid]): errs.append(f'part {pid}: mark {s.get("mark")} outside 0..{maxes[pid]}')
    for pid in maxes:
        if pid not in seen: errs.append(f'part {pid} has no score')
    return errs


def _ink_fraction(gray, x, y, w, sz):
    x0, x1 = int(x * 2), int((x + w) * 2)
    y0, y1 = int((y - sz) * 2), int((y + sz * 0.3) * 2)
    reg = gray[max(0, y0):max(0, y1), max(0, x0):min(x1, gray.shape[1])]
    return float((reg < 140).mean()) if reg.size else 0.0


# ---------------------------------------------------------------- main entry
def render(spec, cfg, src_pdf, out_pdf, preview_dir, key_dir):
    """Returns (totals, warnings). Writes out_pdf and preview PNGs (100 dpi) for every page."""
    with RENDER_SLOTS:
        return _render(spec, cfg, src_pdf, out_pdf, preview_dir, key_dir)


def _render(spec, cfg, src_pdf, out_pdf, preview_dir, key_dir):
    """Pages are drawn one at a time and written to disk, so memory stays small however long the script is."""
    warns = []
    tmp = tempfile.mkdtemp(prefix='mk_')
    try:
        files = rasterise(src_pdf, tmp, 200, 'h')
        npg = len(files)
        seed = abs(hash(str(spec.get('sid')))) % 10000
        acts = {}                                   # page number -> list of functions(Page)
        def on(n, fn): acts.setdefault(int(n), []).append(fn)
        def checked_write(p, n, x, y, text, sz):
            w = _measure(text, sz)
            frac = _ink_fraction(p.img.mean(axis=2), x, y, w, sz)
            if x + w > 815: warns.append(f'p{n}: note runs off the right edge: "{text[:40]}" at ({x},{y})')
            elif frac > 0.03: warns.append(f'p{n}: note overlaps existing writing ({frac:.0%}): "{text[:40]}" at ({x},{y})')
            p.write(x, y, text, sz)

        tot = q_totals(spec, cfg); total = sum(tot); maxes = [qc['max'] for qc in cfg['questions']]
        # cover page: marks table, circled total, short comment
        cov = spec.get('cover') or {}
        def cover(p):
            for (x, y), m in zip(cov.get('cells') or [], tot):
                p.write(x - (6 if m >= 10 else 0), y, str(m), 17)
            if cov.get('total'):
                x, y = cov['total']; p.write(x - (6 if total >= 10 else 0), y, str(total), 19); p.ellipse(x + 8, y - 8, 30, 20)
            if cov.get('note'):
                x, y = cov['note']
                for i, ln in enumerate(spec.get('cover_note', [])[:3]):
                    p.write(x, y + i * 24, ln, 13 if i == 0 else 11)
        on(1, cover)
        # circled part scores
        for sc in spec.get('scores', []):
            if sc.get('page') and 1 <= int(sc['page']) <= npg and sc.get('y') is not None:
                on(sc['page'], lambda p, sc=sc: p.score(sc.get('x', 26), sc['y'], str(sc['mark'])))
            else:
                warns.append(f'part {sc.get("part")}: no page/position for the circled mark')
        # "Qn = x/y" lines
        for qc, m in zip(cfg['questions'], tot):
            pos = (spec.get('qtotal') or {}).get(str(qc['q']))
            if not pos or not 1 <= int(pos[0]) <= npg: continue
            def qline(p, n=int(pos[0]), x=pos[1], y=pos[2], qc=qc, m=m):
                checked_write(p, n, x, y, f"Q{qc['q']} = {m}/{qc['max']}", 13)
                if m == qc['max']: p.write(x + 10, y + 24, 'Excellent!', 11)
            on(pos[0], qline)
        # free ops
        def run_op(p, n, op):
            t, a = op[0], op[1:]
            try:
                if t == 'tick': p.tick(a[0], a[1], a[2] if len(a) > 2 else 1.0)
                elif t == 'cross': p.cross(a[0], a[1], a[2] if len(a) > 2 else 1.0)
                elif t == 'write': checked_write(p, n, a[0], a[1], str(a[2]), min(14, max(8.5, float(a[3]))) if len(a) > 3 else 9.5)
                elif t == 'ellipse': p.ellipse(*a[:4])
                elif t == 'arrow': p.arrow(*a[:5])
                elif t == 'underline': p.underline(*a[:3])
                elif t == 'caret': p.caret(*a[:2])
                elif t == 'strike': p.strike(*a[:3])
                elif t == 'dot': p.dot(*a[:2])
                elif t == 'poly': p._poly(a[0], wob=0.25)
                elif t == 'score': pass   # scores come from spec['scores']
                else: warns.append(f'p{n}: unknown op {t!r} ignored')
            except Exception as e:
                warns.append(f'p{n}: op {op!r} failed: {e}')
        for k, ops in (spec.get('pages') or {}).items():
            n = int(k)
            if not 1 <= n <= npg: warns.append(f'ops given for page {n}, which does not exist'); continue
            for op in ops: on(n, lambda p, n=n, op=op: run_op(p, n, op))

        # examiner's report: on the last page if it is blank, otherwise on an added page
        pct = round(100 * total / max(1, sum(maxes)))
        items = [('title', "Examiner's Report"),
                 ('sub', f"{spec.get('name', '')} ({spec.get('sid', '')})  -  {cfg.get('title', '')}"),
                 ('sub', f"Score: {total} / {sum(maxes)} ({pct}%)  |  " + '  |  '.join(f"Q{qc['q']} {m}/{qc['max']}" for qc, m in zip(cfg['questions'], tot))),
                 ('gap', ''), ('para', spec.get('overall', '')), ('gap', ''), ('head', 'What you did well')]
        items += [('bullet', b) for b in spec.get('good', [])]
        items += [('gap', ''), ('head', 'Areas to improve')] + [('bullet', b) for b in spec.get('improve', [])]
        if spec.get('next'): items += [('gap', ''), ('head', 'Next steps')] + [('bullet', b) for b in spec['next']]
        items += [('gap', ''), ('para', spec.get('closing', ''))]
        items = [(k, t) for k, t in items if k == 'gap' or t]
        def report_on(img_path):
            rp = Page(img_path, seed=seed + 99); rp.ink_rgb = BLUE
            if report(rp, items) > 1130: warns.append('examiner report is too long for one page - shorten it')
            return rp.render()
        last = np.array(Image.open(files[-1]).convert('L'))
        extra = float((last[200:-200, 150:-150] < 120).mean()) > 0.004
        del last
        if extra: warns.append('student wrote on the last page - report placed on an added page')

        ends = {int(qc['end_page']): qc['q'] for qc in cfg['questions'] if qc.get('end_page') and qc.get('key_crop')}
        os.makedirs(preview_dir, exist_ok=True)
        for f in glob.glob(os.path.join(preview_dir, '*.png')): os.remove(f)
        jpgs = []
        def add(im, tag):
            im = im.convert('RGB')
            jp = os.path.join(tmp, f'o{len(jpgs) + 1:03d}.jpg'); im.save(jp, 'JPEG', quality=85); jpgs.append(jp)
            im.resize((827, 1169)).save(os.path.join(preview_dir, f'm{len(jpgs):02d}_{tag}.png'))
        for n in range(1, npg + 1):
            if n in acts:
                pg = Page(files[n - 1], seed=seed + n)
                for fn in acts[n]: fn(pg)
                im = pg.render(); del pg
            else:
                im = Image.open(files[n - 1])
            if n == npg and not extra:              # blue report over the (red-marked) last page
                flat = os.path.join(tmp, 'last.png'); im.save(flat); im = report_on(flat)
            add(im, f's{n:02d}'); del im
            if n in ends: add(keypage(cfg, ends[n], key_dir), f'k{ends[n]:02d}')
        if extra:
            blank = os.path.join(tmp, 'blank.png'); Image.new('RGB', (W, H), 'white').save(blank)
            add(report_on(blank), 'report')
        # assemble the PDF from the JPEG files (embedded as they are, one A4 page each)
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
        c = canvas.Canvas(out_pdf + '.tmp', pagesize=A4)
        for jp in jpgs:
            c.drawImage(jp, 0, 0, width=A4[0], height=A4[1]); c.showPage()
        c.save(); os.replace(out_pdf + '.tmp', out_pdf)
        return tot, warns
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
