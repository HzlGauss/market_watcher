#!/usr/bin/env python3
"""盘中决策数据包：拉取单个标的（个股/ETF）的实时快照、当日资金流、
近 N 日 K 线 + 技术位、近 5 日资金流趋势、5 分钟 K 线支撑压力与做 T 挂单价。

本脚本只负责「取数 + 算指标」，输出一份结构化数据包；「建仓/加仓/减仓/清仓」
的仓位决策与「是否适合做 T + 挂单价」由 AI 依据 SKILL.md 的分析框架在数据包之上生成。

用法:
    py .claude/skills/intraday-signal/analyze_intraday.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股 / 场内 ETF 代码（必填）
    名称   标的名称（可选，提升妙想查询精度）
    天数   拉取的日 K 线根数（可选，默认 60，上限 120；≥60 才能算 MA60 与 MACD）

数据源（按优先级）:
    - 实时快照: 新浪财经（价格/高低开收/均价VWAP/成交额）+ 腾讯（量比/换手率/内外盘/委比）
    - 当日资金流: 东方财富分钟级 fflow（主力/超大/大/中/小 5 档）
    - 日 K 线 / 5 分钟 K 线: 新浪（AKShare 兜底）
    - 近 5 日资金流趋势: 妙想 Miaoxiang（可选，需 MX_APIKEY；无 key 时跳过）

输出: 分 5 段——实时快照 / 当日资金流 / 近N日K线+趋势 / 近5日资金流趋势 / 做T测算。
"""
import re
import sys
from datetime import date
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/intraday-signal 上三级：intraday-signal -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.models import WatchItem, Quote, FundFlowDetail, KlineData
from app.helpers import _detect_market
from app.data_fetcher import fetch_quotes, fetch_fund_flow_detail
from app.technical import (
    fetch_historical_kline,
    calc_support_resistance,
    get_technical_summary,
    detect_market_regime,
    analyze_volume_price,
    is_low_volume,
    is_stagflation,
)
from app.t0_monitor import _compute_suggested_prices

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- 解析工具

def _num(value) -> float | None:
    """从带后缀的字符串（"89.66元"/"-3.716%"/"608万股"）中提取首个数字。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group()) if m else None


def _parse_amount(value) -> float | None:
    """金额字符串 -> 元（健壮处理「万元」「亿元」「万」「亿」及纯数字）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in ("-", "--", "None", "null", "nan"):
        return None
    mult = 1.0
    if "亿" in s:
        mult, s = 1e8, s.replace("亿", "")
    elif "万" in s:
        mult, s = 1e4, s.replace("万", "")
    s = s.replace("元", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _norm_date(s) -> str:
    """日期归一化："2026-08-21(日)" / "2026-08-24 13:07" -> "2026-08-21" """
    m = _DATE_RE.search(str(s or ""))
    return m.group(1) if m else ""


def _fmt_amount(v) -> str:
    """元 -> 亿元字符串（保留符号，2 位小数）。"""
    if v is None:
        return "   N/A"
    return f"{v / 1e8:+.2f}"


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


def _ma(closes: list[float], n: int) -> float | None:
    """简单移动平均（收盘价序列按日期升序）。"""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


# ---------------------------------------------------------------- 取数与计算

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 60
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(5, min(int(a), 120))
        elif a:
            name = a
    return code, name, days


def _fetch_realtime(code: str, name: str) -> Quote | None:
    """实时快照（新浪主源 + 腾讯量比/换手率/内外盘/委比）。"""
    market = _detect_market(code)
    is_etf = code.startswith(("51", "56", "58", "15", "16", "18"))
    item = WatchItem(name=name, code=code, market=market, type="ETF" if is_etf else "个股")
    quotes = fetch_quotes([item])
    return quotes[0] if quotes else None


def _fetch_daily_klines(code: str, market: str, days: int) -> list[KlineData]:
    """近 N 日日 K 线（按日期升序）。"""
    return fetch_historical_kline(code, market, days=days, scale=240)


def _fetch_min_klines(code: str, market: str) -> list[KlineData]:
    """5 分钟 K 线（约 2 个交易日，覆盖 1.7 天）。"""
    return fetch_historical_kline(code, market, days=2, scale=5)


def _print_realtime(q: Quote):
    print("=" * 72)
    print(f"【1. 实时快照】{q.code} {q.name}")
    print("=" * 72)
    print(f"  现价 {_f(q.price, 3)}  涨跌幅 {_f(q.change_pct, 2)}%  振幅 {_f(q.amplitude, 2)}%")
    print(f"  开盘 {_f(q.open, 3)}  最高 {_f(q.high, 3)}  最低 {_f(q.low, 3)}  昨收 {_f(q.pre_close, 3)}")
    print(f"  均价(VWAP) {_f(q.avg_price, 3)}  量比 {_f(q.volume_ratio, 2)}  换手率 {_f(q.turnover_rate, 2)}%")
    amt = q.amount / 1e8 if q.amount else None
    vol = q.volume / 1e6 if q.volume else None  # 股 -> 万手
    print(f"  成交额 {_f(amt, 2)}亿  成交量 {_f(vol, 2)}万手")
    bs = None
    if q.bid_volume and q.ask_volume and q.ask_volume > 0:
        bs = q.bid_volume / q.ask_volume
    print(f"  外盘/内盘 {_f(bs, 2)}（>1 主动买占优）  委比 {_f(q.bid_ask_ratio, 1)}%")
    if q.high and q.low and q.price and q.high > q.low:
        pos = (q.price - q.low) / (q.high - q.low) * 100
        print(f"  日内位置 {_f(pos, 0)}%（现价在今日高低区间的位置，0=最低 100=最高）")


def _print_fund_flow(code: str, market: str, ff: FundFlowDetail | None):
    print()
    print("=" * 72)
    print("【2. 当日资金流（亿元，+净流入 / -净流出）】")
    print("=" * 72)
    if ff is None or not ff.is_valid:
        print("  ⚠️ 未查到当日资金流（非交易时段 / 无数据 / 该标的不支持拆单口径）")
        return
    print(f"  主力 {_fmt_amount(ff.main_net)}  超大单 {_fmt_amount(ff.super_large_net)}  "
          f"大单 {_fmt_amount(ff.large_net)}  中单 {_fmt_amount(ff.medium_net)}  小单 {_fmt_amount(ff.small_net)}")
    print(f"  资金结构: {ff.flow_structure}")
    pcts = []
    for label, p in (("主力", ff.main_pct), ("超大单", ff.super_large_pct),
                     ("大单", ff.large_pct), ("中单", ff.medium_pct), ("小单", ff.small_pct)):
        if p is not None:
            pcts.append(f"{label}{p:+.1f}%")
    if pcts:
        print(f"  净占比: {'  '.join(pcts)}")


def _print_daily_trend(code: str, market: str, days: int, q: Quote) -> list[KlineData] | None:
    print()
    print("=" * 72)
    print(f"【3. 近 {days} 日 K 线 + 趋势】")
    print("=" * 72)
    klines = _fetch_daily_klines(code, market, days)
    if not klines:
        print("  ⚠️ 未查到日 K 线数据")
        return None
    closes = [k.close for k in klines if k.close is not None]
    tech = get_technical_summary(q, klines)
    if closes:
        ma5, ma10, ma20, ma60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
        print(f"  收盘 {_f(closes[-1])}  |  MA5 {_f(ma5)}  MA10 {_f(ma10)}  MA20 {_f(ma20)}  MA60 {_f(ma60)}")
        w = min(30, len(closes))
        seg = closes[-w:]
        rng = (seg[-1] - seg[0]) / seg[0] * 100 if seg and seg[0] else None
        print(f"  均线排列: {tech.ma_alignment or '数据不足'}")
        print(f"  RSI {_f(tech.rsi, 0)}  |  MACD DIF {_f(tech.macd_dif, 3)} DEA {_f(tech.macd_dea, 3)} 柱 {_f(tech.macd_histogram, 3)}"
              f"  |  KDJ K {_f(tech.kdj_k, 0)} D {_f(tech.kdj_d, 0)} J {_f(tech.kdj_j, 0)}")
        print(f"  近{w}日: 高 {_f(max(seg))} / 低 {_f(min(seg))}  区间涨跌幅 {_f(rng, 2)}%")
        if tech.bb_upper is not None:
            print(f"  布林带: 上 {_f(tech.bb_upper)} 中 {_f(tech.bb_middle)} 下 {_f(tech.bb_lower)}  带宽 {_f(tech.bb_width, 1)}%")
        if tech.has_gap:
            print(f"  跳空: {tech.gap_type} {tech.gap_detail}")
        sr = calc_support_resistance(klines, lookback=20)
        print(f"  关键位: 支撑 {_f(sr.support)} / 压力 {_f(sr.resistance)}")
        regime = detect_market_regime(tech, closes[-1], sr.atr)
        print(f"  市场状态: {regime.regime}（置信度 {regime.confidence}）→ {regime.suggestion}")

        # ---- 量能分析（量价关系 + OBV 能量潮 + 地量/滞涨）----
        print(f"  量价关系: {analyze_volume_price(q, klines)}")
        print(f"  OBV能量潮: {tech.obv_signal or '中性'}")
        if is_low_volume(klines):
            print("  ⚠️ 地量: 成交量创近20日新低（交投清淡，变盘前兆）")
        if is_stagflation(q, klines):
            print("  ⚠️ 滞涨: 涨幅小但明显放量（上方抛压/主力出货嫌疑）")
    print(f"  日期        开      高      低      收      涨跌幅")
    tail = klines[-min(10, len(klines)):]
    start_idx = len(klines) - len(tail)
    for i, k in enumerate(tail):
        prev = klines[start_idx + i - 1].close if (start_idx + i) > 0 else None
        pct = (k.close - prev) / prev * 100 if (prev and k.close is not None) else None
        print(f"    {k.date}  {_f(k.open)} {_f(k.high)} {_f(k.low)} {_f(k.close)} "
              f"{_f(pct, 2)}%")
    return klines


def _print_flow_trend(code: str, name: str, mx):
    print()
    print("=" * 72)
    print("【4. 近 5 日资金流趋势（亿元，+净流入 / -净流出）】")
    print("=" * 72)
    if mx is None or not mx.available:
        print("  ⚠️ 未配置 MX_APIKEY，跳过近 5 日资金流趋势（当日资金流见【2】）")
        return None
    window = max(5, int(5 * 1.5) + 3)
    q = (f"{code} {name} 近{window}日 资金流向 "
         f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入").strip()
    rows, seen = [], set()
    try:
        for t in mx.query_structured(q):
            for r in t.get("rows") or []:
                d = _norm_date(r.get("日期"))
                main = _parse_amount(r.get("主力净流入资金"))
                if not d or main is None or d in seen:
                    continue
                seen.add(d)
                rows.append((d, r))
    except Exception:
        rows = []
    if not rows:
        print("  ⚠️ 未查到近 5 日资金流数据")
        return None
    rows.sort(key=lambda x: x[0], reverse=True)
    print("  日期          主力     超大单   大单     中单     小单")
    for d, r in rows[:5]:
        cells = [_fmt_amount(_parse_amount(r.get(k))) for k in
                 ("主力净流入资金", "超大单净流入资金", "大单净流入资金", "中单净流入资金", "小单净流入资金")]
        print(f"  {d}  " + "  ".join(cells))
    tot = sum((_parse_amount(r.get("主力净流入资金")) or 0) for _, r in rows[:5])
    print(f"  → 5 日主力累计净流入: {tot / 1e8:+.2f} 亿")
    return tot


def _print_t0_measure(code: str, market: str, q: Quote):
    print()
    print("=" * 72)
    print("【5. 做 T 测算（5 分钟 K 线）】")
    print("=" * 72)
    klines = _fetch_min_klines(code, market)
    if not klines:
        print("  ⚠️ 未查到 5 分钟 K 线数据，无法做 T 测算")
        return
    sr = calc_support_resistance(klines, lookback=40)
    tech = get_technical_summary(q, klines)
    price = q.price or q.pre_close or 0
    if price <= 0:
        print("  ⚠️ 无有效现价")
        return
    print(f"  支撑 {_f(sr.support)}  压力 {_f(sr.resistance)}  ATR {_f(sr.atr, 3)}")
    # 日内振幅与位置
    is_etf = q.type and "ETF" in q.type
    min_amp = 0.8 if is_etf else 1.5
    amp = pos = None
    if q.high and q.low and q.pre_close and q.high > q.low:
        amp = (q.high - q.low) / q.pre_close * 100
        pos = (price - q.low) / (q.high - q.low) * 100
    print(f"  日内振幅 {_f(amp, 2)}%  |  日内位置 {_f(pos, 0)}%  |  振幅门槛 {min_amp}%（{'ETF' if is_etf else '个股'}）")

    regime = detect_market_regime(tech, price, sr.atr)
    print(f"  市场状态(5min): {regime.regime}（置信度 {regime.confidence}）")

    # ---- 做 T 可行性硬门槛（稳健优先：默认不建议，全部满足才建议操作）----
    reasons = []
    # 1. 单边行情不做 T（趋势上涨/趋势下跌）
    if regime.regime in ("趋势上涨", "趋势下跌"):
        reasons.append(f"单边行情({regime.regime})，做 T 易踏空/套牢")
    elif regime.regime == "窄幅震荡":
        reasons.append("窄幅震荡（即将变盘），做 T 空间小、风险大")
    # 2. 日内振幅不够，价差覆盖不了成本
    if amp is None or amp < min_amp:
        reasons.append(f"日内振幅不足({_f(amp, 2)}% < {min_amp}%)，无利润空间")
    # 3. 支撑/压力区间过窄（至少 0.8% 覆盖成本 + 留利润）
    if sr.support is None or sr.resistance is None:
        reasons.append("无有效支撑/压力位")
    else:
        width_pct = (sr.resistance - sr.support) / price * 100
        if width_pct < 0.8:
            reasons.append(f"支撑压力区间过窄({_f(width_pct, 2)}% < 0.8%)，无利润空间")

    # 无条件给出参考挂单价（技术位参考；是否建议操作由下方判定决定）
    suggested = _compute_suggested_prices(sr, price, q)
    if reasons:
        print(f"  ❌ 今日不适合 T+0 操作：{'；'.join(reasons)}")
        print(f"  参考挂单价（仅技术位参考，不建议下单）: 买入 {_f(suggested['buy_price'], 3)}   卖出 {_f(suggested['sell_price'], 3)}")
    else:
        print(f"  ✅ 适合做 T（震荡 + 振幅/区间充足）")
        print(f"  做 T 建议买单: {_f(suggested['buy_price'], 3)}   卖单: {_f(suggested['sell_price'], 3)}")


# ---------------------------------------------------------------- 抄底信号

def _ema(values: list[float], n: int) -> list[float]:
    """指数移动平均（首值用序列首值初始化），返回与输入等长的序列。"""
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _macd_hist_series(closes: list[float]) -> list[float]:
    """MACD 柱状图序列（(DIF - DEA) * 2），与 closes 对齐；数据不足返回 []。"""
    if len(closes) < 26 + 9:
        return []
    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    dea = _ema(dif, 9)
    return [2.0 * (d - e) for d, e in zip(dif, dea)]


def _detect_bottom_divergence(closes: list[float]) -> tuple[bool, str]:
    """MACD 底背离：最近一段价格创出新低，但 MACD 柱底部抬升。

    分段对比：后段最近 5 根 vs 前段（约前 25~5 根）。
    """
    if len(closes) < 30:
        return False, "数据不足"
    hist = _macd_hist_series(closes)
    if len(hist) < 30:
        return False, "数据不足"
    price_new_low = min(closes[-5:]) < min(closes[-25:-5])
    hist_risen = min(hist[-5:]) > min(hist[-25:-5])
    if price_new_low and hist_risen:
        return True, "价创新低但 MACD 柱未创新低（底背离）"
    return False, "无底背离"


def _detect_reversal_pattern(klines: list[KlineData]) -> tuple[bool, str]:
    """止跌 K 线形态：长下影/锤子线，或看涨吞没。"""
    if len(klines) < 2:
        return False, "数据不足"
    last, prev = klines[-1], klines[-2]
    if None in (last.open, last.high, last.low, last.close):
        return False, "数据不足"
    o, h, l, c = last.open, last.high, last.low, last.close
    rng = h - l
    if rng <= 0:
        return False, "无"
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    if lower_shadow >= 2 * body and lower_shadow >= 0.4 * rng and upper_shadow <= 0.3 * rng:
        return True, f"长下影/锤子线（下影占区间 {lower_shadow / rng * 100:.0f}%）"
    if None not in (prev.open, prev.close):
        if (prev.close < prev.open and c > o
                and o <= min(prev.open, prev.close) and c >= max(prev.open, prev.close)):
            return True, "看涨吞没（阳线吞没前日阴线）"
    return False, "无"


def _print_bottom_signal(code: str, market: str, days: int, q: Quote,
                         ff: FundFlowDetail | None, flow_sum: float | None,
                         klines: list[KlineData] | None):
    print()
    print("=" * 72)
    print("【6. 抄底信号测算（超跌 + 背离 + 止跌确认）】")
    print("=" * 72)
    if not klines:
        print("  ⚠️ 无日 K 线数据，无法测算抄底信号")
        return
    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < 20:
        print("  ⚠️ 日 K 线不足 20 根，无法测算抄底信号")
        return

    tech = get_technical_summary(q, klines)
    is_etf = code.startswith(("51", "56", "58", "15", "16", "18"))
    deep_drop_threshold = -10.0 if is_etf else -15.0

    pct20 = (closes[-1] - closes[-20]) / closes[-20] * 100
    pct5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else None

    # ---- 超跌类 ----
    oversold = (tech.rsi is not None and tech.rsi < 30) or (tech.kdj_j is not None and tech.kdj_j < 0)
    deep_drop = pct20 <= deep_drop_threshold

    # ---- 确认类 ----
    div, div_detail = _detect_bottom_divergence(closes)
    pattern, pattern_detail = _detect_reversal_pattern(klines)
    intraday_flow_div = (q.change_pct is not None and q.change_pct < 0
                         and ff is not None and ff.is_valid
                         and ff.main_net is not None and ff.main_net > 0)
    flow_div = intraday_flow_div or (pct5 is not None and pct5 < 0
                                     and flow_sum is not None and flow_sum > 0)
    near_support = False
    if tech.support and q.price:
        if tech.atr and tech.atr > 0:
            near_support = (q.price - tech.support) / tech.atr <= 1.5
        else:
            near_support = (q.price - tech.support) / tech.support <= 0.03

    def _chk(v: bool) -> str:
        return "✅" if v else "❌"

    print(f"  近20日跌幅 {_f(pct20, 2)}%  近5日跌幅 {_f(pct5, 2)}%  现价 {_f(q.price, 3)}")
    print(f"  超卖(RSI<30/KDJ J<0): {_chk(oversold)}  RSI {_f(tech.rsi, 0)}  KDJ J {_f(tech.kdj_j, 0)}")
    print(f"  深度回撤(≤{deep_drop_threshold:.0f}%{'ETF' if is_etf else '个股'}): {_chk(deep_drop)}")
    print(f"  底背离(MACD): {_chk(div)}  {div_detail}")
    print(f"  止跌形态: {_chk(pattern)}  {pattern_detail}")
    flow_txt = f"近5日主力 {flow_sum / 1e8:+.2f} 亿" if flow_sum is not None else "无数据"
    print(f"  资金背离吸筹(价跌主力流入): {_chk(flow_div)}  {flow_txt}")
    print(f"  靠近强支撑(支撑 {_f(tech.support, 3)}): {_chk(near_support)}")

    # ---- 聚合判定（稳健优先）----
    has_oversold = oversold or deep_drop
    # 主动反转确认：底背离 / 止跌K线形态 / 资金背离吸筹
    # （靠近强支撑仅作加分，不算独立确认——超跌本就近支撑，避免误判「还在跌」为「可抄底」）
    confirm_count = sum(1 for v in (div, pattern, flow_div) if v)
    if not has_oversold:
        verdict = "❌ 无抄底信号（未超跌/跌得不够，仍在半山腰），不接飞刀"
    elif confirm_count == 0:
        verdict = "⚠️ 超跌但未企稳（无底背离/止跌形态/资金吸筹），左侧观望，等止跌信号"
    else:
        verdict = f"✅ 超跌 + {confirm_count} 项止跌确认，可尝试左侧轻仓抄底（设好止损）"
        if near_support:
            verdict += "，且靠近强支撑，胜率加分"
    print(f"  → 判定: {verdict}")


def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/intraday-signal/analyze_intraday.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)
    market = _detect_market(code)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")

    # ---- 实时快照（必取，无则不往下走）----
    q = _fetch_realtime(code, name)
    if q is None:
        print(f"❌ 未获取到 {code} 实时行情（代码无效 / 非交易时段 / 网络异常）")
        return 1
    _print_realtime(q)

    # ---- 当日资金流（东财分钟级，静默降级）----
    ff = None
    try:
        ff = fetch_fund_flow_detail(code, market)
    except Exception:
        ff = None
    _print_fund_flow(code, market, ff)

    # ---- 近 N 日 K 线 + 趋势 ----
    klines = _print_daily_trend(code, market, days, q)

    # ---- 近 5 日资金流趋势（妙想，可选）----
    mx = None
    if config.mx_apikeys:
        try:
            from app.miaoxiang import MXClient
            mx = MXClient(config.mx_apikeys)
        except Exception:
            mx = None
    flow_sum = _print_flow_trend(code, name, mx)

    # ---- 做 T 测算（5 分钟 K 线）----
    _print_t0_measure(code, market, q)

    # ---- 抄底信号测算（超跌 + 背离 + 止跌确认）----
    _print_bottom_signal(code, market, days, q, ff, flow_sum, klines)

    return 0


if __name__ == "__main__":
    sys.exit(main())
