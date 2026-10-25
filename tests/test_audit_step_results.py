import ast
from pathlib import Path

from src.models.audit_models import STEP_RESULT_VALUES


def _result_literals(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _result_literals(node.body) | _result_literals(node.orelse)
    return set()


def test_audit_log_result_literals_exist_in_step_result_enum():
    written_values = set()
    for path in Path(__file__).parents[1].joinpath("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            function_name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else ""
            )
            if function_name != "AuditLog":
                continue
            for keyword in call.keywords:
                if keyword.arg == "result":
                    written_values.update(_result_literals(keyword.value))
    assert written_values <= set(STEP_RESULT_VALUES), written_values
