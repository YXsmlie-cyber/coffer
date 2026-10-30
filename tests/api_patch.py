"""测试辅助：同时替换 backend.main 与各 routes_* 模块中的同名全局对象。

路由拆分后，路由函数读取的是各自模块的全局名；旧测试按 `backend.main`
打补丁的写法改用 `patch_api(name, ...)`，语义与 `patch.object` 一致，
支持 `with` 与 `start()/stop()` 两种用法，所有模块共享同一个替身对象。
"""
from importlib import import_module
from unittest.mock import patch

_MODULE_NAMES = (
    "backend.main",
    "backend.api_wire",
    "backend.routes_auth",
    "backend.routes_cloud",
    "backend.routes_discovery",
    "backend.routes_library",
    "backend.routes_models",
    "backend.routes_ops",
    "backend.routes_taste",
    "backend.routes_tasks",
)


class patch_api:  # noqa: N801 - 与 unittest.mock.patch 的命名习惯保持一致
    def __init__(self, name, *args, **kwargs):
        self._name = name
        self._args = args
        self._kwargs = kwargs
        self._patchers = []

    def start(self):
        modules = [module for module in map(import_module, _MODULE_NAMES)
                   if hasattr(module, self._name)]
        if not modules:
            raise AttributeError(f"没有任何 API 模块定义 {self._name}")
        first = patch.object(modules[0], self._name, *self._args, **self._kwargs)
        replacement = first.start()
        self._patchers = [first]
        for module in modules[1:]:
            patcher = patch.object(module, self._name, replacement)
            patcher.start()
            self._patchers.append(patcher)
        return replacement

    def stop(self):
        while self._patchers:
            self._patchers.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False
