#!/usr/bin/env python3
"""ETF 网格交易分析数据包：拉取 ETF 实时行情 + 日 K 线，计算技术位、箱体震荡诊断、
波动率诊断、支撑/阻力，并给出网格参数参考值。

本脚本只负责「取数 + 算指标」，输出结构化数据包；是否适合买入、是否箱体震荡、
是否适合自动网格、网格如何设置，由 AI 依据 SKILL.md 的分析框架在脚本输出之上生成。

数据源：新浪财经（实时行情 + 日K线，主源），AKShare（K线兜底），妙想（名称兜底）。

用法:
    py .claude/skills/etf-grid/analyze_etf_grid.py <代码> [名称] [天数]

参数:
    代码   6 位场内 ETF 代码（必填，如 510300 / 159915 / 512880）
    名称   ETF 名称（可选，提升精度）
    天数   拉取的日 K 线根数（可选，默认 60，上限 120；网格判断建议 ≥ 60）

输出: 分 6 段——基本信息 / 技术位 / 箱体与波动率诊断 / 支撑阻力 / 市场状态 / 网格参数参考。
"""
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/etf-grid 上三级：etf-grid -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.helpers import _detect_market
from app.models import WatchItem, KlineData
from app.data_fetcher import fetch_quotes
from app.technical import (
    fetch_historical_kline,
    calc_ma_alignment,
    calc_support_resistance,
    calc_bollinger,
    calc_rsi,
)

# 场内 ETF 代码号段（上海：51/56/58，深圳：15/16/18）
_ETF_PREFIX = ("51", "56", "58", "15", "16", "18")


# ---------------------------------------------------------------- 工具函数

def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


def _std(values: list[float]) -> float:
    """总体标准差"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _slope_pct(series: list[float]) -> float:
    """最小二乘线性回归斜率，返回 %/日（相对首值）"""
    n = len(series)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(series) / n
    num = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0.0
    return slope / series[0] * 100 if series[0] else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 解析参数

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 60
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(30, min(int(a), 120))
        elif a:
            name = a
    return code, name, days


# ---------------------------------------------------------------- 主流程

def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/etf-grid/analyze_etf_grid.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)

    if not code.startswith(_ETF_PREFIX):
        print(f"⚠️ {code} 疑似不是场内 ETF 代码（沪 51/56/58、深 15/16/18 开头），"
              f"网格分析仍按该代码继续，请自行确认。")

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    market = _detect_market(code)

    # ---- 1. 实时行情（新浪）+ 名称 ----
    quote = None
    try:
        items = [WatchItem(code=code, market=market, type="ETF")]
        quotes = fetch_quotes(items)
        if quotes:
            quote = quotes[0]
    except Exception:
        quote = None

    if quote is None:
        # 实时行情完全失败时，用妙想兜底名称（尽力而为）。
        # 注意：这里绝不抓价格——对自由文本做「首个数字」正则极不可靠
        # （如把「中证500」的 500 当成现价），价格统一交给下方 K 线收盘价兜底。
        price, change_pct, amplitude, amount = None, None, None, None
        live_name = name
        try:
            from app.miaoxiang import MXClient
            api_keys = config.mx_apikeys
            if api_keys:
                mx = MXClient(api_keys)
                _nm = (mx.query_as_text(f"{code} {name} 证券简称".strip()) or "").strip()
                live_name = _nm or name
        except Exception:
            pass
    else:
        # quote 存在但 price 为 None（盘前集合竞价/停牌时新浪现价返回 0.000）：
        # 名称照用新浪返回，价格保持 None 走 K 线收盘价兜底，避免拿错误价格算指标。
        live_name = quote.name or name
        price = quote.price
        change_pct = quote.change_pct
        amplitude = quote.amplitude
        amount = quote.amount

    # ---- 2. 日 K 线 ----
    klines = fetch_historical_kline(code, market, days=days, scale=240)
    if not klines:
        print("❌ 未能获取 K 线数据（新浪 + AKShare 均失败），请检查代码或稍后重试。")
        return 1

    closes = [k.close for k in klines if k.close is not None]
    highs = [k.high for k in klines if k.high is not None]
    lows = [k.low for k in klines if k.low is not None]
    if not closes:
        print("❌ K 线数据为空。")
        return 1

    # 若实时价缺失，用最后一根 K 线收盘价兜底
    if price is None:
        price = closes[-1]

    # ---- 指标计算 ----
    ma5, ma10, ma20, ma60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    ma_align = calc_ma_alignment(klines)
    sr = calc_support_resistance(klines, lookback=20)
    bb = calc_bollinger(closes)
    rsi = calc_rsi(closes)

    hi60 = max(highs[-60:]); lo60 = min(lows[-60:])
    hi30 = max(highs[-30:]); lo30 = min(lows[-30:])
    hi20 = max(highs[-20:]); lo20 = min(lows[-20:])
    hi10 = max(highs[-10:]); lo10 = min(lows[-10:])

    ret60 = (closes[-1] / closes[0] - 1) * 100 if closes[0] else None
    ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else None

    atr = sr.atr
    atr_pct = atr / price * 100 if atr and price else None

    # 近20日平均日振幅
    amps = [(k.high - k.low) / k.close * 100 for k in klines[-20:]
            if k.high and k.low and k.close and k.close > 0]
    avg_amp = sum(amps) / len(amps) if amps else None

    # 近20日收盘价波动率（std / mean）
    vol20 = _std(closes[-20:]) / (sum(closes[-20:]) / 20) * 100 if len(closes) >= 20 else None

    # 趋势斜率（近60日，%/日）
    slope60 = _slope_pct(closes[-60:]) if len(closes) >= 60 else _slope_pct(closes)

    # 当前价在近60日箱体中的位置（百分位）
    pos_pct = (price - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else 50.0

    # ---- 箱体震荡评分 ----
    score, reasons = 0, []
    if ma_align.alignment == "缠绕":
        score += 2
        reasons.append("均线缠绕（无明确趋势）")
    elif ma_align.alignment in ("多头排列", "空头排列"):
        score -= 2
        reasons.append(f"均线{ma_align.alignment}（趋势市，不利网格）")
    if ret60 is not None:
        if abs(ret60) < 5:
            score += 2
            reasons.append(f"近60日涨跌幅仅{ret60:+.2f}%（窄幅）")
        elif abs(ret60) < 10:
            score += 1
            reasons.append(f"近60日涨跌幅{ret60:+.2f}%（较窄）")
        elif abs(ret60) >= 20:
            score -= 2
            reasons.append(f"近60日涨跌幅{ret60:+.2f}%（单边趋势）")
    if bb.width is not None:
        if bb.width < 8:
            score += 1
            reasons.append(f"布林带宽{bb.width:.1f}%（收窄震荡）")
    if ret20 is not None and abs(ret20) < 4:
        score += 1
        reasons.append(f"近20日涨跌幅{ret20:+.2f}%（近期走平）")
    if abs(slope60) < 0.03:
        score += 1
        reasons.append(f"趋势斜率{slope60:+.3f}%/日（近水平）")

    if score >= 5:
        regime = "强箱体震荡"
    elif score >= 3:
        regime = "箱体震荡（偏中性）"
    elif score >= 1:
        regime = "弱箱体/方向未明"
    else:
        regime = "趋势市（不利网格）"

    # 网格参数参考值
    grid = None
    if hi60 > lo60 and price and price > 0:
        # 间距：以 ATR% 自适应，夹在 1.2% ~ 3.0%
        step_pct = _clamp((atr_pct or 1.5) * 1.5, 1.2, 3.0)
        step = round(price * step_pct / 100, 3)
        lower = round(lo60, 3)
        upper = round(hi60, 3)
        grids = max(1, round((upper - lower) / step))
        if pos_pct < 33:
            base_pct = 60
        elif pos_pct <= 67:
            base_pct = 50
        else:
            base_pct = 40
        grid = {
            "lower": lower, "upper": upper, "step": step,
            "step_pct": round(step_pct, 2), "grids": grids,
            "base_pct": base_pct, "stop": round(lower - step, 3),
        }

    # ============================================================ 输出
    def f(x, n=2):
        return f"{x:.{n}f}" if x is not None else "--"

    def fp(x, n=2):
        return f"{x:+.{n}f}" if x is not None else "--"

    print("=" * 72)
    print(f"【1. 基本信息】{code} {live_name or ''}")
    print("=" * 72)
    print(f"  最新价 {f(price)}  涨跌幅 {fp(change_pct)}%  振幅 {f(amplitude)}%"
          + (f"  成交额 {amount/1e8:.2f}亿" if amount else ""))

    print()
    print("=" * 72)
    print(f"【2. 技术位（近 {len(klines)} 日 K 线）】")
    print("=" * 72)
    print(f"  收盘 {f(closes[-1])}  |  MA5 {f(ma5)}  MA10 {f(ma10)}  MA20 {f(ma20)}  MA60 {f(ma60)}")
    print(f"  均线排列: {ma_align.alignment}  ({ma_align.detail})")
    print(f"  近60日 高 {f(hi60)} / 低 {f(lo60)}  |  近30日 高 {f(hi30)} / 低 {f(lo30)}")
    print(f"  近20日 高 {f(hi20)} / 低 {f(lo20)}  |  近10日 高 {f(hi10)} / 低 {f(lo10)}")
    print(f"  近60日涨跌幅 {fp(ret60)}%  |  近20日涨跌幅 {fp(ret20)}%")

    print()
    print("=" * 72)
    print("【3. 箱体与波动率诊断】")
    print("=" * 72)
    print(f"  箱体区间: [{f(lo60)} ~ {f(hi60)}]  当前价位置: {pos_pct:.0f}%（0%下沿/100%上沿）")
    print(f"  ATR(14) {f(atr)}  |  ATR% {f(atr_pct)}%  |  近20日平均日振幅 {f(avg_amp)}%")
    print(f"  近20日波动率 {f(vol20)}%  |  布林带宽 {f(bb.width)}%  |  RSI(14) {f(rsi)}")
    print(f"  趋势斜率(近60日) {slope60:+.3f}%/日")
    print(f"  → 箱体震荡诊断: {regime}")
    for r in reasons:
        print(f"     · {r}")

    print()
    print("=" * 72)
    print("【4. 支撑 / 阻力】")
    print("=" * 72)
    print(f"  主支撑 {f(sr.support)}  |  主阻力 {f(sr.resistance)}")
    if sr.swing_supports:
        print(f"  摆动支撑: {', '.join(f(x,3) for x in sr.swing_supports)}")
    if sr.swing_resistances:
        print(f"  摆动阻力: {', '.join(f(x,3) for x in sr.swing_resistances)}")
    if sr.volume_clusters:
        print(f"  成交密集区: {', '.join(f(x,3) for x in sr.volume_clusters)}")

    print()
    print("=" * 72)
    print("【5. 网格参数参考（AI 据此生成最终建议）】")
    print("=" * 72)
    if grid:
        print(f"  参考网格区间: [{f(grid['lower'])} ~ {f(grid['upper'])}]")
        print(f"  参考间距: {f(grid['step'])}（约 {grid['step_pct']}%）  → 约 {grid['grids']} 档")
        print(f"  参考首仓(底仓): {grid['base_pct']}%（现价在箱体{pos_pct:.0f}%位置）")
        print(f"  破网止损参考: 收盘跌破 {f(grid['stop'])}")
    else:
        print("  ⚠️ 数据不足，无法给出网格参数参考")

    print()
    print("=" * 72)
    print("【6. 近期 K 线（近 20 日，供 AI 判断形态）】")
    print("=" * 72)
    print("  日期        开      高      低      收      涨跌幅")
    start = max(0, len(klines) - 20)
    for i in range(start, len(klines)):
        k = klines[i]
        prev_close = klines[i - 1].close if i > 0 else None
        pct = ((k.close - prev_close) / prev_close * 100) if (k.close is not None and prev_close) else None
        print(f"  {k.date}  {f(k.open)} {f(k.high)} {f(k.low)} {f(k.close)}  "
              f"{'+' if pct and pct > 0 else ''}{f(pct)}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
