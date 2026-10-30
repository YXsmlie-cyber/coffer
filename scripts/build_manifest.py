"""校验发行工具指纹并生成构建清单，不读取配置、Cookie 或用户数据。"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def tool_paths():
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("找不到发行包需要的 ffmpeg")
    return {"ffmpeg.exe": Path(executable), "ffprobe.exe": Path(executable).with_name("ffprobe.exe")}


def fingerprints(paths):
    return {name: {"sha256": digest(path), "version": subprocess.check_output(
        [str(path), "-version"], text=True, encoding="utf-8", errors="replace", timeout=15
    ).splitlines()[0]} for name, path in paths.items()}


def validate_tools(paths, expected):
    for name, path in paths.items():
        if digest(path) != expected[name]["sha256"]:
            raise RuntimeError(f"{name} 与已验收工具指纹不一致；请使用锁定版本或重新验收后更新清单")


def create_manifest(output):
    expected = json.loads((ROOT / "build-tools.json").read_text(encoding="utf-8"))["tools"]
    packaged_tools = {name: output / "bin" / name for name in expected}
    validate_tools(packaged_tools, expected)
    pins = {}
    for line in (ROOT / "requirements-build.lock").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, wanted = line.split("==", 1)
            actual = metadata.version(name)
            if actual != wanted:
                raise RuntimeError(f"构建环境 {name}=={actual} 与锁定版本 {wanted} 不一致")
            pins[name] = actual
    files = [output / "PH115.exe", *packaged_tools.values(),
             output / "_internal/models/visual/siglip2-vision-int8.onnx"]
    files.extend(sorted(path for path in (output / "_internal/frontend_dist").rglob("*") if path.is_file()))
    manifest = {
        "schema_version": 1,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_dirty": subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0,
        "python": sys.version.split()[0], "platform": sys.platform,
        "dependencies": pins, "tools": expected,
        "locks": {name: digest(ROOT / name) for name in (
            "requirements-release.lock", "requirements-build.lock", "frontend/package-lock.json")},
        "artifacts": {path.relative_to(output).as_posix(): digest(path) for path in files},
    }
    (output / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-tools", action="store_true")
    parser.add_argument("--check-tools", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.update_tools:
        (ROOT / "build-tools.json").write_text(json.dumps(
            {"schema_version": 1, "tools": fingerprints(tool_paths())},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.check_tools:
        validate_tools(tool_paths(), json.loads((ROOT / "build-tools.json").read_text(encoding="utf-8"))["tools"])
    if args.output:
        create_manifest(args.output.resolve())
    print("发行工具与构建清单检查通过")
