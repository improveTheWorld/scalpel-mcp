from src.ast_graph_server.parsers.json_parser import JSONParser
from src.ast_graph_server.parsers import ParserRegistry


def test_json_parser_can_parse():
    parser = JSONParser()
    assert parser.can_parse("package.json") is True
    assert parser.can_parse("config/settings.JSON") is True
    assert parser.can_parse("src/app.py") is False


def test_json_parser_extracts_properties_and_objects():
    code = (
        b'{\n'
        b'  "name": "my-app",\n'
        b'  "version": "1.0.0",\n'
        b'  "dependencies": {\n'
        b'    "express": "^4.18.2",\n'
        b'    "lodash": "^4.17.21"\n'
        b'  }\n'
        b'}\n'
    )
    parser = JSONParser()
    res = parser.parse_file("package.json", code)
    assert res.syntax_ok is True
    syms = {s.id: s for s in res.symbols}

    assert "package.json::name" in syms
    assert syms["package.json::name"].kind == "property"
    assert syms["package.json::name"].parent_id is None

    assert "package.json::dependencies" in syms
    assert syms["package.json::dependencies"].kind == "object"

    assert "package.json::dependencies.express" in syms
    assert syms["package.json::dependencies.express"].kind == "property"
    assert syms["package.json::dependencies.express"].parent_id == "package.json::dependencies"


def test_json_parser_syntax_barrier():
    parser = JSONParser()
    valid = b'{"name": "app", "version": 1}\n'
    ok, details = parser.validate_syntax(valid)
    assert ok is True
    assert len(details) == 0

    invalid = b'{"name": "app",}\n'  # trailing comma
    ok, details = parser.validate_syntax(invalid)
    assert ok is False
    assert len(details) > 0


def test_json_parser_registered():
    registry = ParserRegistry()
    parser = registry.get_parser_for_file("package.json")
    assert parser is not None
    assert isinstance(parser, JSONParser)


def test_json_parser_duplicate_keys_disambiguation():
    code = b'''[
  {"query": "first query", "expect": ["a", "b"]},
  {"query": "second query", "expect": ["c"]}
]'''
    parser = JSONParser()
    res = parser.parse_file("queries.json", code)
    assert res.syntax_ok is True
    ids = [s.id for s in res.symbols]
    assert len(ids) == 9
    assert len(set(ids)) == 9
    assert "queries.json::[0].query" in ids
    assert "queries.json::[1].query" in ids
