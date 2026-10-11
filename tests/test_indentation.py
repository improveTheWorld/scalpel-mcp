"""Unit tests for AST-driven IndentationManager."""

from ast_graph_server.indentation import IndentationManager


def test_detect_file_indent_step() -> None:
    mgr = IndentationManager()
    code_4 = b"class Foo:\n    def bar(self):\n        pass\n"
    assert mgr.detect_file_indent_step(code_4) == "    "

    code_2 = b"class Foo:\n  def bar(self):\n    pass\n"
    assert mgr.detect_file_indent_step(code_2) == "  "

    code_tab = b"class Foo:\n\tdef bar(self):\n\t\tpass\n"
    assert mgr.detect_file_indent_step(code_tab) == "\t"


def test_rebase_indentation_shift() -> None:
    mgr = IndentationManager()
    unindented = "def charge(self):\n    return 42"
    rebased = mgr.rebase_indentation(unindented, target_col=4)
    expected = "    def charge(self):\n        return 42"
    assert rebased == expected


def test_rebase_indentation_already_aligned() -> None:
    mgr = IndentationManager()
    already_aligned = "    def charge(self):\n        return 42"
    rebased = mgr.rebase_indentation(already_aligned, target_col=4)
    assert rebased == already_aligned


def test_multiline_string_protection() -> None:
    mgr = IndentationManager()
    code_with_doc = '''def query(self):
    sql = """SELECT id, name
FROM users
WHERE active = 1"""
    return sql'''

    rebased = mgr.rebase_indentation(code_with_doc, target_col=4)
    # Target col is 4. Notice that FROM users and WHERE active = 1 must NOT be shifted by 4 spaces!
    lines = rebased.splitlines()
    assert lines[0] == "    def query(self):"
    assert lines[1] == '        sql = """SELECT id, name'
    assert lines[2] == "FROM users"
    assert lines[3] == 'WHERE active = 1"""'
    assert lines[4] == "        return sql"
