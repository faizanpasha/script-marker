"""Claude API calls: read the marking key (setup) and mark one script (-> mark spec JSON)."""
import os, io, json, base64, re, glob, random
import anthropic
from . import store, render

DEFAULT_MODEL = 'claude-opus-5-5'
MOCK = os.environ.get('MARKER_MOCK') == '1'      # testing only: fake marks, no API calls
# USD per million tokens (input, output) - used only for the cost estimate shown to the admin
PRICES = {'claude-opus-5-5': (4, 20), 'claude-fable-5-2': (8, 40), 'claude-sonnet-5-5': (2, 10), 'claude-haiku-5-5': (0.1, 0.5)}

DEFAULT_POLICY = """1. Mark strictly against the key (B1/C1/M1/A1 points). Deduct ONLY when a key point is genuinely missing or wrong.
2. Graphs and sketches: lenient - no deduction if within half a small square of the key values. Deduct only when a bold key requirement is violated (wrong shape, touches the axis when it must not, misses required points beyond tolerance). When a sketch is wrong or blank, draw the correct curve in red on the student's axes (poly op).
3. Minor slips (sign on a magnitude or ratio, sig figs, unit text, loose wording with clear meaning) lose NO marks: write a short note starting "Minor (no marks lost): ...".
4. Crossed-out work is ignored if it was replaced; mark the final answer. If the crossed-out work was right, say so in a note.
5. Correct working but wrong answer on the answer line earns the method / C1 marks only.
6. Error carried forward: method marks are allowed when a later part correctly uses the student's own wrong earlier value.
7. Answers that the question itself rules out score 0.
8. Contradictory answers (two curves, two conflicting statements): the mark is lost; add a flag for the teacher.
9. "Consistency" marks in the key are awarded when the structure is right even if the values are wrong.
10. Ignore doodles or remarks by the student; never comment on them."""


class MarkingError(Exception):
    pass


def api_key():
    return store.setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')


def model():
    return store.setting('model', DEFAULT_MODEL)


def cost_usd(tok_in, tok_out, m=None):
    pi, po = PRICES.get(m or model(), (0, 0))
    return (tok_in * pi + tok_out * po) / 1e6


def _img(im, quality=80):
    b = io.BytesIO(); im.convert('RGB').save(b, 'JPEG', quality=quality)
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                        'data': base64.standard_b64encode(b.getvalue()).decode()}}


def _txt(t):
    return {'type': 'text', 'text': t}


def _call(system, content):
    """One streamed request. Returns (text, input_tokens, output_tokens)."""
    key = api_key()
    if not key:
        raise MarkingError('No Anthropic API key is set. Ask the administrator to add it under Settings.')
    client = anthropic.Anthropic(api_key=key, max_retries=4)
    try:
        with client.beta.messages.stream(
            model=model(), max_tokens=64000,
            betas=['server-side-fallback-2026-07-01'], fallbacks='default',
            thinking={'type': 'adaptive'}, output_config={'effort': 'high'},
            system=system, messages=[{'role': 'user', 'content': content}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise MarkingError('The Anthropic API key was rejected. Ask the administrator to check it under Settings.')
    except anthropic.RateLimitError:
        raise MarkingError('The API rate limit was reached. Wait a few minutes and press "Mark" again - finished scripts are kept.')
    except anthropic.APIStatusError as e:
        raise MarkingError(f'API error {e.status_code}: {e.message}')
    except anthropic.APIConnectionError:
        raise MarkingError('Could not reach the Anthropic API (network problem). Try again.')
    if msg.stop_reason == 'refusal':
        raise MarkingError('The model declined this request.')
    if msg.stop_reason == 'max_tokens':
        raise MarkingError('The model output was cut off (too long).')
    text = ''.join(b.text for b in msg.content if b.type == 'text')
    u = msg.usage
    tin = (u.input_tokens or 0) + (getattr(u, 'cache_read_input_tokens', 0) or 0) + (getattr(u, 'cache_creation_input_tokens', 0) or 0)
    return text, tin, u.output_tokens or 0


def _json(text):
    m = re.search(r'```json\s*(.*?)```', text, re.S)
    raw = m.group(1) if m else text[text.find('{'):text.rfind('}') + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        raise MarkingError(f'The model did not return valid JSON ({e}).')


def _key_blocks(tid):
    """Marking-key pages (with grid) - identical for every script of a test, so they are cached."""
    kd = os.path.join(store.test_dir(tid), 'key')
    blocks = []
    for i, f in enumerate(sorted(glob.glob(os.path.join(kd, 'ms-*.png'))), 1):
        blocks += [_txt(f'MARKING KEY page {i}:'), _img(render.grid_image(f, 200).resize((1240, 1754)))]
    return blocks


# ------------------------------------------------------------------ key setup
SETUP_SYSTEM = """You set up an exam-marking tool. You are given the marking key pages of one test and (if available) the blank question paper pages. Every image has a light-blue coordinate grid labelled in page units (x 0-827, y 0-1169).

Return ONE JSON object in a ```json block:
{"questions": [
   {"q": 1, "max": 7, "end_page": 4,
    "key_crop": [[key_page, y0, y1]],
    "parts": [{"id": "1(a)", "max": 2}, {"id": "1(b)", "max": 3}]}
 ],
 "problems": ["..."]}

- "q": question number; "max": its total marks; the part maxima must add up to it. A question without parts has one part whose id is the question number, e.g. "3".
- "parts": the smallest units that carry their own [n] marks tag in the paper, e.g. "2(b)(i)".
- "end_page": the page of the QUESTION PAPER / student script on which the question ends (the cover is page 1). If no question paper is given, use null.
- "key_crop": the region(s) of the marking key that belong to this question, as [key page number, top y, bottom y] in grid units. Include the question's heading and all its rows, with ~10 units of margin, without touching the next question. A question that continues onto the next key page has two entries.
Read the marks from the key itself; do not guess.

"problems": check the key before it is used and list every real problem in plain, short English (one sentence each), for example: marks in the key that do not add up to the question total; a question or part in the question paper with no answer in the key (or the reverse); the key and the question paper being from different tests; mark totals that differ between key and paper; pages that are unreadable or cut off; an answer in the key that is clearly wrong physics or a calculation whose numbers do not work out. Do not list style remarks or things that are fine. Use [] when the key is sound."""


def setup_key(tid):
    """Ask the model to split the key into questions. Returns (questions, notes, tok_in, tok_out)."""
    d = store.test_dir(tid)
    if MOCK:
        n = len(glob.glob(os.path.join(d, 'key', 'ms-*.png')))
        qs = [{'q': i + 1, 'max': 4, 'end_page': 2 + i, 'key_crop': [[1, 60 + 200 * i, 250 + 200 * i]],
               'parts': [{'id': f'{i + 1}(a)', 'max': 2}, {'id': f'{i + 1}(b)', 'max': 2}]} for i in range(3)]
        return qs, ([] if os.environ.get('MARKER_MOCK_PROBLEM') != '1' else ['MOCK problem: Q2 marks do not add up.']), 0, 0
    content = _key_blocks(tid)
    qp = sorted(glob.glob(os.path.join(d, 'qp', 'p-*.png')))
    for i, f in enumerate(qp, 1):
        content += [_txt(f'QUESTION PAPER page {i}:'), _img(render.grid_image(f, 100))]
    content.append(_txt('Return the JSON now.' if qp else 'No question paper was uploaded. Return the JSON now.'))
    text, tin, tout = _call(SETUP_SYSTEM, content)
    j = _json(text)
    return j.get('questions', []), [str(x) for x in j.get('problems', []) if x], tin, tout


# ------------------------------------------------------------------ marking
MARK_SYSTEM = """You are an experienced A-Level Physics examiner (CAIE 9702) marking a scanned student answer script for a teacher. You produce a JSON "mark spec"; a program then draws your marks on the scan in red pen so the script looks hand-checked. Accuracy and consistency of marks matter most; clean placement of annotations matters next.

## What you receive
1. The marking key pages (authoritative).
2. The test structure: questions, parts and maximum marks.
3. The marking policy and the teacher's precedents for this test. Follow them exactly so every student is marked to the same standard.
4. Every page of the student's script. Each page image has a light-blue coordinate grid labelled every 50 units; ALL coordinates you output use this system (x 0-827 left to right, y 0-1169 top to bottom). The grid is not on the real page.

## Output: one JSON object in a ```json block
{
 "name": "Full Name",            // Title Case, exactly as written in the cover name boxes; ignore crossed-out letters
 "sid": "2027010465",            // student ID from the cover boxes; "" if unreadable
 "cover": {"cells": [[x, y], ...], // one per question in order: where to write that question's mark in the cover "Examiner's Use" table (x = left of the number, y = baseline). [] if the cover has no table
           "total": [x, y],        // where to write the total (null if no total cell)
           "note": [x, y]},        // top-left of a blank area on the cover for a 3-line comment (null if none)
 "cover_note": ["Good work, Aabia.", "Strong thermal physics - revise", "fields inside a sphere."],
 "scores": [{"part": "1(a)", "mark": 2, "page": 3, "x": 26, "y": 215}, ...],   // EXACTLY one entry for EVERY part in the test structure
 "qtotal": {"1": [page, x, y], ...},  // for each question: a blank spot on its last page where "Q1 = 5/7" will be written (text is ~110 wide, 13 high; leave 30 units clear below)
 "pages": {"3": [op, op, ...], ...},  // annotation ops per script page
 "flags": [{"part": "5(b)", "type": "borderline", "text": "..."}],   // things the teacher should look at; [] if none
 "overall": "1-2 sentences",
 "good": ["3-5 specific bullets citing question numbers"],
 "improve": ["3-6 bullets with the correct physics; minor no-penalty slips last as 'Minor (no marks lost): ...'"],
 "next": ["1-2 next steps"],
 "closing": "one encouraging line"
}

## Ops
["tick", x, y, scale]      (x, y) = bottom point of the tick; scale 0.5-0.8. One at the END of every creditworthy point.
["cross", x, y, scale]     on every wrong answer / answer line.
["write", x, y, "text", size]   x = left edge, y = baseline. Notes: size 9.5 (never smaller than 9 - small writing is hard for students to read). Code labels such as "C1 M1 A0": size 9.
["ellipse", cx, cy, rx, ry]   ["arrow", x1, y1, x2, y2, curve]   ["underline", x1, x2, y]   ["caret", x, y]   ["strike", x1, x2, y]
["poly", [[x, y], ...]]    freehand stroke, e.g. the correct curve on the student's axes; compute the points from the physics and the axes scale you read off the grid.
Text supports ^{..} and _{..} and the symbols ω π φ Δ ∝ √ ½ ² λ θ μ Ω.

## Rules for annotations
- "scores": the circled part mark goes in the LEFT margin (x = 26) level with that part's printed [n] marks tag; use x = 800 if the student wrote in the left margin there, or x = 44 if the scan has a dark left edge. A blank part gets mark 0 and a ["write", ..., "not attempted", 9.5] in its answer space.
- Do NOT put "score" ops or "Qn = x/y" text in "pages": the program draws them from "scores" and "qtotal".
- Every deduction gets a short, specific red note that gives the correct physics, value or method (for calculations show the key working in 2-4 lines). Label the mark codes near multi-mark parts.
- Notes go ONLY in genuinely blank space - never over printed text, the student's handwriting, diagrams or graphs - and must stay within x 60-800. A size-9.5 note is about 5.3 units wide per character and needs 14 units of clear height; split long notes into several lines 24 units apart (at most ~110 characters per line when starting at x = 110). The bottom of a page or the space after the last answer line is usually the safest place.
- Do not write anything on the last page if it is blank: the examiner's report goes there.
- The examiner's report must fit on one page: keep bullets to one or two lines each.
- Be honest and precise. If handwriting is unreadable or a call is borderline, decide using the policy and add a flag.
"""


def _structure(cfg):
    lines = []
    for qc in cfg['questions']:
        lines.append(f"Q{qc['q']} (max {qc['max']}, ends on script page {qc.get('end_page')}): "
                     + ', '.join(f"{p['id']} [{p['max']}]" for p in qc['parts']))
    return '\n'.join(lines)


def _mock_spec(cfg, pages, filename):
    rnd = random.Random(filename)
    npg = len(pages); scores = []; ops = {}; qtotal = {}
    for qc in cfg['questions']:
        pg = min(max(2, int(qc.get('end_page') or 2)), npg)
        for i, p in enumerate(qc['parts']):
            m = rnd.randint(0, p['max'])
            scores.append({'part': p['id'], 'mark': m, 'page': pg, 'x': 26, 'y': 250 + 220 * i})
            ops.setdefault(str(pg), []).append(['tick' if m else 'cross', 500, 240 + 220 * i, 0.7])
        qtotal[str(qc['q'])] = [pg, 560, 1000]
    nm = re.sub(r'[\d_.-]+|pdf$', ' ', filename, flags=re.I).strip().title() or 'Unknown'
    sid = (re.findall(r'\d{6,}', filename) or [''])[0]
    return {'name': nm, 'sid': sid, 'cover': {'cells': [], 'total': None, 'note': [380, 1000]},
            'cover_note': [f'Good work, {nm.split()[0]}.', 'MOCK marking -', 'not real marks.'],
            'scores': scores, 'qtotal': qtotal, 'pages': ops, 'flags': [],
            'overall': 'MOCK marking for testing the website; these are not real marks.',
            'good': ['Mock bullet one.'], 'improve': ['Mock bullet two.'], 'next': ['Mock next step.'], 'closing': 'Keep going.'}


def mark_script(tid, cfg, page_files, filename):
    """First pass. page_files: 150-dpi PNGs of the script. Returns (spec, tok_in, tok_out)."""
    if MOCK:
        return _mock_spec(cfg, page_files, filename), 0, 0
    content = _key_blocks(tid)
    content.append(_txt('TEST: ' + cfg.get('title', '') + '\n\nTEST STRUCTURE\n' + _structure(cfg)
                        + '\n\nMARKING POLICY\n' + (cfg.get('policy') or DEFAULT_POLICY)
                        + '\n\nNOTES FROM THE TEACHER FOR THIS TEST (apply to every script)\n' + (cfg.get('precedents') or '(none yet)')))
    content[-1]['cache_control'] = {'type': 'ephemeral'}       # everything above is the same for every script
    for i, f in enumerate(page_files, 1):
        content += [_txt(f'STUDENT SCRIPT page {i} of {len(page_files)}:'), _img(render.grid_image(f, 150))]
    content.append(_txt(f'File name of this script: "{filename}" (the cover page is authoritative for name and ID). '
                        'Mark the script and return the JSON mark spec.'))
    text, tin, tout = _call(MARK_SYSTEM, content)
    return _json(text), tin, tout


def fix_spec(tid, cfg, spec, problems, preview_files):
    """Second pass: the renderer found problems (overlaps, missing parts). Returns (spec, tok_in, tok_out)."""
    if MOCK:
        return spec, 0, 0
    content = [_txt('TEST STRUCTURE\n' + _structure(cfg) + '\n\nYour mark spec for this script was rendered. '
                    'These problems were found automatically:\n- ' + '\n- '.join(problems)
                    + '\n\nBelow are the rendered pages concerned (grid added). Fix ONLY these problems - move or '
                    'shorten notes into blank space, add missing parts - and keep every mark and all other content '
                    'unchanged. Return the complete corrected JSON mark spec.\n\nYOUR SPEC:\n```json\n'
                    + json.dumps(spec, ensure_ascii=False) + '\n```')]
    for n, f in preview_files:
        content += [_txt(f'RENDERED script page {n}:'), _img(render.grid_image(f, 100))]
    text, tin, tout = _call(MARK_SYSTEM, content)
    return _json(text), tin, tout
