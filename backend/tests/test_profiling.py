"""Profiling: counts, indicators, and bounded samples (only samples reach the model)."""
from __future__ import annotations

from app.ingest import parse_file
from app.profiling import profile_table


def _profiles(name, data, **kw):
    pf = parse_file(filename=name, data=data, stored_name="s", max_bytes=10_000_000, max_rows=1000)
    return profile_table(pf.tables[0], pf.records, **kw)


def test_profile_counts_and_email_indicator():
    csv = b"Mail,Dept\nalice@x.com,Engineering\nbob@x.com,\ncarol@x.com,Sales\n"
    profs = _profiles("t.csv", csv)
    mail = profs[0]
    assert mail.header == "Mail"
    assert mail.non_empty_count == 3 and mail.missing_count == 0
    assert mail.format_indicators["email_ratio"] == 1.0
    dept = profs[1]
    assert dept.non_empty_count == 2 and dept.missing_count == 1


def test_profile_date_indicators_iso_and_ambiguous_slash():
    csv = b"Joined\n2021-03-15\n03/04/2024\n2020-01-01\n"
    profs = _profiles("d.csv", csv)
    ind = profs[0].format_indicators
    assert ind["looks_date_like"] is True
    assert ind["ambiguous_slash_dates"] is True
    assert ind["slash_date_ratio"] > 0


def test_samples_bounded_and_truncated():
    rows = "H\n" + "\n".join(f"value-{i}" for i in range(50)) + "\n"
    profs = _profiles("s.csv", rows.encode(), max_samples=3, sample_max_chars=6)
    p = profs[0]
    assert len(p.samples) == 3           # bounded
    assert all(len(s) <= 7 for s in p.samples)  # truncated (+ ellipsis char)
    assert p.distinct_count == 50        # full distinct count still recorded


def test_leading_zero_indicator():
    csv = b"EmployeeNumber\n001\n0027\n123\n"
    profs = _profiles("z.csv", csv)
    assert profs[0].format_indicators["has_leading_zero_values"] is True
