"""
ORM 模型聚合导出
=================
`Base.metadata` 只有在模型类被 import 后才包含表定义，
因此这里必须**显式**导入所有模型（顺序无关，但不能漏）。
"""
from app.db.models.dim import (
    DimSectorMember, DimStock, DimStockDividend, DimStockFinancial,
)
from app.db.models.compass import (
    CompassPushLog, DimEtfSectorMap, FactDecisionCard, FactDecisionCheck, FactEtfDaily,
    FactPaperPortfolio, FactScenarioTrigger, FactSectorSentimentPercentile,
    FactSignalFailure,
)
from app.db.models.fact import (
    DailyReport, FactFeatureDaily, FactMarketDaily, FactMoneyflow, FactSectorDaily,
    FactStockDaily, FactStockScoreDaily, MacroRate, MarginTrading, MarketIndex,
    MarketRegimeDaily, MarketTemperatureDaily, SectorScore,
)
from app.db.models.meta import (
    AlgoMeta, IngestJob, MetaDataQuality, ProviderHealth, TradeCalendar,
)
from app.db.models.news import CrawlLog, NewsArticle, NewsSource
from app.db.models.push import PushLog
from app.db.models.strategy import (
    BacktestGrid, BacktestGridCell, BacktestRun, OOSRun, PaperNav, PaperPortfolio,
    StgRun, StgSignal, StgSignalPerf,
)

__all__ = [
    # 新闻
    "NewsSource", "NewsArticle", "CrawlLog",
    # 维度
    "DimStock", "DimSectorMember", "DimStockFinancial", "DimStockDividend",
    # 事实
    "FactStockDaily", "FactSectorDaily", "FactFeatureDaily", "FactStockScoreDaily",
    "FactMoneyflow", "MarketIndex", "MarginTrading", "SectorScore",
    "MarketTemperatureDaily", "MacroRate", "FactMarketDaily", "MarketRegimeDaily",
    "DailyReport",
    # 元数据
    "TradeCalendar", "MetaDataQuality", "IngestJob", "AlgoMeta", "ProviderHealth",
    # 策略
    "StgRun", "StgSignal", "StgSignalPerf", "BacktestRun",
    "BacktestGrid", "BacktestGridCell", "OOSRun",
    "PaperPortfolio", "PaperNav",
    # 推送
    "PushLog",
    # 仓位罗盘
    "DimEtfSectorMap", "FactEtfDaily", "FactSectorSentimentPercentile",
    "FactDecisionCard", "FactDecisionCheck", "FactScenarioTrigger",
    "FactSignalFailure", "FactPaperPortfolio", "CompassPushLog",
]
