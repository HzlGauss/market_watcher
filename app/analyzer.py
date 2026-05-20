"""
分析引擎 —— 市场情绪评估、动态阈值、异动分析
"""

from __future__ import annotations
import statistics
from typing import Optional

from app.models import Quote, Alert, SentimentResult, AnalysisStats, TechnicalSummary, NorthFlowData
from app.config import Config


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
        north_flow=north_data,
    )
    return alerts, stats


# ============================================================
# 扫描历史持久化
# ============================================================

def _load_scan_history() -> list["ScanRecord"]:
    """加载扫描历史（从 JSON 文件）

    Returns:
        扫描记录列表，如果文件不存在或读取失败则返回空列表
    """
    import json
    from pathlib import Path

    state_dir = Path(__file__).resolve().parent.parent / "state"
    history_file = state_dir / "scan_history.json"

    if not history_file.exists():
        return []

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        records: list["ScanRecord"] = []
        from app.models import ScanRecord, FundScanStatus, TechSnapshot

        for item in data:
            funds_status = {}
            for code, status_data in item.get("funds_status", {}).items():
                tech_snapshot = None
                if status_data.get("tech_snapshot"):
                    tech_data = status_data["tech_snapshot"]
                    tech_snapshot = TechSnapshot(
                        rsi=tech_data.get("rsi"),
                        rsi_signal=tech_data.get("rsi_signal", ""),
                        macd_dif=tech_data.get("macd_dif"),
                        macd_dea=tech_data.get("macd_dea"),
                        macd_histogram=tech_data.get("macd_histogram"),
                        macd_signal=tech_data.get("macd_signal", ""),
                        kdj_k=tech_data.get("kdj_k"),
                        kdj_d=tech_data.get("kdj_d"),
                        kdj_j=tech_data.get("kdj_j"),
                        kdj_signal=tech_data.get("kdj_signal", ""),
                        support=tech_data.get("support"),
                        resistance=tech_data.get("resistance"),
                        swing_supports=tech_data.get("swing_supports", []),
                        swing_resistances=tech_data.get("swing_resistances", []),
                        pivot_supports=tech_data.get("pivot_supports", []),
                        pivot_resistances=tech_data.get("pivot_resistances", []),
                        volume_clusters=tech_data.get("volume_clusters", []),
                        atr=tech_data.get("atr"),
                        bb_upper=tech_data.get("bb_upper"),
                        bb_middle=tech_data.get("bb_middle"),
                        bb_lower=tech_data.get("bb_lower"),
                        bb_width=tech_data.get("bb_width"),
                        bb_signal=tech_data.get("bb_signal", ""),
                        signals=tech_data.get("signals", []),
                    )

                funds_status[code] = FundScanStatus(
                    price=status_data.get("price"),
                    change_pct=status_data.get("change_pct"),
                    volume=status_data.get("volume"),
                    vol_ratio=status_data.get("vol_ratio"),
                    alerts=status_data.get("alerts", []),
                    tech_signals=status_data.get("tech_signals", []),
                    tech_snapshot=tech_snapshot,
                )

            records.append(ScanRecord(
                scan_id=item.get("scan_id", 0),
                time=item.get("time", ""),
                timestamp=item.get("timestamp", 0),
                market_sentiment=item.get("market_sentiment", {}),
                alerts_summary=item.get("alerts_summary", {}),
                funds_status=funds_status,
                llm_analysis=item.get("llm_analysis"),
            ))

        return records

    except Exception as e:
        from app.utils import log
        log.warning(f"加载扫描历史失败: {e}")
        return []


def _save_scan_history(scan_history: list["ScanRecord"]) -> None:
    """保存扫描历史到 JSON 文件

    Args:
        scan_history: 扫描记录列表
    """
    import json
    from pathlib import Path

    state_dir = Path(__file__).resolve().parent.parent / "state"
    history_file = state_dir / "scan_history.json"

    try:
        data = []
        for record in scan_history:
            funds_status = {}
            for code, status in record.funds_status.items():
                tech_snapshot_data = None
                if status.tech_snapshot:
                    ts = status.tech_snapshot
                    tech_snapshot_data = {
                        "rsi": ts.rsi,
                        "rsi_signal": ts.rsi_signal,
                        "macd_dif": ts.macd_dif,
                        "macd_dea": ts.macd_dea,
                        "macd_histogram": ts.macd_histogram,
                        "macd_signal": ts.macd_signal,
                        "kdj_k": ts.kdj_k,
                        "kdj_d": ts.kdj_d,
                        "kdj_j": ts.kdj_j,
                        "kdj_signal": ts.kdj_signal,
                        "support": ts.support,
                        "resistance": ts.resistance,
                        "swing_supports": ts.swing_supports,
                        "swing_resistances": ts.swing_resistances,
                        "pivot_supports": ts.pivot_supports,
                        "pivot_resistances": ts.pivot_resistances,
                        "volume_clusters": ts.volume_clusters,
                        "atr": ts.atr,
                        "bb_upper": ts.bb_upper,
                        "bb_middle": ts.bb_middle,
                        "bb_lower": ts.bb_lower,
                        "bb_width": ts.bb_width,
                        "bb_signal": ts.bb_signal,
                        "signals": ts.signals,
                    }

                funds_status[code] = {
                    "price": status.price,
                    "change_pct": status.change_pct,
                    "volume": status.volume,
                    "vol_ratio": status.vol_ratio,
                    "alerts": status.alerts,
                    "tech_signals": status.tech_signals,
                    "tech_snapshot": tech_snapshot_data,
                }

            data.append({
                "scan_id": record.scan_id,
                "time": record.time,
                "timestamp": record.timestamp,
                "market_sentiment": record.market_sentiment,
                "alerts_summary": record.alerts_summary,
                "funds_status": funds_status,
                "llm_analysis": record.llm_analysis,
            })

        state_dir.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        from app.utils import log
        log.warning(f"保存扫描历史失败: {e}")
