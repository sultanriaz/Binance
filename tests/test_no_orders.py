import ast
from pathlib import Path


def test_legacy_api_source_has_no_live_order_submission_routes():
    tree = ast.parse(Path("src/trader/api.py").read_text())
    routes = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.args:
                    if d.func.attr in ("post", "put", "delete", "patch"):
                        routes.append(d.args[0].value)
    assert routes == ["/v1/auth/login"]


def test_repository_never_contains_order_submit_api_calls():
    files = list(Path("src/trader").glob("*.py"))
    assert not any("/api/v3/order" in f.read_text() for f in files)
    assert not any("/fapi/v1/order" in f.read_text() for f in files)
