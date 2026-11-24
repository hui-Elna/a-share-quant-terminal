"""迁移脚本的目标库解析与迁移前备份门禁。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import migrate_db


class _Target(SimpleNamespace):
    database = "gfna_db"
    host = "localhost"
    port = 5432
    username = "postgres"
    password = "secret"


def test_backup_failure_blocks_migration(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.backup.backup_database",
        lambda **kwargs: {"status": "failed", "error": "disk full"},
    )
    assert migrate_db._backup_before_upgrade(_Target(), skip_backup=False) == 1


def test_backup_skip_or_success_allows_migration(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.backup.backup_database",
        lambda **kwargs: {"status": "skipped", "reason": "test_database"},
    )
    assert migrate_db._backup_before_upgrade(_Target(), skip_backup=False) == 0
    assert migrate_db._backup_before_upgrade(_Target(), skip_backup=True) == 0



def test_target_url_defaults_to_configured_db(monkeypatch) -> None:
    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.delenv("GFNA_TEST_DB", raising=False)
    assert migrate_db._target_url().database == "gfna_db"


def test_target_url_respects_test_override(monkeypatch) -> None:
    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.setenv("GFNA_TEST_DB", "1")
    monkeypatch.setenv("GFNA_TEST_DATABASE_NAME", "gfna_unit_test")
    assert migrate_db._target_url().database == "gfna_unit_test"


def test_target_url_respects_demo_override(monkeypatch) -> None:
    monkeypatch.setenv("GFNA_DEMO_DATABASE_NAME", "gfna_demo")
    monkeypatch.delenv("GFNA_TEST_DB", raising=False)
    assert migrate_db._target_url().database == "gfna_demo"


def test_target_url_rejects_unsafe_suffixes(monkeypatch) -> None:
    monkeypatch.setenv("GFNA_DEMO_DATABASE_NAME", "gfna_db")
    with pytest.raises(RuntimeError):
        migrate_db._target_url()

    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.setenv("GFNA_TEST_DB", "1")
    monkeypatch.setenv("GFNA_TEST_DATABASE_NAME", "gfna_db")
    with pytest.raises(RuntimeError):
        migrate_db._target_url()
