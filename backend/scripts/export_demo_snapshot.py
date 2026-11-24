"""导出只读演示站快照（V9 P0-1 / P1-6）。

设计：
  - 只用独立 ``*_demo`` 数据库，绝不读写生产库；
  - 演示行情来自 ``app.fixtures.demo_data.seed_demo_data``，下游走
    ``pipeline.run_task(features/scores/sector_scores/algos)``；
  - 通过 ``httpx.ASGITransport`` 在进程内调用现有 FastAPI 路由，保证导出
    形状与真实 API 完全一致（不启动 HTTP 服务、不走网络）；
  - 输出到 ``frontend/public/demo/*.json``，供 ``vite build --mode demo`` 使用。

用法（backend/ 下）：

    python scripts/export_demo_snapshot.py
    python scripts/export_demo_snapshot.py --output ../frontend/public/demo
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 必须在 import app.* 之前把数据库指到演示库；config.py 会读取这些环境变量
DEMO_DB = os.getenv("GFNA_DEMO_DATABASE_NAME", "gfna_demo").strip() or "gfna_demo"
if not DEMO_DB.endswith("_demo"):
    raise SystemExit(f"演示库名必须以 _demo 结尾，收到: {DEMO_DB!r}")
os.environ["POSTGRES_DATABASE"] = DEMO_DB
os.environ.setdefault("SCHEDULER_ENABLED", "0")
os.environ.setdefault("CATCH_UP_ENABLED", "0")
os.environ.setdefault("AUTO_CREATE_TABLES", "0")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core import config  # noqa: E402

# config.py 加载 .env 时会覆盖同名环境变量；这里在导入 db.session 之前
# 显式把运行期 URL 改到演示库，避免误连生产库。
DEMO_URL = make_url(config.DATABASE_URL).set(database=DEMO_DB).render_as_string(
    hide_password=False
)
DEMO_URL_SYNC = make_url(config.DATABASE_URL_SYNC).set(database=DEMO_DB).render_as_string(
    hide_password=False
)
config.POSTGRES_DATABASE = DEMO_DB
config.DATABASE_URL = DEMO_URL
config.DATABASE_URL_SYNC = DEMO_URL_SYNC
os.environ["GFNA_DEMO_DATABASE_NAME"] = DEMO_DB

from app.db.models import FactStockDaily  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engines  # noqa: E402
from app.fixtures.demo_data import seed_demo_data  # noqa: E402
from app.pipeline import run_task  # noqa: E402

# (输出文件, 请求路径)
EXPORTS: list[tuple[str, str]] = [
    ("health.json", "/health"),
    ("dashboard.json", "/api/v1/market/dashboard"),
    ("overview.json", "/api/v1/market/overview"),
    ("market_indices.json", "/api/v1/market/indices?days=60"),
    ("market_sectors.json", "/api/v1/market/sectors"),
    ("market_bull_bear.json", "/api/v1/market/bull-bear"),
    ("algo_list.json", "/api/v1/algo/list?include_observation=true"),
    ("algo_fields.json", "/api/v1/algo/fields"),
    ("algo_ladder.json", "/api/v1/algo/ladder?window=60&horizon=20"),
    ("algo_consensus.json", "/api/v1/algo/consensus?min_count=2&limit=100&offset=0"),
    ("algo_result_a3.json", "/api/v1/algo/a3"),
    ("algo_runs_a3.json", "/api/v1/algo/a3/runs?limit=10&offset=0"),
    ("algo_result_a4.json", "/api/v1/algo/a4"),
    ("algo_runs_a4.json", "/api/v1/algo/a4/runs?limit=10&offset=0"),
    ("news_stats.json", "/api/v1/stats"),
    ("news_sources.json", "/api/v1/sources"),
    ("news_articles.json", "/api/v1/articles?page=1&page_size=20"),
    ("freshness.json", "/api/v1/meta/freshness"),
    ("provider_health.json", "/api/v1/meta/provider-health?days=7"),
    ("trade_calendar.json", "/api/v1/meta/trade-calendar?only_open=true"),
    ("stock_600519.json", "/api/v1/stocks/600519"),
    ("stock_600519_kline.json", "/api/v1/stocks/600519/kline?limit=250"),
    ("stock_search.json", "/api/v1/stocks/search?q=%E8%8C%85%E5%8F%B0&limit=20"),
    ("stock_sparkline.json", "/api/v1/stocks/sparkline/batch?codes=600519,000001,601318&days=30"),
]


async def _ensure_database() -> None:
    url = make_url(DEMO_URL)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "select 1 from pg_database where datname = $1", DEMO_DB
        )
        if not exists:
            # 库名来自本地配置且已校验 _demo 后缀，不存在注入面
            await conn.execute(f'CREATE DATABASE "{DEMO_DB}"')
            print(f"created database {DEMO_DB}")
    finally:
        await conn.close()


def _migrate() -> None:
    env = os.environ.copy()
    env["GFNA_DEMO_DATABASE_NAME"] = DEMO_DB
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
    )


async def _seed_and_compute() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(func.count()).select_from(FactStockDaily))).scalar_one()
        if not rows:
            info = await seed_demo_data(db)
            print(f"seeded demo data: {info}")
        for task in (
            "features",
            "scores",
            "sector_scores",
            "market_factors",
            "temperature",
            "regime",
            "algos",
        ):
            result = await run_task(task, db=db)
            await db.commit()
            print(f"task {task}: {result.get('status')} rows={result.get('rows')}")


async def _seed_demo_news() -> None:
    """给演示库补几条确定性新闻；仅用于只读演示，不写生产库。"""
    from app.db.models import NewsArticle, NewsSource

    async with AsyncSessionLocal() as db:
        count = (await db.execute(select(func.count()).select_from(NewsArticle))).scalar_one()
        if count:
            return
        sources = [
            NewsSource(name="新浪财经", base_url="https://finance.sina.com.cn", language="zh"),
            NewsSource(name="同花顺", base_url="https://www.10jqka.com.cn", language="zh"),
            NewsSource(name="华尔街见闻", base_url="https://wallstreetcn.com", language="zh"),
        ]
        db.add_all(sources)
        await db.flush()
        now = datetime.now(timezone.utc)
        samples = [
            ("演示：A 股主要指数震荡整理，成交额保持平稳", "市场复盘", 0),
            ("演示：新能源板块出现分化，资金流向呈现结构性特征", "板块观察", 1),
            ("演示：高股息资产相对抗跌，防御型配置受关注", "风格观察", 0),
            ("演示：两市融资余额小幅变化，北向资金暂停披露", "资金面", 1),
            ("演示：财报披露进入密集期，关注业绩与估值的匹配", "基本面", 2),
        ]
        for index, (title, summary, source_index) in enumerate(samples):
            url = f"https://example.invalid/demo/news/{index + 1}"
            db.add(
                NewsArticle(
                    source_id=sources[source_index].id,
                    title=title,
                    url=url,
                    url_hash=NewsArticle.hash_url(url),
                    content=summary,
                    summary=summary,
                    publish_time=now - timedelta(hours=index + 1),
                    crawl_time=now,
                    is_processed=1,
                    sentiment_score=0.1 if index % 2 == 0 else -0.05,
                )
            )
        await db.commit()
        print("seeded demo news: 5 articles / 3 sources")


async def _fetch(app, path: str) -> Any:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://demo.local",
        timeout=120.0,
    ) as client:
        response = await client.get(path)
    if response.status_code >= 400:
        raise RuntimeError(f"{path} -> HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


async def _export(output: Path) -> dict:
    from app.main import app

    output.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for filename, path in EXPORTS:
        try:
            payload = await _fetch(app, path)
        except Exception as exc:  # noqa: BLE001 - 单个端点失败不阻断整批
            print(f"[warn] skip {filename}: {type(exc).__name__}: {exc}")
            continue
        (output / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        written.append(filename)
        print(f"wrote {filename}")

    # 板块评分 sparkline：codes 来自 dashboard，取前 10 个板块
    dashboard_path = output / "dashboard.json"
    if dashboard_path.exists():
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        sectors = dashboard.get("sectors") or []
        codes = [item.get("sector_code") for item in sectors[:10] if item.get("sector_code")]
        if codes:
            path = (
                "/api/v1/market/sector-scores/sparkline?codes="
                + ",".join(codes)
                + "&days=30"
            )
            try:
                payload = await _fetch(app, path)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] skip sector sparkline: {type(exc).__name__}: {exc}")
            else:
                (output / "sector_sparkline.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                written.append("sector_sparkline.json")
                print("wrote sector_sparkline.json")

    manifest = {
        "generated_at": date.today().isoformat(),
        "is_demo": True,
        "database": DEMO_DB,
        "source": "backend/app/fixtures/demo_data.py + pipeline.run_task",
        "files": written,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


async def _run_all(output: Path) -> dict:
    """所有异步步骤共用一个事件循环，避免 asyncpg 引擎跨 loop。"""
    await _ensure_database()
    _migrate()
    await _seed_and_compute()
    await _seed_demo_news()
    manifest = await _export(output)
    await dispose_engines()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="导出只读演示站快照")
    parser.add_argument(
        "--output",
        default=str(PROJECT_DIR / "frontend" / "public" / "demo"),
        help="输出目录，默认 frontend/public/demo",
    )
    args = parser.parse_args()

    manifest = asyncio.run(_run_all(Path(args.output)))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
