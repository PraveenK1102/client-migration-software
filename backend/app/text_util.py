"""Shared, dependency-free header tokenizer.

Splits on non-alphanumeric boundaries AND camelCase / concatenated boundaries so
headers like ``EmployeeNumber`` -> {employee, number} and ``EmpID`` -> {emp, id}
tokenize the same way a spaced header would. Used by the policy and the fake
adapter so header matching is consistent.
"""
from __future__ import annotations

import re

# Matches: ACRONYM before Word (IDValue->ID), leading-cap word, all-lower run, all-upper run, digits.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", text or ""):
        if not part:
            continue
        pieces = _CAMEL.findall(part)
        for p in pieces or [part]:
            out.add(p.lower())
    return out
