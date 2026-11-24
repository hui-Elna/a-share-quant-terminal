"""恢复演练：把最新备份恢复到临时库并比对关键指标。

用法：
    python scripts/restore_drill.py
    python scripts/restore_drill.py --dump D:\\backups\\gfna_20261124_181000.dump

说明：
- 临时库固定为 ``gfna_restore_drill``，演练结束自动删除；
- 需要 ``pg_restore``（容器内已安装 postgresql-client）；
- 默认“快速演练”：只恢复表结构与数据，跳过索引/约束，适合每日/每周校验；
  ``--full`` 执行完整恢复（含索引约束，耗时明显更长）。
- 只读比对源库与恢复库的表数量、关键表行数、最新交易日。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import DATABASE_URL_SYNC  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.services.backup import latest_backup  # noqa: E402

TEMP_DB = "gfna_restore_drill"
CHECK_TABLES = ("fact_stock_daily", "dim_stock", "stg_run", "meta_data_quality")


def _engine(url: str):
    return create_engine(url, pool_pre_ping=True)


def _admin_engine(url):
    return create_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )


def _drop_temp(admin) -> None:
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEMP_DB}" WITH (FORCE)'))


def _create_temp(admin) -> None:
    _drop_temp(admin)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{TEMP_DB}"'))


def _snapshot(url: str) -> dict:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            tables = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public'"
            )).scalar()
            counts = {}
            for table in CHECK_TABLES:
                counts[table] = conn.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar()
            latest = conn.execute(text(
                "SELECT max(trade_date) FROM fact_stock_daily"
            )).scalar()
            return {
                "tables": int(tables or 0),
                "counts": {key: int(value or 0) for key, value in counts.items()},
                "latest_trade_date": latest.isoformat() if latest else None,
            }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="GFNA 备份恢复演练")
    parser.add_argument("--dump", help="指定备份文件，默认取最新一份")
    parser.add_argument("--full", action="store_true", help="完整恢复（含索引/约束）")
    args = parser.parse_args()

    setup_logging()
    dump = Path(args.dump) if args.dump else latest_backup()
    if dump is None or not dump.exists():
        print(json.dumps({"status": "failed", "error": "找不到备份文件"}, ensure_ascii=False))
        return 1
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        print(json.dumps(
            {"status": "failed", "error": "未找到 pg_restore；请安装 postgresql-client"},
            ensure_ascii=False,
        ))
        return 1

    url = make_url(DATABASE_URL_SYNC)
    source_url = url.render_as_string(hide_password=False)
    target_url = url.set(database=TEMP_DB).render_as_string(hide_password=False)
    admin = _admin_engine(url)
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    command = [
        pg_restore,
        "-h", str(url.host),
        "-p", str(url.port),
        "-U", str(url.username),
        "-d", TEMP_DB,
        "--no-owner", "--no-privileges",
    ]
    if not args.full:
        # 只恢复结构 + 数据，跳过 post-data（索引/约束/触发器），把演练压到可接受时长
        command.extend(["--section=pre-data", "--section=data"])
    command.append(str(dump))
    try:
        _create_temp(admin)
        completed = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=1800,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"pg_restore exit={completed.returncode}")
        source = _snapshot(source_url)
        restored = _snapshot(target_url)
        matched = source == restored
        print(json.dumps({
            "status": "ok" if matched else "mismatch",
            "dump": str(dump),
            "source": source,
            "restored": restored,
        }, ensure_ascii=False, indent=2))
        return 0 if matched else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(
            {"status": "failed", "dump": str(dump), "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        ))
        return 1
    finally:
        try:
            _drop_temp(admin)
        finally:
            admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
