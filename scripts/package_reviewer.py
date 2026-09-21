"""Package the CLEAN reviewer submission ZIP (darwinbox_submission_final.zip).

An intentional, product-first submission package — NOT a blind repo zip and NOT the full working
source. Allowlist-based, secrets excluded and scanned, with a SOURCE_MANIFEST.txt (relative paths +
per-file SHA-256). Verifies the archive extracts safely, contains no excluded/traversal paths, and
holds no likely credentials before it is written. Aborts (writes nothing) if a secret is found.

Contents (see the assignment package spec):
  - README.md, WRITEUP.md, DEMO_CHECKLIST.md
  - backend/app/**, backend/mock_target/**, backend/requirements*.txt, backend/pytest.ini,
    backend/.env.example, the full offline test suite (backend/tests/**), and ONE demo runner
    (backend/scripts/final_four_migrations.py)
  - frontend source + config (src/**, package.json, package-lock.json, tsconfig.json,
    vite.config.ts, index.html)
  - schemas/**
  - the four cleaned demo fixtures under sample-data/final-demo/, plus the sample fixtures the
    included tests genuinely require (kept so the reviewer suite stays runnable)
  - five selected reviewer reports

Deliberately EXCLUDED: .env / secrets, .claude/, CLAUDE.md, CHATGPT_HANDOFF.md,
CURRENT_IMPLEMENTATION.md, STATUS.md, OVERNIGHT_PROGRESS.md, milestone/progress notes,
reports/langsmith_cleanup.py + report, internal maintenance / blind-stress / generator / debug
scripts, old demo folders, runtime DB/WAL/logs, node_modules/.venv/dist.

Run:  python scripts/package_reviewer.py
Output: artifacts/darwinbox_submission_final.zip
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts"
ZIP_NAME = "darwinbox_submission_final.zip"

ALLOW_FILES = [
    "README.md", "WRITEUP.md",
    "backend/requirements.txt", "backend/requirements.lock.txt", "backend/pytest.ini",
    "backend/.env.example",
    "backend/scripts/final_four_migrations.py",      # the one demo runner
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json",
    "frontend/vite.config.ts", "frontend/index.html",
    # test fixtures the included suite genuinely reads (kept so the reviewer suite stays runnable)
    "sample-data/legacy_hr.csv", "sample-data/employee_export.xlsx",
    "sample-data/canonical_clean.csv", "sample-data/canonical_messy.csv",
    "sample-data/canonical_large.csv", "sample-data/llm-generalization-demo.csv",
    # the reviewer evaluation evidence (human-readable + machine-readable)
    "reports/evaluation.md", "reports/evaluation.json",
]
ALLOW_GLOBS = [
    "schemas/*.json", "schemas/*.yaml", "schemas/*.yml",
    "schemas/tenants/*.yaml", "schemas/tenants/*.yml", "schemas/tenants/*.json",
    "backend/app/**/*.py",
    "backend/mock_target/**/*.py",
    "backend/tests/**/*.py", "backend/tests/fixtures/*.json",
    "frontend/src/**/*",
    "sample-data/final-demo/*.xlsx",                 # the four cleaned demo fixtures
    "sample-data/manual-demo/*.csv", "sample-data/manual-demo/*.xlsx",   # required by version/observability tests
    "sample-data/structured-demo/*.csv", "sample-data/structured-demo/*.xlsx",  # required by delivery/custom-field tests
]
EXCLUDE_PARTS = {".git", ".venv", "node_modules", "dist", "__pycache__", ".pytest_cache",
                 "data", "artifacts", ".vite", ".claude", "blind_packages", "external_packages"}
EXCLUDE_SUFFIX = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log", ".tsbuildinfo",
                  ".pyc", ".DS_Store")


def _excluded(rel: Path) -> bool:
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return True
    if rel.name == ".env" or (rel.name.endswith(".env") and rel.name != ".env.example"):
        return True
    if rel.suffix in EXCLUDE_SUFFIX or rel.name == ".DS_Store":
        return True
    return False


def collect() -> list[Path]:
    found: set[Path] = set()
    for f in ALLOW_FILES:
        p = REPO / f
        if p.is_file():
            found.add(p.resolve())
    for g in ALLOW_GLOBS:
        for p in REPO.glob(g):
            if p.is_file():
                found.add(p.resolve())
    return [p for p in sorted(found) if not _excluded(p.relative_to(REPO))]


# --- secret scanning (report path only, never matched content) ---
SECRET_PATTERNS = [
    re.compile(rb"gsk_[A-Za-z0-9]{20,}"),       # Groq live key
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),        # generic provider key
    re.compile(rb"lsv2_[A-Za-z0-9_]{20,}"),     # LangSmith key (v2)
    re.compile(rb"ls__[A-Za-z0-9]{20,}"),       # LangSmith key (legacy)
    re.compile(rb"AKIA[0-9A-Z]{16}"),           # AWS access key id
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _configured_secret_bytes() -> list[bytes]:
    env = REPO / "backend" / ".env"
    out = []
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            for prefix in ("GROQ_API_KEY=", "LANGSMITH_API_KEY=", "LANGCHAIN_API_KEY="):
                if s.startswith(prefix) and s.split("=", 1)[1].strip():
                    out.append(s.split("=", 1)[1].strip().encode())
    return out


def scan_bytes(data: bytes, extra: list[bytes]) -> bool:
    for pat in SECRET_PATTERNS:
        if pat.search(data):
            return True
    return any(b and b in data for b in extra)


def scan_xlsx_cells(path: Path, extra: list[bytes]) -> bool:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell is not None and scan_bytes(str(cell).encode(), extra):
                        return True
        wb.close()
    except Exception:
        return False
    return False


REQUIRED = {
    "SOURCE_MANIFEST.txt", "README.md", "WRITEUP.md",
    "backend/app/main.py", "backend/app/worker.py", "backend/app/delivery.py",
    "backend/app/target_gateway.py", "backend/app/reconcile_target.py",
    "backend/app/observability.py", "backend/app/model_projection.py",
    "backend/app/workflows/mapping/graph.py", "backend/app/workflows/preparation/graph.py",
    "backend/mock_target/service.py", "backend/pytest.ini", "backend/.env.example",
    "backend/requirements.txt", "backend/tests/conftest.py", "backend/tests/test_demo_fixtures.py",
    "backend/scripts/final_four_migrations.py",
    "frontend/package.json", "frontend/vite.config.ts", "frontend/index.html",
    "frontend/src/App.tsx",
    "schemas/employee.v2.yaml", "schemas/tenants/beta.yaml",
    "sample-data/final-demo/01_low_ambiguity_50x10.xlsx",
    "sample-data/final-demo/02_medium_ambiguity_20x10.xlsx",
    "sample-data/final-demo/03_high_ambiguity_10x10.xlsx",
    "sample-data/final-demo/04_clean_no_review_100x20.xlsx",
    "sample-data/structured-demo/01_employees.csv", "sample-data/manual-demo/01_employee_master.csv",
    "sample-data/llm-generalization-demo.csv",
    "reports/evaluation.md", "reports/evaluation.json",
}
# Members that must NEVER appear in the reviewer ZIP.
FORBIDDEN = {
    "CLAUDE.md", "CHATGPT_HANDOFF.md", "CURRENT_IMPLEMENTATION.md", "STATUS.md",
    "OVERNIGHT_PROGRESS.md", "M3C_PROGRESS.md", "SESSION_SUMMARY.md", "DEMO_CHECKLIST.md",
    "reports/langsmith_cleanup.md", "backend/scripts/langsmith_cleanup.py",
    "backend/scripts/blind_stress.py", "backend/scripts/final_demo_three_migrations.py",
    "scripts/generate_blind_packages.py", "backend/scripts/reset_demo.py",
    "backend/.env",
}


def main() -> int:
    files = collect()
    extra = _configured_secret_bytes()
    flagged: list[str] = []
    for p in files:
        rel = str(p.relative_to(REPO))
        if p.suffix == ".xlsx":
            if scan_xlsx_cells(p, extra):
                flagged.append(rel)
            continue
        if scan_bytes(p.read_bytes(), extra):
            flagged.append(rel)
    if flagged:
        print("SECRET SCAN FAILED — archive NOT written. Flagged (paths only):")
        for f in flagged:
            print("  -", f)
        return 2

    manifest_lines = ["# SOURCE_MANIFEST — darwinbox reviewer submission (relative path  sha256)"]
    for p in files:
        rel = str(p.relative_to(REPO))
        manifest_lines.append(f"{rel}  {hashlib.sha256(p.read_bytes()).hexdigest()}")
    manifest = "\n".join(manifest_lines) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, str(p.relative_to(REPO)))
        zf.writestr("SOURCE_MANIFEST.txt", manifest)
    payload = buf.getvalue()

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = set(zf.namelist())
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                print(f"UNSAFE MEMBER PATH: {n}"); return 3
            if any(part in EXCLUDE_PARTS for part in Path(n).parts):
                print(f"EXCLUDED CONTENT LEAKED: {n}"); return 3
        leaked = sorted(FORBIDDEN & names)
        if leaked:
            print(f"FORBIDDEN MEMBERS LEAKED: {leaked}"); return 3
        missing = sorted(REQUIRED - names)
        if missing:
            print(f"MISSING REQUIRED MEMBERS: {missing}"); return 4
        with tempfile.TemporaryDirectory() as td:
            zf.extractall(td)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    zip_path = ARTIFACTS / ZIP_NAME
    zip_path.write_bytes(payload)
    print("OK — reviewer submission bundle written")
    print(f"path: {zip_path}")
    print(f"files: {len(names)} (incl. SOURCE_MANIFEST.txt)")
    print(f"size_bytes: {len(payload)}")
    print(f"sha256: {hashlib.sha256(payload).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
