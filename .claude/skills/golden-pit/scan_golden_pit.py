#!/usr/bin/env python3
"""黄金坑批量扫描：从白马/蓝筹指数成分股（沪深300 ∪ 上证50 ∪ 中证红利）构建候选池，
逐股跑「黄金坑」六层检测打分，按信号强度输出潜在候选排序表。

黄金坑 = 基本面扎实的白马/蓝筹股被短期错杀，价格深度挖坑（回撤 20%~45%）后缩量企稳、
开始修复。候选天然来自「指数成分股」（本身就是白马蓝筹的代理定义），再用技术面判「坑」。

本脚本只做「初筛 + 打分排序 + 基本面快查」；确认单只的估值/买点/止损，再用单股版
analyze_golden_pit.py 细看。

用法:
    py .claude/skills/golden-pit/scan_golden_pit.py [板块/行业|白名单/CSV] [输出数量] [--sync] [--full]

参数:
    板块/行业  可选。默认扫沪深300 ∪ 上证50 ∪ 中证红利成分股；也可指定 白马蓝筹 /
               .csv 白名单路径 / 指数名(沪深300/上证50...) / 行业名(半导体/化工...)
    输出数量   输出的候选数量上限（可选，默认 20）
    --sync    先增量同步本地 marketdb 到最新（可能触发下载，遇 429 限流会失败）
    --full    跳过本地库 streaks 初筛，全池逐股拉新浪 K 线（更慢但召回最全）

数据源: akshare 指数成分股（index_stock_cons，需 pip install akshare）
+ 本地 duckdb 日 K 线（前复权，缺该标的才回退新浪）。妙想（MX_APIKEY）可选，用于 top 候选基本面快查。
"""
import logging
import os
import re
import sys
import time
from pathlib import Path

# 强制 UTF-8 输出 + 禁用 akshare/tqdm 进度条
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/golden-pit 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 加载 .env（HITHINK_FINANCE_API_KEY / MX_APIKEY）：同花顺板块解析兜底依赖此 key，
# 必须在 resolve_board_pool 之前加载，否则「封测/先进封装」等同花顺专属板块无法解析。
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

# 抑制 app 模块的 WARNING 噪音（如个别 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.helpers import _detect_market
from app.data_fetcher import fetch_turnover_map
from app.technical import (
    calc_sma,
    calc_rsi,
    calc_macd,
    calc_kdj,
)
from app.kline_local import fetch_daily_local_first
from app.regime import get_board_regime


# 候选池指数（代码 -> 白马成色分）：上证50(超大盘) > 沪深300(大盘蓝筹) > 中证红利(高股息白马)
INDICES = [
    ("000016", "上证50", 10),
    ("000300", "沪深300", 8),
    ("000922", "中证红利", 6),
]

# 全市场模式关键字（B1：纯技术全扫 + 基本面兜底）。候选池由 tools.marketdb_local
# query_golden_pit_prescreen 的单条 SQL 从全市场 5225 只初筛而来，不再限定指数成分。
_WHOLE_MARKET_KEYS = {"全市场", "全部", "全部a股", "全a", "all"}

# 市值分层 → 白马成色分（回测：深坑抄底仅在沪深300/中证500 有净 edge，中证1000 边缘、
# 微盘无 edge）。黄金坑的灵魂是「白马/蓝筹被错杀」，小盘深跌多半是「跌」不是「坑」。
_SIZE_QUALITY = {"沪深300": 8, "中证500": 6, "中证1000": 3, "微盘": 0}

# 市值缓存（get_size_tier 每次重读 JSON，全市场 1000+ 只逐只读太慢，这里模块级缓存一次）
_size_map_cache: dict | None = None


def _size_tier_of(code: str) -> str:
    global _size_map_cache
    if _size_map_cache is None:
        _size_map_cache = _load_size_map()
    return _size_map_cache.get(str(code)[:6], "微盘")


def _load_size_map() -> dict:
    try:
        from app.regime import _size_tier_map
        return _size_tier_map()
    except Exception:
        return {}


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 候选池

def _fetch_mktcap_map() -> dict[str, float]:
    """从新浪沪深300成分行情拉市值映射 {code: 亿元}（免费、快）。"""
    mkt: dict[str, float] = {}
    try:
        import akshare as ak
        df = ak.index_stock_cons_sina(symbol="000300")
    except Exception:
        return mkt
    if df is None or getattr(df, "empty", True) or "mktcap" not in df.columns:
        return mkt
    for _, r in df.iterrows():
        code = str(r.get("code", "")).zfill(6)
        try:
            v = float(r.get("mktcap"))
        except (TypeError, ValueError):
            continue
        if code and v and v > 0:
            mkt[code] = v / 1e4  # 万元 -> 亿元
    return mkt


def _build_pool() -> dict[str, dict]:
    """拉指数成分股并合并去重，返回 {code: {name, indices, quality, mktcap}}。"""
    pool: dict[str, dict] = {}
    try:
        import akshare as ak
    except ImportError:
        print("❌ 未安装 akshare，无法构建指数成分候选池（pip install akshare）")
        return pool
    for idx_code, idx_name, quality in INDICES:
        try:
            df = ak.index_stock_cons(symbol=idx_code)
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            code = str(r.get("品种代码", "")).zfill(6)
            if not code or code in ("nan", "000000"):
                continue
            cur = pool.setdefault(code, {"name": str(r.get("品种名称", "")),
                                        "indices": set(), "quality": 0})
            cur["indices"].add(idx_name)
            cur["quality"] = max(cur["quality"], quality)
    mkt = _fetch_mktcap_map()
    for code, cur in pool.items():
        cur["mktcap"] = mkt.get(code)
    return pool


def _build_pool_from_query(query: str) -> dict[str, dict]:
    """白名单/CSV/板块/行业 → 候选池（复用 app.board_pool），归一成 _build_pool 同构：
    {code: {name, indices:{source}, quality, mktcap, fund_csv}}。白马/蓝筹层级 quality 由 source 映射。"""
    try:
        from app.board_pool import resolve_board_pool
    except Exception:
        return {}
    pool = resolve_board_pool(query)
    if not pool:
        return {}
    mkt = _fetch_mktcap_map()
    out: dict[str, dict] = {}
    for code, stock in pool.items():
        src = str(stock.get("source", "")).strip()
        tier = _size_tier_of(code)
        if src == "白马":
            quality = 10
        elif src == "蓝筹":
            quality = 8
        else:
            # 板块/行业输入无「白马成色」信息 → 市值分层兜底（回测：小盘深跌不是黄金坑）
            quality = _SIZE_QUALITY.get(tier, 0)
        out[code] = {
            "name": stock.get("name", ""),
            "indices": {src} if src else {"白名单"},
            "quality": quality,
            "mktcap": mkt.get(code),
            "fund_csv": stock.get("fund_csv"),
            "size_tier": tier,
        }
    return out


def _pool_from_prescreen() -> dict[str, dict]:
    """全市场 SQL 初筛短名单 → 候选池（{code: {name, indices, quality, mktcap}}）。

    B1 全市场模式：用 tools.marketdb_local 的单条 SQL 扫全市场，常规坑（回撤20~45% +
    站半年线 + 缩量）、深度坑（回撤>45% + 缩量）、浅回调（回撤5~20% + 均线多头 + 站上MA20）
    合并成候选池；「白马成色」不靠指数成分，先由市值分层给个技术侧代理（沪深300/中证500
    大盘才配当黄金坑，小盘深跌多半是「跌」不是「坑」），再由后续基本面门槛
    （净利同比/ROE/PB分位）兜底判定。深度坑在 _score_candidate 里按 dd>45 路由到
    deep_results，再由基本面门槛拆「深度错杀 vs 暴雷」；浅回调仅在牛市 regime 由
    _score_bull_pullback 取用（熊市/震荡丢弃）。
    """
    try:
        from tools.marketdb_local import (
            query_golden_pit_bull,
            query_golden_pit_deep,
            query_golden_pit_prescreen,
        )
    except Exception:
        return {}
    pool: dict[str, dict] = {}
    for fn in (query_golden_pit_prescreen, query_golden_pit_deep, query_golden_pit_bull):
        try:
            df = fn()
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            code = str(r.get("ticker", "")).zfill(6)
            if not code or code == "000000":
                continue
            tier = _size_tier_of(code)
            pool.setdefault(code, {
                "name": str(r.get("name", "") or ""),
                "indices": {"全市场"},
                "quality": _SIZE_QUALITY.get(tier, 0),
                "mktcap": None,
                "size_tier": tier,
            })
    return pool


# ---------------------------------------------------------------- 黄金坑检测

def _mean_vol(klines) -> float | None:
    vols = [k.volume for k in klines if k.volume is not None]
    return sum(vols) / len(vols) if vols else None


def _detect_reversal_pattern(klines) -> bool:
    """止跌 K 线形态：长下影/锤子线，或看涨吞没。"""
    if len(klines) < 2:
        return False
    last, prev = klines[-1], klines[-2]
    if None in (last.open, last.high, last.low, last.close):
        return False
    o, h, l, c = last.open, last.high, last.low, last.close
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    if lower_shadow >= 2 * body and lower_shadow >= 0.4 * rng and upper_shadow <= 0.3 * rng:
        return True
    if None not in (prev.open, prev.close):
        if (prev.close < prev.open and c > o
                and o <= min(prev.open, prev.close) and c >= max(prev.open, prev.close)):
            return True
    return False


def _score_candidate(code: str, stock: dict, klines, turnover: float | None = None,
                     size_tier: str | None = None, bull_mode: bool = False) -> dict | None:
    """对单只候选股做黄金坑检测 + 打分。太浅(<20%)直接排除；过深(>45%)不排除，
    标记 overdeep=True 单独列出（供人工判断基本面是「深度错杀」还是「暴雷」）。

    常规坑(20~45%)两道门槛（回测结论）：
    - 市值门槛：微盘深坑 20日净edge≈0，深坑抄底仅大盘(沪深300/中证500)有意义，微盘排除。
    - 站半年线软化：跌破半年线不再一票否决（回测破位股反弹不弱），降级保留、靠基本面兜底。

    bull_mode=True（牛市路由）：常规坑(20~45%)直接返回 None（牛市深坑无 edge，超跌是真
    弱），仅保留深度坑(>45%)——回测深度坑全 regime 有效，仍交基本面兜底拆错杀 vs 暴雷。
    """
    n = len(klines)
    if n < 60:
        return None
    size_tier = size_tier or _size_tier_of(code)
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-60:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # 近期高点（回看 120 根，识别挖坑前的「坑沿」）
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    if peak is None or peak <= 0:
        return None
    gap = n - 1 - hi
    dd = (peak - price) / peak * 100

    if dd < 20:              # 太浅不算「坑」
        return None
    if gap < 5:              # 距高点不足 5 日 = 仍在顶部/刚开始跌，坑尚未成形
        return None

    # 均线
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma120 = calc_sma(closes, 120)[-1] if len(closes) >= 120 else None
    ma250 = calc_sma(closes, 250)[-1] if len(closes) >= 250 else None

    # 缩量：坑段(高点之后)均量 vs 高点前 20 日均量
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    vol_ratio = (pit_vol / pre_vol) if (pit_vol and pre_vol) else None

    # 企稳信号
    above_ma20 = ma20 is not None and price >= ma20
    macd_sig, macd_ok = "", False
    try:
        macd = calc_macd(closes)
        macd_sig = macd.signal if macd else ""
        macd_ok = bool(macd) and macd.signal in ("金叉", "多头") and (macd.histogram or 0) >= 0
    except Exception:
        pass
    kdj_ok = False
    try:
        kdj = calc_kdj([k.high for k in klines], [k.low for k in klines], closes)
        kdj_ok = bool(kdj) and kdj.k is not None and kdj.d is not None and kdj.k > kdj.d
    except Exception:
        pass
    pattern = _detect_reversal_pattern(klines)

    overdeep = dd > 45
    above_ma120 = ma120 is None or price >= ma120

    # 牛市路由：常规坑(20~45%)在牛市无 edge（超跌是真弱），仅保留深度坑(>45%)交基本面兜底
    if bull_mode and not overdeep:
        return None

    base = {
        "code": code,
        "name": stock["name"],
        "indices": stock["indices"],
        "source": stock.get("source", ""),
        "fund_csv": stock.get("fund_csv"),
        "mktcap": stock.get("mktcap"),
        "dd": dd,
        "gap": gap,
        "vol_ratio": vol_ratio,
        "turnover": turnover,
        "price": price,
        "peak": peak,
        "ma20": ma20,
        "ma120": ma120,
        "ma250": ma250,
        "above_ma20": above_ma20,
        "above_ma120": above_ma120,
        "macd_sig": macd_sig,
        "kdj_ok": kdj_ok,
        "pattern": pattern,
        "overdeep": overdeep,
        "size_tier": size_tier,
    }

    # ---- 深度超阈值（>45%）：不跑半年线/放量硬门槛，标记后单独列出 ----
    if overdeep:
        base["score"] = 0
        return base

    # ---- 常规硬门槛（仅 20%~45% 区间）----
    if size_tier == "微盘":   # 回测：微盘深坑 20日净edge≈0，深坑抄底仅大盘有意义
        return None
    if vol_ratio is None:
        return None
    if vol_ratio >= 1.2:       # 放量下跌 = 疑似出货/基本面暴雷，非「缩量挖坑」
        return None

    # ---- 打分（0~100）----
    score = stock["quality"]                       # 白马成色 0~10

    # 深坑幅度 25（25%~35% 最理想：风险充分释放、又未破位）
    if 25 <= dd <= 35:
        score += 25
    elif 20 <= dd < 25 or 35 < dd <= 40:
        score += 18
    else:
        score += 10

    # 坑底缩量 25
    if vol_ratio < 0.6:
        score += 25
    elif vol_ratio < 0.8:
        score += 18
    elif vol_ratio < 1.0:
        score += 10

    # 换手率确认（±5）：低换手=地量见底确认真缩量；高换手=抛压未尽，缩量存疑
    if turnover is not None:
        if turnover < 1.0:
            score += 5      # 地量，缩量可信
        elif turnover < 2.0:
            score += 3      # 低换手，抛压衰减
        elif turnover >= 5.0:
            score -= 3      # 高换手，仍有资金活跃，缩量存疑

    # 企稳反转 25（封顶）
    stab = 0
    if above_ma20:
        stab += 8
    if macd_ok:
        stab += 6
    if kdj_ok:
        stab += 6
    if pattern:
        stab += 5
    score += min(stab, 25)

    # 未破长期趋势 15（站上年线更健康）
    if ma250 is not None and price >= ma250:
        score += 15
    else:
        score += 8

    # 站半年线软化（回测：跌破半年线不否决，破位股反弹不弱；站上给小幅加分）
    if ma120 is not None and price >= ma120:
        score += 5

    base["score"] = score
    return base


def _score_bull_pullback(code: str, stock: dict, klines, turnover: float | None = None,
                         size_tier: str | None = None) -> dict | None:
    """牛市黄金坑变体：浅回调上车（右侧）。回测结论：牛市里深坑抄底无 edge（超跌是真弱），
    换成「浅回调 5~20% + 站上 MA20 + 均线多头」才有正 edge；熊市该信号反向（接飞刀）。

    与 _score_candidate（深坑抄底，熊市信号）互补：牛市路由本函数，熊市/震荡路由深坑。
    """
    n = len(klines)
    if n < 60:
        return None
    size_tier = size_tier or _size_tier_of(code)
    if size_tier == "微盘":   # 黄金坑灵魂是白马/蓝筹；微盘浅回调熊市 −3.82% 波动大，排除
        return None
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-60:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # 近期高点（回看 120 根）
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    if peak is None or peak <= 0:
        return None
    gap = n - 1 - hi
    dd = (peak - price) / peak * 100

    if dd < 5:               # 回调太浅，没上车点
        return None
    if dd >= 20:             # 回调过深 → 已落入深坑范畴，牛市里是真弱，不接
        return None
    if gap < 3:              # 距高点不足 3 日，回调尚未成形
        return None

    # 均线多头 + 站上 MA20（右侧核心）
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma120 = calc_sma(closes, 120)[-1] if len(closes) >= 120 else None
    ma250 = calc_sma(closes, 250)[-1] if len(closes) >= 250 else None
    bull = (ma5 is not None and ma10 is not None and ma20 is not None
            and ma5 > ma10 > ma20)
    above_ma20 = ma20 is not None and price >= ma20
    if not bull or not above_ma20:
        return None

    # 缩量回调（坑段均量 vs 高点前 20 日均量）
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    vol_ratio = (pit_vol / pre_vol) if (pit_vol and pre_vol) else None

    macd_sig, macd_ok = "", False
    try:
        macd = calc_macd(closes)
        macd_sig = macd.signal if macd else ""
        macd_ok = bool(macd) and macd.signal in ("金叉", "多头") and (macd.histogram or 0) >= 0
    except Exception:
        pass
    pattern = _detect_reversal_pattern(klines)

    base = {
        "code": code,
        "name": stock["name"],
        "indices": stock["indices"],
        "source": stock.get("source", ""),
        "fund_csv": stock.get("fund_csv"),
        "mktcap": stock.get("mktcap"),
        "dd": dd,
        "gap": gap,
        "vol_ratio": vol_ratio,
        "turnover": turnover,
        "price": price,
        "peak": peak,
        "ma20": ma20,
        "ma120": ma120,
        "ma250": ma250,
        "above_ma20": above_ma20,
        "above_ma120": ma120 is None or price >= ma120,
        "macd_sig": macd_sig,
        "kdj_ok": False,
        "pattern": pattern,
        "overdeep": False,
        "size_tier": size_tier,
    }

    score = stock["quality"]

    # 回调幅度 30（10~15% 是牛市最理想的浅回调：有肉又不走坏）
    if 10 <= dd <= 15:
        score += 30
    elif 8 <= dd < 10 or 15 < dd <= 18:
        score += 22
    else:
        score += 14

    # 缩量回调 20
    if vol_ratio is not None:
        if vol_ratio < 0.7:
            score += 20
        elif vol_ratio < 1.0:
            score += 14
        elif vol_ratio < 1.2:
            score += 6

    # 换手率确认（±5）
    if turnover is not None:
        if turnover < 1.0:
            score += 5
        elif turnover < 2.0:
            score += 3
        elif turnover >= 5.0:
            score -= 3

    # 企稳反转 20（封顶）
    stab = 0
    if macd_ok:
        stab += 10
    if pattern:
        stab += 10
    score += min(stab, 20)

    # 长期趋势 15（站上年线最健康，其次半年线）
    if ma250 is not None and price >= ma250:
        score += 15
    elif ma120 is not None and price >= ma120:
        score += 10
    else:
        score += 5

    base["score"] = score
    return base


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


def _fmt_mktcap(v) -> str:
    """市值（亿元）-> 短字符串，None 显示 --，≥1万亿 转万亿。"""
    if v is None:
        return "  --"
    if v >= 10000:
        return f"{v / 10000:.1f}万亿"
    return f"{v:.0f}亿"


# ---------------------------------------------------------------- 基本面/估值（妙想，确定性门槛）

def _num(value) -> float | None:
    """从带后缀字符串（"88.58%"/"4.281倍"/"-34.11%"）提取首个带符号数字。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group()) if m else None


def _find_col_value(tables, *keywords) -> float | None:
    """在 query_structured 表格里，找列名同时含所有 keywords 的列，返回最新一行的数值。"""
    for t in tables:
        for col in (t.get("columns") or []):
            if all(k in col for k in keywords):
                for r in (t.get("rows") or []):
                    v = _num(r.get(col))
                    if v is not None:
                        return v
    return None


def _fetch_fundamental(mx, name: str, code: str) -> dict:
    """本地（market.duckdb 估值分位 + 三张报表）优先 → 妙想兜底 → {pb_pct, roe, profit_growth}。

    mx 为 None（无 MX_APIKEY）时仅本地字段可用，缺则 None。
    """
    f = {"pb_pct": None, "roe": None, "profit_growth": None}
    try:
        from app import fundamental_local
        v = fundamental_local.valuation_percentile(code)
        if v:
            f["pb_pct"] = v.get("pb_pct")
        fs = fundamental_local.financial_snapshot(code)
        if fs:
            f["roe"] = fs.get("roe")
            f["profit_growth"] = fs.get("profit_growth")
    except Exception:
        pass
    if mx is None:
        return f
    if f["pb_pct"] is None:
        try:
            tables = mx.query_structured(f"{name} {code} 市净率 历史分位")
            f["pb_pct"] = _find_col_value(tables, "市净率", "百分位")
        except Exception:
            pass
    time.sleep(0.4)
    if f["roe"] is None or f["profit_growth"] is None:
        try:
            tables = mx.query_structured(f"{name} {code} 最新财报 净利润 同比增长 ROE")
            if f["profit_growth"] is None:
                f["profit_growth"] = _find_col_value(tables, "净利润", "同比")
            if f["roe"] is None:
                f["roe"] = _find_col_value(tables, "ROE")
        except Exception:
            pass
    return f


def _fund_gate(f: dict) -> tuple[str, str]:
    """确定性门槛：返回 (标签, 说明)。技术面初筛通过后再做基本面否决。并列列出所有命中的问题。"""
    if all(v is None for v in f.values()):
        return "--", "未查到（缺 key/查询失败）"
    issues = []
    if f["profit_growth"] is not None and f["profit_growth"] < 0:
        issues.append(f"净利同比 {f['profit_growth']:.1f}%")
    if f["pb_pct"] is not None and f["pb_pct"] > 70:
        issues.append(f"PB分位 {f['pb_pct']:.1f}%")
    if f["roe"] is not None and f["roe"] < 8:
        issues.append(f"ROE {f['roe']:.1f}%")
    if issues:
        # 优先级取最严重标签：真跌(净利<0) > 估值高(PB>70) > 成色弱(ROE<8)
        if f["profit_growth"] is not None and f["profit_growth"] < 0:
            label = "❌ 真跌"
        elif f["pb_pct"] is not None and f["pb_pct"] > 70:
            label = "⚠️ 估值高"
        else:
            label = "⚠️ 成色弱"
        return label, "；".join(issues)
    if f["pb_pct"] is None:
        return "-- 待查", "估值分位缺失，需人工确认 PB 分位后再定"
    return "✅ 通过", "估值+基本面 双过"


def _init_mx():
    """初始化妙想客户端（无 MX_APIKEY 返回 None）。"""
    try:
        from app.config import Config
        from app.utils import load_env
        from app.miaoxiang import MXClient
    except Exception:
        return None
    try:
        load_env(_ROOT)
        config = Config(_ROOT / "watchlist_config.json")
        keys = config.mx_apikeys
        if keys:
            return MXClient(keys)
    except Exception:
        pass
    return None


def _fundamental_check(results, top: int) -> None:
    """对 top 候选跑估值分位 + 净利同比 + ROE 确定性门槛，否决/降级不达标者。

    本地 market.duckdb（估值分位 + 三张报表）优先，妙想兜底；两者都无时字段为 None，
    由 _fund_gate 判「-- 待查」。不再因缺 MX_APIKEY 跳过（本地数据可独立填字段）。
    """
    mx = _init_mx()

    n_fund = min(top, len(results), 8)
    print()
    print("=" * 72)
    print(f"基本面/估值确定性门槛（前 {n_fund} 名，本地库 + 妙想）")
    print("=" * 72)
    print(f"  {'代码':<8}{'名称':<10}{'PB分位':>8}{'ROE':>8}{'净利同比':>10}  门槛判定")
    print("  " + "-" * 68)
    for r in results[:n_fund]:
        fc = r.get("fund_csv")
        if fc and any(v is not None for v in (fc.get("pb_pct"), fc.get("roe"), fc.get("profit_growth"))):
            # 白名单/CSV 已自带筛选当日的估值+基本面快照，直接复用（免重复拉取本地库/远端）
            f = {"pb_pct": fc.get("pb_pct"), "roe": fc.get("roe"), "profit_growth": fc.get("profit_growth")}
        else:
            f = _fetch_fundamental(mx, r["name"], r["code"])
        r["fund"] = f
        r["fund_verdict"] = _fund_gate(f)
        label, note = r["fund_verdict"]
        pb_txt = f"{f['pb_pct']:.1f}%" if f["pb_pct"] is not None else " --"
        roe_txt = f"{f['roe']:.1f}%" if f["roe"] is not None else " --"
        pg_txt = f"{f['profit_growth']:.1f}%" if f["profit_growth"] is not None else " --"
        print(f"  {r['code']:<8}{r['name']:<10}{pb_txt:>8}{roe_txt:>8}{pg_txt:>10}  {label} {note}")
    print()
    print("  门槛规则: ❌真跌(净利同比<0) / ⚠️估值高(PB分位>70) / ⚠️成色弱(ROE<8) / --待查(PB分位缺失) / ✅通过")
    print("  仅 ✅通过 才对单只跑 analyze_golden_pit.py 细看买点/止损；❌/⚠️/待查 建议排除或人工补查后观望。")


# 基本面兜底判定分级（❌真跌最重 → --待查最轻），用于 B1 暴雷组排序。
_FUND_SEVERITY = {"❌ 真跌": 0, "⚠️ 估值高": 1, "⚠️ 成色弱": 2, "-- 待查": 3}


def _gate_list(rows: list, mx) -> tuple[list, list]:
    """对一组技术候选逐只跑基本面门槛，返回 (passed, failed)。ST/退市直接判 ❌ 不查基本面。"""
    passed: list = []
    failed: list = []
    for r in rows:
        if "ST" in r["name"].upper() or "退" in r["name"]:
            r["fund"] = {"pb_pct": None, "roe": None, "profit_growth": None}
            r["fund_verdict"] = ("❌ 真跌", "ST/退市风险")
            failed.append(r)
            continue
        f = _fetch_fundamental(mx, r["name"], r["code"])
        r["fund"] = f
        r["fund_verdict"] = _fund_gate(f)
        label, _ = r["fund_verdict"]
        (passed if label == "✅ 通过" else failed).append(r)
    return passed, failed


def _fundamental_gate_b1(results: list, deep_results: list, top: int) -> None:
    """B1 全市场基本面兜底：对技术短名单逐只跑估值分位+ROE+净利同比门槛，
    拆「错杀候选(✅通过)」与「疑似暴雷/价值陷阱(❌/⚠️)」两组。

    全市场技术初筛会捞进大量基本面走坏的票，黄金坑的灵魂是「错杀 vs 暴雷」——
    这里用本地 duckdb 估值分位+三张报表（净利同比<0→❌真跌、ROE<8→⚠️成色弱、
    PB分位>70→⚠️估值高）把暴雷的剔掉，剩下的才是「错杀」黄金坑。全短名单(上限 cap)
    都查，本地优先 + 妙想兜底；缺数据标 --待查（不放行 ✅）。

    常规坑(20~45%)按技术分 -score 排序；深度坑(>45%)技术分恒 0，按回撤 -dd 排序
    （最深跌的最可能暴雷，但也可能有深度错杀）。深度坑技术面筛不动，全靠这里的基本面
    门槛把「深度错杀」与「深度暴雷」分开。
    """
    mx = _init_mx()
    cap = 60  # 技术短名单通常几十~几百，上限保护

    checked = results[:cap]
    passed, failed = _gate_list(checked, mx)
    passed.sort(key=lambda x: -x["score"])
    failed.sort(key=lambda x: (_FUND_SEVERITY.get(x["fund_verdict"][0], 9), -x["score"]))

    deep_checked = deep_results[:cap]
    deep_passed, deep_failed = _gate_list(deep_checked, mx)
    deep_passed.sort(key=lambda x: -x["dd"])
    deep_failed.sort(key=lambda x: (_FUND_SEVERITY.get(x["fund_verdict"][0], 9), -x["dd"]))

    def _rows(lst):
        for r in lst:
            f = r["fund"]
            label, note = r["fund_verdict"]
            pb_txt = f"{f['pb_pct']:.1f}%" if f["pb_pct"] is not None else "  --"
            roe_txt = f"{f['roe']:.1f}%" if f["roe"] is not None else "  --"
            pg_txt = f"{f['profit_growth']:.1f}%" if f["profit_growth"] is not None else "  --"
            yield (r["code"], r["name"], r["dd"], r["score"], pb_txt, roe_txt, pg_txt, label, note)

    def _dump(title, lst, n_show):
        print()
        print(f"{title}（共 {len(lst)} 只，显示前 {n_show}）")
        if lst:
            print(f"  {'代码':<8}{'名称':<10}{'回撤%':>7}{'技术分':>7}{'PB分位':>8}{'ROE':>8}{'净利同比':>10}  判定")
            print("  " + "-" * 74)
            for code, name, dd, score, pb_txt, roe_txt, pg_txt, label, note in list(_rows(lst))[:n_show]:
                print(f"  {code:<8}{name:<10}{dd:>7.1f}{score:>7}{pb_txt:>8}{roe_txt:>8}{pg_txt:>10}  {label}"
                      + (f"  {note}" if note and note != "估值+基本面 双过" else ""))
        else:
            print("  （无）")

    print()
    print("=" * 72)
    print(f"基本面兜底（B1 全市场，常规坑 {len(checked)} 只 + 深度坑 {len(deep_checked)} 只：本地库 + 妙想）")
    print("=" * 72)

    _dump("✅ 错杀候选（常规坑基本面通过）—— 这才是「黄金坑」", passed, min(top, len(passed)))
    _dump("❌/⚠️ 疑似暴雷/价值陷阱（常规坑基本面不达标）—— 勿把「跌」当「坑」", failed, min(20, len(failed)))
    _dump("✅ 深度错杀候选（回撤>45% 但基本面通过）—— 深跌里难得的错杀", deep_passed, min(top, len(deep_passed)))
    _dump("❌/⚠️ 深度暴雷（回撤>45% 且基本面不达标）—— 深跌≠深坑", deep_failed, min(20, len(deep_failed)))

    print()
    print("  门槛: ❌真跌(净利同比<0) / ⚠️估值高(PB分位>70) / ⚠️成色弱(ROE<8) / --待查(PB分位缺失) / ✅通过")
    print("  仅 ✅错杀候选 / ✅深度错杀候选 才对单只跑 analyze_golden_pit.py 细看买点/止损；❌/⚠️ 是基本面坏了，抄底是接飞刀。")


# ---------------------------------------------------------------- 本地库 streaks 初筛

def _prescreen(pool: dict, kinds: list, min_pool: int = 40) -> tuple[dict, str]:
    """本地 marketdb streaks 初筛（实现见 tools.marketdb_local.prescreen_pool）。"""
    from tools.marketdb_local import prescreen_pool
    return prescreen_pool(pool, kinds, min_pool=min_pool, label="连续下跌/缩量")


def _sync_local_db() -> None:
    """--sync 时先增量同步本地 marketdb（可能触发下载，遇到 429 限流会失败）。"""
    from tools.marketdb_local import sync_db_echo
    sync_db_echo()


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    query = None
    top = 20
    sync = False
    full = False
    for a in argv[1:]:
        a = a.strip()
        if not a:
            continue
        if a.isdigit():
            top = max(5, min(int(a), 50))
        elif a == "--sync":
            sync = True
        elif a in ("--full", "--no-prescreen"):
            full = True
        elif query is None:
            query = a
    return query, top, sync, full


def _scan_pool(pool: dict) -> tuple[list, list, list]:
    """逐股跑黄金坑检测（本地日K优先），返回 (results, deep_results, bull_results)。

    按该股板块广度 regime 路由（回测结论）：
    - 牛市 → 浅回调上车（_score_bull_pullback，右侧）+ 深度坑仍查（全 regime 有效）
    - 熊市/震荡 → 深坑抄底（_score_candidate，左侧）
    regime 不可用（过期/缺失）回退深坑抄底（默认黄金坑语义）。
    """
    print()
    print(f"  逐股检测中（{len(pool)} 只）...")
    turnover_map = fetch_turnover_map(list(pool.keys()))
    results: list = []
    deep_results: list = []
    bull_results: list = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_daily_local_first(code, market, days=250)
            if not klines or len(klines) < 60:
                continue
            reg = get_board_regime(code)
            regime = reg["regime"] if reg else "震荡"
            if regime == "牛市":
                r = _score_bull_pullback(code, stock, klines, turnover_map.get(code))
                if r is not None:
                    bull_results.append(r)
                # 深度坑(>45%)全 regime 有效，牛市照查（bull_mode 下只留 overdeep）
                r2 = _score_candidate(code, stock, klines, turnover_map.get(code),
                                      bull_mode=True)
                if r2 is not None:
                    deep_results.append(r2)
            else:
                r = _score_candidate(code, stock, klines, turnover_map.get(code))
                if r is not None:
                    (deep_results if r.get("overdeep") else results).append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)
    return results, deep_results, bull_results


def main():
    query, top, sync, full = _parse_args(sys.argv)
    whole_market = bool(query) and query.strip().lower() in _WHOLE_MARKET_KEYS

    scope = query if query else "沪深300 ∪ 上证50 ∪ 中证红利"
    print("=" * 72)
    print(f"黄金坑批量扫描（{scope}）")
    print("=" * 72)

    if sync:
        _sync_local_db()

    if whole_market:
        pool = _pool_from_prescreen()
        if not pool:
            print("❌ 全市场 SQL 初筛无候选或本地库不可用（需 data/market.duckdb + duckdb）")
            return 1
    elif query:
        pool = _build_pool_from_query(query)
        if not pool:
            print(f"❌ 无法解析「{query}」的候选池（白名单/CSV/板块/行业名未匹配或数据源不可达）")
            print("   支持: 全市场 / 白马蓝筹 / .csv 路径 / 指数(沪深300/上证50/中证红利...) / 行业(半导体/化工...)")
            return 1
    else:
        pool = _build_pool()
        if not pool:
            print("❌ 未获取到指数成分候选池（akshare 未装 / 网络异常）")
            return 1
    print(f"  候选池: {len(pool)} 只"
          f"{'（全市场 SQL 初筛）' if whole_market else ('（白名单/板块/行业输入）' if query else '白马/蓝筹成分股')}")

    if not full and not whole_market:
        pool, prescreen_note = _prescreen(pool, ["down", "vol-down"])
        if prescreen_note:
            print(prescreen_note)

    results, deep_results, bull_results = _scan_pool(pool)

    if not results and not deep_results and not bull_results:
        msg = "当前无符合条件的黄金坑候选（成分股多处于「未明显回调」或「已破位走坏」状态）"
        print(f"\n❌ {msg}")
        return 0

    results.sort(key=lambda x: -x["score"])
    bull_results.sort(key=lambda x: -x["score"])
    # 深坑股按「错杀嫌疑」排序：站半年线 → 缩量 → 回撤更深 优先
    deep_results.sort(key=lambda x: (
        not x.get("above_ma120"),
        not (x.get("vol_ratio") is not None and x["vol_ratio"] < 1.0),
        -x["dd"],
    ))

    print()
    print("=" * 72)
    print(f"黄金坑候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    if results:
        header = (f"  {'代码':<8}{'名称':<10}{'板块':<16}{'市值':>8}{'现价':>8}{'回撤%':>7}"
                  f"{'缩量比':>7}{'换手%':>6}{'站MA20':>7}{'MACD':>6}{'KDJ':>6}{'止跌':>5}{'分':>4}  分级")
        print(header)
        print("  " + "-" * 106)
        for r in results[:top]:
            idx_txt = "/".join(sorted(r["indices"]))
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            tr_txt = f"{r['turnover']:.1f}" if r["turnover"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{idx_txt:<16}{_fmt_mktcap(r['mktcap']):>8}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{tr_txt:>6}{('✅' if r['above_ma20'] else '❌'):>7}{r['macd_sig']:>6}"
                  f"{('✅' if r['kdj_ok'] else '❌'):>6}{('✅' if r['pattern'] else '❌'):>5}"
                  f"{r['score']:>4}  {_verdict(r['score'])}")
    else:
        print("  （无 20%~45% 区间的常规黄金坑候选）")

    print()
    print("  说明:")
    print("    - 板块=候选所属指数（上证50/沪深300/中证红利）或白名单层级（白马/蓝筹）；市值=总市值（行情源未覆盖的显示 —）；回撤%=现价距近120日高点跌幅")
    print("    - 缩量比=坑段均量/高点前20日均量（<0.6 健康）；站MA20/MACD金叉/KDJ金叉/止跌形态=企稳信号")
    print("    - 换手%=今日换手率（<1% 地量见底确认真缩量 / ≥5% 高换手则缩量存疑）")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 确认估值/基本面/买点止损请对单只运行: py .claude/skills/golden-pit/analyze_golden_pit.py <代码>")

    # ---- 牛市浅回调上车（右侧，回测：牛市深坑无 edge，换浅回调多头）----
    if bull_results:
        n_bull = min(len(bull_results), 20)
        print()
        print("=" * 72)
        print(f"🟢 牛市浅回调上车（回撤 5~20% + 均线多头，共 {len(bull_results)} 只，显示前 {n_bull}）")
        print("=" * 72)
        print("  当前属板块广度牛市：深坑抄底无 edge（超跌是真弱），换成「浅回调上车」顺势接回调。")
        print()
        bheader = (f"  {'代码':<8}{'名称':<10}{'板块':<16}{'市值':>8}{'现价':>8}{'回撤%':>7}"
                   f"{'缩量比':>7}{'换手%':>6}{'站MA20':>7}{'MACD':>6}{'止跌':>5}{'分':>4}  分级")
        print(bheader)
        print("  " + "-" * 100)
        for r in bull_results[:n_bull]:
            idx_txt = "/".join(sorted(r["indices"]))
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            tr_txt = f"{r['turnover']:.1f}" if r["turnover"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{idx_txt:<16}{_fmt_mktcap(r['mktcap']):>8}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{tr_txt:>6}{('✅' if r['above_ma20'] else '❌'):>7}{r['macd_sig']:>6}"
                  f"{('✅' if r['pattern'] else '❌'):>5}"
                  f"{r['score']:>4}  {_verdict(r['score'])}")
        print()
        print("  说明: 浅回调=现价距近120日高点回撤 5~20%；均线多头(MA5>MA10>MA20)+站上MA20=上升趋势未破坏。")

    # ---- 深度超阈值（>45%）单独列出，供人工判断基本面 ----
    if deep_results:
        n_deep = min(len(deep_results), 20)
        print()
        print("=" * 72)
        print(f"❌ 深度超阈值候选（回撤 >45%，超出黄金坑常规范畴，共 {len(deep_results)} 只，显示前 {n_deep}）")
        print("=" * 72)
        print("  这些股跌得过深，大概率已非「短期错杀」而是趋势性下跌/基本面暴雷；")
        print("  但个别可能属「深度错杀」，需对单只跑 analyze_golden_pit.py 确认基本面是否未恶化。")
        print()
        dheader = (f"  {'代码':<8}{'名称':<10}{'板块':<16}{'市值':>8}{'现价':>8}{'回撤%':>7}"
                   f"{'缩量比':>7}{'换手%':>6}{'站半年线':>8}{'MACD':>6}{'KDJ':>6}{'止跌':>5}")
        print(dheader)
        print("  " + "-" * 106)
        for r in deep_results[:n_deep]:
            idx_txt = "/".join(sorted(r["indices"]))
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            tr_txt = f"{r['turnover']:.1f}" if r["turnover"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{idx_txt:<16}{_fmt_mktcap(r['mktcap']):>8}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{tr_txt:>6}{('✅' if r['above_ma120'] else '❌'):>8}{r['macd_sig']:>6}"
                  f"{('✅' if r['kdj_ok'] else '❌'):>6}{('✅' if r['pattern'] else '❌'):>5}")
        print()
        print("  说明: 站半年线(MA120)=长期趋势未完全破坏；缩量比<1.0=抛压衰竭。「站半年线✅且缩量」者更可能是深度错杀，值得进一步查基本面。")

    if whole_market:
        _fundamental_gate_b1(results, deep_results, top)
    else:
        _fundamental_check(results, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
