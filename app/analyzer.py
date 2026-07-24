"""
分析引擎 —— 市场情绪评估、动态阈值、异动分析
"""

from __future__ import annotations
import statistics
from typing import Optional

from app.models import Quote, Alert, SentimentResult, AnalysisStats, TechnicalSummary, NorthFlowData, MarketBreadth
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

def calc_market_sentiment(
    quotes: list[Quote],
    breadth: Optional["MarketBreadth"] = None,
) -> SentimentResult:
    """基于全市场广度 + 自选标的，综合评估市场情绪

    两层评分机制：
    1. 如果有全市场广度数据 → 全市场权重 55%，自选权重 45%
    2. 如果无广度数据 → 回退到仅自选标的评分（兼容旧版）

    全市场维度:
    - 涨跌比: 0-35分
    - 涨跌停情绪: 0-10分
    - 全市场成交额判断（相对于万亿基准）: 0-10分

    自选标的维度:
    - 涨跌幅中位数: 0-25分
    - 涨跌比: 0-10分
    - 分化度(标准差): 0-10分
    """
    # ---- 自选标的分析 ----
    valid = [q for q in quotes if q.change_pct is not None]
    if not valid:
        return SentimentResult()

    pcts = [q.change_pct for q in valid]  # type: ignore
    n = len(pcts)
    watch_up_ratio = sum(1 for p in pcts if p > 0) / n
    median_pct = statistics.median(pcts)
    mean_pct = sum(pcts) / n

    # 标准差：衡量涨跌分化程度
    if n >= 2:
        std_pct = (sum((p - mean_pct) ** 2 for p in pcts) / (n - 1)) ** 0.5
    else:
        std_pct = 0.0

    if breadth is not None and breadth.is_valid:
        # ============ 双层评分：全市场(55%) + 自选(45%) ============

        # --- 全市场涨跌比 (0-35) ---
        # up_ratio=0.5(涨跌各半) → 17.5分，up_ratio=0.8 → 28分，up_ratio=0.2 → 7分
        ratio_score = breadth.up_ratio * 35

        # --- 涨跌停情绪 (0-10) ---
        # 涨停多+跌停少 → 高，涨停少+跌停多 → 低，正常 → 5
        if breadth.limit_up >= 80 and breadth.limit_down < 10:
            limit_score = 10.0  # 亢奋
        elif breadth.limit_down >= 50 and breadth.limit_up < 20:
            limit_score = 0.0   # 恐慌
        elif breadth.limit_up >= 50 and breadth.limit_down >= 30:
            limit_score = 4.0   # 分化加剧
        elif breadth.limit_up < 30 and breadth.limit_down < 10:
            limit_score = 5.0   # 平淡
        else:
            # 正常：根据涨跌停比线性插值
            if breadth.limit_down > 0:
                limit_ratio = breadth.limit_up / breadth.limit_down
                limit_score = max(0.0, min(10.0, 5.0 + (limit_ratio - 1) * 2))
            else:
                limit_score = 7.0 if breadth.limit_up > 0 else 5.0

        # --- 全市场成交额 (0-10) ---
        # 万亿以上在牛市中才有支撑力，8000-10000亿中性，<6000亿弱势
        if breadth.total_amount >= 12000:
            vol_score = 10.0
        elif breadth.total_amount >= 8000:
            vol_score = 5.0 + (breadth.total_amount - 8000) / 4000 * 5.0
        elif breadth.total_amount >= 5000:
            vol_score = (breadth.total_amount - 5000) / 3000 * 5.0
        else:
            vol_score = 0.0

        macro_score = ratio_score + limit_score + vol_score  # 0-55

        # --- 自选标的：中位数 (0-25) ---
        # median_pct=-3% → 0分，median_pct=0% → 12.5分，median_pct=+3% → 25分
        watch_median_score = max(0.0, min(25.0, (median_pct + 3) / 6 * 25))

        # --- 自选标的：涨跌比 (0-10) ---
        watch_ratio_score = watch_up_ratio * 10

        # --- 自选标的：分化度 (0-10) ---
        # std=0 → 10分，std=3 → 0分
        watch_std_score = max(0.0, min(10.0, 10.0 - std_pct / 3 * 10))

        watch_score = watch_median_score + watch_ratio_score + watch_std_score  # 0-45

        score = round(macro_score + watch_score)
        score = max(0, min(100, score))

        # 定性标签（引入全市场数据后更准确）
        if score >= 75:
            label, detail = "强势 🔥", (
                f"{breadth.breadth_label}，{breadth.up_count}涨{breadth.down_count}跌"
                f"，成交{breadth.total_amount:.0f}亿"
            )
        elif score >= 60:
            label, detail = "偏强 📈", (
                f"{breadth.breadth_label}，{breadth.up_count}涨{breadth.down_count}跌"
                f"，成交{breadth.total_amount:.0f}亿"
            )
        elif score >= 40:
            label, detail = "震荡 ⚖️", (
                f"{breadth.breadth_label}，涨跌各半"
                f"，成交{breadth.total_amount:.0f}亿"
            )
        elif score >= 25:
            label, detail = "偏弱 📉", (
                f"{breadth.breadth_label}，{breadth.up_count}涨{breadth.down_count}跌"
                f"，成交{breadth.total_amount:.0f}亿"
            )
        else:
            label, detail = "弱势 ❄️", (
                f"{breadth.breadth_label}，{breadth.up_count}涨{breadth.down_count}跌"
                f"，成交{breadth.total_amount:.0f}亿"
            )

    else:
        # ============ 仅自选标的评分（兼容旧版）============
        ratio_score = watch_up_ratio * 40
        median_score = max(0.0, min(40.0, (median_pct + 3) / 6 * 40))
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
        up_ratio=round(watch_up_ratio, 2),
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
    """根据市场情绪不对称调整阈值

    核心原则：不做对称漂移，而是基于交易逻辑不对称调整。

    强势市场（普涨）:
    - 涨幅阈值 ↑ 放宽：普涨中大涨不稀有，减少噪音
    - 跌幅阈值 ↓ 收紧：普涨中还跌的标的更值得警惕（跑输市场）

    弱势市场（普跌）:
    - 涨幅阈值 ↓ 收紧：普跌中逆势上涨才是真强势，值得关注
    - 跌幅阈值 ↑ 放宽：普跌中跌是正常的，减少噪音

    震荡市场：双向中性调整
    """
    if not config.dynamic_threshold_enabled:
        return dict(base)

    intensity = config.adjustment_intensity
    score = sentiment.score
    t = dict(base)

    if score >= STRONG:  # 强势：放宽涨幅、收紧跌幅
        t["涨幅预警"] = base["涨幅预警"] + 1.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 1.0 * intensity
        # 强势中跌是异类 → 收紧跌幅阈值（更容易触发）
        t["跌幅预警"] = base["跌幅预警"] + 1.0 * intensity   # 例如 -2.5→-1.0，更容易触发
        t["跌幅关注"] = base["跌幅关注"] + 0.8 * intensity
    elif score >= SLIGHTLY_UP:  # 偏强
        t["涨幅预警"] = base["涨幅预警"] + 0.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] + 0.3 * intensity
        t["跌幅预警"] = base["跌幅预警"] + 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] + 0.3 * intensity
    elif score <= WEAK:  # 弱势：收紧涨幅、放宽跌幅
        # 弱势中涨是异类 → 收紧涨幅阈值（更容易触发）
        t["涨幅预警"] = base["涨幅预警"] - 1.0 * intensity   # 例如 3.0→1.5，更容易触发
        t["涨幅关注"] = base["涨幅关注"] - 0.8 * intensity
        t["跌幅预警"] = base["跌幅预警"] - 1.5 * intensity   # 放宽
        t["跌幅关注"] = base["跌幅关注"] - 1.0 * intensity
        t["大跌预警"] = base.get("大跌预警", -5.0) - 1.0 * intensity
    elif score <= SLIGHTLY_DOWN:  # 偏弱
        t["涨幅预警"] = base["涨幅预警"] - 0.5 * intensity
        t["涨幅关注"] = base["涨幅关注"] - 0.3 * intensity
        t["跌幅预警"] = base["跌幅预警"] - 0.5 * intensity
        t["跌幅关注"] = base["跌幅关注"] - 0.3 * intensity

    # 安全限幅
    t["涨幅预警"] = max(t["涨幅预警"], 0.5)
    t["涨幅关注"] = max(t["涨幅关注"], 0.3)
    t["跌幅预警"] = min(t["跌幅预警"], -0.3)
    t["跌幅关注"] = min(t["跌幅关注"], -0.2)
    if "大跌预警" in t:
        t["大跌预警"] = min(t["大跌预警"], -1.5)

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
    market_breadth: Optional["MarketBreadth"] = None,
) -> tuple[list[Alert], AnalysisStats]:
    """执行全部分析，返回异动列表和统计结果"""
    base = config.thresholds
    sentiment = calc_market_sentiment(quotes, breadth=market_breadth)
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

        # ---- 量价关系（使用 turnover_rate 估算量比）----
        # 注意：不能直接用当前成交量除以上一次扫描的成交量，
        # 因为成交量是当日累计值，会随时间不断增加。
        # 正确的量比应该用：当前成交量 / 过去 N 日平均成交量
        # 这个分析已经在技术指标中通过 analyze_volume_price() 完成
        # 这里只保留 turnover_rate（换手率）作为辅助判断
        if q.turnover_rate is not None and q.turnover_rate > 5:
            items.append(f"🔥 高换手 {q.turnover_rate:.2f}%")

        # ---- 趋势变化 ----
        prev = prev_state.get(q.code, {})
        if cp is not None and prev.get("change_pct") is not None:
            prev_cp = prev["change_pct"]
            if cp > 0 and prev_cp < 0:
                items.append("🔄 由跌转涨")
            elif cp < 0 and prev_cp > 0:
                items.append("🔄 由涨转跌")

        # ---- 板块异动 ----
        dev = sector_dev.get(q.code)
        if dev and cp is not None and abs(dev["deviation"]) >= sector_threshold:
            direction = "领涨" if dev["deviation"] > 0 else "领跌"
            items.append(f"🏷️ {dev['sector']}中{direction} {dev['deviation']:+.2f}%")

        # ---- 振幅异常 ----
        if amp is not None and amp >= amp_warn:
            items.append(f"💫 振幅 {amp:.2f}%")

        # ---- 主力资金异动 ----
        inflow = q.main_net_inflow
        amount = q.amount
        if inflow is not None and amount and amount > 0:
            inflow_pct = inflow / amount * 100  # 主力净流入占成交额百分比
            if inflow > 0 and inflow_pct >= 15 and cp is not None and cp > 0:
                items.append(f"🔵 主力大幅买入(净{inflow/1e8:.2f}亿, 占{inflow_pct:.0f}%)")
                alert_count += 1
            elif inflow < 0 and abs(inflow_pct) >= 10 and cp is not None and cp < -1:
                items.append(f"🔴 主力大幅出逃(净{inflow/1e8:.2f}亿, 占{abs(inflow_pct):.0f}%)")
                alert_count += 1

            # 量价背离
            if cp is not None and cp > 2 and inflow < 0:
                items.append(f"⚠️ 拉升出货(涨{cp:+.1f}%但主力净流出{inflow/1e8:.2f}亿)")
                alert_count += 1
            elif cp is not None and cp < -2 and inflow > 0 and inflow_pct >= 5:
                items.append(f"💎 打压吸筹(跌{cp:+.1f}%但主力净流入{inflow/1e8:.2f}亿)")
                alert_count += 1

        # ---- 技术指标信号 ----
        if tech_summaries and q.code in tech_summaries:
            tech = tech_summaries[q.code]
            # 跳空信号单独处理（醒目 + 计为异动）
            if tech.has_gap:
                gap_emoji = "⬆️" if tech.gap_type == "向上跳空" else "⬇️"
                if abs(tech.gap_pct) >= 2:
                    items.append(f"{gap_emoji} 大幅跳空({tech.gap_detail})")
                    alert_count += 1
                elif tech.gap_filled_pct >= 80 and tech.gap_filled_pct < 100:
                    items.append(f"{gap_emoji} 跳空近回补({tech.gap_detail})")
                elif not tech.signals or all("跳空" not in s for s in tech.signals):
                    items.append(f"{gap_emoji} {tech.gap_detail}")
            # 突破信号单独处理
            if tech.breakout_type:
                items.append(f"🎯 {tech.breakout_detail}")
                alert_count += 1
            # 其他指标信号
            for sig in tech.signals:
                # 跳过已在上面处理过的跳空/突破信号
                if "跳空" not in sig and "突破" not in sig and "跌破" not in sig:
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
        market_breadth=market_breadth,
    )
    return alerts, stats


# ============================================================
# 扫描历史持久化
# ============================================================

# 扫描历史最大保留条目数（约覆盖最近 3-5 个交易日）
MAX_SCAN_HISTORY = 200


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
                        ma5=tech_data.get("ma5"),
                        ma10=tech_data.get("ma10"),
                        ma20=tech_data.get("ma20"),
                        ma60=tech_data.get("ma60"),
                        ma_alignment=tech_data.get("ma_alignment", ""),
                        ma_alignment_detail=tech_data.get("ma_alignment_detail", ""),
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
        # 只保留最近 N 条记录，防止文件无限增长
        recent = scan_history[-MAX_SCAN_HISTORY:] if len(scan_history) > MAX_SCAN_HISTORY else scan_history

        data = []
        for record in recent:
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
                        "ma5": ts.ma5,
                        "ma10": ts.ma10,
                        "ma20": ts.ma20,
                        "ma60": ts.ma60,
                        "ma_alignment": ts.ma_alignment,
                        "ma_alignment_detail": ts.ma_alignment_detail,
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
