"""M3E anti-overfitting + deterministic pre-flight (order §2/§3/§13).

Proves the generalization claim rests on the MODEL, not on a growing dictionary of hard-coded
headers: every intended live-demo header is absent from the canonical-key set AND the declared-alias
set, and the REAL deterministic mapper leaves each one unresolved (on real profiles, not just header
strings). The frozen deterministic surface is pinned by a hash so an accidental alias addition fails
CI. The live half (real Groq must then map them) lives in the gated live test.
"""
from __future__ import annotations

import csv
from pathlib import Path

from app.alias_audit import (
    alias_inventory,
    deterministic_header_keys,
    inventory_hash,
    resolves_deterministically,
)
from app.mapping_rules import _compact, map_table_deterministic, table_context_name
from app.profiling import profile_table
from app.schema_loader import get_target_schema

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO / "sample-data" / "llm-generalization-demo.csv"

# Header -> intended target the MODEL should discover (never a deterministic alias).
INTENDED = {
    "Employee Reference": "employee_id",
    "Legal Display Name": "full_name",
    "Corporate Email": "work_email",
    "Joining Effective Date": "hire_date",
    "Identity Sex": "gender",
    "Lifecycle Status": "employment_status",
    "Engagement Category": "employment_type",
    "Org Function": "department",
    "Reports To Reference": "manager_employee_id",
    "Position Caption": "designation",
}

# Frozen deterministic-surface hash (canonical keys + declared header aliases, core + collections).
# If you INTENTIONALLY change schema aliases, re-run scripts/schema_alias_audit.py, confirm the change
# is defensible universal HR terminology (not dataset tuning), then update this constant. A surprise
# change here means an alias was added/removed — likely overfitting.
FROZEN_SURFACE_HASH = "d5495aead458cd17a1422b640ccef4f47c80dc65a25fca64d089f230b077eec0"


def _fixture_headers() -> list[str]:
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


def test_fixture_headers_present_and_expected():
    headers = _fixture_headers()
    assert set(headers) == set(INTENDED), headers


def test_intended_headers_absent_from_canonical_and_alias_sets():
    """§13.1/2: the intended headers are in NEITHER the canonical-key set NOR the alias set."""
    schema = get_target_schema()
    inv = alias_inventory(schema)
    canonical = set(inv["canonical_compact_set"])
    aliases = set(inv["header_alias_compact_set"])
    for h in INTENDED:
        c = _compact(h)
        assert c not in canonical, f"{h!r} collides with a canonical key ({c}) — change the fixture header"
        assert c not in aliases, f"{h!r} collides with a declared alias ({c}) — change the fixture header"


def test_deterministic_preflight_all_unresolved():
    """§3 pre-flight: the REAL deterministic mapper, on REAL profiles, resolves NONE of the intended
    headers. Prints `deterministic result -> unresolved` for each (the proof the later result is the
    model's, not a hard-coded alias)."""
    schema = get_target_schema()
    data = FIXTURE.read_bytes()
    from app.ingest import parse_file

    pf = parse_file(filename=FIXTURE.name, data=data, stored_name="x.csv",
                    max_bytes=5_000_000, max_rows=5000, file_id="f1")
    table = pf.tables[0]
    profiles = profile_table(table, pf.records)
    tname = table_context_name(FIXTURE.name, None)
    results = {rm.header: rm for rm in map_table_deterministic(profiles, schema, table_name=tname)}
    for h in INTENDED:
        rm = results[h]
        print(f"{h:24s} deterministic result -> {'unresolved' if not rm.resolved else rm.target}")
        assert not rm.resolved and rm.target is None, (h, rm.target)
        # and the standalone resolver agrees
        assert resolves_deterministically(schema, h) is None


def test_frozen_deterministic_surface_hash_unchanged():
    """§13: the frozen alias inventory (hash + counts) did not grow. No alias was added for the fixture."""
    schema = get_target_schema()
    inv = alias_inventory(schema)
    assert inv["counts"]["core_fields"] == 24, inv["counts"]
    assert inv["counts"]["deterministic_header_keys"] == 97, inv["counts"]
    assert inventory_hash(schema) == FROZEN_SURFACE_HASH, (
        "deterministic alias surface changed; re-audit and update FROZEN_SURFACE_HASH intentionally")


def test_deterministic_key_count_matches_lookup():
    """The audited surface equals the mapper's actual employee lookup (no drift between report/code)."""
    schema = get_target_schema()
    assert len(deterministic_header_keys(schema)) == 97
