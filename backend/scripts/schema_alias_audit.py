#!/usr/bin/env python3
"""Generate reports/schema_alias_audit.md (M3E §2 — target-schema + alias audit / anti-overfitting).

For every CORE target field it reports the canonical key, type, why the field belongs in a
representative employee-migration contract, the exact DETERMINISTIC header aliases (the only synonyms
``map_table_deterministic`` resolves without a model call), the deterministic enum/value aliases, and
a per-field verdict on whether those aliases are universal HR terminology or dataset-specific.

The ``why`` / ``verdict`` text is authored here (human judgement, committed with the code); everything
else is read straight from the schema so the report cannot drift from the actual contract.

Run:  cd backend && ./.venv/bin/python scripts/schema_alias_audit.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on path

from app.alias_audit import alias_inventory, inventory_hash  # noqa: E402
from app.schema_loader import get_target_schema  # noqa: E402

REPO = Path(__file__).resolve().parents[2]  # repo root (backend/scripts/ -> backend/ -> repo)
OUT = REPO / "reports" / "schema_alias_audit.md"

# Per-field rationale + alias verdict (authored; the alias LISTS come from the schema).
WHY: dict[str, str] = {
    "employee_id": "Canonical identity key for every downstream join, reconciliation and delivery; required.",
    "full_name": "Human-readable identity shown across the target; required, never split into first/last.",
    "preferred_name": "Common optional HR field (informal/known-as name) distinct from legal name.",
    "date_of_birth": "Standard HR demographic; drives age/eligibility and two-digit-year century resolution.",
    "gender": "Standard self-declared demographic with a small controlled vocabulary.",
    "nationality": "Common HR demographic (citizenship) captured for compliance/reporting.",
    "hire_date": "Core employment milestone; required; deliberately distinct from contract_start_date.",
    "contract_start_date": "Optional employment date that is frequently confused with hire_date (date-role group).",
    "employment_type": "Standard engagement classification (full/part/contract/intern/temp).",
    "employment_status": "Standard lifecycle state (active/on_leave/notice/terminated).",
    "designation": "Job title/designation; free-text business label.",
    "grade": "Job grade/band; common compensation-structure field.",
    "probation_end_date": "Standard confirmation/probation milestone.",
    "termination_date": "Standard separation milestone (last working day).",
    "notice_period_days": "Common numeric employment-terms field.",
    "department": "Controlled ORG taxonomy — a company's chosen functional structure, small allowed set.",
    "business_unit": "Common higher-level org grouping (SBU) above department.",
    "cost_center": "Standard finance/allocation code attached to an employee.",
    "work_location": "Standard office/site label.",
    "manager_employee_id": "Reporting line as an IDENTIFIER (never a name); needs referential proof.",
    "work_email": "Required unique work contact; primary reconciliation key alongside employee_id.",
    "personal_email": "Optional secondary contact, deliberately distinct from work_email.",
    "mobile_phone": "Standard personal contact number.",
    "work_phone": "Standard office/desk contact number.",
}

# Fields whose header aliases warrant a specific note; default verdict is "universal".
VERDICT_NOTE: dict[str, str] = {
    "business_unit": "`sbu` = strategic business unit; universal abbreviation, not dataset-specific.",
    "department": "Header aliases are generic (`dept`, `department name`); the ORG VALUE set is a "
                  "company taxonomy, so unseen department VALUES escalate to review rather than auto-map.",
    "gender": "`value_aliases` are universal single-letter codes / the fixed privacy phrase — lexical, "
              "never an org taxonomy; unseen phrasings (e.g. 'Does not disclose') still go to the model.",
}


def main() -> None:
    schema = get_target_schema()
    inv = alias_inventory(schema)
    by_key = {f["key"]: f for f in inv["core_fields"]}
    lines: list[str] = []
    A = lines.append

    A("# Schema + Alias Audit — Anti-Overfitting (M3E §2)")
    A("")
    A(f"_Generated {date.today().isoformat()} from `schemas/{schema.version}.yaml` via "
      "`backend/scripts/schema_alias_audit.py`. Factual columns are read from the schema; the "
      "*why* / *verdict* text is authored judgement committed with the script._")
    A("")
    A("## Frozen inventory (the overfitting surface)")
    A("")
    A(f"- Schema version: **{inv['schema_version']}**")
    A(f"- Core target fields: **{inv['counts']['core_fields']}**")
    A(f"- Core canonical keys (compact): **{inv['counts']['core_canonical_keys']}**")
    A(f"- Declared core header aliases (compact, deduped): **{inv['counts']['core_header_aliases']}**")
    A(f"- Distinct deterministic header keys (canonical + aliases, minus ambiguous): "
      f"**{inv['counts']['deterministic_header_keys']}**")
    A(f"- Collections: **{inv['counts']['collections']}**")
    A(f"- **Frozen deterministic-surface hash (SHA-256): `{inventory_hash(schema)}`**")
    A("")
    A("What the deterministic mapper resolves WITHOUT a model call = the compact form of a canonical "
      "key or a declared `aliases:` entry, exact match only (`app/mapping_rules.py`). Two things are "
      "explicitly **NOT** part of that surface:")
    A("")
    A("- **`disambiguation_keywords`** — used only by `policy.classify_proposal` to give a MODEL "
      "proposal independent semantic support. They never resolve a header on their own, so a header "
      "that merely shares a keyword still goes through the model.")
    A("- **`value_aliases`** — normalize enum VALUES (e.g. gender `F`→`female`), never headers.")
    A("")
    A("No aliases were added or removed for the M3E live fixture. Every alias below is universal HR "
      "terminology defensible independently of any test/Kaggle dataset; nothing is dataset-specific, "
      "so there was nothing to remove. The set is **frozen** for the generalization test.")
    A("")
    A("## Core fields")
    A("")
    for f in schema.fields:
        k = f.name
        item = by_key[k]
        A(f"### `{k}` — {f.label} ({f.value_type}{', required' if f.required_in_final else ''})")
        A("")
        A(f"- **Why it exists:** {WHY.get(k, '(representative employee field)')}")
        aliases = item["header_aliases"]
        A(f"- **Deterministic header aliases ({len(aliases)}):** "
          + (", ".join(f"`{a}`" for a in aliases) if aliases else "_none (canonical key only)_"))
        if item["value_aliases"]:
            va = "; ".join(f"{canon} ← {', '.join(al)}" for canon, al in item["value_aliases"].items())
            A(f"- **Deterministic value aliases:** {va}")
        if f.enum_values:
            A(f"- **Allowed values:** {', '.join(f.enum_values)}")
        A(f"- **Disambiguation keywords (policy-only, NOT deterministic):** "
          f"{', '.join(item['disambiguation_keywords']) or '_none_'}")
        note = VERDICT_NOTE.get(k)
        A(f"- **Verdict:** All header aliases are universal/common HR terminology."
          + (f" {note}" if note else ""))
        A("")

    A("## Collections (child-table item fields)")
    A("")
    for c in schema.collections:
        A(f"- **{c.key}** (table aliases: {', '.join(f'`{a}`' for a in c.table_aliases)}): "
          + ", ".join(f"`{fld.name}`" for fld in c.fields))
    A("")
    A("## Live-fixture headers vs the deterministic surface")
    A("")
    A("The 10 headers in `sample-data/llm-generalization-demo.csv` are asserted absent from the "
      "deterministic surface by `tests/test_m3e_anti_overfitting.py` (preflight). None is a canonical "
      "key or a declared alias, so each must be interpreted by the model:")
    A("")
    FIXTURE = [
        ("Employee Reference", "employee_id"), ("Legal Display Name", "full_name"),
        ("Corporate Email", "work_email"), ("Joining Effective Date", "hire_date"),
        ("Identity Sex", "gender"), ("Lifecycle Status", "employment_status"),
        ("Engagement Category", "employment_type"), ("Org Function", "department"),
        ("Reports To Reference", "manager_employee_id"), ("Position Caption", "designation"),
    ]
    from app.alias_audit import resolves_deterministically
    A("| Source header | Intended target | Deterministic result |")
    A("|---|---|---|")
    for h, tgt in FIXTURE:
        r = resolves_deterministically(schema, h)
        A(f"| {h} | {tgt} | {'RESOLVED: ' + r if r else '**unresolved → model**'} |")
    A("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(lines)} lines); hash={inventory_hash(schema)}")


if __name__ == "__main__":
    main()
