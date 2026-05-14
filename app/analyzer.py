"""
分析引擎 —— 市场情绪评估、动态阈值、异动分析
"""

from __future__ import annotations
from typing import Optional

from app.models import Quote, Alert, SentimentResult, AnalysisStats, TechnicalSummary
from app.config import Config


# ============================================================
# 市场情绪评估
# ============================================================

def calc_market_sentiment(quotes: list[Quote]) -> SentimentResult:
    """基于所有监控标的（含指数）评估市场情绪

    评分维度:
    - 涨跌比: 0-40分
    - 涨跌幅中位数: 0-40分
    - 涨跌幅均值: 0-20分
    """
    valid = [q for q in quotes if q.change_pct is not None]
    if not valid:
        return SentimentResult()

    pcts = [q.change_pct for q in valid]  # type: ignore
    up_ratio = sum(1 for p in pcts if p > 0) / len(pcts)
    median_pct = sorted(pcts)[len(pcts) // 2]
    mean_pct = sum(pcts) / len(pcts)

    ratio_score = up_ratio * 40
    median_score = max(0.0, min(40.0, (median_pct + 3) / 6 * 40))
    mean_score = max(0.0, min(20.0, (mean_pct + 3) / 6 * 20))

    score = round(ratio_score + median_score + mean_score)
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

    if score >= 70:  # 强势
        t["涨幅预警"] = base["涨幅预警"] + 1.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 1.0 * intensity
        t["跌幅预警"] = base["跌幅预警"] + 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] + 0.3 * intensity
    elif score >= 55:  # 偏强
        t["涨幅预警"] = base["涨幅预警"] + 0.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 0.3 * intensity
    elif score <= 25:  # 弱势
        t["涨幅预警"] = base["涨幅预警"] - 1.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] - 1.0 * intensity
        t["跌幅预警"] = base["跌幅预警"] - 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] - 0.3 * intensity
        t["大跌预警"] = base["大跌预警"] - 1.0 * intensity
    elif score <= 40:  # 偏弱
        t["涨幅预警"] = base["涨幅预警"] - 0.5 * intensity
        t["跌幅预警"] = base["跌幅预警"] - 0.3 * intensity

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
) -> tuple[list[Alert], AnalysisStats]:
    """执行全部分析，返回异动列表和统计结果"""
    base = config.thresholds
    sentiment = calc_market_sentiment(quotes)
    thresholds = adjust_thresholds(base, sentiment, config)

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
    )
    return alerts, stats
