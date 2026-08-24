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
from app.technical import fetch_historical_kline, calc_support_resistance, get_technical_summary, detect_market_regime
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


def _print_daily_trend(code: str, market: str, days: int, q: Quote):
    print()
    print("=" * 72)
    print(f"【3. 近 {days} 日 K 线 + 趋势】")
    print("=" * 72)
    klines = _fetch_daily_klines(code, market, days)
    if not klines:
        print("  ⚠️ 未查到日 K 线数据")
        return
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
    print(f"  日期        开      高      低      收      涨跌幅")
    tail = klines[-min(10, len(klines)):]
    start_idx = len(klines) - len(tail)
    for i, k in enumerate(tail):
        prev = klines[start_idx + i - 1].close if (start_idx + i) > 0 else None
        pct = (k.close - prev) / prev * 100 if (prev and k.close is not None) else None
        print(f"    {k.date}  {_f(k.open)} {_f(k.high)} {_f(k.low)} {_f(k.close)} "
              f"{_f(pct, 2)}%")


def _print_flow_trend(code: str, name: str, mx):
    print()
    print("=" * 72)
    print("【4. 近 5 日资金流趋势（亿元，+净流入 / -净流出）】")
    print("=" * 72)
    if mx is None or not mx.available:
        print("  ⚠️ 未配置 MX_APIKEY，跳过近 5 日资金流趋势（当日资金流见【2】）")
        return
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
        return
    rows.sort(key=lambda x: x[0], reverse=True)
    print("  日期          主力     超大单   大单     中单     小单")
    for d, r in rows[:5]:
        cells = [_fmt_amount(_parse_amount(r.get(k))) for k in
                 ("主力净流入资金", "超大单净流入资金", "大单净流入资金", "中单净流入资金", "小单净流入资金")]
        print(f"  {d}  " + "  ".join(cells))
    tot = sum((_parse_amount(r.get("主力净流入资金")) or 0) for _, r in rows[:5])
    print(f"  → 5 日主力累计净流入: {tot / 1e8:+.2f} 亿")


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

    # ---- 做 T 可行性硬门槛（稳健优先：默认不做，全部满足才给挂单价）----
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

    if reasons:
        print(f"  ❌ 不适合做 T：{'；'.join(reasons)}")
        print(f"  → 不给出做 T 挂单价（稳健优先，宁可错过不可做错）")
        return

    suggested = _compute_suggested_prices(sr, price, q)
    print(f"  ✅ 适合做 T（震荡 + 振幅/区间充足）")
    print(f"  做 T 建议买单: {_f(suggested['buy_price'], 3)}   卖单: {_f(suggested['sell_price'], 3)}")


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
    _print_daily_trend(code, market, days, q)

    # ---- 近 5 日资金流趋势（妙想，可选）----
    mx = None
    if config.mx_apikeys:
        try:
            from app.miaoxiang import MXClient
            mx = MXClient(config.mx_apikeys)
        except Exception:
            mx = None
    _print_flow_trend(code, name, mx)

    # ---- 做 T 测算（5 分钟 K 线）----
    _print_t0_measure(code, market, q)

    return 0


if __name__ == "__main__":
    sys.exit(main())
