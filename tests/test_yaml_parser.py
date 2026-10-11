from src.ast_graph_server.parsers.yaml_parser import YAMLParser
from src.ast_graph_server.parsers import ParserRegistry


def test_yaml_parser_can_parse():
    parser = YAMLParser()
    assert parser.can_parse("docker-compose.yml") is True
    assert parser.can_parse("workflow.YAML") is True
    assert parser.can_parse("config.json") is False


def test_yaml_parser_extracts_properties_and_mappings():
    code = (
        b"name: my-pipeline\n"
        b"version: 1.0\n"
        b"services:\n"
        b"  web:\n"
        b"    image: nginx:latest\n"
        b"    ports:\n"
        b"      - 80:80\n"
    )
    parser = YAMLParser()
    res = parser.parse_file("pipeline.yml", code)
    assert res.syntax_ok is True
    syms = {s.id: s for s in res.symbols}

    assert "pipeline.yml::name" in syms
    assert syms["pipeline.yml::name"].kind == "property"

    assert "pipeline.yml::services" in syms
    assert syms["pipeline.yml::services"].kind == "mapping"

    assert "pipeline.yml::services.web" in syms
    assert syms["pipeline.yml::services.web"].kind == "mapping"
    assert syms["pipeline.yml::services.web"].parent_id == "pipeline.yml::services"

    assert "pipeline.yml::services.web.image" in syms
    assert syms["pipeline.yml::services.web.image"].kind == "property"
    assert syms["pipeline.yml::services.web.image"].parent_id == "pipeline.yml::services.web"


def test_yaml_parser_syntax_barrier():
    parser = YAMLParser()
    valid = b"key: value\nlist:\n  - a\n  - b\n"
    ok, details = parser.validate_syntax(valid)
    assert ok is True
    assert len(details) == 0


def test_yaml_parser_registered():
    registry = ParserRegistry()
    parser = registry.get_parser_for_file("ci.yaml")
    assert parser is not None
    assert isinstance(parser, YAMLParser)


def test_yaml_parser_duplicate_keys_disambiguation():
    code = b"""
- name: item1
  value: 10
- name: item2
  value: 20
"""
    parser = YAMLParser()
    res = parser.parse_file("items.yml", code)
    assert res.syntax_ok is True
    ids = [s.id for s in res.symbols]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert "items.yml::[0].name" in ids
    assert "items.yml::[1].name" in ids
