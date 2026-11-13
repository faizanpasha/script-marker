"""Answer-Script Marker - web app (FastAPI, server-rendered pages)."""
import os, io, re, json, time, glob, shutil, zipfile, tempfile
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from . import store, render, marking, jobs, export

store.init()
app = FastAPI(title='Answer-Script Marker')
app.add_middleware(SessionMiddleware, secret_key=store.secret_key(), max_age=14 * 86400, same_site='lax',
                   https_only=os.environ.get('MARKER_HTTPS') == '1')
T = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))
ROLE_LABEL = {'admin': 'Administrator', 'teacher': 'Teacher', 'operator': 'Academic team'}


# ------------------------------------------------------------------ helpers
class Redirect(Exception):
    def __init__(self, url): self.url = url

@app.exception_handler(Redirect)
async def _redir(request, exc):
    return RedirectResponse(exc.url, 303)

def user(request, *roles):
    uid = request.session.get('uid')
    u = store.q('SELECT * FROM users WHERE id=? AND active=1', (uid,), one=True) if uid else None
    if not u: raise Redirect('/login')
    if roles and u['role'] not in roles: raise HTTPException(403, 'You do not have permission to do this.')
    return dict(u)

def page(request, name, **ctx):
    ctx.setdefault('msg', request.session.pop('msg', None))
    ctx['ROLE_LABEL'] = ROLE_LABEL
    return T.TemplateResponse(request, name, ctx)

def flash(request, text):
    request.session['msg'] = text

def the_test(tid):
    t = store.get_test(tid)
    if not t: raise HTTPException(404, 'Test not found')
    return t

def the_script(sid):
    s = store.get_script(sid)
    if not s: raise HTTPException(404, 'Script not found')
    return s

def save_upload(up, dst):
    with open(dst, 'wb') as f: shutil.copyfileobj(up.file, f)

def is_pdf(path):
    with open(path, 'rb') as f: return f.read(5) == b'%PDF-'


# ------------------------------------------------------------------ login
@app.get('/login', response_class=HTMLResponse)
def login_form(request: Request):
    return page(request, 'login.html', u=None)

@app.post('/login')
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    u = store.q('SELECT * FROM users WHERE email=? AND active=1', (email.strip().lower(),), one=True)
    if not u or not store.check_pw(password, u['pw']):
        time.sleep(1); flash(request, 'Wrong email or password.'); return RedirectResponse('/login', 303)
    request.session['uid'] = u['id']
    return RedirectResponse('/', 303)

@app.get('/logout')
def logout(request: Request):
    request.session.clear(); return RedirectResponse('/login', 303)


# ------------------------------------------------------------------ tests
@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    u = user(request)
    tests = store.q('''SELECT t.*, (SELECT COUNT(*) FROM scripts s WHERE s.test_id=t.id) n,
                       (SELECT COUNT(*) FROM scripts s WHERE s.test_id=t.id AND s.status='done') done
                       FROM tests t ORDER BY t.id DESC''')
    return page(request, 'tests.html', u=u, tests=tests, has_key=bool(marking.api_key()) or marking.MOCK)

@app.post('/tests')
def create_test(request: Request, title: str = Form(...), teacher: str = Form(''), section: str = Form(''),
                date: str = Form(''), key: UploadFile = File(...), qp: UploadFile = File(None)):
    u = user(request)
    cfg = {'title': title.strip(), 'subject': 'Physics 9702', 'teacher': teacher.strip(), 'section': section.strip(),
           'date': date.strip(), 'college': store.setting('college', 'Heuser College for A-Levels'),
           'questions': [], 'policy': marking.DEFAULT_POLICY, 'precedents': ''}
    tid = store.q('INSERT INTO tests(title,cfg,created_by,created) VALUES(?,?,?,?)', (cfg['title'], json.dumps(cfg), u['id'], time.time()))
    d = store.test_dir(tid)
    try:
        save_upload(key, os.path.join(d, 'key.pdf'))
        if not is_pdf(os.path.join(d, 'key.pdf')): raise ValueError('The marking key must be a PDF file.')
        jobs.ingest_pdf(os.path.join(d, 'key.pdf'), os.path.join(d, 'key'), 'ms', 200)
        if qp is not None and qp.filename:
            save_upload(qp, os.path.join(d, 'qp.pdf'))
            if not is_pdf(os.path.join(d, 'qp.pdf')): raise ValueError('The question paper must be a PDF file.')
            jobs.ingest_pdf(os.path.join(d, 'qp.pdf'), os.path.join(d, 'qp'), 'p', 100)
    except Exception as e:
        store.q('DELETE FROM tests WHERE id=?', (tid,)); shutil.rmtree(d, ignore_errors=True)
        flash(request, f'Could not create the test: {e}'); return RedirectResponse('/', 303)
    store.q("UPDATE tests SET setup_status='running' WHERE id=?", (tid,))
    jobs.POOL.submit(jobs.run_setup, tid)
    return RedirectResponse(f'/tests/{tid}', 303)

def script_rows(t):
    cfg = t['cfg']; rows = []; seen = {}
    for r in store.q('SELECT id FROM scripts WHERE test_id=? ORDER BY id', (t['id'],)):
        s = store.get_script(r['id']); flags = []
        if s['status'] == 'done' and s['spec']:
            s['q'] = render.q_totals(s['spec'], cfg)
            flags += [f"{f.get('part', '')}: {f.get('text', '')}" for f in s['spec'].get('flags', [])]
            flags += s['warnings']
            fid = re.findall(r'\d{6,}', s['filename'])
            if not s['sid']: flags.append('No student ID found')
            elif fid and s['sid'] not in fid: flags.append(f"ID on cover ({s['sid']}) differs from file name ({fid[0]})")
            if s['sid']: seen.setdefault(s['sid'], []).append(s)
        s['flags'] = flags; rows.append(s)
    for sid, ss in seen.items():
        if len(ss) > 1:
            for s in ss: s['flags'].append(f'Duplicate student ID {sid}')
    rows.sort(key=lambda s: (s['name'] or s['filename']).lower())
    return rows

@app.get('/tests/{tid}', response_class=HTMLResponse)
def test_page(request: Request, tid: int):
    u = user(request); t = the_test(tid); rows = script_rows(t)
    counts = {}
    for s in rows: counts[s['status']] = counts.get(s['status'], 0) + 1
    busy = sum(counts.get(k, 0) for k in ('queued', 'marking', 'rendering'))
    tin = sum(s['tok_in'] or 0 for s in rows); tout = sum(s['tok_out'] or 0 for s in rows)
    return page(request, 'test.html', u=u, t=t, cfg=t['cfg'], rows=rows, counts=counts, busy=busy,
                total_max=sum(x['max'] for x in t['cfg']['questions']), cost=marking.cost_usd(tin, tout),
                can_edit=u['role'] in ('admin', 'teacher'), model=marking.model())

@app.get('/tests/{tid}/status.json')
def test_status(request: Request, tid: int):
    user(request); t = the_test(tid)
    c = {r['status']: r['n'] for r in store.q('SELECT status, COUNT(*) n FROM scripts WHERE test_id=? GROUP BY status', (tid,))}
    return JSONResponse({'setup': t['setup_status'], 'counts': c})

@app.post('/tests/{tid}/setup/auto')
def setup_auto(request: Request, tid: int):
    user(request); the_test(tid)
    store.q("UPDATE tests SET setup_status='running', setup_error=NULL WHERE id=?", (tid,))
    jobs.POOL.submit(jobs.run_setup, tid)
    return RedirectResponse(f'/tests/{tid}', 303)

@app.post('/tests/{tid}/setup')
async def setup_save(request: Request, tid: int):
    user(request); t = the_test(tid); cfg = t['cfg']; f = await request.form()
    qs = []; errs = []
    for i in range(int(f.get('rows', 0))):
        qn = (f.get(f'q{i}') or '').strip()
        if not qn: continue
        try:
            parts = []
            for chunk in re.split(r'[,;\n]+', f.get(f'parts{i}', '')):
                if not chunk.strip(): continue
                pid, mx = chunk.rsplit(':', 1); parts.append({'id': pid.strip(), 'max': int(mx)})
            crops = []
            for chunk in re.split(r'[,;\n]+', f.get(f'crop{i}', '')):
                if not chunk.strip(): continue
                pg, rng = chunk.split(':'); y0, y1 = rng.split('-'); crops.append([int(pg), int(float(y0)), int(float(y1))])
            end = (f.get(f'end{i}') or '').strip()
            qd = {'q': int(qn), 'max': int(f.get(f'max{i}')), 'end_page': int(end) if end else None, 'key_crop': crops, 'parts': parts}
            if not parts: qd['parts'] = [{'id': str(qd['q']), 'max': qd['max']}]
            if sum(p['max'] for p in qd['parts']) != qd['max']:
                errs.append(f"Q{qn}: the part marks add up to {sum(p['max'] for p in qd['parts'])}, not {qd['max']}")
            qs.append(qd)
        except Exception:
            errs.append(f'Q{qn}: could not read this row - check the format')
    ids = [p['id'] for x in qs for p in x['parts']]
    if len(ids) != len(set(ids)): errs.append('Two parts have the same name')
    cfg['questions'] = sorted(qs, key=lambda x: x['q'])
    cfg['policy'] = f.get('policy', cfg.get('policy', '')); cfg['precedents'] = f.get('precedents', cfg.get('precedents', ''))
    for k in ('title', 'teacher', 'section', 'date'):
        if f.get(k) is not None: cfg[k] = f.get(k).strip()
    store.save_cfg(tid, cfg)
    render.clear_keypage_cache(os.path.join(store.test_dir(tid), 'key'))
    ok = bool(qs) and not errs
    store.q('UPDATE tests SET setup_status=? WHERE id=?', ('confirmed' if ok else 'review', tid))
    flash(request, 'Marking key saved and confirmed.' if ok else 'Saved, but please fix: ' + '; '.join(errs or ['add at least one question']))
    return RedirectResponse(f'/tests/{tid}', 303)

@app.post('/tests/{tid}/precedents')
def save_precedents(request: Request, tid: int, precedents: str = Form('')):
    user(request, 'admin', 'teacher'); t = the_test(tid)
    t['cfg']['precedents'] = precedents; store.save_cfg(tid, t['cfg'])
    flash(request, 'Marking rules saved. They apply to scripts marked from now on; use "Mark again" on scripts already done.')
    return RedirectResponse(f'/tests/{tid}', 303)

@app.get('/tests/{tid}/keypage/{qn}.png')
def keypage_png(request: Request, tid: int, qn: int):
    user(request); t = the_test(tid)
    try:
        im = render.keypage(t['cfg'], qn, os.path.join(store.test_dir(tid), 'key'))
    except Exception:
        raise HTTPException(404)
    b = io.BytesIO(); im.resize((827, 1169)).save(b, 'PNG')
    return Response(b.getvalue(), media_type='image/png', headers={'Cache-Control': 'no-store'})

@app.get('/tests/{tid}/keyimg/{n}.png')
def keyimg(request: Request, tid: int, n: int):
    user(request)
    f = os.path.join(store.test_dir(tid), 'key', f'ms-{n:02d}.png')
    if not os.path.exists(f): raise HTTPException(404)
    b = io.BytesIO(); render.grid_image(f, 200).resize((827, 1169)).save(b, 'PNG')
    return Response(b.getvalue(), media_type='image/png')

@app.post('/tests/{tid}/upload')
def upload_scripts(request: Request, tid: int, files: list[UploadFile] = File(...)):
    user(request); the_test(tid); added = 0; skipped = []
    existing = {r['filename'] for r in store.q('SELECT filename FROM scripts WHERE test_id=?', (tid,))}
    def add_pdf(name, fsrc):
        nonlocal added
        name = os.path.basename(name.replace('\\', '/'))
        if name in existing: skipped.append(f'{name} (already uploaded)'); return
        sid = store.q('INSERT INTO scripts(test_id,filename,updated) VALUES(?,?,?)', (tid, name, time.time()))
        dst = os.path.join(store.script_dir(tid, sid), 'src.pdf')
        with open(dst, 'wb') as f: shutil.copyfileobj(fsrc, f)
        if not is_pdf(dst):
            store.q('DELETE FROM scripts WHERE id=?', (sid,)); shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
            skipped.append(f'{name} (not a PDF)'); return
        existing.add(name); added += 1
    for up in files:
        fn = up.filename or ''
        if fn.lower().endswith('.zip'):
            with tempfile.TemporaryFile() as tf:
                shutil.copyfileobj(up.file, tf); tf.seek(0)
                try:
                    with zipfile.ZipFile(tf) as z:
                        for zi in z.infolist():
                            base = os.path.basename(zi.filename)
                            if zi.is_dir() or '__MACOSX' in zi.filename or base.startswith('.') or not base.lower().endswith('.pdf'): continue
                            with z.open(zi) as src: add_pdf(base, src)
                except zipfile.BadZipFile:
                    skipped.append(f'{fn} (not a valid zip)')
        elif fn.lower().endswith('.pdf'):
            add_pdf(fn, up.file)
        elif fn:
            skipped.append(f'{fn} (only PDF or zip)')
    flash(request, f'{added} script(s) added.' + (' Skipped: ' + ', '.join(skipped[:8]) if skipped else ''))
    return RedirectResponse(f'/tests/{tid}', 303)

@app.post('/tests/{tid}/mark')
def mark(request: Request, tid: int, mode: str = Form('all'), n: int = Form(2)):
    user(request); t = the_test(tid)
    if t['setup_status'] != 'confirmed':
        flash(request, 'Confirm the marking key first (step 1).'); return RedirectResponse(f'/tests/{tid}', 303)
    if not (marking.api_key() or marking.MOCK):
        flash(request, 'No Anthropic API key is set. Ask the administrator to add it under Settings.'); return RedirectResponse(f'/tests/{tid}', 303)
    ids = [r['id'] for r in store.q("SELECT id FROM scripts WHERE test_id=? AND status IN ('uploaded','error') ORDER BY id", (tid,))]
    if mode == 'sample': ids = ids[:max(1, min(n, 5))]
    jobs.queue(ids)
    flash(request, f'Marking started for {len(ids)} script(s). This page updates by itself.' if ids else 'Nothing left to mark.')
    return RedirectResponse(f'/tests/{tid}', 303)

@app.post('/tests/{tid}/delete')
def delete_test(request: Request, tid: int):
    user(request, 'admin'); the_test(tid)
    store.q('DELETE FROM scripts WHERE test_id=?', (tid,)); store.q('DELETE FROM tests WHERE id=?', (tid,))
    shutil.rmtree(store.test_dir(tid), ignore_errors=True)
    return RedirectResponse('/', 303)


# ------------------------------------------------------------------ export
def _dl(data, name, mt):
    return Response(data, media_type=mt, headers={'Content-Disposition': f'attachment; filename="{export.safe(name)}"'})

@app.get('/tests/{tid}/sheet.pdf')
def dl_pdf(request: Request, tid: int):
    user(request); t = the_test(tid)
    return _dl(export.sheet_pdf(t), f"{t['title']} - Marks Sheet.pdf", 'application/pdf')

@app.get('/tests/{tid}/sheet.xlsx')
def dl_xlsx(request: Request, tid: int):
    user(request); t = the_test(tid)
    return _dl(export.sheet_xlsx(t), f"{t['title']} - Marks Sheet.xlsx", 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get('/tests/{tid}/marked.zip')
def dl_zip(request: Request, tid: int):
    user(request); t = the_test(tid)
    p = export.build_zip(t, os.path.join(store.test_dir(tid), 'export.zip'))
    return FileResponse(p, filename=export.safe(f"{t['title']} - Marked Scripts.zip"), media_type='application/zip')


# ------------------------------------------------------------------ one script
@app.get('/scripts/{sid}', response_class=HTMLResponse)
def script_page(request: Request, sid: int):
    u = user(request); s = the_script(sid); t = the_test(s['test_id'])
    prev = sorted(os.path.basename(f) for f in glob.glob(os.path.join(jobs.paths(t['id'], sid)['prev'], 'm*.png')))
    marks = {x['part']: x['mark'] for x in (s['spec'] or {}).get('scores', [])}
    return page(request, 'script.html', u=u, s=s, t=t, cfg=t['cfg'], prev=prev, marks=marks,
                q=render.q_totals(s['spec'], t['cfg']) if s['spec'] else [], can_edit=u['role'] in ('admin', 'teacher'),
                v=int(s['updated'] or 0))

@app.get('/scripts/{sid}/preview/{name}')
def script_preview(request: Request, sid: int, name: str):
    user(request); s = the_script(sid)
    if not re.fullmatch(r'm\d\d_[a-z0-9]+\.png', name): raise HTTPException(404)
    f = os.path.join(jobs.paths(s['test_id'], sid)['prev'], name)
    if not os.path.exists(f): raise HTTPException(404)
    return FileResponse(f, headers={'Cache-Control': 'no-store'})

@app.get('/scripts/{sid}/marked.pdf')
def script_pdf(request: Request, sid: int):
    user(request); s = the_script(sid); f = jobs.paths(s['test_id'], sid)['out']
    if not os.path.exists(f): raise HTTPException(404)
    return FileResponse(f, filename=export.safe(f"{s['name']} {s['sid']} - Marked.pdf"), media_type='application/pdf')

@app.get('/scripts/{sid}/original.pdf')
def script_src(request: Request, sid: int):
    user(request); s = the_script(sid)
    return FileResponse(jobs.paths(s['test_id'], sid)['src'], media_type='application/pdf')

@app.post('/scripts/{sid}/marks')
async def edit_marks(request: Request, sid: int):
    user(request, 'admin', 'teacher'); s = the_script(sid); t = the_test(s['test_id']); f = await request.form()
    spec = s['spec']
    if not spec: raise HTTPException(400, 'This script has not been marked yet.')
    maxes = {p['id']: p['max'] for qc in t['cfg']['questions'] for p in qc['parts']}
    for sc in spec['scores']:
        v = f.get('m_' + sc['part'])
        if v is not None and v != '': sc['mark'] = max(0, min(maxes.get(sc['part'], 99), int(v)))
    spec['name'] = (f.get('name') or spec.get('name', '')).strip(); spec['sid'] = (f.get('sid') or '').strip()
    store.set_script(sid, spec=spec, name=spec['name'], sid=spec['sid'], status='rendering')
    jobs.POOL.submit(jobs.rerender, sid)
    flash(request, 'Saved. The marked PDF is being redrawn - refresh in a few seconds.')
    return RedirectResponse(f'/scripts/{sid}', 303)

@app.post('/scripts/{sid}/remark')
def remark(request: Request, sid: int):
    user(request); s = the_script(sid)
    if s['status'] in ('queued', 'marking', 'rendering'):
        flash(request, 'This script is already being marked.')
    else:
        jobs.queue([sid]); flash(request, 'Marking this script again with the current rules.')
    return RedirectResponse(f"/tests/{s['test_id']}", 303)

@app.post('/scripts/{sid}/delete')
def delete_script(request: Request, sid: int):
    user(request); s = the_script(sid)
    store.q('DELETE FROM scripts WHERE id=?', (sid,)); shutil.rmtree(jobs.paths(s['test_id'], sid)['dir'], ignore_errors=True)
    return RedirectResponse(f"/tests/{s['test_id']}", 303)


# ------------------------------------------------------------------ admin
@app.get('/admin', response_class=HTMLResponse)
def admin(request: Request):
    u = user(request, 'admin')
    tot = store.q('SELECT COALESCE(SUM(tok_in),0) a, COALESCE(SUM(tok_out),0) b, COUNT(*) n FROM scripts WHERE tok_in>0', one=True)
    return page(request, 'admin.html', u=u, users=store.q('SELECT * FROM users ORDER BY id'), roles=store.ROLES,
                key_set=bool(store.setting('anthropic_api_key')), env_key=bool(os.environ.get('ANTHROPIC_API_KEY')),
                model=marking.model(), models=list(marking.PRICES), college=store.setting('college', 'Heuser College for A-Levels'),
                usage=dict(tot), cost=marking.cost_usd(tot['a'], tot['b']))

@app.post('/admin/users')
def admin_add_user(request: Request, email: str = Form(...), name: str = Form(...), role: str = Form(...), password: str = Form(...)):
    user(request, 'admin')
    if role not in store.ROLES or len(password) < 8:
        flash(request, 'Choose a role and a password of at least 8 characters.')
    elif store.q('SELECT 1 FROM users WHERE email=?', (email.strip().lower(),), one=True):
        flash(request, 'That email already has an account.')
    else:
        store.add_user(email, name.strip(), role, password); flash(request, f'Account created for {email}.')
    return RedirectResponse('/admin', 303)

@app.post('/admin/users/{uid}')
def admin_edit_user(request: Request, uid: int, action: str = Form(...), password: str = Form('')):
    me = user(request, 'admin')
    if action == 'toggle':
        if uid == me['id']: flash(request, 'You cannot disable your own account.')
        else: store.q('UPDATE users SET active=1-active WHERE id=?', (uid,))
    elif action == 'password':
        if len(password) < 8: flash(request, 'Password must be at least 8 characters.')
        else: store.q('UPDATE users SET pw=? WHERE id=?', (store.hash_pw(password), uid)); flash(request, 'Password changed.')
    return RedirectResponse('/admin', 303)

@app.post('/admin/settings')
def admin_settings(request: Request, api_key: str = Form(''), model: str = Form(''), college: str = Form('')):
    user(request, 'admin')
    if api_key.strip(): store.set_setting('anthropic_api_key', api_key.strip())
    if model in marking.PRICES: store.set_setting('model', model)
    if college.strip(): store.set_setting('college', college.strip())
    flash(request, 'Settings saved.')
    return RedirectResponse('/admin', 303)

@app.post('/account/password')
def own_password(request: Request, old: str = Form(...), new: str = Form(...)):
    u = user(request)
    if not store.check_pw(old, u['pw']): flash(request, 'Current password is wrong.')
    elif len(new) < 8: flash(request, 'New password must be at least 8 characters.')
    else: store.q('UPDATE users SET pw=? WHERE id=?', (store.hash_pw(new), u['id'])); flash(request, 'Password changed.')
    return RedirectResponse('/', 303)
