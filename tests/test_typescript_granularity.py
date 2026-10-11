"""Tests unitaires pour la granularité AST JS/TS (Lot G).

Vérifie l'extraction des appels de test (test, it, describe, hooks) et des closures
imbriquées comme symboles adressables dans TypeScriptParser.
"""

import pytest
from ast_graph_server.parsers.typescript_parser import TypeScriptParser


def test_typescript_test_calls_extraction():
    parser = TypeScriptParser()
    code = b"""
import { test, describe, it } from 'node:test';

test('premier test racine', () => {
    console.log("hello");
});

test.skip('test ignore', () => {
    // skip
});

describe('suite de tests', () => {
    it('sous test un', () => {
        expect(1).toBe(1);
    });
    it.only('sous test deux', () => {
        expect(2).toBe(2);
    });
});
"""
    result = parser.parse_file("tests/sample.test.mjs", code)
    names = {s.name for s in result.symbols}
    assert "premier test racine" in names
    assert "test ignore" in names
    assert "suite de tests" in names
    assert "sous test un" in names
    assert "sous test deux" in names

    # Vérification des IDs et de la hiérarchie
    suite_sym = next(s for s in result.symbols if s.name == "suite de tests")
    assert suite_sym.parent_id is None
    assert suite_sym.kind == "function"

    sub_sym = next(s for s in result.symbols if s.name == "sous test un")
    assert sub_sym.parent_id == suite_sym.id
    assert sub_sym.id == f"tests/sample.test.mjs::suite de tests.sous test un"


def test_typescript_hooks_extraction():
    parser = TypeScriptParser()
    code = b"""
beforeEach(() => {
    setup();
});

afterAll(() => {
    cleanup();
});
"""
    result = parser.parse_file("tests/hooks.test.mjs", code)
    names = {s.name for s in result.symbols}
    assert "beforeEach" in names
    assert "afterAll" in names


def test_typescript_nested_closures_in_function():
    parser = TypeScriptParser()
    code = b"""
function outer(a, b) {
    function innerHelper(x) {
        return x * 2;
    }
    const innerArrow = (y) => {
        return innerHelper(y) + 1;
    };
    return innerArrow(a + b);
}
"""
    result = parser.parse_file("src/math.js", code)
    names = {s.name for s in result.symbols}
    assert "outer" in names
    assert "innerHelper" in names
    assert "innerArrow" in names

    outer_sym = next(s for s in result.symbols if s.name == "outer")
    helper_sym = next(s for s in result.symbols if s.name == "innerHelper")
    arrow_sym = next(s for s in result.symbols if s.name == "innerArrow")

    assert helper_sym.parent_id == outer_sym.id
    assert helper_sym.id == "src/math.js::outer.innerHelper"
    assert arrow_sym.parent_id == outer_sym.id
    assert arrow_sym.id == "src/math.js::outer.innerArrow"


def test_typescript_nested_closures_in_arrow_variable():
    parser = TypeScriptParser()
    code = b"""
const createService = () => {
    function subService() {
        return 42;
    }
    return { subService };
};
"""
    result = parser.parse_file("src/service.js", code)
    names = {s.name for s in result.symbols}
    assert "createService" in names
    assert "subService" in names

    parent_sym = next(s for s in result.symbols if s.name == "createService")
    child_sym = next(s for s in result.symbols if s.name == "subService")
    assert child_sym.parent_id == parent_sym.id
    assert child_sym.id == "src/service.js::createService.subService"


def test_typescript_separator_sanitization_in_names():
    parser = TypeScriptParser()
    code = b"""
test('module::component::should work', () => {
    // test with double colon
});
"""
    result = parser.parse_file("test/sep.test.js", code)
    names = {s.name for s in result.symbols}
    # :: doit être remplacé par ' - '
    assert "module - component - should work" in names
    sym = next(s for s in result.symbols if s.name == "module - component - should work")
    assert sym.id == "test/sep.test.js::module - component - should work"


def test_typescript_non_pollution_simple_statements():
    parser = TypeScriptParser()
    code = b"""
import fs from 'node:fs';
console.log('hello');
dotenv.config();
const x = 123;
"""
    result = parser.parse_file("src/simple.js", code)
    names = {s.name for s in result.symbols}
    assert "x" in names
    assert "console" not in names
    assert "console.log" not in names
    assert "dotenv" not in names
    assert "dotenv.config" not in names
    assert "hello" not in names