"""SQLite storage, file paths and password hashing."""
import os, sqlite3, json, hashlib, secrets, time

DATA = os.environ.get('MARKER_DATA', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, 'marker.db')
ROLES = ('admin', 'teacher', 'operator')   # operator = academic team: runs marking, cannot edit marks

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def q(sql, args=(), one=False):
    with db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        if sql.lstrip().upper().startswith('INSERT'):
            return cur.lastrowid
    return (rows[0] if rows else None) if one else rows

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, role TEXT, pw TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY, title TEXT, cfg TEXT, setup_status TEXT DEFAULT 'none',
            setup_error TEXT, created_by INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY, test_id INTEGER, filename TEXT, status TEXT DEFAULT 'uploaded',
            error TEXT, spec TEXT, name TEXT, sid TEXT, total INTEGER, warnings TEXT, tok_in INTEGER DEFAULT 0,
            tok_out INTEGER DEFAULT 0, updated REAL);
        ''')
    # work that was running when the server stopped goes back to the start
    q("UPDATE scripts SET status='uploaded' WHERE status IN ('queued','marking','rendering')")
    q("UPDATE tests SET setup_status='none' WHERE setup_status='running'")
    if not q('SELECT 1 FROM users', one=True):
        email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
        pw = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(9)
        add_user(email, 'Administrator', 'admin', pw)
        print(f'\n*** First run: admin account created.  Email: {email}   Password: {pw}\n', flush=True)

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f'{salt}${h}'

def check_pw(pw, stored):
    salt, h = stored.split('$')
    return secrets.compare_digest(hash_pw(pw, salt), stored)

def add_user(email, name, role, pw):
    assert role in ROLES
    return q('INSERT INTO users(email,name,role,pw) VALUES(?,?,?,?)', (email.strip().lower(), name, role, hash_pw(pw)))

def setting(k, default=None):
    r = q('SELECT v FROM settings WHERE k=?', (k,), one=True)
    return r['v'] if r and r['v'] else default

def set_setting(k, v):
    q('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))

def secret_key():
    p = os.path.join(DATA, 'session.key')
    if not os.path.exists(p):
        with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), 'w') as f: f.write(secrets.token_hex(32))
    return open(p).read().strip()

def test_dir(tid):
    d = os.path.join(DATA, 'tests', str(tid)); os.makedirs(d, exist_ok=True); return d

def script_dir(tid, sid):
    d = os.path.join(test_dir(tid), 'scripts', str(sid)); os.makedirs(d, exist_ok=True); return d

def get_test(tid):
    t = q('SELECT * FROM tests WHERE id=?', (tid,), one=True)
    if not t: return None
    t = dict(t); t['cfg'] = json.loads(t['cfg'] or '{}'); return t

def save_cfg(tid, cfg):
    q('UPDATE tests SET cfg=?, title=? WHERE id=?', (json.dumps(cfg), cfg.get('title', ''), tid))

def get_script(sid):
    s = q('SELECT * FROM scripts WHERE id=?', (sid,), one=True)
    if not s: return None
    s = dict(s); s['spec'] = json.loads(s['spec']) if s['spec'] else None
    s['warnings'] = json.loads(s['warnings']) if s['warnings'] else []
    return s

def set_script(script_id, /, **kw):
    kw['updated'] = time.time()
    for k in ('spec', 'warnings'):
        if k in kw and not isinstance(kw[k], (str, type(None))): kw[k] = json.dumps(kw[k])
    q(f"UPDATE scripts SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), script_id))
