"""Deterministic acceptance / escalation policy.

This runs AFTER the model proposal is structurally validated. It is the defensible
boundary between what the agent decides autonomously and what it escalates.

Documented rules (a valid schema target + compatible type are necessary, never
sufficient):

1. Unresolved: proposed target is null.
   - If the model also flags ambiguity or lists alternatives -> NEEDS_REVIEW.
   - Otherwise -> UNMAPPED (a recorded "this column maps to nothing" disposition,
     not an escalation). Any uncovered required field stays visible elsewhere.
2. Invalid target: proposed field is not in the schema -> NEEDS_REVIEW
   (never silently accepted).
3. Type incompatibility: observed values are incompatible with the target's value
   type -> NEEDS_REVIEW.
4. Date-role ambiguity: if the proposed target is in the schema's date-role group
   (hire_date / contract_start_date), auto-accept ONLY when the header clearly
   disambiguates which role it is and the sibling is not a competing candidate.
   A generic/underspecified date header (e.g. "Start") -> NEEDS_REVIEW *even if the
   model is highly confident*. Date-like values alone never decide the role.
4b. Generic sibling/role ambiguity (M3F §F): if the proposed target belongs to a
   declared sibling family (email {work_email, personal_email}, phone {mobile_phone,
   work_phone}, name {full_name, preferred_name}, identifier {employee_id,
   manager_employee_id}, date {hire/contract/dob/probation/termination}), the header
   must carry a DISTINCTIVE qualifier for that specific member. A header carrying only
   the shared family word ("Email", "Phone", "Name", "ID", "Start Date") -> NEEDS_REVIEW
   *even when the model returns alternatives=[] and the type corroborates*; a header
   whose distinctive qualifier points at a DIFFERENT sibling -> NEEDS_REVIEW. A header
   with the right distinctive qualifier ("Corporate Email", "Office Telephone") may
   auto-accept, and that qualifier match itself is the independent semantic support (§6).
   Model confidence is never a substitute for a distinctive qualifier.
5. Competing meaning: the model flagged ambiguity, or listed any alternative target
   -> NEEDS_REVIEW.
6. Independent semantic support (heuristic, labelled): auto-accept requires at least
   one shared concept token between the source header and the target concept
   (field name + the schema's disambiguation keywords). The model's self-reported
   confidence is NOT used as a gate and never overrides missing support.

Within-table conflicts (two accepted columns targeting the same field) are resolved
by :func:`resolve_table_conflicts`, which downgrades all conflicting sides to review
so an accepted mapping is never silently overwritten.

Cross-file mappings to the same target are allowed (handled at assess/finalize).

policy.v2 (M3A.2): the set of valid destinations is the EFFECTIVE contract's target paths (core
scalar fields + collection item paths + the tenant's custom-field paths). A proposed path outside
that set is never accepted — mapping never invents an unknown target path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .llm.schema import ProposalItem
from .profiling import ColumnProfile
from .schema_loader import TargetField, TargetSchema
from .text_util import tokens as _tok

POLICY_VERSION = "policy.v2"


class Decision(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    NEEDS_REVIEW = "needs_review"
    UNMAPPED = "unmapped"


@dataclass
class PolicyResult:
    source_column_id: str
    header: str
    decision: Decision
    target_field: str | None
    reason: str
    candidate_target_fields: list[str] = field(default_factory=list)
    evidence_summary: dict = field(default_factory=dict)


def _tokens(s: str) -> set[str]:
    return _tok(s)


def _concept_tokens(tf: TargetField) -> set[str]:
    toks = _tokens(tf.name)
    for kw in tf.disambiguation_keywords:
        toks |= _tokens(kw)
    return toks


def _header_support(header: str, tf: TargetField) -> tuple[set[str], float]:
    ht = _tokens(header)
    matched = ht & _concept_tokens(tf)
    ratio = round(len(matched) / len(ht), 3) if ht else 0.0
    return matched, ratio


def _kw_hits(header: str, tf: TargetField) -> bool:
    hl = header.lower()
    for kw in tf.disambiguation_keywords:
        if " " in kw:
            if kw in hl:
                return True
        elif kw in _tokens(header):
            return True
    return False


def _member_tokens(schema: TargetSchema, group, member: str) -> set[str]:
    """Tokens that could indicate `member`: the group's declared distinctive qualifiers plus the
    member's disambiguation keywords. Deliberately EXCLUDES the field-name tokens, because a sibling's
    key can share a family word (e.g. ``manager_employee_id`` contains ``employee``) which would
    otherwise cancel a genuine distinctive token of the other member."""
    toks: set[str] = set(group.qualifier_map().get(member, ()))
    tf = schema.get(member)
    if tf is not None:
        for kw in tf.disambiguation_keywords:
            toks |= _tokens(kw)
    return toks


def _distinctive_tokens(schema: TargetSchema, group, member: str) -> set[str]:
    """Tokens that pin `member` specifically within its sibling group: its tokens minus the shared
    generic family words and minus any token also used by a sibling (so a shared word never decides)."""
    generic = set(group.generic)
    mine = _member_tokens(schema, group, member)
    others: set[str] = set()
    for m in group.members:
        if m != member:
            others |= _member_tokens(schema, group, m)
    return (mine - generic) - others


def _sibling_ambiguity(header: str, target: str, schema: TargetSchema):
    """M3F §F. Given a proposed sibling-group target, decide whether the header distinctively supports
    it. Returns (verdict, candidates, detail):
      * verdict "ok"       -> header distinctively pins `target`; strong support (satisfies gate 6).
      * verdict "generic"  -> header carries only the family concept word (e.g. bare "Email"); ambiguous
                              across the whole group -> REVIEW even if the model is confident.
      * verdict "conflict" -> header distinctively points at a DIFFERENT sibling than proposed -> REVIEW.
      * verdict None       -> not a sibling-group target, or no family signal at all (let later gates
                              decide, preserving the plain "no semantic support" path).
    """
    group = schema.sibling_group_for(target)
    if group is None:
        return None, [], {}
    # hire_date / contract_start_date are governed by the dedicated date-role gate (gate 4), which
    # returns their own 2-field candidate list; don't double-handle them here.
    if target in schema.date_role_group:
        return None, [], {}
    hdr = _tokens(header)
    matched = [m for m in group.members if hdr & _distinctive_tokens(schema, group, m)]
    detail = {"sibling_group": group.key, "group_members": list(group.members),
              "distinctively_matched": matched}
    if target in matched and len(matched) == 1:
        return "ok", [target], detail
    if matched:  # matches a sibling (or several) but not uniquely the proposed target
        cands = sorted(set(matched) | {target})
        return "conflict", cands, detail
    if hdr & set(group.generic):  # a family word (email/phone/name/id/date) but no distinctive qualifier
        return "generic", list(group.members), detail
    return None, [], detail


def _structural_evidence(tf: TargetField, profile: ColumnProfile) -> dict:
    """Target-specific deterministic evidence recorded on the decision (M3E §7). This is the
    corroboration a model proposal is auto-accepted WITH — never the model's self-reported confidence.
    It is additive/observational (the accept/review decision itself is made by the rules below); it
    exists so a reviewer / LangSmith / the metrics UI can see WHY an unseen header was trusted."""
    ind = profile.format_indicators or {}
    ev: dict = {"redaction_class": ind.get("redaction_class"),
                "distinct_count": profile.distinct_count,
                "non_empty_count": profile.non_empty_count}
    t = tf.value_type
    if t == "email":
        ev["email_ratio"] = ind.get("email_ratio")
    elif t == "date":
        ev.update({"iso_date_ratio": ind.get("iso_date_ratio"),
                   "slash_date_ratio": ind.get("slash_date_ratio"),
                   "looks_date_like": ind.get("looks_date_like")})
    elif t == "phone":
        ev["phone_like_ratio"] = ind.get("phone_like_ratio")
    elif t == "number":
        ev["numeric_ratio"] = ind.get("numeric_ratio")
    elif t in ("enum", "multiselect"):
        ev.update({"likely_low_cardinality_category": ind.get("likely_low_cardinality_category"),
                   "duplicate_ratio": ind.get("duplicate_ratio")})
    if tf.name in ("employee_id", "manager_employee_id"):
        ev["likely_identifier"] = ind.get("likely_identifier")
        ev["has_leading_zero_values"] = ind.get("has_leading_zero_values")
    if tf.name == "manager_employee_id":
        # The column mapping may auto-accept on header/type support, but the ID VALUES must still clear
        # referential proof against the employee-key domain — done in source analysis, not here.
        ev["referential_proof"] = "deferred to source analysis (analyze_source._reference_domain)"
    return ev


def _type_compatible(tf: TargetField, profile: ColumnProfile) -> tuple[bool, str]:
    ind = profile.format_indicators
    if profile.non_empty_count == 0:
        return True, "no non-empty values to check"
    if tf.value_type == "email":
        if float(ind.get("email_ratio", 0.0)) >= 0.5:
            return True, "values look like emails"
        return False, "values do not look like email addresses"
    if tf.value_type == "date":
        if ind.get("looks_date_like") or (
            float(ind.get("iso_date_ratio", 0)) + float(ind.get("slash_date_ratio", 0)) >= 0.5
        ):
            return True, "values look date-like"
        return False, "values do not look like dates"
    if tf.value_type == "enum":
        numeric = profile.observed_types.get("number", 0)
        if numeric and numeric >= profile.non_empty_count:
            return False, "values are numeric, not category labels"
        return True, "values are category-like strings"
    if tf.value_type == "phone":
        if float(ind.get("email_ratio", 0)) >= 0.5:
            return False, "values look like emails, not phone numbers"
        if ind.get("looks_date_like"):
            return False, "values look like dates, not phone numbers"
        return True, "values are phone-compatible strings"
    if tf.value_type == "number":
        if profile.observed_types.get("number", 0) >= max(1, profile.non_empty_count) / 2:
            return True, "values are numeric"
        return False, "values are not numeric"
    if tf.value_type == "boolean":
        if profile.observed_types.get("bool", 0) >= max(1, profile.non_empty_count) / 2:
            return True, "values are boolean-like"
        return False, "values are not boolean-like"
    if tf.value_type == "multiselect":
        return True, "values are string-compatible"
    # string (employee_id, full_name): accept text/number; reject pure date/email columns.
    if tf.value_type == "string":
        if float(ind.get("email_ratio", 0)) >= 0.8:
            return False, "values look like emails, not a plain string field"
        if ind.get("looks_date_like"):
            return False, "values look like dates, not a plain string field"
        return True, "values are string-compatible"
    return True, "no type constraint"


def classify_proposal(
    proposal: ProposalItem, profile: ColumnProfile, schema: TargetSchema
) -> PolicyResult:
    header = proposal.source_header or profile.header
    valid_fields = set(schema.target_paths)          # effective contract: core + collection + tenant custom
    all_paths = list(schema.target_paths)

    base_summary = {
        "header": header,
        "proposed_target_field": proposal.proposed_target_field,
        "model_alternatives": proposal.alternative_target_fields,
        "model_is_ambiguous": proposal.is_ambiguous,
        "model_ambiguity_reason": proposal.ambiguity_reason,
        "model_confidence": proposal.confidence,  # advisory only, never a gate
        "model_evidence": proposal.evidence,
        "affected_non_empty_rows": profile.non_empty_count,
        "observed_types": profile.observed_types,
        "sample_values": profile.samples,
        "policy_version": POLICY_VERSION,
    }

    def result(decision, target, reason, candidates=None, extra=None):
        summ = dict(base_summary)
        if extra:
            summ.update(extra)
        return PolicyResult(
            source_column_id=proposal.source_column_id,
            header=header,
            decision=decision,
            target_field=target,
            reason=reason,
            candidate_target_fields=candidates or [],
            evidence_summary=summ,
        )

    target = proposal.proposed_target_field

    # 1. Unresolved.
    if target is None:
        if proposal.is_ambiguous or proposal.alternative_target_fields:
            cands = [t for t in proposal.alternative_target_fields if t in valid_fields]
            return result(
                Decision.NEEDS_REVIEW, None,
                "Model left the mapping unresolved and flagged competing meanings.",
                candidates=cands or all_paths,
            )
        return result(
            Decision.UNMAPPED, None,
            "Model proposed no target for this column; recorded as unmapped (no target).",
        )

    # 2. Invalid target.
    if target not in valid_fields:
        return result(
            Decision.NEEDS_REVIEW, None,
            f"Proposed target '{target}' is not a destination in the effective contract "
            f"({schema.version}); mapping never invents an unknown target path.",
            candidates=all_paths,
        )

    tf = schema.get(target)
    assert tf is not None
    struct = _structural_evidence(tf, profile)

    # 3. Type compatibility.
    type_ok, type_reason = _type_compatible(tf, profile)
    if not type_ok:
        return result(
            Decision.NEEDS_REVIEW, target,
            f"Type incompatibility for '{target}': {type_reason}.",
            candidates=[target] + [t for t in proposal.alternative_target_fields if t in valid_fields],
            extra={"type_check": type_reason, "structural_evidence": struct},
        )

    # 4. Date-role ambiguity (schema-driven; independent of model confidence).
    if target in schema.date_role_group:
        siblings = [f for f in schema.date_role_group if f != target]
        matches_me = _kw_hits(header, tf)
        matches_sibling = any(_kw_hits(header, schema.get(s)) for s in siblings if schema.get(s))
        sibling_alt = any(s in proposal.alternative_target_fields for s in siblings)
        if proposal.is_ambiguous or sibling_alt or (not matches_me) or matches_sibling:
            reason = (
                "Date-role ambiguity: the header does not clearly identify whether this is a "
                "hire date or a contract start date, and date-like values alone cannot decide it."
            )
            return result(
                Decision.NEEDS_REVIEW, target, reason,
                candidates=list(schema.date_role_group),
                extra={"date_role_group": list(schema.date_role_group), "header_disambiguates": matches_me,
                       "structural_evidence": struct},
            )

    # 4b. Generic sibling/role ambiguity (M3F §F; schema-driven; independent of model confidence).
    # An overconfident model may map a GENERIC header ("Email", "Phone", "Name", "ID", "Start Date")
    # to one member of a same-shape family with no alternatives. Type/shape corroborate the family but
    # never the specific member, so a generic header is escalated even when the model returns
    # alternatives=[]. A header with a distinctive qualifier ("Corporate Email", "Office Telephone")
    # may still auto-accept, and the qualifier match counts as independent semantic support (gate 6).
    sib_verdict, sib_candidates, sib_detail = _sibling_ambiguity(header, target, schema)
    sibling_confirmed = sib_verdict == "ok"
    if sib_verdict == "generic":
        return result(
            Decision.NEEDS_REVIEW, target,
            f"Generic '{sib_detail.get('sibling_group', 'role')}' header: '{header}' names the field "
            f"family but not which specific field it is; escalating rather than letting model "
            f"confidence pick one of {sib_candidates}.",
            candidates=sib_candidates,
            extra={"sibling_role_ambiguity": sib_detail, "structural_evidence": struct},
        )
    if sib_verdict == "conflict":
        return result(
            Decision.NEEDS_REVIEW, target,
            f"Sibling-role conflict: the header '{header}' points at a different field than the "
            f"proposed '{target}'; escalating to confirm the destination among {sib_candidates}.",
            candidates=sib_candidates,
            extra={"sibling_role_ambiguity": sib_detail, "structural_evidence": struct},
        )

    # 5. Competing meaning from the model.
    valid_alts = [t for t in proposal.alternative_target_fields if t in valid_fields and t != target]
    if proposal.is_ambiguous or valid_alts:
        return result(
            Decision.NEEDS_REVIEW, target,
            "A plausible competing target meaning exists; escalating rather than guessing.",
            candidates=[target] + valid_alts,
            extra={"structural_evidence": struct},
        )

    # 6. Independent semantic support (heuristic, labelled).
    matched, ratio = _header_support(header, tf)
    if not matched and not sibling_confirmed:
        return result(
            Decision.NEEDS_REVIEW, target,
            "No independent semantic support: the source header shares no concept token with "
            f"the target '{target}'. Not auto-accepting on model confidence alone.",
            candidates=[target] + valid_alts,
            extra={"heuristic_header_overlap_ratio": ratio, "matched_tokens": sorted(matched),
                   "structural_evidence": struct},
        )

    support = (f"header supports '{target}' (shared tokens {sorted(matched)})" if matched
               else f"header distinctively identifies '{target}' among its sibling fields")
    return result(
        Decision.AUTO_ACCEPT, target,
        f"Auto-accepted: {support}, type compatible ({type_reason}), no competing meaning.",
        candidates=[target],
        extra={
            "heuristic_header_overlap_ratio": ratio,
            "heuristic_label": "header_token_overlap (weight: presence of >=1 shared concept token; not an accuracy score)",
            "matched_tokens": sorted(matched),
            "sibling_role_disambiguation": sib_detail if sibling_confirmed else None,
            "type_check": type_reason,
            "structural_evidence": struct,
        },
    )


def resolve_table_conflicts(results: list[PolicyResult]) -> list[PolicyResult]:
    """Downgrade within-table auto-accepts that target the same field to review.

    An accepted mapping is never silently overwritten by another column in the same
    source table. Cross-file conflicts are intentionally NOT resolved here.
    """
    by_target: dict[str, list[PolicyResult]] = {}
    for r in results:
        if r.decision is Decision.AUTO_ACCEPT and r.target_field:
            by_target.setdefault(r.target_field, []).append(r)

    for target, group in by_target.items():
        if len(group) > 1:
            headers = [g.header for g in group]
            for g in group:
                g.decision = Decision.NEEDS_REVIEW
                g.reason = (
                    f"Within-table conflict: columns {headers} in this source table all map to "
                    f"'{target}'. Escalating so an accepted mapping is not silently overwritten."
                )
                g.candidate_target_fields = [target]
                g.evidence_summary["within_table_conflict"] = headers
    return results
