"""Tests for TypeScript function and method overload disambiguation."""
import pytest
from ast_graph_server.parsers.typescript_parser import TypeScriptParser


def test_typescript_function_and_method_overloads_have_distinct_ids() -> None:
    code = b"""
function parse(input: string): string;
function parse(input: number): number;
function parse(input: any): any {
    return input;
}

class ParserService {
    format(value: string): string;
    format(value: number): string;
    format(value: any): string {
        return String(value);
    }
}
"""
    parser = TypeScriptParser()
    res = parser.parse_file("src/parser.ts", code)

    assert res.syntax_ok
    symbols = res.symbols

    # Top-level function overloads
    parse_symbols = [s for s in symbols if s.name == "parse"]
    assert len(parse_symbols) == 3, f"Expected 3 parse symbols, found {len(parse_symbols)}: {[s.id for s in parse_symbols]}"
    parse_ids = [s.id for s in parse_symbols]
    assert len(set(parse_ids)) == 3, f"Expected 3 distinct IDs, got {parse_ids}"
    for pid in parse_ids:
        assert pid.startswith("src/parser.ts::parse#"), f"ID {pid} does not have expected overload suffix"

    # Class method overloads
    format_symbols = [s for s in symbols if s.name == "format"]
    assert len(format_symbols) == 3, f"Expected 3 format symbols, found {len(format_symbols)}: {[s.id for s in format_symbols]}"
    format_ids = [s.id for s in format_symbols]
    assert len(set(format_ids)) == 3, f"Expected 3 distinct IDs, got {format_ids}"
    for s in format_symbols:
        assert s.id.startswith("src/parser.ts::ParserService.format#"), f"ID {s.id} does not have expected overload suffix"
        assert s.parent_id == "src/parser.ts::ParserService"


def test_homonymous_functions_disambiguation() -> None:
    code = b"""
function handle(req: Request): Response {
    return new Response();
}

function handle(req: any): any {
    return null;
}
"""
    parser = TypeScriptParser()
    res = parser.parse_file("src/handler.ts", code)

    assert res.syntax_ok
    handle_symbols = [s for s in res.symbols if s.name == "handle"]
    assert len(handle_symbols) == 2
    ids = [s.id for s in handle_symbols]
    assert len(set(ids)) == 2
    for hid in ids:
        assert hid.startswith("src/handler.ts::handle#")