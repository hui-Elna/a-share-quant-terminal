"""手动数据库备份。

用法：
    python scripts/backup_db.py                 # 备份到 BACKUP_DIR / /app/backups
    python scripts/backup_db.py --dir D:\\backups
    python scripts/backup_db.py --keep-daily 14 --keep-weekly 8

退出码：0=成功或跳过，1=失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.logging import setup_logging  # noqa: E402
from app.services.backup import backup_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="GFNA 数据库备份")
    parser.add_argument("--dir", help="备份目录，默认 BACKUP_DIR 或 /app/backups")
    parser.add_argument("--keep-daily", type=int, default=7)
    parser.add_argument("--keep-weekly", type=int, default=4)
    parser.add_argument("--reason", default="cli")
    args = parser.parse_args()

    setup_logging()
    result = backup_database(
        reason=args.reason,
        directory=Path(args.dir) if args.dir else None,
        keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
