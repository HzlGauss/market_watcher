#!/usr/bin/env python3
"""龙回头批量扫描：从近 N 个交易日的涨停股池构建候选池，逐股跑龙回头检测打分，
按「板块热度 + 信号强度」输出潜在龙回头候选排序表。

龙回头 = 龙头股强势首波后缩量浅回调、赌二次启动。候选天然来自「近期涨停/连板过的
股票」，故以涨停股池为池子；当前仍在连板的（还在飞）与已破位的（退潮）被硬门槛排除。

本脚本只做「初筛 + 打分排序」；确认单只标的的买卖点/止损，再用单股版
analyze_dragon_pullback.py 细看。

用法:
    py .claude/skills/dragon-pullback/scan_dragon_pullback.py [回溯天数] [输出数量]

参数:
    回溯天数   回看多少个已收盘交易日构建涨停候选池（可选，默认 10）
    输出数量   输出的候选数量上限（可选，默认 20）

数据源: akshare 涨停股池（stock_zt_pool_em，需 pip install akshare）+ 新浪日 K 线
（fetch_historical_kline）。核心不依赖 MX_APIKEY。
"""
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# 强制 UTF-8 输出 + 禁用 akshare/tqdm 进度条
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/dragon-pullback 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块的 WARNING 噪音（如北交所 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.helpers import _detect_market
from app.models import KlineData
from app.technical import fetch_historical_kline, calc_sma


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 候选池

def _build_pool(lookback: int) -> dict[str, dict]:
    """拉近 lookback 个已收盘交易日的涨停股池，合并去重。

    返回 {code: {name, consec(最高连板数), last_date(最近涨停日), industry}}。
    akshare 未装或某日无数据时静默降级。
    """
    pool: dict[str, dict] = {}
    try:
        import akshare as ak
    except ImportError:
        print("❌ 未安装 akshare，无法构建涨停候选池（pip install akshare）")
        return pool
    try:
        cal = ak.tool_trade_date_hist_sina()
        dates = [d for d in cal["trade_date"] if d < date.today()]
        recent = sorted(dates)[-lookback:]
    except Exception:
        return pool

    print(f"  回溯交易日: {len(recent)} 个（{recent[0]} ~ {recent[-1]}）")
    for d in recent:
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=ds)
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            code = str(r.get("代码", "")).zfill(6)
            if not code or code in ("nan", "000000"):
                continue
            consec = int(r.get("连板数", 0) or 0)
            cur = pool.get(code)
            if cur is None:
                pool[code] = {
                    "name": str(r.get("名称", "")),
                    "consec": consec,
                    "last_date": ds,
                    "industry": str(r.get("所属行业", "")),
                }
            else:
                cur["consec"] = max(cur["consec"], consec)
                cur["last_date"] = max(cur["last_date"], ds)
    return pool


def _industry_heat(pool: dict[str, dict]) -> list[tuple[str, int]]:
    """板块热度：近 N 日涨停家数按所属行业聚合，取 TOP10。"""
    cnt = Counter(s["industry"] for s in pool.values() if s.get("industry"))
    return cnt.most_common(10)


# ---------------------------------------------------------------- 龙回头检测（与单股版同口径）

def _mean_vol(klines: list[KlineData]) -> float | None:
    vols = [k.volume for k in klines if k.volume is not None]
    return sum(vols) / len(vols) if vols else None


def _find_first_wave(klines: list[KlineData], recent: int = 20) -> dict | None:
    """识别最近一波拉升段：首波顶点 = 近 recent 日最高 high，起涨点 = 顶点前最低 low。"""
    n = len(klines)
    if n < 10:
        return None
    start = max(0, n - recent)
    highs = [k.high for k in klines]
    lows = [k.low for k in klines]
    hi = max(range(start, n), key=lambda i: highs[i] if highs[i] is not None else -1e9)
    if highs[hi] is None:
        return None
    si = min(range(start, hi + 1), key=lambda i: lows[i] if lows[i] is not None else 1e18)
    if si >= hi or lows[si] is None or lows[si] <= 0:
        return None
    wave_high = highs[hi]
    start_low = lows[si]
    rise_pct = (wave_high - start_low) / start_low * 100
    return {
        "hi": hi, "si": si,
        "start_low": start_low, "wave_high": wave_high,
        "start_date": klines[si].date, "high_date": klines[hi].date,
        "rise_pct": rise_pct, "days": hi - si + 1,
    }


def _detect_reversal_pattern(klines: list[KlineData]) -> bool:
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


def _second_start(klines: list[KlineData], price: float | None, ma5: float | None) -> bool:
    """二次启动确认：放量阳线站上 MA5。"""
    if len(klines) < 2:
        return False
    last, prev = klines[-1], klines[-2]
    if None in (last.open, last.close, last.volume):
        return False
    yang = last.close > last.open
    fangliang = prev.volume is not None and last.volume > prev.volume * 1.2
    above_ma5 = ma5 is not None and price is not None and price >= ma5
    return yang and fangliang and above_ma5


def _score_candidate(code: str, stock: dict, klines: list[KlineData]) -> dict | None:
    """对单只候选股做龙回头检测 + 打分。不满足硬门槛返回 None。"""
    wave = _find_first_wave(klines)
    if wave is None:
        return None
    n = len(klines)
    hi, si = wave["hi"], wave["si"]
    gap = n - 1 - hi
    rise = wave["rise_pct"]
    consec = stock["consec"]
    closes = [k.close for k in klines if k.close is not None]
    if not closes:
        return None
    price = closes[-1]
    start_low = wave["start_low"]
    wave_high = wave["wave_high"]

    # ---- 硬门槛：不满足直接排除 ----
    if gap < 2 or gap > 15:      # 已见顶、正在回调（不是刚涨停/还在连板，也不是久远高点）
        return None
    if rise < 15 and consec < 2:  # 曾是龙头（首波够强 或 曾连板）
        return None
    if price < start_low:         # 跌破起涨平台 = 龙头退潮
        return None

    # ---- 打分（质量维度）----
    score = 0
    # 龙头强度 30
    if consec >= 3 or rise >= 30:
        score += 30
    elif consec >= 2 or rise >= 20:
        score += 24
    else:
        score += 16

    # 缩量回调 25
    wave_vol = _mean_vol(klines[si:hi + 1])
    pull_vol = _mean_vol(klines[hi + 1:]) if hi < n - 1 else None
    vol_ratio = (pull_vol / wave_vol) if (wave_vol and pull_vol) else None
    if vol_ratio is not None:
        if vol_ratio < 0.7:
            score += 25
        elif vol_ratio < 0.85:
            score += 18
        elif vol_ratio < 1.0:
            score += 10

    # 回调位置 25（黄金分割：现价回撤占首波涨幅比例，0.382~0.5 最理想）
    # 注意用现价（收盘价）而非盘中最低，避免盘中插针误判回调深度
    fib = (wave_high - price) / (wave_high - start_low) if price else None
    if fib is not None:
        if 0.382 <= fib < 0.5:
            score += 25
        elif 0.2 <= fib < 0.382 or 0.5 <= fib < 0.618:
            score += 18
        else:
            score += 8   # 几乎没回调（追高）或回调过深（转弱）

    # 企稳/二次启动 20
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    if _detect_reversal_pattern(klines) or _second_start(klines, price, ma5):
        score += 20
    else:
        last = klines[-1]
        if last.close is not None and last.open is not None and last.close > last.open:
            score += 10  # 缩量小阳，弱企稳

    retrace = (wave_high - price) / wave_high * 100 if price else None
    return {
        "score": score,
        "consec": consec,
        "rise": rise,
        "gap": gap,
        "retrace": retrace,
        "vol_ratio": vol_ratio,
        "fib": fib,
        "industry": stock["industry"],
        "name": stock["name"],
        "last_date": stock["last_date"],
    }


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    lookback, top = 10, 20
    nums = [int(a) for a in argv[1:] if a.isdigit()]
    if nums:
        lookback = max(3, min(nums[0], 30))
    if len(nums) >= 2:
        top = max(5, min(nums[1], 50))
    return lookback, top


def main():
    lookback, top = _parse_args(sys.argv)

    print("=" * 72)
    print(f"龙回头批量扫描（回溯 {lookback} 个交易日涨停池）")
    print("=" * 72)

    pool = _build_pool(lookback)
    if not pool:
        print("❌ 未获取到涨停候选池（akshare 未装 / 网络异常 / 近期无涨停数据）")
        return 1
    print(f"  候选池: {len(pool)} 只近期涨停股")

    # ---- 板块热度 ----
    heat = _industry_heat(pool)
    if heat:
        print()
        print(f"  板块热度 TOP10（近 {lookback} 日涨停家数）:")
        for ind, cnt in heat:
            print(f"    {ind:<12} {cnt} 家")

    # ---- 逐股检测打分 ----
    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 1~2 分钟）...")
    results = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_historical_kline(code, market, days=60, scale=240)
            if not klines or len(klines) < 20:
                continue
            r = _score_candidate(code, stock, klines)
            if r is not None:
                r["code"] = code
                results.append(r)
        except Exception:
            pass
        done += 1
        if done % 30 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results:
        print("\n❌ 当前无符合条件的龙回头候选（近期涨停股多处于「还在连板」或「已破位退潮」状态）")
        return 0

    results.sort(key=lambda x: -x["score"])

    # ---- 输出候选表 ----
    print()
    print("=" * 72)
    print(f"龙回头候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    header = f"  {'代码':<8}{'名称':<10}{'行业':<10}{'连板':>4}{'首波%':>7}{'回撤%':>7}{'缩量比':>7}{'黄金位':>7}{'分':>4}  分级"
    print(header)
    print("  " + "-" * 78)
    for r in results[:top]:
        fib_txt = f"{r['fib'] * 100:.0f}%" if r["fib"] is not None else "  --"
        vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
        print(f"  {r['code']:<8}{r['name']:<10}{r['industry']:<10}{r['consec']:>4}"
              f"{r['rise']:>7.1f}{r['retrace']:>7.1f}{vol_txt:>7}{fib_txt:>7}{r['score']:>4}  {_verdict(r['score'])}")

    print()
    print("  说明:")
    print("    - 连板=近N日最高连板数；缩量比=回调均量/首波均量（<0.7 健康）；黄金位=回撤占首波涨幅比例")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 确认买卖点/止损请对单只运行: py .claude/skills/dragon-pullback/analyze_dragon_pullback.py <代码>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
