"""数据库备份、保留策略与恢复演练支撑（P0-1）。

设计：
- 用 ``pg_dump -Fc`` 生成自定义格式备份，写入宿主机目录（compose 挂载到
  ``/app/backups``），同时生成 ``.sha256`` 校验文件；
- 保留最近 N 份日备份 + 最近 M 个自然周各一份；
- 先写临时文件、校验通过后再原子改名，避免半截备份被误用；
- 备份失败只返回 failed，不抛异常；调用方（迁移/巡检）决定是否阻断。

测试库/演示库（``*_test`` / ``*_demo``）默认跳过，避免 CI 与演练时误备份。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from app.core import config
from app.core.logging import get_logger

logger = get_logger("backup")

_TZ = ZoneInfo(config.TIMEZONE)
_DEFAULT_DAILY = 7
_DEFAULT_WEEKLY = 4
_GLOB = "gfna_*.dump"


@dataclass(frozen=True)
class BackupResult:
    status: str
    reason: str = ""
    path: str | None = None
    bytes: int = 0
    sha256: str | None = None
    deleted: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "deleted": self.deleted,
            "error": self.error,
        }


def backup_directory() -> Path:
    """备份目录：BACKUP_DIR > 容器挂载点 /app/backups > backend/var/backups。"""
    raw = os.getenv("BACKUP_DIR", "").strip()
    if raw:
        path = Path(raw)
    elif Path("/app/backups").exists():
        path = Path("/app/backups")
    else:
        path = config.BACKEND_DIR / "var" / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_filename(now: datetime | None = None) -> str:
    moment = (now or datetime.now(_TZ)).astimezone(_TZ)
    return moment.strftime("gfna_%Y%m%d_%H%M%S.dump")


def parse_backup_time(path: Path) -> datetime:
    try:
        return datetime.strptime(path.stem, "gfna_%Y%m%d_%H%M%S").replace(tzinfo=_TZ)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=_TZ)


def list_backups(directory: Path | None = None) -> list[Path]:
    folder = directory or backup_directory()
    return sorted(
        (path for path in folder.glob(_GLOB) if path.is_file()),
        key=parse_backup_time,
        reverse=True,
    )


def latest_backup(directory: Path | None = None) -> Path | None:
    backups = list_backups(directory)
    return backups[0] if backups else None


def backup_due(
    directory: Path | None = None, now: datetime | None = None
) -> bool:
    """当天是否还没有成功备份。"""
    latest = latest_backup(directory)
    if latest is None:
        return True
    today = (now or datetime.now(_TZ)).astimezone(_TZ).date()
    return parse_backup_time(latest).date() != today


def select_backups_to_delete(
    paths: Iterable[Path],
    *,
    keep_daily: int = _DEFAULT_DAILY,
    keep_weekly: int = _DEFAULT_WEEKLY,
) -> list[Path]:
    """保留最近 N 份日备份 + 最近 M 个自然周各一份，返回待删除列表。"""
    ordered = sorted(paths, key=parse_backup_time, reverse=True)
    if keep_daily < 0 or keep_weekly < 0:
        raise ValueError("保留份数不能为负")
    keep: set[Path] = set(ordered[:keep_daily])
    weeks: set[tuple[int, int]] = set()
    for path in ordered[keep_daily:]:
        year, week, _ = parse_backup_time(path).isocalendar()
        key = (year, week)
        if key in weeks:
            continue
        if len(weeks) >= keep_weekly:
            break
        weeks.add(key)
        keep.add(path)
    return [path for path in ordered if path not in keep]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip_reason(database: str) -> str | None:
    name = (database or "").strip().lower()
    if name.endswith("_test"):
        return "test_database"
    if name.endswith("_demo"):
        return "demo_database"
    return None


def backup_database(
    *,
    reason: str = "manual",
    database: str | None = None,
    host: str | None = None,
    port: int | str | None = None,
    user: str | None = None,
    password: str | None = None,
    directory: Path | None = None,
    keep_daily: int = _DEFAULT_DAILY,
    keep_weekly: int = _DEFAULT_WEEKLY,
    timeout_sec: int = 1800,
) -> dict:
    """执行一次备份；永不抛异常。"""
    url = make_url(config.DATABASE_URL_SYNC)
    target_db = database or url.database or config.POSTGRES_DATABASE
    skip = _skip_reason(target_db or "")
    if skip:
        return BackupResult("skipped", reason=skip).as_dict()

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return BackupResult(
            "failed",
            reason=reason,
            error="未找到 pg_dump；请在容器内运行或安装 postgresql-client",
        ).as_dict()

    folder = directory or backup_directory()
    folder.mkdir(parents=True, exist_ok=True)
    final_path = folder / backup_filename()
    tmp_path = final_path.with_suffix(".dump.tmp")
    env = os.environ.copy()
    target_password = password if password is not None else url.password
    if target_password:
        env["PGPASSWORD"] = str(target_password)
    command = [
        pg_dump,
        "-h", str(host or url.host or config.POSTGRES_HOST),
        "-p", str(port or url.port or config.POSTGRES_PORT),
        "-U", str(user or url.username or config.POSTGRES_USER),
        "-d", str(target_db),
        "-Fc", "--no-owner", "--no-privileges",
        "-f", str(tmp_path),
    ]
    try:
        completed = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            message = detail[-1] if detail else f"pg_dump exit={completed.returncode}"
            return BackupResult("failed", reason=reason, error=message[:500]).as_dict()
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return BackupResult("failed", reason=reason, error="备份文件为空").as_dict()
        with tmp_path.open("rb") as handle:
            if handle.read(5) != b"PGDMP":
                return BackupResult("failed", reason=reason, error="备份文件头非法").as_dict()
        checksum = _sha256(tmp_path)
        os.replace(tmp_path, final_path)
        final_path.with_suffix(".dump.sha256").write_text(
            f"{checksum}  {final_path.name}\n", encoding="utf-8"
        )
        deleted = _apply_retention(folder, keep_daily=keep_daily, keep_weekly=keep_weekly)
        logger.info(
            "数据库备份完成 reason=%s file=%s bytes=%d deleted=%d",
            reason, final_path.name, final_path.stat().st_size, len(deleted),
        )
        return BackupResult(
            "success",
            reason=reason,
            path=str(final_path),
            bytes=final_path.stat().st_size,
            sha256=checksum,
            deleted=[path.name for path in deleted],
        ).as_dict()
    except subprocess.TimeoutExpired:
        return BackupResult("failed", reason=reason, error="pg_dump 超时").as_dict()
    except Exception as exc:  # noqa: BLE001 - 备份失败不能抛给调用方
        logger.exception("数据库备份失败 reason=%s", reason)
        return BackupResult(
            "failed", reason=reason, error=f"{type(exc).__name__}: {exc}"
        ).as_dict()
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("清理临时备份文件失败 path=%s", tmp_path)


def _apply_retention(
    folder: Path, *, keep_daily: int, keep_weekly: int
) -> list[Path]:
    deleted: list[Path] = []
    for path in select_backups_to_delete(
        list_backups(folder), keep_daily=keep_daily, keep_weekly=keep_weekly
    ):
        try:
            path.unlink()
            checksum = path.with_suffix(".dump.sha256")
            if checksum.exists():
                checksum.unlink()
            deleted.append(path)
        except OSError:
            logger.warning("删除过期备份失败 path=%s", path)
    return deleted


def backup_age_hours(
    directory: Path | None = None, now: datetime | None = None
) -> float | None:
    latest = latest_backup(directory)
    if latest is None:
        return None
    moment = (now or datetime.now(_TZ)).astimezone(_TZ)
    return round((moment - parse_backup_time(latest)).total_seconds() / 3600, 1)


__all__ = [
    "BackupResult", "backup_directory", "backup_filename", "backup_due",
    "list_backups", "latest_backup", "select_backups_to_delete",
    "backup_database", "backup_age_hours",
]
