"""从已验收环境生成/检查完整依赖闭包；仅记录包名和版本。"""
import argparse
from collections import deque
from importlib import metadata
from pathlib import Path
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def requirements(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            yield from requirements(path.parent / line[3:].strip())
        else:
            yield Requirement(line)


def installed_closure(roots):
    pending = deque((req, {""}) for req in roots)
    pins, expanded = {}, {}
    while pending:
        requirement, parent_extras = pending.popleft()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": extra})
                                          for extra in parent_extras):
            continue
        name = canonicalize_name(requirement.name)
        distribution = metadata.distribution(name)
        version = distribution.version
        if requirement.specifier and not requirement.specifier.contains(version, prereleases=True):
            raise RuntimeError(f"{name}=={version} 不满足 {requirement.specifier}")
        pins[name] = version
        extras = {"", *requirement.extras}
        new_extras = extras - expanded.get(name, set())
        if not new_extras:
            continue
        expanded.setdefault(name, set()).update(extras)
        pending.extend((Requirement(child), new_extras) for child in distribution.requires or [])
    return dict(sorted(pins.items()))


def lock_text(pins):
    return ("# Windows x64 / Python 3.14；由 scripts/lock_dependencies.py 从验收环境生成。\n"
            "# 完整传递依赖均固定版本；升级需重新生成并完成回归与冻结包验收。\n"
            + "".join(f"{name}=={version}\n" for name, version in pins.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--output", type=Path, default=Path("requirements-release.lock"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14):
        raise SystemExit("发行锁定清单的验收目标是 Windows / Python 3.14")
    pins = installed_closure(requirements(args.input))
    expected = lock_text(pins)
    if args.write:
        args.output.write_text(expected, encoding="utf-8", newline="\n")
    elif args.output.read_text(encoding="utf-8") != expected:
        raise SystemExit("依赖环境与发行锁定清单不一致；请安装对应 lock 文件")
    print(f"依赖闭包通过：{len(pins)} 个包（{args.output.name}）")


if __name__ == "__main__":
    main()
