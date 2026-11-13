"""Marks sheet (PDF + Excel, real numbers) and the per-student zip."""
import os, io, zipfile, re
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from . import store, render

RED = 'B4141E'

def safe(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name or '').strip() or 'Unnamed'

def rows_for(test):
    cfg = test['cfg']; rows = []
    for r in store.q("SELECT id FROM scripts WHERE test_id=? AND status='done'", (test['id'],)):
        s = store.get_script(r['id'])
        rows.append({'id': s['id'], 'name': s['name'] or s['filename'], 'sid': s['sid'] or '', 'q': render.q_totals(s['spec'], cfg)})
    rows.sort(key=lambda r: r['name'].lower())
    return rows

def _head(test):
    c = test['cfg']; maxes = [x['max'] for x in c['questions']]; tot = sum(maxes)
    sub = '  |  '.join(x for x in [c.get('college', ''), f"Teacher: {c['teacher']}" if c.get('teacher') else '',
                                   f"Section {c['section']}" if c.get('section') else '', c.get('date', ''), f'Total marks: {tot}'] if x)
    return c.get('title', 'Test'), sub, maxes, tot

def _stats(rows, maxes):
    cols = list(zip(*[r['q'] + [sum(r['q'])] for r in rows])) if rows else [[0]] * (len(maxes) + 1)
    N = max(1, len(rows))
    return [('Class average', [round(sum(c) / N, 1) for c in cols]), ('Highest', [max(c) for c in cols]), ('Lowest', [min(c) for c in cols])]

def sheet_xlsx(test):
    title, sub, maxes, TOT = _head(test); rows = rows_for(test); nq = len(maxes)
    wb = Workbook(); ws = wb.active; ws.title = 'Marks'
    ws['A1'] = title; ws['A1'].font = Font(bold=True, size=13, color=RED)
    ws['A2'] = sub; ws['A2'].font = Font(italic=True, size=10, color='555555')
    thin = Side(style='thin', color='BBBBBB'); bd = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr = ['S.No', 'Student ID', 'Student Name'] + [f"Q{x['q']} ({x['max']})" for x in test['cfg']['questions']] + [f'Total ({TOT})', '%']
    for c, h in enumerate(hdr, 1):
        x = ws.cell(row=4, column=c, value=h); x.font = Font(bold=True, color='FFFFFF'); x.fill = PatternFill('solid', fgColor=RED)
        x.alignment = Alignment(horizontal='center'); x.border = bd
    for i, r in enumerate(rows):
        t = sum(r['q']); vals = [i + 1, r['sid'], r['name']] + r['q'] + [t, t / TOT]
        for c, v in enumerate(vals, 1):
            x = ws.cell(row=5 + i, column=c, value=v); x.border = bd
            if c != 3: x.alignment = Alignment(horizontal='center')
            if i % 2: x.fill = PatternFill('solid', fgColor='F5F5F5')
        ws.cell(row=5 + i, column=nq + 5).number_format = '0%'; ws.cell(row=5 + i, column=nq + 4).font = Font(bold=True)
    s = 5 + len(rows) + 1
    for k, (lab, vals) in enumerate(_stats(rows, maxes)):
        ws.cell(row=s + k, column=3, value=lab).font = Font(bold=True)
        for j, v in enumerate(vals):
            x = ws.cell(row=s + k, column=4 + j, value=v); x.font = Font(bold=True); x.alignment = Alignment(horizontal='center')
        x = ws.cell(row=s + k, column=nq + 5, value=vals[-1] / TOT); x.number_format = '0%'; x.alignment = Alignment(horizontal='center')
    ws.cell(row=s + 3, column=3, value='Number of students').font = Font(bold=True); ws.cell(row=s + 3, column=4, value=len(rows))
    for i, w in enumerate([6, 14, 30] + [9] * nq + [11, 8], 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A5'
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def sheet_pdf(test):
    title, sub, maxes, TOT = _head(test); rows = rows_for(test); nq = len(maxes); N = len(rows)
    red = colors.HexColor('#' + RED); b = io.BytesIO()
    ps = landscape(A4) if nq > 8 else A4
    def footer(c, d):
        c.saveState(); c.setFont('Helvetica', 8); c.setFillColor(colors.grey)
        c.drawString(12 * mm, 10 * mm, f"{test['cfg'].get('college', '')}  ·  {title}"); c.drawRightString(ps[0] - 12 * mm, 10 * mm, f'Page {d.page}'); c.restoreState()
    doc = SimpleDocTemplate(b, pagesize=ps, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=18 * mm)
    ss = getSampleStyleSheet()
    st1 = ParagraphStyle('t', parent=ss['Title'], textColor=red, fontSize=14, spaceAfter=2)
    st2 = ParagraphStyle('s', parent=ss['Normal'], fontSize=8.5, textColor=colors.HexColor('#555555'), alignment=1)
    story = [Paragraph(title + ' — Marks Sheet', st1), Paragraph(sub, st2), Spacer(1, 6)]
    data = [['S.No', 'Student ID', 'Student Name'] + [f"Q{x['q']}\n({x['max']})" for x in test['cfg']['questions']] + [f'Total\n({TOT})', '%']]
    data += [[i + 1, r['sid'], r['name']] + r['q'] + [sum(r['q']), f"{round(100 * sum(r['q']) / TOT)}%"] for i, r in enumerate(rows)]
    for lab, vals in _stats(rows, maxes):
        data.append(['', '', lab] + [f'{v:.1f}' if isinstance(v, float) else v for v in vals] + [f'{round(100 * vals[-1] / TOT)}%'])
    data.append(['', '', 'Number of students', N] + [''] * (nq + 1))
    avail = ps[0] - 24 * mm - (10 + 25 + 50 + 14 + 12) * mm
    tb = Table(data, colWidths=[10 * mm, 25 * mm, 50 * mm] + [min(12 * mm, avail / nq)] * nq + [14 * mm, 12 * mm], repeatRows=1)
    sty = [('FONT', (0, 0), (-1, -1), 'Helvetica', 8.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5), ('BACKGROUND', (0, 0), (-1, 0), red),
           ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('ALIGN', (2, 1), (2, -1), 'LEFT'),
           ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#BBBBBB')),
           ('FONT', (nq + 3, 1), (nq + 3, -1), 'Helvetica-Bold', 8.5), ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
           ('FONT', (0, N + 1), (-1, -1), 'Helvetica-Bold', 8.5), ('BACKGROUND', (0, N + 1), (-1, -1), colors.HexColor('#FBE9EA')),
           ('LINEABOVE', (0, N + 1), (-1, N + 1), 1.2, red)]
    for i in range(2, N + 1, 2): sty.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F5F5F5')))
    tb.setStyle(TableStyle(sty)); story.append(tb)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return b.getvalue()

def build_zip(test, path):
    root = safe(test['cfg'].get('title', 'Test')) + ' - Marked'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr(f'{root}/Marks Sheet.pdf', sheet_pdf(test))
        z.writestr(f'{root}/Marks Sheet.xlsx', sheet_xlsx(test))
        used = set()
        for r in rows_for(test):
            base = safe(f"{r['name']} {r['sid']}"); n = base; k = 2
            while n.lower() in used: n = f'{base} ({k})'; k += 1
            used.add(n.lower())
            pdf = os.path.join(store.script_dir(test['id'], r['id']), 'marked.pdf')
            if os.path.exists(pdf): z.write(pdf, f'{root}/{n}/{n} - Marked.pdf')
    return path
