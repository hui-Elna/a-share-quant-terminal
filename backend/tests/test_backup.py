"""数据库备份保留策略与跳过规则测试。"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import backup

TZ = ZoneInfo("Asia/Shanghai")


def _make_backup(folder, moment: datetime):
    path = folder / backup.backup_filename(moment)
    path.write_bytes(b"PGDMP" + b"0" * 64)
    return path


def test_backup_filename_is_sortable_timestamp() -> None:
    name = backup.backup_filename(datetime(2026, 11, 24, 18, 10, 5, tzinfo=TZ))
    assert name == "gfna_20261124_181005.dump"


def test_retention_keeps_daily_and_one_per_week(tmp_path) -> None:
    base = datetime(2026, 11, 24, 18, 0, tzinfo=TZ)
    paths = [_make_backup(tmp_path, base - timedelta(days=index)) for index in range(14)]

    to_delete = backup.select_backups_to_delete(
        paths, keep_daily=7, keep_weekly=2
    )
    keep = set(paths) - set(to_delete)

    assert len(keep) == 9  # 7 份日备份 + 更早的 2 个自然周各 1 份
    assert len(to_delete) == 5
    assert all(path.exists() for path in paths)


def test_retention_never_deletes_when_under_limits(tmp_path) -> None:
    base = datetime(2026, 11, 24, 18, 0, tzinfo=TZ)
    paths = [_make_backup(tmp_path, base - timedelta(days=index)) for index in range(3)]
    assert backup.select_backups_to_delete(paths, keep_daily=7, keep_weekly=4) == []


def test_backup_due_by_latest_file_date(tmp_path) -> None:
    now = datetime(2026, 11, 24, 18, 0, tzinfo=TZ)
    assert backup.backup_due(tmp_path, now=now) is True

    _make_backup(tmp_path, now)
    assert backup.backup_due(tmp_path, now=now) is False

    assert backup.backup_due(
        tmp_path, now=now + timedelta(days=1)
    ) is True


def test_backup_skips_test_and_demo_databases(tmp_path) -> None:
    result = backup.backup_database(
        reason="test", database="gfna_test", directory=tmp_path
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "test_database"

    result = backup.backup_database(
        reason="test", database="gfna_demo", directory=tmp_path
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "demo_database"


def test_backup_reports_failure_when_pg_dump_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    result = backup.backup_database(
        reason="test", database="gfna_db", directory=tmp_path
    )
    assert result["status"] == "failed"
    assert "pg_dump" in result["error"]
    assert list(tmp_path.glob("*.dump")) == []


def test_worker_backup_skips_when_today_already_done(monkeypatch) -> None:
    import asyncio

    from app import worker

    called: list[dict] = []
    monkeypatch.setattr("app.services.backup.backup_due", lambda: False)
    monkeypatch.setattr(
        "app.services.backup.backup_database",
        lambda **kwargs: called.append(kwargs) or {"status": "success"},
    )

    asyncio.run(worker.run_backup_job(None))

    assert called == []


def test_worker_backup_runs_when_due(monkeypatch) -> None:
    import asyncio

    from app import worker

    called: list[dict] = []
    monkeypatch.setattr("app.services.backup.backup_due", lambda: True)
    monkeypatch.setattr(
        "app.services.backup.backup_database",
        lambda **kwargs: called.append(kwargs) or {"status": "success"},
    )

    asyncio.run(worker.run_backup_job(None))

    assert called and called[0]["reason"] == "scheduler"

