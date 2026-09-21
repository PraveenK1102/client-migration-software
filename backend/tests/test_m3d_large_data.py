"""M3D section 10: large-data performance sanity (bounded, deterministic, offline).

Proves a 20k-row table ingests COMPLETELY (no loss) and profiles, and that the MODEL-VISIBLE payload
per column stays bounded regardless of row count (so model cost scales with columns, not employees).
Kept to a single 20k case so the default suite stays fast.
"""
from __future__ import annotations

import io

from app.ingest import parse_file
from app.model_projection import model_safe_samples
from app.profiling import profile_table

_HEADERS = ["EmployeeID", "Full Name", "Work Email", "Department", "Date of Joining", "Status"]


def _csv(n: int) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(_HEADERS) + "\n")
    for i in range(1, n + 1):
        buf.write(f"E{i:06d},Person {i},person{i}@example.com,"
                  f"{['Engineering','Sales','Finance','People Operations'][i % 4]},"
                  f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/2015,"
                  f"{['Active','On Leave','Terminated'][i % 3]}\n")
    return buf.getvalue().encode()


def test_20k_rows_ingest_completely_and_payload_is_bounded():
    n = 20_000
    pf = parse_file(filename="big.csv", data=_csv(n), stored_name="s",
                    max_bytes=200_000_000, max_rows=n)
    # No silent loss: every row is present.
    assert pf.tables[0].n_rows == n and len(pf.records) == n
    assert pf.records[0].cells[0].value == "E000001"
    assert pf.records[-1].cells[0].value == f"E{n:06d}"

    profiles = profile_table(pf.tables[0], pf.records)
    # The model-visible payload per column is bounded no matter how many rows exist.
    for p in profiles:
        assert len(model_safe_samples(p)) <= 5
    # Distinct-count statistics are still exact over the full column (computed, not sampled).
    email = next(p for p in profiles if p.header == "Work Email")
    assert email.distinct_count == n            # all 20k emails counted
    assert model_safe_samples(email) == ["<EMAIL>"]   # but none of them sent to the model
