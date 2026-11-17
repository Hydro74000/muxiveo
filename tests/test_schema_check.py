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
