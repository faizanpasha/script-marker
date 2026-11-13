# Script Marker

Web app that marks scanned A-Level Physics (CAIE 9702) answer scripts against a marking key and returns
"hand-checked" PDFs (red-pen ticks, circled part marks, notes, examiner's report, marking-key pages), a marks
sheet (PDF + Excel) and a zip with one folder per student.

## Run

    pip install -r requirements.txt        # also needs poppler (pdftoppm)
    ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=choose-one uvicorn app.main:app --port 8000

or `docker build -t marker . && docker run -p 8000:8000 -v marker-data:/data -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... marker`.

On first start an administrator account is created (password printed in the log if `ADMIN_PASSWORD` is not set).
The administrator adds the Anthropic API key and the other accounts under **Users & settings**.

Environment: `MARKER_DATA` (data folder, default `./data`), `ANTHROPIC_API_KEY` (alternative to saving the key in
Settings), `MARKER_WORKERS` (scripts marked in parallel, default 3), `MARKER_HTTPS=1` (secure cookies behind HTTPS),
`MARKER_MOCK=1` (testing only: fake marks, no API calls).

## Roles
- **Academic team (operator):** create tests, upload scripts, run marking, download results. Cannot change marks.
- **Teacher:** the above, plus change marks / name / ID and add marking rules (precedents).
- **Administrator:** the above, plus users, API key, model, deleting tests.

## Layout
- `app/main.py` routes and pages · `app/store.py` SQLite + files · `app/jobs.py` background pipeline
- `app/marking.py` Claude API prompts and calls · `app/render.py` + `app/redpen.py` deterministic renderer and QA
- `app/export.py` marks sheet and zip · `app/templates/` pages

## Not built yet
Drag-and-drop note editing, "apply to all" for a changed decision, cross-student consistency pass, test-variant
detection, Google Drive export, splitting very large zips.
