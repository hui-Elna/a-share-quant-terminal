"""
种子数据：策略元信息（A3/A4/C1/C2/C3/C6）
================================
参数的**唯一定义处**：后端 `strategy/specs.py` 的 SPECS 也引用同一份默认值，
前端则通过 `/algo/list` 拿 `params_schema` 渲染表单——避免三处各写一套默认值（V4 的坑）。

A1/A2 已按真实回测结果下线：A1 近三年收益 -16.4%、A2 -33.7%（最大回撤 -59%）。
B1–B5 及 A1/A2 已下线并移除实现（历史 run 仍保留在库中）；C1/C2/C3/C6 已于
2026-11-28 并入正式启用集，种子条目从 `SPECS` 生成，避免与接口清单漂移。
"""
from __future__ import annotations

from app.strategy.specs import SPECS

ALGO_SEED: list[dict] = [
    {
        "algo_code": "a3",
        "algo_name": "央企 × 综合评分TOP",
        "algo_type": "score",
        "short_desc": "央企属性 + 本地六因子综合评分（价值/质量/动量/波动/流动性/情绪）",
        "full_desc": (
            "筛选条件：① `dim_stock.ownership = 央企`（基于央企名录 + 名称规则，见 core/soe.py）；"
            "② 综合评分 ≥ min_score（0–10 分制，默认 6.0 分）。"
            "排序：综合评分降序。\n"
            "回测（2025-01-01 起）：收益 +22.9%、最大回撤 -10.4%、Sharpe 0.85。"
        ),
        "params_schema": [
            {"key": "limit", "label": "返回数量", "type": "select", "default": 20,
             "options": [10, 20, 50]},
            {"key": "min_score", "label": "最低综合评分(0-10)", "type": "number",
             "default": 6.0, "min": 0, "max": 10, "step": 0.5},
        ],
        "sort_order": 1,
    },
    {
        "algo_code": "a4",
        "algo_name": "高股息质量 × 低估值",
        "algo_type": "value",
        "short_desc": "股息率达标 + PE/PB 受控 + 质量与趋势过滤 = 红利质量股",
        "full_desc": (
            "筛选条件：① PE_TTM > 0 且 < pe_max（默认 15）；② 股息率在 "
            "div_min（默认 4%）到 dv_max（默认 15%）之间；③ 0 < PB <= pb_max（默认 2.5）；"
            "④ 综合评分 >= min_score（默认 5.0/10）；⑤ 20 日均成交额 >= min_amount；"
            "⑥ 收盘 >= MA120 × trend_dev（默认 0.9）。"
            "排序：股息率降序。需注意「股息陷阱」——一次性高分红会污染 TTM 口径。"
        ),
        "params_schema": [
            {"key": "limit", "label": "返回数量", "type": "select", "default": 20,
             "options": [10, 20, 50]},
            {"key": "pe_max", "label": "PE 上限", "type": "number",
             "default": 15, "min": 0.01, "max": 1000, "step": 1},
            {"key": "div_min", "label": "股息率下限(%)", "type": "number",
             "default": 4, "min": 0, "max": 100, "step": 0.5},
            {"key": "dv_max", "label": "股息率上限(%)", "type": "number",
             "default": 15, "min": 0.1, "max": 100, "step": 0.5},
            {"key": "pb_max", "label": "PB 上限", "type": "number",
             "default": 2.5, "min": 0.1, "max": 50, "step": 0.1},
            {"key": "min_score", "label": "最低综合评分(0-10)", "type": "number",
             "default": 5.0, "min": 0, "max": 10, "step": 0.5},
            {"key": "min_amount", "label": "20日均额下限(元)", "type": "number",
             "default": 5.0e7, "min": 0, "max": 1.0e12, "step": 1.0e7},
            {"key": "trend_dev", "label": "MA120 趋势容忍度", "type": "number",
             "default": 0.9, "min": 0.5, "max": 1.5, "step": 0.05},
        ],
        "sort_order": 2,
    },
]

#: C1/C2/C3/C6 的种子从 SPECS 生成：名称、描述与参数和接口完全同源。
_LEGACY_SEED_CODES = {"a3", "a4"}
ALGO_SEED.extend(
    {
        "algo_code": spec.code,
        "algo_name": spec.name,
        "algo_type": spec.algo_type,
        "short_desc": spec.short_desc,
        "full_desc": spec.full_desc,
        "params_schema": [dict(item) for item in spec.params_schema],
        "sort_order": spec.sort_order,
    }
    for spec in sorted(SPECS.values(), key=lambda item: item.sort_order)
    if spec.code not in _LEGACY_SEED_CODES
)

__all__ = ["ALGO_SEED"]
