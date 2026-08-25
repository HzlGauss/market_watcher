"""
分析引擎 —— 市场情绪评估、动态阈值、异动分析
"""

from __future__ import annotations
import statistics
import time
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
    """按板块类型/行业计算偏离度，用于识别板块内领涨/领跌

    优先使用 industry 字段（真实行业分类），
    回退到 type 字段（ETF 类型标签）。
    """
    sectors: dict[str, list[float]] = {}
    for q in quotes:
        key = q.industry or q.type
        if not key:
            continue
        if key not in sectors:
            sectors[key] = []
        if q.change_pct is not None:
            sectors[key].append(q.change_pct)

    means = {st: sum(v) / len(v) for st, v in sectors.items() if v}

    deviations = {}
    for q in quotes:
        key = q.industry or q.type
        if key in means and q.change_pct is not None:
            dev = round(q.change_pct - means[key], 2)
            deviations[q.code] = {
                "sector": key,
                "sector_mean": round(means[key], 2),
                "deviation": dev,
            }
    return deviations


# ============================================================
# 行业名 → 板块名 对齐（精确 → 手工别名表 → 模糊包含）
# ============================================================

_alias_cache: dict = {"_ts": 0.0, "_data": {}}
_ALIAS_TTL = 300  # 别名表每 5 分钟重读一次，手工改表后最多 5 分钟生效


def _load_industry_alias() -> dict[str, str]:
    """读取 state/industry_alias.json 手工别名表（带缓存）

    表内容形如 {"券商": "证券Ⅱ", "军工": "国防军工", ...}，
    用于把 ETF 名称推断出的粗粒度行业名对齐到东财实时板块名。
    """
    global _alias_cache
    now = time.time()
    if now - _alias_cache["_ts"] < _ALIAS_TTL:
        return _alias_cache["_data"]

    from pathlib import Path
    import json as _json
    from app.utils import log

    path = Path(__file__).resolve().parent.parent / "state" / "industry_alias.json"
    alias: dict[str, str] = {}
    try:
        if path.exists():
            raw = _json.loads(path.read_text(encoding="utf-8"))
            alias = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str) and v}
    except Exception as e:
        log.warning(f"行业别名表读取失败: {e}")
    _alias_cache = {"_ts": now, "_data": alias}
    return alias


def _resolve_board_name(industry: str, board_names: set) -> Optional[str]:
    """把（ETF 推断的）行业名解析到实时板块名

    三层匹配：精确命中 → 手工别名表 → 模糊包含（板块名包含行业词）。
    都匹配不到返回 None，调用方降级为「无板块指数」。
    """
    if not industry:
        return None
    # 1. 精确命中
    if industry in board_names:
        return industry
    # 2. 手工别名表（同义词/改名，模糊救不了的）
    alias = _load_industry_alias()
    if industry in alias and alias[industry] in board_names:
        return alias[industry]
    # 3. 模糊：板块名包含行业词，优先非 III 级、名称最短
    candidates = [bn for bn in board_names if industry in bn]
    if candidates:
        candidates.sort(key=lambda b: (b.endswith("Ⅲ"), len(b)))
        return candidates[0]
    return None


# 宽基指数 ETF 行业标签 → 大盘指数基准名（对应 fetch_major_indices 返回的 name）
_INDEX_ETF_BENCHMARK: dict[str, str] = {
    "创业板": "创业板指",
    "创业板50": "创业板50",
    "沪深300": "沪深300",
    "中证500": "中证500",
    "中证1000": "中证1000",
    "科创50": "科创50",
    "科创": "科创50",
}


def analyze_sector_context(
    quotes: list[Quote],
    sector_boards: Optional[list] = None,
    major_indices: Optional[list[Quote]] = None,
) -> dict:
    """综合分析标的与所属行业板块的关系

    Args:
        quotes: 已填充 industry 字段的行情列表
        sector_boards: 行业板块数据列表（SectorBoard），可选
        major_indices: 大盘指数行情列表（Quote），可选；宽基指数 ETF
            匹配不到行业板块时，回退用它做基准对比

    Returns:
        {
            "sector_ranks": {板块名: 排名信息},
            "portfolio_sectors": {板块名: [标的列表]},
            "per_stock": {code: {sector, sector_chg, relative_strength, label}},
            "top_sectors": [(板块名, 涨跌幅), ...],
            "bottom_sectors": [(板块名, 涨跌幅), ...],
        }
    """
    result: dict = {
        "sector_ranks": {},
        "portfolio_sectors": {},
        "per_stock": {},
        "top_sectors": [],
        "bottom_sectors": [],
    }

    # 1. 建立板块涨跌映射
    sector_chg_map: dict[str, float] = {}
    if sector_boards:
        for i, sb in enumerate(sector_boards):
            if sb.name and sb.change_pct is not None:
                sector_chg_map[sb.name] = sb.change_pct
                result["sector_ranks"][sb.name] = {
                    "rank": i + 1,
                    "total": len(sector_boards),
                    "change_pct": sb.change_pct,
                    "leader": sb.leader_stock,
                    "main_net": sb.main_net_inflow,
                }

    # 全部板块名集合（含 change_pct 为空的板块），供行业名对齐用
    board_names = {sb.name for sb in sector_boards if sb.name} if sector_boards else set()

    # 2. Top/Bottom 板块
    if sector_boards:
        sorted_boards = sorted(
            [sb for sb in sector_boards if sb.change_pct is not None],
            key=lambda sb: sb.change_pct, reverse=True,
        )
        result["top_sectors"] = [(sb.name, sb.change_pct) for sb in sorted_boards[:5]]
        result["bottom_sectors"] = [(sb.name, sb.change_pct) for sb in sorted_boards[-5:]]

    # 3. 按行业分组持仓
    industry_groups: dict[str, list[Quote]] = {}
    for q in quotes:
        ind = q.industry or q.type or "其他"
        if ind not in industry_groups:
            industry_groups[ind] = []
        industry_groups[ind].append(q)

    for ind, group in industry_groups.items():
        result["portfolio_sectors"][ind] = [
            {"name": q.name, "code": q.code, "chg": q.change_pct}
            for q in group
        ]

    # 大盘指数涨跌映射（宽基指数 ETF 兜底基准）
    index_chg_map: dict[str, float] = {}
    if major_indices:
        index_chg_map = {
            m.name: m.change_pct
            for m in major_indices
            if m.name and m.change_pct is not None
        }

    # 4. 每只标的 vs 板块对比
    for q in quotes:
        ind = q.industry or q.type or ""
        if not ind or q.change_pct is None:
            continue
        resolved = _resolve_board_name(ind, board_names)
        sector_chg = sector_chg_map.get(resolved) if resolved else None
        benchmark = "板块"  # 基准标签：行业板块，或回退后的宽基指数
        if sector_chg is None:
            # 行业板块匹配不到时，宽基指数 ETF 回退到大盘指数基准
            index_name = _INDEX_ETF_BENCHMARK.get(ind)
            if index_name and index_name in index_chg_map:
                sector_chg = index_chg_map[index_name]
                benchmark = index_name
        info: dict = {
            "sector": ind,
            "sector_chg": sector_chg,
            "relative_strength": None,
            "label": "",
        }
        if sector_chg is not None:
            rs = round(q.change_pct - sector_chg, 2)
            info["relative_strength"] = rs
            if rs > 1.0:
                info["label"] = f"领先{benchmark}{rs:+.1f}%"
            elif rs < -1.0:
                info["label"] = f"落后{benchmark}{rs:+.1f}%"
            else:
                info["label"] = f"与{benchmark}同步"
        else:
            info["label"] = f"板块{ind}(无板块指数)"

        if ind in result["sector_ranks"]:
            info["sector_rank"] = result["sector_ranks"][ind]["rank"]
            info["sector_total"] = result["sector_ranks"][ind]["total"]

        result["per_stock"][q.code] = info

    return result


# 拆单检测阈值（可调）
_SPLIT_SMALL_PCT = 6.0    # 小单净流入/流出占成交额比重的下限（%）
_SPLIT_BIG_PCT = 3.0      # 大单+超大单净流入占成交额比重的上限（%，拆单时大单应接近无动作）
_SPLIT_PRICE_MAX = 3.0    # 价格温和变动幅度上限（%，排除暴涨暴跌）


def detect_split_order(ff, amount: float, change_pct: Optional[float]) -> Optional[str]:
    """检测疑似主力拆单行为（把小单伪装成散户，隐藏真实意图）

    核心逻辑：小单持续单边流入/流出 + 大单/超大单几乎无动作 = 资金在刻意隐藏。

    优先使用东方财富直接提供的净占比字段（f57-f61），
    缺失时回退到「净额 / 成交额」计算。

    Args:
        ff: FundFlowDetail 资金流明细
        amount: 成交额（元）
        change_pct: 涨跌幅（%）

    Returns:
        拆单信号字符串，无拆单迹象时返回 None
    """
    if not ff:
        return None

    # 小单净占比（%）：优先直接字段，回退到净额/成交额
    if ff.small_pct is not None:
        small_pct = ff.small_pct
    elif ff.small_net is not None and amount and amount > 0:
        small_pct = ff.small_net / amount * 100
    else:
        return None

    # 大单+超大单净占比（%）：拆单时大单/超大单净额接近零
    if ff.large_pct is not None or ff.super_large_pct is not None:
        big_pct = abs(ff.large_pct or 0) + abs(ff.super_large_pct or 0)
    elif amount and amount > 0:
        big_pct = (abs(ff.super_large_net or 0) + abs(ff.large_net or 0)) / amount * 100
    else:
        return None

    # 拆单吸筹：小单显著流入 + 大单无动作 + 温和上涨（非暴涨）
    if (small_pct >= _SPLIT_SMALL_PCT and big_pct <= _SPLIT_BIG_PCT
            and change_pct is not None and 0 < change_pct < _SPLIT_PRICE_MAX):
        return f"🔍 疑似拆单吸筹(小单流入占{small_pct:.0f}%但大单仅{big_pct:.0f}%，价格温和涨{change_pct:+.1f}%)"
    # 拆单出货：小单显著流出 + 大单无动作 + 温和下跌（非暴跌）
    if (small_pct <= -_SPLIT_SMALL_PCT and big_pct <= _SPLIT_BIG_PCT
            and change_pct is not None and -_SPLIT_PRICE_MAX < change_pct < 0):
        return f"🔍 疑似拆单出货(小单流出占{abs(small_pct):.0f}%但大单仅{big_pct:.0f}%，价格温和跌{change_pct:+.1f}%)"
    return None


def _detect_flow_anomaly(
    inflow_pct: float,
    history: Optional[list[float]],
    window: int,
    z_threshold: float,
    min_days: int,
) -> Optional[str]:
    """纵向资金异动：今日主力净流入占成交额%是否异于该标的自身近 N 日常态。

    用 z-score（(今日 - 均值) / 标准差）比较，只依赖标的自身历史，天然适配盘子大小。
    冷启动（历史不足 min_days）返回 None，由调用方回退到方案 A 的占比阈值。
    """
    if not history or inflow_pct is None:
        return None
    recent = history[-window:]
    if len(recent) < min_days:
        return None
    mean = sum(recent) / len(recent)
    var = sum((x - mean) ** 2 for x in recent) / len(recent)
    std = var ** 0.5
    if std < 1e-6:
        return None  # 历史几乎无波动，无法判断偏离
    z = (inflow_pct - mean) / std
    if z >= z_threshold:
        return (f"📊 资金异动(纵向)：今日主力净流入占成交额{inflow_pct:+.1f}%，"
                f"显著高于自身近{len(recent)}日均值{mean:+.1f}%(z={z:.1f})")
    if z <= -z_threshold:
        return (f"📊 资金异动(纵向)：今日主力净流入占成交额{inflow_pct:+.1f}%，"
                f"显著低于自身近{len(recent)}日均值{mean:+.1f}%(z={z:.1f})")
    return None


def analyze(
    quotes: list[Quote],
    prev_state: dict,
    config: Config,
    tech_summaries: dict[str, TechnicalSummary] | None = None,
    north_data: Optional["NorthFlowData"] = None,
    market_breadth: Optional["MarketBreadth"] = None,
    flow_history: Optional[dict[str, list[float]]] = None,
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
        priority_items: list[str] = []  # 高优先级资金流提醒（转向/背离），展示时排最前

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

        # ---- 成交量分析（量比 + 量价配合） ----
        vr = q.volume_ratio
        if vr is not None and cp is not None:
            # 量比告警
            if vr >= 2.5:
                if cp > 0:
                    items.append(f"📈 大幅放量上涨(量比{vr:.1f}) — 资金积极入场")
                elif cp < -1:
                    items.append(f"📉 大幅放量下跌(量比{vr:.1f}) — 恐慌抛售⚠️")
                else:
                    items.append(f"📊 大幅放量(量比{vr:.1f}) — 多空分歧加大")
                alert_count += 1
            elif vr >= 1.8:
                if cp > 1:
                    items.append(f"📈 放量上涨(量比{vr:.1f}) — 量价配合")
                elif cp < -1:
                    items.append(f"📉 放量下跌(量比{vr:.1f}) — 资金出逃")
                else:
                    items.append(f"📊 放量(量比{vr:.1f})")
            elif vr <= 0.4:
                if cp > 0:
                    items.append(f"📈 缩量上涨(量比{vr:.1f}) — 买盘不强")
                elif cp < 0:
                    items.append(f"📉 缩量下跌(量比{vr:.1f}) — 抛压减弱")
                else:
                    items.append(f"📊 地量(量比{vr:.1f}) — 交投清淡")
            elif vr <= 0.6:
                if cp > 0:
                    items.append(f"📈 偏缩量上涨(量比{vr:.1f})")
                elif cp < 0:
                    items.append(f"📉 偏缩量下跌(量比{vr:.1f})")

        # 换手率告警
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

        # ---- 主力资金异动（增强版：自适应阈值 + 趋势 + 强度分析） ----
        inflow = q.main_net_inflow
        amount = q.amount
        ff = q.fund_flow  # 资金流向明细
        total_net = ff.total_net if ff else None  # 总体净流入（超大+大+中+小）
        if inflow is not None and amount and amount > 0:
            inflow_pct = inflow / amount * 100  # 主力净流入占成交额百分比

            # 自适应阈值：ETF 和指数流动性好，阈值降低；个股阈值较高
            qtype = q.type or ""
            is_etf = "ETF" in qtype
            is_index = "指数" in qtype
            if is_index:
                buy_threshold, sell_threshold, diverge_threshold = 8, 5, 3
            elif is_etf:
                buy_threshold, sell_threshold, diverge_threshold = 10, 7, 4
            else:
                buy_threshold, sell_threshold, diverge_threshold = 15, 10, 5

            # 获取历史流强（从 prev_state 读取上轮数据做趋势对比）
            prev_flow = prev_state.get(q.code, {}).get("main_net_inflow") if isinstance(prev_state.get(q.code, {}), dict) else None
            prev_flow_pct = prev_state.get(q.code, {}).get("flow_pct") if isinstance(prev_state.get(q.code, {}), dict) else None
            flow_trend = ""  # 趋势标注
            flow_intensity = ""  # 强度标注
            if prev_flow_pct is not None and prev_flow is not None:
                # 趋势：连续同向且幅度加大 → 加速；幅度减小 → 衰减
                if inflow > 0 and prev_flow > 0:
                    if inflow_pct > prev_flow_pct * 1.3:
                        flow_trend = "↑加速流入"
                    elif inflow_pct < prev_flow_pct * 0.7:
                        flow_trend = "↘流入放缓"
                    else:
                        flow_trend = "→持续流入"
                elif inflow < 0 and prev_flow < 0:
                    if abs(inflow_pct) > abs(prev_flow_pct) * 1.3:
                        flow_trend = "↓加速流出"
                    elif abs(inflow_pct) < abs(prev_flow_pct) * 0.7:
                        flow_trend = "↗流出放缓"
                    else:
                        flow_trend = "→持续流出"
                elif inflow > 0 and prev_flow < 0:
                    flow_trend = "🔄由出转入"
                elif inflow < 0 and prev_flow > 0:
                    flow_trend = "🔄由入转出"
                # 强度：相对历史均值的偏离
                if prev_flow_pct != 0:
                    intensity_ratio = abs(inflow_pct) / max(abs(prev_flow_pct), 0.1)
                    if intensity_ratio >= 2.0:
                        flow_intensity = "[异常高强度]"
                    elif intensity_ratio >= 1.5:
                        flow_intensity = "[偏强]"

            # ---- 资金流转向提醒（主力，跨扫描符号反转） ----
            # 口径改为「占成交额%」相对阈值：绝对净额(0.1亿)对小盘/大盘标的不公平，
            # 小盘股一笔 0.1 亿就是巨量、大盘股却无感，故用占比过滤假转向。
            # 总资金转向已废弃：主力(超大+大)与散户(小单)天然反向，总资金≈0 是噪音。
            reversal_pct = config.flow_reversal_pct
            if prev_flow is not None and inflow is not None:
                if inflow > 0 and prev_flow < 0 and inflow_pct >= reversal_pct:
                    priority_items.append(f"🔄 主力资金由流出转流入(净{inflow/1e8:.2f}亿,占{inflow_pct:.1f}%)")
                    alert_count += 1
                elif inflow < 0 and prev_flow > 0 and abs(inflow_pct) >= reversal_pct:
                    priority_items.append(f"🔄 主力资金由流入转流出(净{inflow/1e8:.2f}亿,占{abs(inflow_pct):.1f}%)")
                    alert_count += 1

            # 主力大幅买入
            if inflow > 0 and inflow_pct >= buy_threshold:
                context = f"(涨{cp:+.1f}%)" if cp and cp > 0 else (f"(跌{cp:+.1f}%)" if cp and cp < 0 else "(平盘)")
                trend_str = f" {flow_trend}" if flow_trend else ""
                intense_str = f" {flow_intensity}" if flow_intensity else ""
                if ff:
                    struct = ff.flow_structure
                    if ff.is_institution_absorbing:
                        items.append(f"🔵🔥 机构深度吸筹{context}{trend_str}{intense_str}(超大单+{ff.super_large_net/1e8:.2f}亿,中单{ff.medium_net/1e8:+.2f}亿,散户{ff.small_net/1e8:+.2f}亿)")
                    elif ff.is_institution_driven:
                        items.append(f"🔵 机构吸筹{context}{trend_str}{intense_str}(超大单+{ff.super_large_net/1e8:.2f}亿,散户{ff.small_net/1e8:+.2f}亿)")
                    elif ff.is_mid_capital_active:
                        items.append(f"🔵 游资活跃{context}{trend_str}(中单{ff.medium_net/1e8:+.2f}亿,主力{ff.main_net/1e8:+.2f}亿)")
                    else:
                        extra = f",总体{total_net/1e8:+.2f}亿" if total_net is not None else ""
                        items.append(f"🔵 主力买入{context}{trend_str}(净{inflow/1e8:.2f}亿,占{inflow_pct:.0f}%{extra})")
                else:
                    items.append(f"🔵 主力买入{context}{trend_str}(净{inflow/1e8:.2f}亿,占{inflow_pct:.0f}%)")
                alert_count += 1
            # 主力大幅卖出
            elif inflow < 0 and abs(inflow_pct) >= sell_threshold:
                context = f"(跌{cp:+.1f}%)" if cp and cp < -1 else (f"(涨{cp:+.1f}%)" if cp and cp > 1 else "(平盘)")
                trend_str = f" {flow_trend}" if flow_trend else ""
                intense_str = f" {flow_intensity}" if flow_intensity else ""
                if ff:
                    if ff.is_distribution:
                        items.append(f"🔴 机构出货{context}{trend_str}{intense_str}(超大单{ff.super_large_net/1e8:+.2f}亿,散户接盘+{ff.small_net/1e8:.2f}亿)")
                    elif ff.is_retail_driven:
                        items.append(f"🔴 主力出逃{context}{trend_str}(主力{ff.main_net/1e8:+.2f}亿,散户接盘+{ff.small_net/1e8:.2f}亿)")
                    else:
                        extra = f",总体{total_net/1e8:+.2f}亿" if total_net is not None else ""
                        items.append(f"🔴 主力卖出{context}{trend_str}(净{inflow/1e8:.2f}亿,占{abs(inflow_pct):.0f}%{extra})")
                else:
                    items.append(f"🔴 主力卖出{context}{trend_str}(净{inflow/1e8:.2f}亿,占{abs(inflow_pct):.0f}%)")
                alert_count += 1

            # 量价背离：价升但资金流出 / 价跌但资金流入（仅主力口径，占成交额%）
            # 回测结论：价升+资金流出若仅要求「任意净流出」会频繁「喊跌却涨」
            # （日线回测 5 日反向 +1.67%、5 日胜率仅 41.7%），属普通获利了结而非顶部派发。
            # 故收紧：主力净流出/净流入占比需达标才算有效。
            # 总资金口径已废弃：主力与散户反向，总资金≈0 会造出「价跌资金流入+0.00亿」假信号。
            diverge_price = config.flow_diverge_pct
            main_outflow = inflow < 0 and abs(inflow_pct) >= sell_threshold  # 主力净流出占比达标
            main_inflow = inflow > 0 and inflow_pct >= diverge_threshold  # 主力净流入占比达标
            if cp is not None and cp > diverge_price and main_outflow:
                if ff and ff.super_large_net is not None and ff.super_large_net < 0:
                    priority_items.append(f"⚠️ 拉升出货(涨{cp:+.1f}%但超大单净流出{abs(ff.super_large_net)/1e8:.2f}亿)")
                else:
                    priority_items.append(f"⚠️ 拉升出货(涨{cp:+.1f}%但主力净流出{inflow/1e8:.2f}亿,占{abs(inflow_pct):.0f}%)")
                alert_count += 1
            elif cp is not None and cp < -diverge_price and main_inflow:
                if ff and ff.is_institution_driven:
                    priority_items.append(f"💎 打压吸筹(跌{cp:+.1f}%但超大单流入+{ff.super_large_net/1e8:.2f}亿)")
                else:
                    priority_items.append(f"💎 打压吸筹(跌{cp:+.1f}%但主力净流入{inflow/1e8:.2f}亿,占{inflow_pct:.0f}%)")
                alert_count += 1

            # 纵向异动：今日主力净流入强度是否异于自身近N日常态（方案B，z-score）
            hist = flow_history.get(q.code) if flow_history else None
            longitudinal_signal = _detect_flow_anomaly(
                inflow_pct, hist,
                window=config.flow_longitudinal_window,
                z_threshold=config.flow_longitudinal_z,
                min_days=config.flow_longitudinal_min_days,
            )
            if longitudinal_signal:
                items.append(longitudinal_signal)
                alert_count += 1

            # 散户主导上涨（追高风险）
            if ff and ff.is_retail_driven and cp is not None and cp > 3:
                items.append(f"🟡 散户推涨(小单+{ff.small_net/1e8:.2f}亿,主力{ff.main_net/1e8:+.2f}亿) — 注意追高风险")
                alert_count += 1

            # 主力减仓散户接盘（下跌中继）
            if ff and ff.is_distribution and cp is not None and cp < 0:
                items.append(f"🟠 散户接盘(超大单{ff.super_large_net/1e8:+.2f}亿,散户+{ff.small_net/1e8:.2f}亿)")

            # 疑似主力拆单（把小单伪装成散户，隐藏真实意图）
            split_signal = detect_split_order(ff, q.amount or 0, cp)
            if split_signal:
                items.append(split_signal)
                alert_count += 1

        # ---- 拥挤度预警（多指标同时极端 = 反转概率高） ----
        crowd_signals = 0
        if cp is not None:
            if cp > 3:
                crowd_signals += 1  # 大涨
            elif cp < -3:
                crowd_signals += 1  # 大跌
        if q.volume_ratio and q.volume_ratio >= 2.5:
            crowd_signals += 1  # 大幅放量
        if inflow is not None and amount and amount > 0:
            extreme_flow = abs(inflow) / amount * 100
            if extreme_flow >= 25:
                crowd_signals += 2  # 资金极端（权重加倍）
        if q.turnover_rate and q.turnover_rate >= 10:
            crowd_signals += 1  # 超高换手
        if q.avg_price and q.price and q.avg_price > 0:
            if abs(q.price - q.avg_price) / q.avg_price * 100 > 4:
                crowd_signals += 1  # 均价偏离大
        if crowd_signals >= 4:
            direction = "追涨" if (cp and cp > 0) else "杀跌"
            items.append(f"🚨 拥挤度极高({crowd_signals}重信号共振) — 注意{direction}风险")
            alert_count += 1
        elif crowd_signals >= 3:
            items.append(f"⚠️ 拥挤度高({crowd_signals}重信号) — 短线反转概率上升")

        # ---- 分时均价（黄线）信号 ----
        if q.avg_price and q.price and q.avg_price > 0:
            vwap_dev = (q.price - q.avg_price) / q.avg_price * 100
            if vwap_dev > 2.0:
                items.append(f"📊 强势运行(高于均价{vwap_dev:.1f}%，获利盘多)")
                alert_count += 1
            elif vwap_dev < -2.0:
                items.append(f"📉 弱势运行(低于均价{abs(vwap_dev):.1f}%，套牢盘压力)")
                alert_count += 1
            elif vwap_dev > 1.0:
                items.append(f"📊 偏强(高于均价{vwap_dev:.1f}%)")
            elif vwap_dev < -1.0:
                items.append(f"📉 偏弱(低于均价{abs(vwap_dev):.1f}%)")

        # ---- 顶底综合检测 ----
        if tech_summaries and q.code in tech_summaries:
            tech = tech_summaries[q.code]

            # 1. 均线乖离（MA60 极端偏离 = 中期顶底）
            if tech.ma60 and q.price and tech.ma60 > 0:
                ma60_dev = (q.price - tech.ma60) / tech.ma60 * 100
                if ma60_dev > 30:
                    items.append(f"📈 均线乖离+{ma60_dev:.0f}%(距MA60极远，中期顶部风险)")
                    alert_count += 1
                elif ma60_dev < -25:
                    items.append(f"📉 均线乖离{ma60_dev:.0f}%(距MA60极远，中期底部区间)")
                    alert_count += 1

            # 2. 布林带挤压（BB 带宽收窄 = 变盘前兆）
            if tech.bb_width is not None and tech.bb_width < 5.0:
                if tech.bb_signal in ("触及上轨",):
                    items.append(f"⚠️ BB窄幅+触及上轨(带宽{tech.bb_width:.1f}%) — 变盘向下风险")
                elif tech.bb_signal in ("触及下轨",):
                    items.append(f"💡 BB窄幅+触及下轨(带宽{tech.bb_width:.1f}%) — 变盘向上机会")
                else:
                    items.append(f"⏳ BB挤压(带宽{tech.bb_width:.1f}%) — 即将变盘")

            # 3. 价量背离（与 K 线历史对比）
            if cp is not None and q.volume_ratio is not None:
                # 顶部价量背离：价格上涨(>2%)但缩量(量比<0.6)
                if cp > 2 and q.volume_ratio < 0.6:
                    items.append(f"⚠️ 价量顶背离(涨{cp:+.1f}%但缩量{q.volume_ratio:.1f}x) — 上涨乏力")
                    alert_count += 1
                # 底部价量背离：价格下跌(<-2%)但缩量(量比<0.5)
                elif cp < -2 and q.volume_ratio < 0.5:
                    items.append(f"💡 价量底背离(跌{cp:.1f}%但缩量{q.volume_ratio:.1f}x) — 抛压衰竭")
                    alert_count += 1

            # 4. 成交量极值
            if q.volume_ratio is not None:
                if q.volume_ratio >= 4.0:
                    items.append(f"📊 天量(量比{q.volume_ratio:.1f}) — 注意顶部或趋势加速")
                    alert_count += 1
                elif q.volume_ratio <= 0.25:
                    items.append(f"📊 地量(量比{q.volume_ratio:.1f}) — 底部区域或变盘前兆")

        # ---- 均线位置分析 ----
        if tech_summaries and q.code in tech_summaries:
            tech = tech_summaries[q.code]
            ma_items: list[str] = []
            if q.price:
                for ma_name, ma_val, threshold in [
                    ("MA5", tech.ma5, 1.0), ("MA10", tech.ma10, 1.5),
                    ("MA20", tech.ma20, 2.0), ("MA60", tech.ma60, 2.5),
                ]:
                    if ma_val is None or ma_val <= 0:
                        continue
                    dev = (q.price - ma_val) / ma_val * 100
                    if dev > threshold:
                        ma_items.append(f"{ma_name}↑{dev:.1f}%")
                    elif dev < -threshold:
                        ma_items.append(f"{ma_name}↓{abs(dev):.1f}%")
                # 均线排列状态
                if tech.ma_alignment and tech.ma_alignment != "数据不足":
                    align_labels = {
                        "多头排列": "🟢 均线多头排列",
                        "空头排列": "🔴 均线空头排列",
                        "多头回调": "🟡 多头回调(回踩均线)",
                        "空头反弹": "🟡 空头反弹(反压均线)",
                    }
                    label = align_labels.get(tech.ma_alignment, "")
                    if label:
                        ma_items.append(label)
            if ma_items:
                items.append(" | ".join(ma_items))

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
            # 关键位动态行为信号
            if tech.has_resistance_rejection:
                items.append(f"🔴 {tech.resistance_rejection_detail}")
                alert_count += 1
            if tech.has_support_confirmation:
                items.append(f"🟢 {tech.support_confirmation_detail}")
                alert_count += 1
            if tech.has_support_breakdown:
                items.append(f"🚨 {tech.support_breakdown_detail}")
                alert_count += 1
            if tech.has_breakout_retest:
                items.append(f"✅ {tech.breakout_retest_detail}")
                alert_count += 1
            # 其他指标信号
            KEY_LEVEL_FILTERS = ("跳空", "突破", "跌破", "受压回落", "支撑确认", "突破回踩确认", "支撑", "压力")
            for sig in tech.signals:
                # 跳过已在上面处理过的跳空/突破/关键位信号
                if any(kw in sig for kw in KEY_LEVEL_FILTERS):
                    continue
                items.append(f"📐 {sig}")

        # 多信号共振评分 + 市场状态（非指数标的）
        if q.type != "指数" and tech_summaries and q.code in tech_summaries:
            from app.technical import calc_composite_score, detect_market_regime
            tech = tech_summaries[q.code]
            flow_pct_val = None
            if q.main_net_inflow and q.amount and q.amount > 0:
                flow_pct_val = q.main_net_inflow / q.amount * 100
            score_info = calc_composite_score(tech, q.price or 0, flow_pct=flow_pct_val)
            regime = detect_market_regime(tech, q.price or 0, tech.atr)
            final_score = score_info["score"]
            score_info["label"] = ("🟢 强烈看多" if final_score >= 75 else
                                  "🟢 偏多" if final_score >= 60 else
                                  "⚪ 中性" if final_score >= 45 else
                                  "🟡 偏空" if final_score >= 35 else "🔴 强烈看空")
            score_info["regime"] = regime.regime
            score_info["regime_suggestion"] = regime.suggestion
            score_info["bb_squeeze"] = regime.bb_squeeze
            items.insert(0, f"📊 {score_info['label']}(评分{final_score}) [{regime.regime}] — {regime.suggestion}")
        elif q.type != "指数":
            items.insert(0, "📊 数据不足，无法评分")

        if priority_items or items:
            alerts.append(Alert(
                code=q.code, name=q.name,
                messages=priority_items + items,
                priority=bool(priority_items),
            ))

    # 高优先级资金流提醒（转向/背离）排在最前
    alerts.sort(key=lambda a: not a.priority)

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


# ============================================================
# 资金流纵向历史持久化（方案B：每标的每日主力净流入占成交额%）
# ============================================================

# 每个标的最多保留的交易日数
MAX_FLOW_HISTORY_DAYS = 60


def _load_flow_history(today: str) -> dict[str, list[float]]:
    """加载每标的每日「主力净流入占成交额%」历史，返回 code -> 按日期升序的占比列表。

    Args:
        today: 今日日期字符串（YYYY-MM-DD），加载时排除今日，避免盘中本日数值污染基线。
    """
    import json
    from pathlib import Path

    state_dir = Path(__file__).resolve().parent.parent / "state"
    history_file = state_dir / "fund_flow_history.json"
    if not history_file.exists():
        return {}

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        result: dict[str, list[float]] = {}
        for code, days in data.items():
            if not isinstance(days, dict):
                continue
            vals = [float(days[d]) for d in sorted(days)
                    if d != today and isinstance(days[d], (int, float))]
            if vals:
                result[code] = vals
        return result
    except Exception as e:
        from app.utils import log
        log.warning(f"加载资金流历史失败: {e}")
        return {}


def _update_flow_history(today: str, today_flow_pcts: dict[str, float]) -> None:
    """把今日各标的的主力净流入占成交额%写入历史（按日期去重，当日覆盖），滚动保留近N日。

    Args:
        today: 今日日期字符串（YYYY-MM-DD）。
        today_flow_pcts: code -> 今日主力净流入占成交额%（盘中随扫描更新）。
    """
    import json
    from pathlib import Path

    state_dir = Path(__file__).resolve().parent.parent / "state"
    history_file = state_dir / "fund_flow_history.json"

    try:
        data: dict[str, dict[str, float]] = {}
        if history_file.exists():
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        for code, pct in today_flow_pcts.items():
            if pct is None:
                continue
            days = data.setdefault(code, {})
            days[today] = round(float(pct), 2)
            # 只保留最近 N 个交易日，防止文件无限增长
            if len(days) > MAX_FLOW_HISTORY_DAYS:
                keep = sorted(days)[-MAX_FLOW_HISTORY_DAYS:]
                data[code] = {d: days[d] for d in keep}
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        from app.utils import log
        log.warning(f"保存资金流历史失败: {e}")
