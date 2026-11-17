"""Vérificateur du cœur (``core/service/schema_check.py``) : sémantique JSON Schema sur le sous-ensemble publié."""

from __future__ import annotations

import re

import pytest

from core.service.schema_check import portable_pattern, schema_errors

_ROOT: dict = {}


@pytest.mark.parametrize("value, schema", [
    ([True], {"const": [1]}),
    ({"a": True}, {"enum": [{"a": 1}]}),
    (0, {"const": False}),
    (True, {"type": "integer"}),
    ([1, 2], {"prefixItems": [{"type": "integer"}], "items": False}),
    ({"a": 1}, {"propertyNames": False}),
    ({"a": 1}, {"properties": {"a": {"type": "integer"}}, "patternProperties": {"^a": {"type": "string"}}}),
    ("0\n", {"pattern": "^[0-9]+$"}),
    ({"0\n": 1}, {"propertyNames": {"pattern": "^[0-9]+$"}}),
    (0, {"exclusiveMinimum": 0}),
    (1, False),
    (1, {"anyOf": [False]}),
    (1, {"anyOf": []}),
    (1, {"oneOf": [True, True]}),
    (1, {"not": True}),
    (1, {"not": {}}),
    (["a", "a"], {"uniqueItems": True}),
    ([1, 1.0], {"uniqueItems": True}),
    ([{"a": 1, "b": 2}, {"b": 2, "a": 1}], {"uniqueItems": True}),
])
def test_refused(value, schema):
    assert schema_errors(value, schema, _ROOT)


@pytest.mark.parametrize("value, schema", [
    ([1], {"const": [1.0]}),
    (1.0, {"type": "integer"}),
    ("0", {"pattern": "^[0-9]+$"}),
    ({"_x": 1}, {"properties": {}, "patternProperties": {"^[_$]": {}}, "additionalProperties": False}),
    (1, True),
    (1, {"anyOf": [False, True]}),
    (1, {"oneOf": [False, True]}),
    (1, {"not": False}),
    ([1, True, "1"], {"uniqueItems": True}),
    (["a", "a"], {"uniqueItems": False}),
])
def test_accepted(value, schema):
    assert not schema_errors(value, schema, _ROOT)


def test_portable_patterns_end_the_text_in_python_and_ecma():
    assert portable_pattern("^[_$]a$") == r"^[_$]a(?![\s\S])"
    assert portable_pattern(r"^a\$$") == r"^a\$(?![\s\S])"
    assert re.search(portable_pattern("^[0-9]+$"), "0\n") is None and re.search(portable_pattern("^[0-9]+$"), "0")


def test_published_model_patterns_refuse_a_trailing_newline():
    from core.service.schemas.encode import EncodeBlock

    published = repr(EncodeBlock.json_schema())
    assert "$'" not in published and "(?![\\\\s\\\\S])" in published


def test_walk_stays_linear_and_skips_unconstrained_values():
    """Chemins construits au seul écart ; une très longue clé et une longue liste libre ne coûtent qu'un parcours."""
    import time

    value = {"k" * 1_000_000: [0] * 1_000_000, "x": False}
    schema = {"properties": {"x": {"type": "string"}}, "additionalProperties": {"type": "array"}}
    started = time.monotonic()
    assert schema_errors(value, schema, _ROOT) == [("x", "string attendu")]
    assert schema_errors({"_" + "k" * 1_000_000: [0] * 1_000_000}, {"additionalProperties": {}}, _ROOT) == []
    assert time.monotonic() - started < 10


def test_distinct_keeps_one_diagnostic_per_cause():
    schema = {"type": "object", "properties": {"a": {"anyOf": [{"required": ["y"]}, {"required": ["z"]}],
                                                     "properties": {"x": {"type": "string"}}}}}
    assert len(schema_errors({"a": {"x": 1}}, schema, _ROOT)) == 2
    assert schema_errors({"a": {"x": 1}}, schema, _ROOT, distinct=True) == [("a.x", "string attendu")]


def test_optional_hdr_text_is_refused_in_linear_time():
    import time

    from core.hdr_formats import MAX_CLL_BODY, optional_text_pattern

    pattern = re.compile(optional_text_pattern(MAX_CLL_BODY))
    started = time.monotonic()
    assert pattern.fullmatch(" " * 200_000 + "!") is None
    assert time.monotonic() - started < 2
    assert all(pattern.fullmatch(text) for text in ("", "  ", "1000,400", " \t1000,400 ", "1000,400\t"))
    assert not any(pattern.fullmatch(text) for text in ("1000", " 1000, 400", "1000,400 x"))
