"""把历史 SQLite 库重建为当前受支持 Schema，同时保留可恢复原库。

默认只生成一个经过校验的新数据库，绝不替换 ``data.db``。工具先通过 SQLite
backup API 取得包含 WAL 的一致快照，再按当前 ``Base.metadata`` 创建空库，只复制
目标 Schema 仍认识的表和列。被回退版本遗留的表/列会列入 JSON 报告；源库和快照
在整个验证完成前始终保留。

示例：

    python -m scripts.rebuild_database --source data/data.db

成功后会生成 ``data/data.rebuilt.db`` 与旁边的 JSON 报告。确认应用已停止、备份
可用并人工审阅报告后，才可在维护窗口自行做 copy-swap；本命令不提供在线替换。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import URL

from backend.database import Base, _migrate, _normalize_model_name_key


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection, schema: str) -> set[str]:
    rows = connection.execute(
        f"SELECT name FROM {_quote(schema)}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {str(row[0]) for row in rows}


def _columns(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
) -> list[dict]:
    # table_xinfo 的 hidden=2/3 表示 VIRTUAL/STORED generated column，不能作为
    # INSERT 目标；普通列 hidden=0。
    rows = connection.execute(
        f"PRAGMA {_quote(schema)}.table_xinfo({_quote(table)})"
    ).fetchall()
    return [{
        "name": str(row[1]),
        "type": str(row[2] or ""),
        "not_null": bool(row[3]),
        "default": row[4],
        "primary_key": int(row[5] or 0),
        "hidden": int(row[6] or 0) if len(row) > 6 else 0,
    } for row in rows]


def _snapshot(source: Path, destination: Path) -> None:
    uri = source.resolve().as_uri() + "?mode=ro"
    # sqlite3.Connection 的上下文管理器只提交/回滚事务，并不会关闭句柄；
    # Windows 上若不显式 close，临时快照会一直处于占用状态。
    with closing(sqlite3.connect(uri, uri=True)) as source_db:
        with closing(sqlite3.connect(destination)) as snapshot_db:
            source_db.backup(snapshot_db)
            if snapshot_db.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0] != "ok":
                raise RuntimeError("源数据库一致快照未通过 integrity_check")


async def _create_current_schema(destination: Path) -> None:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(destination.resolve()))
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # 极旧库可能含迁移逻辑已能安全解除绑定的重复任务。导入阶段先移除
            # 当前唯一索引，复制完再通过同一 _migrate 规则修复并重建索引。
            await connection.exec_driver_sql(
                "DROP INDEX IF EXISTS uq_task_model_viewkey_type")
    finally:
        await engine.dispose()


def _python_default(column):
    """求值 SQLAlchemy 的 Python 侧默认值，供旧 Schema 缺列时批量填充。"""
    default = column.default
    if default is None:
        raise RuntimeError(f"列 {column.table.name}.{column.name} 没有可用默认值")
    if default.is_scalar:
        value = default.arg
    elif default.is_callable:
        # SQLAlchemy 会把无参 callable 包成可接收 execution context 的形式。
        value = default.arg(None)
    else:
        raise RuntimeError(
            f"列 {column.table.name}.{column.name} 使用不可离线求值的 SQL 默认值"
        )
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return value


def _copy_supported_data(snapshot: Path, destination: Path) -> dict:
    report = {
        "copied_tables": {},
        "source_only_tables": [],
        "source_only_columns": {},
        "target_only_columns": {},
        "target_only_tables": [],
        "defaulted_target_columns": {},
    }
    with closing(sqlite3.connect(destination)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        attached = False
        try:
            connection.execute("ATTACH DATABASE ? AS legacy", (str(snapshot),))
            attached = True
            source_tables = _tables(connection, "legacy")
            target_tables = _tables(connection, "main")
            report["source_only_tables"] = sorted(source_tables - target_tables)
            report["target_only_tables"] = sorted(target_tables - source_tables)

            # Base.metadata.sorted_tables 保持父表先于子表；不认识的新表保持为空。
            ordered_targets = [
                table.name for table in Base.metadata.sorted_tables
                if table.name in target_tables
            ]
            for table in ordered_targets:
                if table not in source_tables:
                    continue
                source_columns = _columns(connection, "legacy", table)
                target_columns = _columns(connection, "main", table)
                source_names = {column["name"] for column in source_columns}
                source_count = int(connection.execute(
                    f"SELECT COUNT(*) FROM legacy.{_quote(table)}"
                ).fetchone()[0])
                report["source_only_columns"][table] = sorted(
                    source_names - {column["name"] for column in target_columns}
                )
                report["target_only_columns"][table] = sorted(
                    {column["name"] for column in target_columns} - source_names
                )
                if source_count == 0:
                    report["copied_tables"][table] = 0
                    continue

                metadata_columns = Base.metadata.tables[table].c
                insertable: list[str] = []
                select_expressions: list[str] = []
                parameters: list[object] = []
                defaulted: list[str] = []
                for target_column in target_columns:
                    name = target_column["name"]
                    if target_column["hidden"] != 0:
                        continue
                    metadata_column = metadata_columns[name]
                    if name in source_names:
                        insertable.append(name)
                        select_expressions.append(_quote(name))
                        continue
                    if metadata_column.primary_key:
                        raise RuntimeError(
                            f"旧表 {table} 缺少主键列 {name}，无法安全保留行身份"
                        )
                    if metadata_column.server_default is not None:
                        # INSERT 不列出该列，让当前 DDL 的 server default 生效。
                        defaulted.append(name)
                        continue
                    if metadata_column.default is not None:
                        insertable.append(name)
                        select_expressions.append("?")
                        parameters.append(_python_default(metadata_column))
                        defaulted.append(name)
                        continue
                    if metadata_column.nullable:
                        # 省略后 SQLite 写入 NULL。
                        continue
                    raise RuntimeError(
                        f"旧表 {table} 缺少当前必填列 {name}，且该列没有默认值"
                    )
                if not insertable:
                    raise RuntimeError(f"表 {table} 有 {source_count} 行但没有可复制列")
                columns_sql = ", ".join(_quote(name) for name in insertable)
                select_sql = ", ".join(select_expressions)
                table_sql = _quote(table)
                statement = (
                    f"INSERT INTO main.{table_sql} ({columns_sql}) "
                    f"SELECT {select_sql} FROM legacy.{table_sql}"
                )
                if parameters:
                    connection.execute(statement, tuple(parameters))
                else:
                    connection.execute(statement)
                target_count = int(connection.execute(
                    f"SELECT COUNT(*) FROM main.{table_sql}"
                ).fetchone()[0])
                if target_count != source_count:
                    raise RuntimeError(
                        f"表 {table} 复制行数不一致：{source_count} -> {target_count}"
                    )
                report["copied_tables"][table] = target_count
                if defaulted:
                    report["defaulted_target_columns"][table] = sorted(defaulted)

            # library_name_key 的语义包含 Python Unicode casefold 与内部空白
            # 折叠，不能用 SQLite lower(trim()) 近似。旧源库缺列或值陈旧时，
            # 在新库中一次性精确回填；源库已有的 snapshot_hash 则原样保留。
            normalized_updates = []
            if "models" in target_tables:
                for model_id, name, current_key in connection.execute(
                        "SELECT id, name, library_name_key FROM main.models"):
                    desired = _normalize_model_name_key(name)
                    if str(current_key or "") != desired:
                        normalized_updates.append((desired, int(model_id)))
            if normalized_updates:
                connection.executemany(
                    "UPDATE main.models SET library_name_key = ? WHERE id = ?",
                    normalized_updates,
                )
            report["normalized_model_names"] = len(normalized_updates)
            connection.commit()
        except BaseException:
            # INSERT 失败时必须先结束事务，否则 Windows/SQLite 上 DETACH 会以
            # “database legacy is locked”覆盖真正的 NOT NULL/UNIQUE 错误。
            connection.rollback()
            raise
        finally:
            if attached:
                connection.execute("DETACH DATABASE legacy")
    report["source_only_columns"] = {
        table: columns
        for table, columns in report["source_only_columns"].items()
        if columns
    }
    report["target_only_columns"] = {
        table: columns
        for table, columns in report["target_only_columns"].items()
        if columns
    }
    return report


async def _finalize_current_schema(destination: Path) -> None:
    """在隔离副本上重放当前幂等迁移，完成旧数据修复及索引重建。"""
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(destination.resolve()))
    )
    try:
        async with engine.begin() as connection:
            # 源库可能声称自己已在最新版，但物理 Schema 正是本工具要修复的对象。
            # 副本必须无条件重放；源库的设置行从未被修改。
            await connection.exec_driver_sql(
                "DELETE FROM runtime_settings "
                "WHERE key = 'schema_migration_version'"
            )
            await _migrate(connection)
    finally:
        await engine.dispose()


def _validate_database(snapshot: Path, destination: Path, report: dict) -> dict:
    """在所有数据修复后复核外键、完整性及每张共同表的最终行数。"""
    with closing(sqlite3.connect(destination)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("ATTACH DATABASE ? AS legacy", (str(snapshot),))
        try:
            validated_counts = {}
            for table, copied_count in report["copied_tables"].items():
                table_sql = _quote(table)
                source_count = int(connection.execute(
                    f"SELECT COUNT(*) FROM legacy.{table_sql}"
                ).fetchone()[0])
                target_count = int(connection.execute(
                    f"SELECT COUNT(*) FROM main.{table_sql}"
                ).fetchone()[0])
                expected = source_count
                if table == "runtime_settings":
                    had_version = connection.execute(
                        "SELECT 1 FROM legacy.runtime_settings "
                        "WHERE key = 'schema_migration_version'"
                    ).fetchone() is not None
                    expected += 0 if had_version else 1
                if target_count != expected or int(copied_count) != source_count:
                    raise RuntimeError(
                        f"表 {table} 最终行数不一致："
                        f"源 {source_count} / 初始复制 {copied_count} / 目标 {target_count}"
                    )
                validated_counts[table] = target_count
        finally:
            connection.execute("DETACH DATABASE legacy")

        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"重建数据库存在 {len(foreign_key_errors)} 条外键错误"
            )
        integrity = str(connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"重建数据库 integrity_check 失败：{integrity}")
        connection.execute("ANALYZE")
        connection.execute("PRAGMA optimize")
        connection.commit()
        return {
            "validated_table_counts": validated_counts,
            "integrity_check": integrity,
            "foreign_key_errors": 0,
            "page_count": int(connection.execute(
                "PRAGMA page_count").fetchone()[0]),
            "freelist_count": int(connection.execute(
                "PRAGMA freelist_count").fetchone()[0]),
        }


async def rebuild_database(source: Path, destination: Path) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源数据库不存在：{source}")
    if source == destination:
        raise ValueError("目标路径不能与源数据库相同")
    report_path = destination.with_suffix(destination.suffix + ".report.json")
    if report_path.resolve() == source:
        raise ValueError("报告路径不能与源数据库相同")
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有目标：{destination}")
    if report_path.exists():
        raise FileExistsError(f"拒绝覆盖已有报告：{report_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    started = datetime.now()
    with tempfile.TemporaryDirectory(
        prefix="ph115-db-rebuild-", dir=str(destination.parent)
    ) as temporary:
        snapshot = Path(temporary) / "source.snapshot.db"
        staged_database = Path(temporary) / "rebuilt.staging.db"
        staged_report = Path(temporary) / "rebuilt.report.json"
        _snapshot(source, snapshot)
        await _create_current_schema(staged_database)
        details = await asyncio.to_thread(
            _copy_supported_data, snapshot, staged_database)
        await _finalize_current_schema(staged_database)
        details.update(await asyncio.to_thread(
            _validate_database, snapshot, staged_database, details))

        result = {
            "source": str(source),
            "destination": str(destination),
            "report_path": str(report_path),
            "source_bytes": source.stat().st_size,
            "destination_bytes": staged_database.stat().st_size,
            "started_at": started.isoformat(),
            "finished_at": datetime.now().isoformat(),
            **details,
        }
        staged_report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # 所有昂贵工作先在同盘临时目录完成。最后用 x 模式原子占位，既消除
        # exists-check 的竞态，也保证任何失败都不会留下半成品或覆盖用户文件。
        reserved: list[Path] = []
        try:
            for artifact in (destination, report_path):
                with artifact.open("xb"):
                    pass
                reserved.append(artifact)
            os.replace(staged_database, destination)
            os.replace(staged_report, report_path)
        except BaseException:
            for artifact in reversed(reserved):
                artifact.unlink(missing_ok=True)
            raise
        return result


def _parse_args():
    parser = argparse.ArgumentParser(
        description="只生成并验证当前 Schema 的紧凑数据库副本，不替换源库",
    )
    parser.add_argument("--source", type=Path, default=Path("data/data.db"))
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    source = args.source
    destination = args.output or source.with_name(
        f"{source.stem}.rebuilt{source.suffix}"
    )
    result = await rebuild_database(source, destination)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
