"""旧 HTTP 契约、排序与分层边界的自动回归。"""
import ast
import json
from pathlib import Path
import unittest

from backend.main import app
from backend.database import Task
from backend.task_contracts import TASK_STATUS_RANK, TASK_STATUS_SQL


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_existing_http_paths_and_schema_remain_compatible(self):
        expected = json.loads((ROOT / "tests/contracts/openapi-baseline.json").read_text(encoding="utf-8"))
        actual = app.openapi()
        for path, definition in expected["paths"].items():
            with self.subTest(path=path):
                self.assertEqual(actual["paths"].get(path), definition)
        for name, definition in expected["components"]["schemas"].items():
            with self.subTest(schema=name):
                self.assertEqual(actual["components"]["schemas"].get(name), definition)

    def test_frontend_order_and_database_order_share_contract(self):
        source = (ROOT / "frontend/src/contracts/taskState.js").read_text(encoding="utf-8")
        definition = source.split("Object.freeze(", 1)[1].split(")", 1)[0]
        self.assertEqual(json.loads(definition), TASK_STATUS_RANK)
        self.assertEqual(str(Task.__table__.c.status_rank.computed.sqltext), TASK_STATUS_SQL)

    def test_backend_import_graph_remains_acyclic(self):
        files = {path.stem: path for path in (ROOT / "backend").glob("*.py")}
        edges = {name: set() for name in files}
        for name, path in files.items():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    if node.module in files:
                        edges[name].add(node.module)
                    elif node.module is None:
                        edges[name].update(alias.name for alias in node.names if alias.name in files)
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend."):
                    edges[name].add(node.module.split(".")[1])
        visiting, complete = set(), set()

        def visit(name):
            self.assertNotIn(name, visiting, f"循环依赖涉及 {name}")
            if name in complete:
                return
            visiting.add(name)
            for dependency in edges.get(name, ()):
                visit(dependency)
            visiting.remove(name)
            complete.add(name)

        for name in edges:
            visit(name)
        for name in ("task_queries", "model_queries", "inventory_writer", "task_contracts"):
            self.assertFalse(edges[name] & {"main", "task_manager", "patrol"}, name)
        for name in ("taste_service", "taste_model", "taste_visual"):
            self.assertFalse(edges[name] & {"main", "task_manager", "patrol", "notifier"}, name)
        # 路由模块只被 main 装配，不能反向依赖 main 或彼此依赖。
        route_modules = {name for name in files if name.startswith("routes_")}
        self.assertTrue(route_modules)
        for name in route_modules | {"api_schemas", "api_wire"}:
            self.assertFalse(edges[name] & ({"main"} | route_modules), name)

    def test_page_consumers_use_public_scraper_boundary(self):
        route_modules = sorted(path.stem for path in (ROOT / "backend").glob("routes_*.py"))
        for name in ("avatar_audit", "avatar_repair", "avatar_service", "main",
                     "model_sync", "preview_image_store", *route_modules):
            tree = ast.parse((ROOT / "backend" / f"{name}.py").read_text(encoding="utf-8"))
            private = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                       and isinstance(node.value, ast.Name) and node.value.id == "scraper"
                       and node.attr.startswith("_")]
            self.assertEqual(private, [], name)
