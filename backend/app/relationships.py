"""Table-level code/display relationship discovery (M3C).

A careful human notices that ``GenderID`` + ``Sex`` (or ``MaritalStatusID`` + ``MaritalDesc``) are
two representations of the SAME concept — a source-system code and its human-readable label — and
migrates the meaningful one instead of both. This module discovers such pairs from the ACTUAL data,
never from column names alone, and PROVES the relationship statistically before anything is called
redundant.

Bounded (see order §22): only genuinely low-cardinality columns are candidates, and pairs are
restricted to columns that share a name stem (``DeptID``/``Department``) or have matching small
domains — so this never becomes O(rows·columns²) on a wide file. For each candidate pair it computes
functional-dependency coverage in both directions, contradictions, null mismatches and a codebook.

A perfect 1:1 pairing is reported; an INCONSISTENT pairing (e.g. ``DeptID`` -> ``Department`` that is
not one-to-one) is reported as such and must NOT be treated as redundant. The redundancy DISPOSITION
(which column, and whether the label is mapped to a target semantic field) is decided by the caller
in :mod:`transform_plan` / the graph — this module only supplies proven evidence.

Pure and deterministic. No model calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .profiling import ColumnProfile

RELATIONSHIP_VERSION = "rel.v1"

# Trailing tokens stripped to compute a shared "concept stem" for candidate selection only.
_STEM_SUFFIXES = ("id", "code", "cd", "key", "no", "num", "number", "desc", "description",
                  "name", "label", "status", "type", "text", "value")


def _tokens(header: str) -> list[str]:
    # split camelCase and delimiter-separated headers: "MaritalStatusID" -> [marital, status, id]
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", header or "")
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t]


def _stem(header: str) -> str:
    toks = _tokens(header)
    while len(toks) > 1 and toks[-1] in _STEM_SUFFIXES:
        toks = toks[:-1]
    return "".join(toks)


def _looks_code(profile: ColumnProfile) -> float:
    """Heuristic 'is this the CODE side' score (higher = more code-like)."""
    ind = profile.format_indicators
    score = 0.0
    if float(ind.get("numeric_ratio", 0)) >= 0.8:
        score += 2.0
    toks = set(_tokens(profile.header))
    if {"id", "code", "cd", "key", "num", "no"} & toks:
        score += 2.0
    st = profile.stats or {}
    if isinstance(st.get("max_len"), int) and st["max_len"] <= 4:
        score += 1.0
    return score


# A code column must clear this score before its display twin can auto-retire it as redundant.
# The classic source-system code (``GenderID`` 0/1, ``MaritalStatusID`` 1/2/3) scores >= 3.0
# (numeric + an id/code token, or numeric + short width); an independent textual attribute that
# merely happens to line up 1:1 on a small sample (``T-Shirt Size`` M/L/XL vs ``Badge Colour``)
# scores ~1.0 and is never dropped. See :func:`source_intelligence.analyze_relationships_for_table`.
CODE_SHAPE_MIN_SCORE = 3.0


def _is_categorical_member(p: ColumnProfile, max_cardinality: int) -> bool:
    """Is this column a plausible code/display MEMBER (a repeating categorical column)?

    Requiring ``distinct_count < non_empty_count`` (values actually repeat) is what defeats the
    trivial-bijection trap: on a small table every pair of all-unique columns (ids, names, emails,
    dates, phones) is a perfect 1:1 mapping — a coincidence of equal cardinality, not a code/display
    relationship. An all-unique / identifier-like column carries no evidence of a stable code<->label
    map, so it is never a candidate. This is also the §22 performance guard (only genuinely
    low-cardinality repeating columns are ever paired)."""
    if p.non_empty_count < 2 or not (2 <= p.distinct_count <= max_cardinality):
        return False
    if p.distinct_count >= p.non_empty_count:
        return False   # all-unique: no repetition ⇒ any 1:1 alignment is coincidental
    if p.format_indicators.get("likely_identifier"):
        return False
    return True


@dataclass
class PairEvidence:
    code_profile_id: str
    label_profile_id: str
    code_col: int
    label_col: int
    code_header: str
    label_header: str
    relationship: str            # one_to_one | code_display | inconsistent
    a_to_b_coverage: float       # code -> label functional-dependency coverage
    b_to_a_coverage: float       # label -> code functional-dependency coverage
    distinct_code: int
    distinct_label: int
    matched_rows: int
    contradictions: int
    null_mismatch: int
    codebook: list[dict] = field(default_factory=list)   # [{"code","label"}]
    reason: str = ""
    code_score: float = 0.0      # _looks_code(code side): how code-shaped the code column is
    label_score: float = 0.0     # _looks_code(label side)

    @property
    def code_is_coded(self) -> bool:
        """The code column is a genuine source-system code (numeric/id-shaped), not just the
        higher-scoring of two textual attributes — the precondition for auto-redundancy."""
        return self.code_score >= CODE_SHAPE_MIN_SCORE

    def to_evidence(self) -> dict:
        return {
            "version": RELATIONSHIP_VERSION, "relationship": self.relationship,
            "code_header": self.code_header, "label_header": self.label_header,
            "code_col": self.code_col, "label_col": self.label_col,
            "code_profile_id": self.code_profile_id, "label_profile_id": self.label_profile_id,
            "a_to_b_coverage": self.a_to_b_coverage, "b_to_a_coverage": self.b_to_a_coverage,
            "distinct_code": self.distinct_code, "distinct_label": self.distinct_label,
            "matched_rows": self.matched_rows, "contradictions": self.contradictions,
            "null_mismatch": self.null_mismatch, "codebook": self.codebook[:50], "reason": self.reason,
            "code_score": self.code_score, "label_score": self.label_score,
            "code_is_coded": self.code_is_coded,
        }


def _coverage(pairs: list[tuple[str, str]], src_idx: int, dst_idx: int) -> tuple[float, int]:
    """Functional-dependency coverage src->dst over aligned (a,b) pairs. Returns (coverage, contradictions).

    For each source value, the dominant dst value is the mode; coverage = fraction of rows consistent
    with each source's dominant dst; contradictions = rows that disagree with their dominant dst."""
    groups: dict[str, dict[str, int]] = {}
    for p in pairs:
        s, d = p[src_idx], p[dst_idx]
        groups.setdefault(s, {})
        groups[s][d] = groups[s].get(d, 0) + 1
    total = len(pairs)
    consistent = 0
    for s, dcounts in groups.items():
        consistent += max(dcounts.values())
    if total == 0:
        return 0.0, 0
    return round(consistent / total, 4), total - consistent


def detect_code_display_pairs(value_columns: dict[int, list], profiles: list[ColumnProfile], *,
                              max_cardinality: int = 64, max_pairs: int = 200) -> list[PairEvidence]:
    """Discover code/display column pairs in one table. ``value_columns`` maps col_index -> the full
    aligned column of raw cell values (None for empty)."""
    by_col = {p.col_index: p for p in profiles}
    # Candidate columns: genuinely low-cardinality REPEATING categoricals (never all-unique keys).
    cand = [p for p in profiles if _is_categorical_member(p, max_cardinality)]
    cand.sort(key=lambda p: p.col_index)

    # Candidate PAIRS: share a concept stem OR have equal small domains (bounded).
    pair_indices: list[tuple[int, int]] = []
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i], cand[j]
            stem_match = _stem(a.header) and _stem(a.header) == _stem(b.header)
            equal_small = (a.distinct_count == b.distinct_count and a.distinct_count <= 24)
            if stem_match or equal_small:
                pair_indices.append((a.col_index, b.col_index))
            if len(pair_indices) >= max_pairs:
                break
        if len(pair_indices) >= max_pairs:
            break

    out: list[PairEvidence] = []
    for ci, cj in pair_indices:
        col_a = value_columns.get(ci, [])
        col_b = value_columns.get(cj, [])
        n = min(len(col_a), len(col_b))
        aligned: list[tuple[str, str]] = []
        null_mismatch = 0
        for r in range(n):
            va = col_a[r]
            vb = col_b[r]
            va = va.strip() if isinstance(va, str) else va
            vb = vb.strip() if isinstance(vb, str) else vb
            a_empty = va is None or va == ""
            b_empty = vb is None or vb == ""
            if a_empty and b_empty:
                continue
            if a_empty != b_empty:
                null_mismatch += 1
                continue
            aligned.append((str(va), str(vb)))
        if len(aligned) < 2:
            continue

        a2b, contra_ab = _coverage(aligned, 0, 1)
        b2a, contra_ab2 = _coverage(aligned, 1, 0)
        distinct_a = len({p[0] for p in aligned})
        distinct_b = len({p[1] for p in aligned})

        pa, pb = by_col[ci], by_col[cj]
        score_a, score_b = _looks_code(pa), _looks_code(pb)
        # Decide which side is the CODE and which is the LABEL.
        if score_a >= score_b:
            code_p, label_p, code_col, label_col = pa, pb, ci, cj
            cov_code_label, cov_label_code = a2b, b2a
            code_score, label_score = score_a, score_b
            code_first = True
        else:
            code_p, label_p, code_col, label_col = pb, pa, cj, ci
            cov_code_label, cov_label_code = b2a, a2b
            code_score, label_score = score_b, score_a
            code_first = False
        contradictions = max(contra_ab, contra_ab2)

        perfect = (cov_code_label >= 0.999 and cov_label_code >= 0.999
                   and distinct_a == distinct_b and contradictions == 0)
        if perfect:
            relationship = "one_to_one"
            reason = "code and label are a perfect 1:1 pairing across every non-null row"
        elif cov_code_label >= 0.999:
            relationship = "code_display"
            reason = ("each code maps to exactly one label, but the mapping is not 1:1 "
                      "(several codes may share a label); not a redundant duplicate")
        else:
            relationship = "inconsistent"
            reason = (f"code->label coverage {cov_code_label} and label->code {cov_label_code}: "
                      f"the columns are not a clean code/display pair")

        # Codebook (code -> dominant label), bounded.
        book: dict[str, str] = {}
        for a, b in aligned:
            code_v, label_v = (a, b) if code_first else (b, a)
            book.setdefault(code_v, label_v)
        codebook = [{"code": k, "label": v} for k, v in sorted(book.items())]

        out.append(PairEvidence(
            code_profile_id=code_p.profile_id, label_profile_id=label_p.profile_id,
            code_col=code_col, label_col=label_col,
            code_header=code_p.header, label_header=label_p.header,
            relationship=relationship, a_to_b_coverage=cov_code_label, b_to_a_coverage=cov_label_code,
            distinct_code=distinct_a if code_first else distinct_b,
            distinct_label=distinct_b if code_first else distinct_a,
            matched_rows=len(aligned), contradictions=contradictions, null_mismatch=null_mismatch,
            codebook=codebook, reason=reason, code_score=code_score, label_score=label_score))
    return out
