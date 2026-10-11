from src.ast_graph_server.parsers.python_parser import PythonParser


def test_python_nested_function_has_function_kind():
    code = b"def outer():\n    def inner():\n        pass\n"
    res = PythonParser().parse_file("test.py", code)
    symbols = {s.name: s for s in res.symbols}
    assert symbols["inner"].kind == "function", f"Attendu 'function', obtenu '{symbols['inner'].kind}'"


def test_python_method_inside_class_remains_method():
    code = b"class MyClass:\n    def my_method(self):\n        def local_fn():\n            pass\n"
    res = PythonParser().parse_file("test.py", code)
    symbols = {s.name: s for s in res.symbols}
    assert symbols["my_method"].kind == "method"
    assert symbols["local_fn"].kind == "function"