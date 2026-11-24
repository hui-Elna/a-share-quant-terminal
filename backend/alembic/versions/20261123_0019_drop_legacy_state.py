"""移除历史应用状态表。

Revision ID: 0019_drop_legacy_state
Revises: 0018_compass
Create Date: 2026-11-23

应用状态模块已整体移除；本迁移只负责删除既有库中的历史状态表。
全新库不会创建该表，因此使用 ``has_table`` 做幂等保护。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_drop_legacy_state"
down_revision: str | None = "0018_compass"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("app_watchlist_state"):
        op.drop_table("app_watchlist_state")


def downgrade() -> None:
    # 模块已移除，回滚不重建状态表。
    pass
