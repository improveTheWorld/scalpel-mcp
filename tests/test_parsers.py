"""Unit tests for AST parsers (Python and TypeScript)."""

from ast_graph_server.parsers import ParserRegistry, PythonParser, TypeScriptParser


def test_python_parser_classes_methods_calls() -> None:
    code = b"""
import logging

@dataclass
class BillingService(BaseService):
    \"\"\"Handles customer billing and payment gateways.\"\"\"
    
    def __init__(self, gateway: StripeGateway):
        self.gateway = gateway

    @retry(attempts=3)
    def charge(self, user_id: str, amount: float) -> Receipt:
        \"\"\"Process payment for given user.\"\"\"
        if not self.validate_user(user_id):
            raise InvalidUserError(user_id)
        receipt = self.gateway.post_charge(amount)
        self.audit.log_event(receipt.id)
        return receipt

    def validate_user(self, user_id: str) -> bool:
        return True
"""
    parser = PythonParser()
    res = parser.parse_file("src/billing.py", code)

    assert res.syntax_ok
    assert len(res.errors) == 0

    sym_map = {s.id: s for s in res.symbols}
    assert "src/billing.py::BillingService" in sym_map
    assert "src/billing.py::BillingService.__init__" in sym_map
    assert "src/billing.py::BillingService.charge" in sym_map
    assert "src/billing.py::BillingService.validate_user" in sym_map

    class_sym = sym_map["src/billing.py::BillingService"]
    assert class_sym.kind == "class"
    assert "Handles customer billing" in (class_sym.docstring or "")
    assert "class BillingService(BaseService):" in class_sym.signature

    charge_sym = sym_map["src/billing.py::BillingService.charge"]
    assert charge_sym.kind == "method"
    assert "Process payment" in (charge_sym.docstring or "")
    assert "def charge(self, user_id: str, amount: float) -> Receipt" in charge_sym.signature
    # Verify decorators are included in byte span
    assert code[charge_sym.start_byte : charge_sym.end_byte].startswith(b"    @retry")

    # Verify calls
    charge_calls = [c for c in res.calls if c.caller_id == charge_sym.id]
    callee_names = [c.callee_name for c in charge_calls]
    assert "validate_user" in callee_names
    assert "gateway.post_charge" in callee_names or "post_charge" in [c.callee_name.split(".")[-1] for c in charge_calls]
    assert "InvalidUserError" in callee_names


def test_python_syntax_error_barrier() -> None:
    bad_code = b"""
def broken_syntax(:
    pass
"""
    parser = PythonParser()
    syntax_ok, errors = parser.validate_syntax(bad_code)
    assert not syntax_ok
    assert len(errors) > 0
    assert any("Syntax error" in e for e in errors)


def test_typescript_parser() -> None:
    ts_code = b"""
interface User {
    id: string;
    name: string;
}

/**
 * Service for managing user operations.
 */
class UserService {
    /**
     * Finds a user by ID.
     */
    async getUser(id: string): Promise<User> {
        const user = await this.fetchRemote(id);
        logger.info(user.id);
        return user;
    }
}
"""
    parser = TypeScriptParser()
    res = parser.parse_file("src/users.ts", ts_code)

    assert res.syntax_ok
    sym_map = {s.id: s for s in res.symbols}
    assert "src/users.ts::User" in sym_map
    assert sym_map["src/users.ts::User"].kind == "interface"

    assert "src/users.ts::UserService" in sym_map
    assert "src/users.ts::UserService.getUser" in sym_map

    method_sym = sym_map["src/users.ts::UserService.getUser"]
    assert method_sym.kind == "method"
    assert "Finds a user by ID" in (method_sym.docstring or "")

    calls = [c.callee_name for c in res.calls if c.caller_id == method_sym.id]
    assert "fetchRemote" in calls


def test_parser_registry() -> None:
    reg = ParserRegistry()
    assert isinstance(reg.get_parser_for_file("app/main.py"), PythonParser)
    assert isinstance(reg.get_parser_for_file("app/index.ts"), TypeScriptParser)
    assert isinstance(reg.get_parser_for_file("app/component.tsx"), TypeScriptParser)
    assert reg.get_parser_for_file("notes.txt") is None
