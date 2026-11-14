"""Background work: key setup and the per-script marking pipeline."""
import os, glob, re, shutil, traceback, threading
from concurrent.futures import ThreadPoolExecutor
from . import store, render, marking, keycrop

POOL = ThreadPoolExecutor(max_workers=int(os.environ.get('MARKER_WORKERS', '2')))
_lock = threading.Lock()


def ingest_pdf(src, out_dir, prefix, dpi):
    files = render.rasterise(src, out_dir, dpi, 'tmp')
    out = []
    for i, f in enumerate(files, 1):
        dst = os.path.join(out_dir, f'{prefix}-{i:02d}.png'); os.replace(f, dst); out.append(dst)
    return out


def structure_problems(qs):
    """Problems in the question structure that make marking impossible or unreliable."""
    out = []
    if not qs: return ['No questions could be read from the marking key.']
    ids = [p.get('id') for x in qs for p in x.get('parts', [])]
    if len(ids) != len(set(ids)): out.append('Two parts of the key were read with the same name.')
    for x in qs:
        parts = x.get('parts') or []
        if not parts: out.append(f"Question {x.get('q')}: no parts were read.")
        elif sum(int(p.get('max', 0)) for p in parts) != int(x.get('max', -1)):
            out.append(f"Question {x.get('q')}: the part marks add up to {sum(int(p.get('max', 0)) for p in parts)}, but the question total is {x.get('max')}.")
    return out


def start_marking(tid):
    ids = [r['id'] for r in store.q("SELECT id FROM scripts WHERE test_id=? AND status IN ('uploaded','error') ORDER BY id", (tid,))]
    queue(ids)
    return len(ids)


def run_setup(tid):
    """Read the marking key, check it, and start marking if it is sound."""
    try:
        t = store.get_test(tid); cfg = t['cfg']
        qs, problems, tin, tout = marking.setup_key(tid)
        blocking = structure_problems(qs)
        # where each question sits in the key: measured from the key's text when possible, else the model's estimate
        d = store.test_dir(tid)
        npages = len(glob.glob(os.path.join(d, 'key', 'ms-*.png')))
        try:
            found = keycrop.from_text(os.path.join(d, 'key.pdf'), [int(x['q']) for x in qs], os.path.join(d, 'key')) if not blocking else None
        except Exception:
            traceback.print_exc(); found = None
        if found:
            for x in qs: x['key_crop'] = found[int(x['q'])]
        else:
            missing = keycrop.sanitise(qs, npages)
            if missing: problems.append('The marking-key page could not be located for question(s) ' + ', '.join(str(m) for m in missing) + '; it will be left out of the checked papers.')
        cfg['questions'] = qs; cfg['key_problems'] = blocking + problems; cfg['key_blocked'] = bool(blocking)
        cfg['setup_tok'] = [tin, tout]
        store.save_cfg(tid, cfg)
        render.clear_keypage_cache(os.path.join(store.test_dir(tid), 'key'))
        if cfg['key_problems']:
            store.q("UPDATE tests SET setup_status='review', setup_error=NULL WHERE id=?", (tid,))
        else:
            # queue first, so the page never sees "key accepted" with nothing in progress
            start_marking(tid)
            store.q("UPDATE tests SET setup_status='confirmed', setup_error=NULL WHERE id=?", (tid,))
    except Exception as e:
        if not isinstance(e, marking.MarkingError): traceback.print_exc()
        store.q("UPDATE tests SET setup_status='error', setup_error=? WHERE id=?", (str(e), tid))


def paths(tid, sid):
    d = store.script_dir(tid, sid)
    return {'dir': d, 'src': os.path.join(d, 'src.pdf'), 'out': os.path.join(d, 'marked.pdf'),
            'prev': os.path.join(d, 'preview'), 'p150': os.path.join(d, 'p150'),
            'key': os.path.join(store.test_dir(tid), 'key')}


def rerender(sid):
    """Render the stored spec again (after a mark edit). Returns warnings."""
    s = store.get_script(sid); t = store.get_test(s['test_id']); P = paths(t['id'], sid)
    tot, warns = render.render(s['spec'], t['cfg'], P['src'], P['out'], P['prev'], P['key'])
    store.set_script(sid, total=sum(tot), warnings=warns, status='done', error=None)
    return warns


def mark_one(sid):
    s = store.get_script(sid); tid = s['test_id']; P = paths(tid, sid)
    try:
        cfg = store.get_test(tid)['cfg']
        store.set_script(sid, status='marking', error=None)
        pages = sorted(glob.glob(os.path.join(P['p150'], 'p-*.png'))) or ingest_pdf(P['src'], P['p150'], 'p', 150)
        spec, tin, tout = marking.mark_script(tid, cfg, pages, s['filename'])
        if not spec.get('sid'):
            spec['sid'] = (re.findall(r'\d{6,}', s['filename']) or [''])[0]
        for attempt in range(2):
            store.set_script(sid, status='rendering', spec=spec, name=spec.get('name'), sid=spec.get('sid'), tok_in=tin, tok_out=tout)
            problems = render.validate(spec, cfg)
            warns = []
            if not problems:
                tot, warns = render.render(spec, cfg, P['src'], P['out'], P['prev'], P['key'])
            fixable = problems + [w for w in warns if 'overlaps' in w or 'right edge' in w or 'too long' in w]
            if not fixable or attempt == 1: break
            pg = sorted({int(m.group(1)) for w in fixable for m in [re.match(r'p(\d+):', w)] if m})
            prevs = [(n, f) for n in pg for f in glob.glob(os.path.join(P['prev'], f'm*_s{n:02d}.png'))]
            spec2, a, b = marking.fix_spec(tid, cfg, spec, fixable, prevs)
            tin += a; tout += b
            spec2.setdefault('sid', spec.get('sid')); spec = spec2
        if problems:
            raise marking.MarkingError('The marking came back incomplete: ' + '; '.join(problems[:4]))
        store.set_script(sid, status='done', spec=spec, total=sum(tot), warnings=warns, tok_in=tin, tok_out=tout)
    except marking.MarkingError as e:
        store.set_script(sid, status='error', error=str(e))
    except Exception as e:
        traceback.print_exc()
        store.set_script(sid, status='error', error=f'Unexpected error: {e}')


def queue(sids):
    for sid in sids:
        store.set_script(sid, status='queued', error=None)
        POOL.submit(mark_one, sid)
