#!/usr/bin/env python3
"""Generate the M3E LLM-generalization demo fixture (deterministic, synthetic, no real PII).

Writes ``sample-data/llm-generalization-demo.csv`` — ~50 employees whose HEADERS are semantically
clear but are NOT canonical target keys and NOT declared deterministic aliases (proven by the
preflight test), and whose ENUM VALUES use unseen vocabulary that must be interpreted by the model
and re-validated deterministically. ``Org Function`` uses a company-specific taxonomy that must
escalate to ONE column-scoped human review.

Run:  cd backend && ./.venv/bin/python scripts/generate_llm_demo.py
The output is committed to the repo; regenerating it must produce a byte-identical file.
"""
from __future__ import annotations

import csv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OUT = REPO / "sample-data" / "llm-generalization-demo.csv"

HEADERS = [
    "Employee Reference",      # -> employee_id
    "Legal Display Name",      # -> full_name
    "Corporate Email",         # -> work_email
    "Joining Effective Date",  # -> hire_date
    "Identity Sex",            # -> gender          (Man/Woman/Does not disclose)
    "Lifecycle Status",        # -> employment_status (Currently Employed/Voluntarily Exited/Dismissed)
    "Engagement Category",     # -> employment_type   (Permanent Staff/Part Time Staff/External Contractor)
    "Org Function",            # -> department        (Product Engineering/Revenue/People & Culture) -> REVIEW
    "Reports To Reference",    # -> manager_employee_id (references other Employee Reference values)
    "Position Caption",        # -> designation
]

FIRST = ["Ava", "Noah", "Mia", "Liam", "Zoe", "Ethan", "Aria", "Kai", "Nora", "Leo",
         "Ivy", "Owen", "Ruby", "Finn", "Isla", "Max", "Lena", "Cole", "Maya", "Reid",
         "Tara", "Jude", "Elsa", "Rhys", "Nina", "Sean", "Cleo", "Dana", "Omar", "Faye",
         "Beau", "Gwen", "Hugo", "Iris", "Jack", "Kira", "Luca", "Moss", "Neil", "Opal",
         "Pia", "Quin", "Rory", "Sana", "Theo", "Uma", "Vera", "Wade", "Xena", "Yara"]
LAST = ["Okafor", "Reyes", "Novak", "Chen", "Haas", "Bauer", "Costa", "Imai", "Faber", "Dahl",
        "Roy", "Sato", "Vance", "Weber", "Kaur", "Lund", "Marsh", "Ng", "Pace", "Quill",
        "Serra", "Tanaka", "Udal", "Vega", "Wren", "Yee", "Zhao", "Ashby", "Boone", "Cira",
        "Doria", "Efron", "Frey", "Gomez", "Hollis", "Ives", "Jansen", "Kelso", "Loft", "Mora",
        "Nash", "Oduya", "Prasad", "Quist", "Rana", "Solis", "Toma", "Ursu", "Volk", "Ward"]

# Company-specific / unseen vocabularies (NOT declared value_aliases; must reach the model).
GENDER = ["Man", "Woman", "Does not disclose"]
STATUS = ["Currently Employed", "Voluntarily Exited", "Dismissed"]
ENGAGEMENT = ["Permanent Staff", "Part Time Staff", "External Contractor"]
ORG_FUNCTION = ["Product Engineering", "Revenue", "People & Culture"]  # company taxonomy -> review
TITLES = {
    "Product Engineering": ["Software Engineer", "Senior Software Engineer", "Engineering Lead", "QA Engineer"],
    "Revenue": ["Account Executive", "Sales Manager", "Solutions Consultant", "Account Manager"],
    "People & Culture": ["People Partner", "Recruiter", "People Operations Lead", "Talent Coordinator"],
}


def _pick(seq, i):
    return seq[i % len(seq)]


def build_rows() -> list[dict]:
    rows: list[dict] = []
    n = 50
    # Deterministic managers: first two per function are "leaders" (blank manager); others report
    # to a valid earlier Employee Reference within the same function.
    function_of = [_pick(ORG_FUNCTION, i * 7) for i in range(n)]
    leaders: dict[str, str] = {}
    for i in range(n):
        emp_ref = f"EMP-{1001 + i}"
        first, last = _pick(FIRST, i), _pick(LAST, i * 3 + 1)
        func = function_of[i]
        if func not in leaders:
            leaders[func] = emp_ref
            manager = ""  # a function head reports to nobody in this dataset
        else:
            manager = leaders[func]
        rows.append({
            "Employee Reference": emp_ref,
            "Legal Display Name": f"{first} {last}",
            "Corporate Email": f"{first.lower()}.{last.lower()}@northwind-demo.example",
            "Joining Effective Date": f"{2016 + (i % 9)}-{1 + (i % 12):02d}-{1 + (i % 27):02d}",
            "Identity Sex": _pick(GENDER, i * 5),          # spreads all three, incl. the rare one
            "Lifecycle Status": (STATUS[1] if i % 17 == 3 else STATUS[2] if i % 19 == 5 else STATUS[0]),
            "Engagement Category": (ENGAGEMENT[1] if i % 8 == 2 else ENGAGEMENT[2] if i % 11 == 4 else ENGAGEMENT[0]),
            "Org Function": func,
            "Reports To Reference": manager,
            "Position Caption": _pick(TITLES[func], i),
        })
    return rows


def main() -> None:
    rows = build_rows()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)
    # Report the distinct enum vocabularies so the fixture's intent is auditable.
    def distinct(col):
        return sorted({r[col] for r in rows})
    print(f"wrote {len(rows)} rows -> {OUT}")
    for col in ("Identity Sex", "Lifecycle Status", "Engagement Category", "Org Function"):
        print(f"  {col}: {distinct(col)}")


if __name__ == "__main__":
    main()
