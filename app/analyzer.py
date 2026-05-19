"""
分析引擎 —— 市场情绪评估、动态阈值、异动分析
"""

from __future__ import annotations
import json
import statistics
from pathlib import Path
from typing import Optional

from app.models import (
    Quote, Alert, SentimentResult, AnalysisStats, TechnicalSummary,
    NorthFlowData, ScanRecord, FundScanStatus
)
from app.config import Config

# 盯盘历史文件路径
MONITOR_HISTORY_PATH = Path(__file__).resolve().parent.parent / "state" / "monitor_history.json"


def _load_scan_history() -> list[ScanRecord]:
    """加载盯盘扫描历史"""
    if not MONITOR_HISTORY_PATH.exists():
        return []

    try:
        with open(MONITOR_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        records = []
        for item in data:
            funds_status = {}
            for code, status_data in item.get("funds_status", {}).items():
                funds_status[code] = FundScanStatus(
                    price=status_data.get("price"),
                    change_pct=status_data.get("change_pct"),
                    volume=status_data.get("volume"),
                    vol_ratio=status_data.get("vol_ratio"),
                    alerts=status_data.get("alerts", []),
                    tech_signals=status_data.get("tech_signals", [])
                )

            records.append(ScanRecord(
                scan_id=item.get("scan_id", 0),
                time=item.get("time", ""),
                timestamp=item.get("timestamp", 0),
                market_sentiment=item.get("market_sentiment", {}),
                alerts_summary=item.get("alerts_summary", {}),
                funds_status=funds_status,
                llm_analysis=item.get("llm_analysis")
            ))

        return records
    except Exception as e:
        from app.utils import log
        log.warning(f"加载扫描历史失败: {e}")
        return []


def _save_scan_history(records: list[ScanRecord]) -> None:
    """保存盯盘扫描历史（保留最近20条，自动删除更早的记录）"""
    try:
        # records 已经包含了历史记录+新记录，直接截取最近20条
        # 早于第20条的记录会被自动丢弃
        if len(records) > 20:
            # 删除早于前20条的记录
            records_to_save = records[-20:]
        else:
            records_to_save = records

        data = []
        for record in records_to_save:
            funds_status_dict = {}
            for code, status in record.funds_status.items():
                funds_status_dict[code] = {
                    "price": status.price,
                    "change_pct": status.change_pct,
                    "volume": status.volume,
                    "vol_ratio": status.vol_ratio,
                    "alerts": status.alerts,
                    "tech_signals": status.tech_signals
                }

            data.append({
                "scan_id": record.scan_id,
                "time": record.time,
                "timestamp": record.timestamp,
                "market_sentiment": record.market_sentiment,
                "alerts_summary": record.alerts_summary,
                "funds_status": funds_status_dict,
                "llm_analysis": record.llm_analysis
            })

        MONITOR_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MONITOR_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        from app.utils import log
        log.warning(f"保存扫描历史失败: {e}")


# ============================================================
# 情绪拐点检测
# ============================================================

def _detect_sentiment_reversal(
    scan_history: list[ScanRecord],
    current_score: int,
    min_consecutive: int = 3,
    min_reversal_amount: int = 5
) -> tuple[str | None, str]:
    """
    检测情绪拐点

    Args:
        scan_history: 扫描历史记录
        current_score: 当前情绪分数
        min_consecutive: 最少连续变化次数
        min_reversal_amount: 最少反转幅度

    Returns:
        (拐点类型, 描述信息)
        - ("bottom", "描述") - 底部拐点
        - ("top", "描述") - 顶部拐点
        - (None, "") - 无拐点
    """
    if not scan_history or len(scan_history) < min_consecutive:
        return None, ""

    # 获取最近N次的情绪分数
    recent_scores = [
        s.market_sentiment.get("score", 50)
        for s in scan_history[-min_consecutive:]
    ]

    # 检测底部拐点：连续下降后首次回升
    is_consecutive_down = all(
        recent_scores[i] > recent_scores[i+1]
        for i in range(len(recent_scores) - 1)
    )

    if is_consecutive_down and current_score > recent_scores[-1] + min_reversal_amount:
        drop_amount = recent_scores[0] - recent_scores[-1]
        rise_amount = current_score - recent_scores[-1]
        return "bottom", f"📊 情绪拐点：连续{min_consecutive}期下跌（{recent_scores[0]}→{recent_scores[-1]}）后回升{rise_amount}分至{current_score}分，可能见底"

    # 检测顶部拐点：连续上升后首次回落
    is_consecutive_up = all(
        recent_scores[i] < recent_scores[i+1]
        for i in range(len(recent_scores) - 1)
    )

    if is_consecutive_up and current_score < recent_scores[-1] - min_reversal_amount:
        rise_amount = recent_scores[-1] - recent_scores[0]
        drop_amount = recent_scores[-1] - current_score
        return "top", f"📊 情绪拐点：连续{min_consecutive}期上涨（{recent_scores[0]}→{recent_scores[-1]}）后回落{drop_amount}分至{current_score}分，可能见顶"

    return None, ""


# ============================================================
# 情绪等级边界常量
# ============================================================
STRONG = 70       # 强势门槛
SLIGHTLY_UP = 55  # 偏强门槛
SLIGHTLY_DOWN = 40  # 偏弱门槛
WEAK = 25         # 弱势门槛

# ============================================================
# 市场情绪评估
# ============================================================

def calc_market_sentiment(quotes: list[Quote]) -> SentimentResult:
    """基于所有监控标的（含指数）评估市场情绪

    评分维度:
    - 涨跌比: 0-40分
    - 涨跌幅中位数: 0-40分
    - 分化度: 0-20分（标准差越小越一致，越有方向性）
    """
    valid = [q for q in quotes if q.change_pct is not None]
    if not valid:
        return SentimentResult()

    pcts = [q.change_pct for q in valid]  # type: ignore
    n = len(pcts)
    up_ratio = sum(1 for p in pcts if p > 0) / n
    median_pct = statistics.median(pcts)
    mean_pct = sum(pcts) / n

    # 标准差：衡量涨跌分化程度
    if n >= 2:
        std_pct = (sum((p - mean_pct) ** 2 for p in pcts) / (n - 1)) ** 0.5
    else:
        std_pct = 0.0

    ratio_score = up_ratio * 40
    median_score = max(0.0, min(40.0, (median_pct + 3) / 6 * 40))
    # 标准差越小 → 市场越一致 → 方向性越强，分数更高
    # std=0 → 20分，std=3 → 0分
    std_score = max(0.0, min(20.0, 20.0 - std_pct / 3 * 20))

    score = round(ratio_score + median_score + std_score)
    score = max(0, min(100, score))

    if score >= 75:
        label, detail = "强势 🔥", f"普涨格局，中位数{median_pct:+.2f}%"
    elif score >= 60:
        label, detail = "偏强 📈", f"涨多跌少，中位数{median_pct:+.2f}%"
    elif score >= 40:
        label, detail = "震荡 ⚖️", f"涨跌互现，中位数{median_pct:+.2f}%"
    elif score >= 25:
        label, detail = "偏弱 📉", f"跌多涨少，中位数{median_pct:+.2f}%"
    else:
        label, detail = "弱势 ❄️", f"普跌格局，中位数{median_pct:+.2f}%"

    return SentimentResult(
        score=score,
        label=label,
        detail=detail,
        up_ratio=round(up_ratio, 2),
        median_pct=median_pct,
    )


# ============================================================
# 动态阈值
# ============================================================

def adjust_thresholds(
    base: dict[str, float],
    sentiment: SentimentResult,
    config: Config,
) -> dict[str, float]:
    """根据市场情绪动态调整阈值"""
    if not config.dynamic_threshold_enabled:
        return dict(base)

    intensity = config.adjustment_intensity
    score = sentiment.score
    t = dict(base)

    if score >= STRONG:  # 强势：放宽涨跌幅阈值（更不敏感）
        t["涨幅预警"] = base["涨幅预警"] + 1.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 1.0 * intensity
        t["跌幅预警"] = base["跌幅预警"] + 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] + 0.3 * intensity
    elif score >= SLIGHTLY_UP:  # 偏强
        t["涨幅预警"] = base["涨幅预警"] + 0.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 0.3 * intensity
    elif score <= WEAK:  # 弱势：收紧涨幅阈值、放宽跌幅阈值（更敏感）
        t["涨幅预警"] = base["涨幅预警"] - 1.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] - 1.0 * intensity
        t["跌幅预警"] = base["跌幅预警"] + 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] + 0.3 * intensity
        t["大跌预警"] = base["大跌预警"] + 1.0 * intensity
    elif score <= SLIGHTLY_DOWN:  # 偏弱
        t["涨幅预警"] = base["涨幅预警"] - 0.5 * intensity
        t["跌幅预警"] = base["跌幅预警"] + 0.3 * intensity

    # 安全限幅
    t["涨幅预警"] = max(t["涨幅预警"], 1.0)
    t["涨幅关注"] = max(t["涨幅关注"], 0.5)
    t["跌幅预警"] = min(t["跌幅预警"], -0.5)
    t["跌幅关注"] = min(t["跌幅关注"], -0.3)
    t["大跌预警"] = min(t["大跌预警"], -2.0)

    return {k: round(v, 1) for k, v in t.items()}


# ============================================================
# 板块偏离度
# ============================================================

def calc_sector_deviations(quotes: list[Quote]) -> dict[str, dict]:
    """按板块类型计算偏离度，用于识别板块内领涨/领跌"""
    sectors: dict[str, list[float]] = {}
    for q in quotes:
        if q.type not in sectors:
            sectors[q.type] = []
        if q.change_pct is not None:
            sectors[q.type].append(q.change_pct)

    means = {st: sum(v) / len(v) for st, v in sectors.items() if v}

    deviations = {}
    for q in quotes:
        if q.type in means and q.change_pct is not None:
            dev = round(q.change_pct - means[q.type], 2)
            deviations[q.code] = {
                "sector": q.type,
                "sector_mean": round(means[q.type], 2),
                "deviation": dev,
            }
    return deviations


# ============================================================
# 异动分析（主入口）
# ============================================================

def analyze(
    quotes: list[Quote],
    prev_state: dict,
    config: Config,
    tech_summaries: dict[str, TechnicalSummary] | None = None,
    north_data: Optional["NorthFlowData"] = None,
    scan_history: list[ScanRecord] | None = None,
) -> tuple[list[Alert], AnalysisStats]:
    """执行全部分析，返回异动列表和统计结果"""
    base = config.thresholds
    sentiment = calc_market_sentiment(quotes)
    thresholds = adjust_thresholds(base, sentiment, config)

    # 检测情绪拐点
    sentiment_reversal = None
    sentiment_reversal_msg = ""
    if scan_history and len(scan_history) >= 3:
        reversal_type, reversal_msg = _detect_sentiment_reversal(
            scan_history, sentiment.score
        )
        if reversal_type:
            sentiment_reversal = reversal_type
            sentiment_reversal_msg = reversal_msg
            from app.utils import log
            log.info(f"  {reversal_msg}")

    up_warn = thresholds["涨幅预警"]
    up_notice = thresholds["涨幅关注"]
    down_warn = thresholds["跌幅预警"]
    down_crash = thresholds.get("大跌预警", -5.0)
    down_notice = thresholds["跌幅关注"]
    amp_warn = base.get("振幅预警", 5.0)
    vol_ratio = base.get("成交量倍率", 2.0)
    shrink_ratio = base.get("缩量倍率", 0.7)

    sector_dev = calc_sector_deviations(quotes)
    sector_threshold = config.sector_threshold

    alerts: list[Alert] = []
    up_count = down_count = alert_count = 0

    for q in quotes:
        if q.type == "指数":
            # 指数只参与情绪计算，不触发报警
            if q.change_pct is not None:
                if q.change_pct > 0:
                    up_count += 1
                elif q.change_pct < 0:
                    down_count += 1
            continue

        cp = q.change_pct
        vol = q.volume
        amp = q.amplitude
        items: list[str] = []

        # ---- 涨跌幅异动 ----
        if cp is not None:
            if cp >= up_warn:
                items.append(f"🔥 大涨 {cp:+.2f}%")
                alert_count += 1
            elif cp >= up_notice:
                items.append(f"📈 上涨 {cp:+.2f}%")

            if cp <= down_crash:
                items.append(f"🚨 暴跌 {cp:+.2f}%")
                alert_count += 1
            elif cp <= down_warn:
                items.append(f"⚠️ 大跌 {cp:+.2f}%")
                alert_count += 1
            elif cp <= down_notice:
                items.append(f"📉 下跌 {cp:+.2f}%")

            if cp > 0:
                up_count += 1
            elif cp < 0:
                down_count += 1

        # ---- 量价关系 ----
        prev = prev_state.get(q.code, {})
        prev_vol = prev.get("volume")

        if cp is not None and vol is not None and prev_vol and prev_vol > 0:
            vol_change = vol / prev_vol
            if cp > 0 and vol_change >= vol_ratio:
                items.append(f"📈💪 放量上涨 {vol_change:.1f}倍")
            elif cp > 0 and vol_change <= shrink_ratio:
                items.append(f"📈🤏 缩量上涨（买盘不强）")
            elif cp < 0 and vol_change >= vol_ratio:
                items.append(f"📉💥 放量下跌 {vol_change:.1f}倍⚠️")
                alert_count += 1
            elif cp < 0 and vol_change <= shrink_ratio:
                items.append(f"📉🤫 缩量下跌（抛压减弱）")
            elif vol_change >= vol_ratio:
                items.append(f"📊 量放 {vol_change:.1f}倍")

        # ---- 板块异动 ----
        dev = sector_dev.get(q.code)
        if dev and cp is not None and abs(dev["deviation"]) >= sector_threshold:
            direction = "领涨" if dev["deviation"] > 0 else "领跌"
            items.append(f"🏷️ {dev['sector']}中{direction} {dev['deviation']:+.2f}%")

        # ---- 趋势变化 ----
        if cp is not None and prev.get("change_pct") is not None:
            prev_cp = prev["change_pct"]
            if cp > 0 and prev_cp < 0:
                items.append("🔄 由跌转涨")
            elif cp < 0 and prev_cp > 0:
                items.append("🔄 由涨转跌")

        # ---- 振幅异常 ----
        if amp is not None and amp >= amp_warn:
            items.append(f"💫 振幅 {amp:.2f}%")

        # ---- 技术指标信号 ----
        if tech_summaries and q.code in tech_summaries:
            tech = tech_summaries[q.code]
            if tech.signals:
                for sig in tech.signals:
                    items.append(f"📐 {sig}")

        # ---- 历史趋势增强判断 ----
        if scan_history and len(scan_history) >= 3 and cp is not None:
            # 获取最近3次扫描中该基金的涨跌幅
            changes = [
                s.funds_status.get(q.code, {}).get("change_pct", 0)
                for s in scan_history[-3:]
                if s.funds_status.get(q.code)
            ]

            if len(changes) >= 3:
                # 连续3次上涨且幅度扩大 → 加速上涨预警
                if all(c > 0 for c in changes) and changes[0] < changes[1] < changes[2]:
                    items.append(f"🚀 加速上涨（连续3期放大）")
                    alert_count += 1

                # 连续3次下跌且幅度扩大 → 加速下跌预警
                elif all(c < 0 for c in changes) and abs(changes[0]) < abs(changes[1]) < abs(changes[2]):
                    items.append(f"📉 加速下跌（连续3期放大）⚠️")
                    alert_count += 1

            # 量能连续放大 → 资金持续关注
            vol_ratios = [
                s.funds_status.get(q.code, {}).get("vol_ratio", 1)
                for s in scan_history[-3:]
                if s.funds_status.get(q.code) and s.funds_status[q.code].vol_ratio
            ]

            if len(vol_ratios) >= 3 and all(v >= 1.5 for v in vol_ratios):
                items.append(f"💰 量能持续放大（资金持续关注）")

            # 同一警报连续触发3次 → 警报升级
            fund_alerts_history = []
            for s in scan_history[-3:]:
                if q.code in s.funds_status:
                    fund_alerts_history.extend(s.funds_status[q.code].alerts)

            # 统计相同类型的警报出现次数
            from collections import Counter
            alert_counts = Counter(fund_alerts_history)
            for alert_msg, count in alert_counts.items():
                if count >= 3 and alert_msg not in items:
                    items.append(f"🔁 {alert_msg}（连续3次触发）")
                    alert_count += 1

        if items:
            alerts.append(Alert(code=q.code, name=q.name, messages=items))

    stats = AnalysisStats(
        total=len(quotes),
        up=up_count,
        down=down_count,
        flat=len(quotes) - up_count - down_count,
        alert_count=alert_count,
        sentiment=sentiment,
        thresholds=thresholds,
        base_thresholds=base,
        dynamic_enabled=config.dynamic_threshold_enabled,
        north_flow=north_data,
    )
    return alerts, stats
