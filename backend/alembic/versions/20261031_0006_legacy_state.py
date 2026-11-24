"""历史迁移占位（应用状态表已移除）。

Revision ID: 0006_watchlist_state
Revises: 0005_signal_perf
Create Date: 2026-10-31

该 revision 仅用于保持既有数据库的 Alembic 版本链连续；应用状态表已由
``0019_drop_legacy_state`` 删除，本迁移不再创建任何表。
"""
from __future__ import annotations

revision: str = "0006_watchlist_state"
down_revision: str | None = "0005_signal_perf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
