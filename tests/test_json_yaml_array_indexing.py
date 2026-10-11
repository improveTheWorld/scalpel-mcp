import pytest
from ast_graph_server.parsers.json_parser import JSONParser
from ast_graph_server.parsers.yaml_parser import YAMLParser


def test_json_root_array_and_nested_array_indexing():
    code = b'''[
  {"query": "q1", "expect": [1]},
  {"query": "q2", "expect": [2]}
]'''
    parser = JSONParser()
    res = parser.parse_file("file.json", code)
    assert res.syntax_ok is True

    sym_by_id = {s.id: s for s in res.symbols}

    assert "file.json::[0]" in sym_by_id
    assert sym_by_id["file.json::[0]"].kind == "object"
    assert sym_by_id["file.json::[0]"].parent_id is None

    assert "file.json::[0].query" in sym_by_id
    assert sym_by_id["file.json::[0].query"].kind == "property"
    assert sym_by_id["file.json::[0].query"].parent_id == "file.json::[0]"

    assert "file.json::[0].expect" in sym_by_id
    assert sym_by_id["file.json::[0].expect"].kind == "array"
    assert sym_by_id["file.json::[0].expect"].parent_id == "file.json::[0]"

    assert "file.json::[0].expect[0]" in sym_by_id
    assert sym_by_id["file.json::[0].expect[0]"].kind == "property"
    assert sym_by_id["file.json::[0].expect[0]"].parent_id == "file.json::[0].expect"

    assert "file.json::[1]" in sym_by_id
    assert sym_by_id["file.json::[1]"].kind == "object"
    assert sym_by_id["file.json::[1]"].parent_id is None

    assert "file.json::[1].query" in sym_by_id
    assert sym_by_id["file.json::[1].query"].kind == "property"
    assert sym_by_id["file.json::[1].query"].parent_id == "file.json::[1]"


def test_json_object_with_array_properties():
    code = b'''{
  "tasks": [
    {"name": "clean", "val": 5},
    {"name": "build", "val": 6}
  ],
  "tags": ["a", "b"]
}'''
    parser = JSONParser()
    res = parser.parse_file("file.json", code)
    assert res.syntax_ok is True

    sym_by_id = {s.id: s for s in res.symbols}

    assert "file.json::tasks" in sym_by_id
    assert sym_by_id["file.json::tasks"].kind == "array"

    assert "file.json::tasks[0]" in sym_by_id
    assert sym_by_id["file.json::tasks[0]"].kind == "object"
    assert sym_by_id["file.json::tasks[0]"].parent_id == "file.json::tasks"

    assert "file.json::tasks[0].name" in sym_by_id
    assert sym_by_id["file.json::tasks[0].name"].kind == "property"
    assert sym_by_id["file.json::tasks[0].name"].parent_id == "file.json::tasks[0]"

    assert "file.json::tasks[0].val" in sym_by_id
    assert sym_by_id["file.json::tasks[0].val"].kind == "property"
    assert sym_by_id["file.json::tasks[0].val"].parent_id == "file.json::tasks[0]"

    assert "file.json::tasks[1]" in sym_by_id
    assert sym_by_id["file.json::tasks[1]"].kind == "object"
    assert sym_by_id["file.json::tasks[1]"].parent_id == "file.json::tasks"

    assert "file.json::tasks[1].val" in sym_by_id
    assert sym_by_id["file.json::tasks[1].val"].kind == "property"
    assert sym_by_id["file.json::tasks[1].val"].parent_id == "file.json::tasks[1]"

    assert "file.json::tags" in sym_by_id
    assert sym_by_id["file.json::tags"].kind == "array"

    assert "file.json::tags[0]" in sym_by_id
    assert sym_by_id["file.json::tags[0]"].kind == "property"
    assert sym_by_id["file.json::tags[0]"].parent_id == "file.json::tags"

    assert "file.json::tags[1]" in sym_by_id
    assert sym_by_id["file.json::tags[1]"].kind == "property"
    assert sym_by_id["file.json::tags[1]"].parent_id == "file.json::tags"


def test_yaml_sequence_of_mappings():
    code = b"""tasks:
  - name: clean
    val: 5
  - name: build
    val: 6
"""
    parser = YAMLParser()
    res = parser.parse_file("file.yml", code)
    assert res.syntax_ok is True

    sym_by_id = {s.id: s for s in res.symbols}

    assert "file.yml::tasks" in sym_by_id
    assert sym_by_id["file.yml::tasks"].kind == "sequence"

    assert "file.yml::tasks[0]" in sym_by_id
    assert sym_by_id["file.yml::tasks[0]"].kind == "mapping"
    assert sym_by_id["file.yml::tasks[0]"].parent_id == "file.yml::tasks"

    assert "file.yml::tasks[0].name" in sym_by_id
    assert sym_by_id["file.yml::tasks[0].name"].kind == "property"
    assert sym_by_id["file.yml::tasks[0].name"].parent_id == "file.yml::tasks[0]"

    assert "file.yml::tasks[1]" in sym_by_id
    assert sym_by_id["file.yml::tasks[1]"].kind == "mapping"
    assert sym_by_id["file.yml::tasks[1]"].parent_id == "file.yml::tasks"

    assert "file.yml::tasks[1].val" in sym_by_id
    assert sym_by_id["file.yml::tasks[1].val"].kind == "property"
    assert sym_by_id["file.yml::tasks[1].val"].parent_id == "file.yml::tasks[1]"


def test_json_and_yaml_array_schema_signatures():
    # JSON test
    json_code = b'''{
  "tasks": [
    {"name": "clean", "val": 5},
    {"name": "build", "val": 6}
  ],
  "tags": ["a", "b"]
}'''
    jparser = JSONParser()
    jres = jparser.parse_file("file.json", json_code)
    jsyms = {s.id: s for s in jres.symbols}

    assert "file.json::tasks" in jsyms
    assert jsyms["file.json::tasks"].signature == "tasks [2]{string name, int val}"

    assert "file.json::tags" in jsyms
    assert jsyms["file.json::tags"].signature == "tags [2]string"

    # YAML test
    yaml_code = b"""tasks:
  - name: clean
    val: 5
  - name: build
    val: 6
tags:
  - a
  - b
"""
    yparser = YAMLParser()
    yres = yparser.parse_file("file.yml", yaml_code)
    ysyms = {s.id: s for s in yres.symbols}

    assert "file.yml::tasks" in ysyms
    assert ysyms["file.yml::tasks"].signature == "tasks [2]{string name, int val}"

    assert "file.yml::tags" in ysyms
    assert ysyms["file.yml::tags"].signature == "tags [2]string"
    # Additional scalar types & heterogeneous tests in JSON
    json_extra = b'''{
  "numbers": [1, 2, 3],
  "floats": [1.5, 2.5],
  "flags": [true, false],
  "empty": [],
  "mixed": [1, "two"]
}'''
    jres2 = jparser.parse_file("extra.json", json_extra)
    jsyms2 = {s.id: s for s in jres2.symbols}
    assert jsyms2["extra.json::numbers"].signature == "numbers [3]int"
    assert jsyms2["extra.json::floats"].signature == "floats [2]number"
    assert jsyms2["extra.json::flags"].signature == "flags [2]bool"
    assert jsyms2["extra.json::empty"].signature == '"empty": [ ... ]'
    assert jsyms2["extra.json::mixed"].signature == '"mixed": [ ... ]'

    # Additional scalar types & heterogeneous tests in YAML
    yaml_extra = b"""numbers:
  - 1
  - 2
  - 3
floats:
  - 1.5
  - 2.5
flags:
  - true
  - false
empty: []
mixed:
  - 1
  - two
"""
    yres2 = yparser.parse_file("extra.yml", yaml_extra)
    ysyms2 = {s.id: s for s in yres2.symbols}
    assert ysyms2["extra.yml::numbers"].signature == "numbers [3]int"
    assert ysyms2["extra.yml::floats"].signature == "floats [2]number"
    assert ysyms2["extra.yml::flags"].signature == "flags [2]bool"
    assert ysyms2["extra.yml::empty"].signature == "empty: [...]"
    assert ysyms2["extra.yml::mixed"].signature == "mixed: [...]"