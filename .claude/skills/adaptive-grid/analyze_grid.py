#!/usr/bin/env python3
"""自适应交易网格数据包：对指定个股/ETF，按现价上下方最近的支撑/压力 + 筹码峰
生成**非等距**网格（买/卖档位 + 来源 + 强度），并判断是否适合做网格。

与 etf-grid（等距网格、只看 ETF）不同，本脚本：
  - 个股/ETF 通用（个股用本地 DuckDB 成交额算筹码分布，ETF 回退新浪成交量近似）
  - 网格线落在「支撑 / 压力 / 筹码峰」的真实价位上，非平均划分
  - 每档标注来源（摆动/枢轴/成交密集区/筹码峰）与强度，供 AI 判断档位取舍

本脚本只负责「取数 + 算指标」，输出结构化数据包；网格如何取舍、是否值得做，
由 AI 依据 SKILL.md 的分析框架在脚本输出之上生成。

数据源：本地 DuckDB（前复权日K + 成交额，主源）→ 新浪（日K兜底，仅个股无 ETF 时）
→ 新浪实时行情（现价）。筹码分布为「成交额 × 时间衰减」成本分布近似，非交易所原始
筹码数据（东财筹码接口已被 IP 封锁，无真实获利盘接口）。

用法:
    py .claude/skills/adaptive-grid/analyze_grid.py <代码> [名称] [天数]

参数:
    代码   6 位代码（个股或场内 ETF，如 600519 / 510300）
    名称   标的名称（可选，提升精度）
    天数   日K根数（可选，默认 120，上限 250；筹码分布建议 ≥ 120）

输出: 分 6 段——基本信息 / 技术位与箱体阶段 / 支撑压力位明细 / 筹码分布 /
      自适应网格 / 是否适合网格。
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

# 定位项目根目录（skills/adaptive-grid 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.helpers import _detect_market
from app.models import WatchItem, KlineData
from app.data_fetcher import fetch_quotes
from app.hithink import thscode
from app.technical import (
    fetch_historical_kline,
    calc_support_resistance,
    detect_box_regime,
)

# 筹码分布：成交额时间衰减半衰期（交易日）。半衰期 40 日 ≈ 日均换手 2% 的衰减节奏。
_CHIP_HALFLIFE = 40
# 网格线合并阈值：过近的支撑/压力/筹码峰合并成一条（0.5×ATR 与 0.6% 现价取大者）
_MIN_GAP_ATR = 0.5
_MIN_GAP_PCT = 0.6


# ---------------------------------------------------------------- 工具函数

def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


def _read_local_bars(code: str, market: str, days: int) -> list[dict] | None:
    """读本地 DuckDB v_daily_qfq（前复权日K + 成交额），不可用返回 None。

    本地库只覆盖 A 股（5573 只，无 ETF）。ETF / 缺库 / 缺该标的时返回 None，
    由调用方回退新浪 K 线（无成交额，用成交量近似筹码分布）。
    """
    try:
        import duckdb  # noqa: F401
    except Exception:
        return None
    db = _ROOT / "data" / "market.duckdb"
    if not db.exists():
        return None
    ts = thscode(code, market)
    try:
        import duckdb as _duck
        con = _duck.connect(str(db), read_only=True)
        rows = con.execute(
            "SELECT date, open, high, low, close, volume, turnover "
            "FROM v_daily_qfq WHERE thscode = ? ORDER BY date DESC LIMIT ?",
            [ts, days],
        ).fetchall()
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    rows = list(reversed(rows))  # 升序
    bars = []
    for _date, o, h, l, c, v, t in rows:
        bars.append({
            "date": str(_date)[:10],
            "open": o, "high": h, "low": l, "close": c,
            "volume": v, "amount": t,
        })
    return bars


def _chip_distribution(bars: list[dict], price: float,
                       num_bins: int = 120, halflife: int = _CHIP_HALFLIFE) -> dict | None:
    """筹码分布（成交额时间衰减成本分布近似）。

    用「成交额 × 时间衰减」作每日筹码量，按三角分布（峰值在收盘价，顶点收在 [low, high]）
    摊到价格区间，累加成成本分布直方图，再算获利盘比例 / 平均成本 / 90%·70%成本区间 / 筹码峰。

    bars: 升序 [{date, high, low, close, volume, amount}]，amount 为成交额（可能 None，用 volume 兜底）。
    """
    n = len(bars)
    if n < 20 or price is None or price <= 0:
        return None

    weights = []
    for b in bars:
        w = b.get("amount")
        if w is None or w <= 0:
            w = b.get("volume")
        weights.append(w if (w is not None and w > 0) else 0.0)
    if sum(weights) <= 0:
        return None

    highs = [b["high"] for b in bars if b.get("high")]
    lows = [b["low"] for b in bars if b.get("low")]
    if not highs or not lows:
        return None
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        return None
    span = pmax - pmin
    pmin -= span * 0.02  # 外扩避免峰贴边
    pmax += span * 0.02

    def bin_center(j: int) -> float:
        return pmin + (j + 0.5) * (pmax - pmin) / num_bins

    hist = [0.0] * num_bins
    for i, b in enumerate(bars):
        mass = weights[i] * (0.5 ** ((n - 1 - i) / halflife))
        if mass <= 0 or b.get("high") is None or b.get("low") is None:
            continue
        lo, hi = b["low"], b["high"]
        mid = b.get("close") or ((lo + hi) / 2)
        mid = min(max(mid, lo), hi)
        lo_idx = int((lo - pmin) / (pmax - pmin) * num_bins)
        hi_idx = int((hi - pmin) / (pmax - pmin) * num_bins)
        lo_idx = max(0, min(lo_idx, num_bins - 1))
        hi_idx = max(0, min(hi_idx, num_bins - 1))
        if hi_idx <= lo_idx:
            hist[lo_idx] += mass
            continue
        raw = []
        for j in range(lo_idx, hi_idx + 1):
            center = bin_center(j)
            if center <= mid:
                frac = (center - lo) / (mid - lo) if mid > lo else 1.0
            else:
                frac = (hi - center) / (hi - mid) if hi > mid else 1.0
            raw.append(max(0.0, min(1.0, frac)))
        s = sum(raw)
        if s <= 0:
            hist[lo_idx] += mass
            continue
        for k, j in enumerate(range(lo_idx, hi_idx + 1)):
            hist[j] += mass * raw[k] / s

    total = sum(hist)
    if total <= 0:
        return None
    hist = [h / total for h in hist]
    centers = [bin_center(j) for j in range(num_bins)]

    # 获利盘比例（现价下方筹码占比）
    profit_pct = sum(h for j, h in enumerate(hist) if centers[j] < price) * 100

    # 平均成本
    avg_cost = sum(h * c for h, c in zip(hist, centers))

    # 成本分位（90%成本区间 = [P5, P95]，70% = [P15, P85]）
    def percentile(p: float) -> float:
        cum = 0.0
        for j, h in enumerate(hist):
            cum += h
            if cum >= p:
                return centers[j]
        return centers[-1]

    p5, p95 = percentile(0.05), percentile(0.95)
    p15, p85 = percentile(0.15), percentile(0.85)

    # 筹码峰（局部极大，按高度降序取前 4）
    peaks = []
    for j in range(1, num_bins - 1):
        if hist[j] >= hist[j - 1] and hist[j] > hist[j + 1]:
            peaks.append((centers[j], hist[j]))
    peaks.sort(key=lambda x: -x[1])
    top_peaks = peaks[:4]

    return {
        "profit_pct": profit_pct,
        "avg_cost": avg_cost,
        "cost90_low": p5, "cost90_high": p95,
        "cost70_low": p15, "cost70_high": p85,
        "conc90": (p95 - p5) / (p95 + p5) * 100 if (p95 + p5) else None,
        "peaks": top_peaks,
        "amount_based": any(b.get("amount") for b in bars),
    }


# ---------------------------------------------------------------- 网格生成

def _build_grid(sr, chip, price: float, atr: float | None,
                box_lower: float | None, box_upper: float | None) -> list[dict] | None:
    """收集支撑/压力/筹码峰候选 → 带宽过滤 → 合并过近水平 → 非等距网格线。

    返回按价格升序的网格线列表，每项 {price, dir(买/卖), sources, weight}。
    """
    cand: list[tuple[float, str, float]] = []  # (价格, 来源, 基础强度)

    def add(levels, source, base):
        for lv in (levels or []):
            if lv is not None and lv > 0:
                cand.append((float(lv), source, base))

    # 来源按「几何类型」标注（摆动低点/高点/枢轴/成交密集区/筹码峰），
    # 方向（买/卖）由「相对现价位置」决定：摆动高点跌破后是支撑、摆动低点涨破后是压力。
    add(sr.swing_supports, "摆动低点", 3.0)
    add(sr.swing_resistances, "摆动高点", 3.0)
    add(sr.pivot_supports, "枢轴S", 1.0)
    add(sr.pivot_resistances, "枢轴R", 1.0)
    add(sr.volume_clusters, "成交密集区", 2.0)

    # 筹码峰：以最高峰为基准换算强度（最高峰 4.5，与强支撑相当）
    peaks = (chip or {}).get("peaks") or []
    if peaks:
        top_mass = peaks[0][1] if peaks[0][1] > 0 else 1.0
        for pk_price, pk_mass in peaks:
            base = 1.5 + 3.0 * (pk_mass / top_mass)
            cand.append((float(pk_price), "筹码峰", base))

    if not cand:
        return None

    # 带宽：箱体上下沿为主，ATR 外扩 3 档兜底，硬上限 ±20%
    low_bound = box_lower if box_lower else price * 0.9
    high_bound = box_upper if box_upper else price * 1.1
    if atr:
        low_bound = min(low_bound, price - 3 * atr)
        high_bound = max(high_bound, price + 3 * atr)
    low_bound = max(low_bound, price * 0.80)
    high_bound = min(high_bound, price * 1.20)

    kept = [x for x in cand if low_bound <= x[0] <= high_bound]
    if not kept:
        return None

    kept.sort(key=lambda x: x[0])
    min_gap = max(atr * _MIN_GAP_ATR if atr else 0.0, price * _MIN_GAP_PCT / 100)

    # 合并过近水平（按强度加权平均，来源去重拼接，强度累加 = 共振加分）。
    # 关键：现价下方 / 上方分两组各自合并，避免「现价上方的压力」被链式合并拖进下方买入档。
    def _merge_group(group: list) -> list[list]:
        out: list[list] = []
        for p, src, base in group:
            if out and (p - out[-1][0]) < min_gap:
                mp, msrc, mbase = out[-1]
                w0, w1 = mbase, base
                new_src = msrc if src in msrc.split("·") else f"{msrc}·{src}"
                out[-1] = [
                    (mp * w0 + p * w1) / (w0 + w1),
                    new_src,
                    w0 + w1,
                ]
            else:
                out.append([p, src, base])
        return out

    merged = _merge_group([x for x in kept if x[0] < price]) \
        + _merge_group([x for x in kept if x[0] >= price])
    merged.sort(key=lambda x: x[0])

    lines = []
    for p, src, base in merged:
        lines.append({
            "price": round(p, 3),
            "dir": "买" if p < price else "卖",
            "sources": src,
            "weight": round(base, 2),
        })
    return lines


def _strength_label(w: float) -> str:
    if w >= 4.5:
        return "强"
    if w >= 2.5:
        return "中"
    return "弱"


def _score_suitability(box, chip, lines, price: float) -> tuple[str, int, list[str]]:
    """综合趋势阶段 + 箱体位置 + 波动率 + 网格线分离度 + 筹码结构 + 上下档均衡，
    给网格适用性打分。箱体阶段是「闸门」：非明确箱体不给「适合」。"""
    score = 40
    reasons: list[str] = []
    r = box.regime

    # 1) 趋势阶段（闸门，主导）
    if r == "强箱体震荡":
        score += 25
        reasons.append("强箱体震荡，网格最友好")
    elif r == "箱体震荡（偏中性）":
        score += 12
        reasons.append("箱体震荡（偏中性）")
    elif r == "弱箱体/方向未明":
        score += 0
        reasons.append("弱箱体/方向未明，非明确箱体")
    else:
        score -= 30
        reasons.append("趋势市，非等距网格易破网")

    # 2) 箱体位置（对称性：现价越贴边，网格越单向）
    pos = box.pos_pct
    if 25 <= pos <= 75:
        score += 10
        reasons.append(f"现价在箱体中段({pos:.0f}%)，上下皆有空间")
    elif 15 <= pos < 25 or 75 < pos <= 85:
        score += 3
        reasons.append(f"现价偏箱体一侧({pos:.0f}%)")
    else:
        score -= 6
        reasons.append(f"现价贴近箱体边沿({pos:.0f}%)，一侧空间不足")

    # 3) 波动率
    amp = box.avg_amp
    if amp is not None:
        if 1.0 <= amp <= 4.0:
            score += 15
            reasons.append(f"日均振幅{amp:.2f}%适中，够吃价差")
        elif amp > 4.0:
            reasons.append(f"日均振幅{amp:.2f}%过大，破位风险高")
        else:
            reasons.append(f"日均振幅{amp:.2f}%过低，价差不够")

    # 4) 网格线分离度（相邻线间距中位数）
    if lines:
        ps = sorted(g["price"] for g in lines)
        gaps = [(ps[i + 1] - ps[i]) / price * 100 for i in range(len(ps) - 1)]
        med = sorted(gaps)[len(gaps) // 2]
        if 1.0 <= med <= 6.0:
            score += 15
            reasons.append(f"网格线间距中位{med:.2f}%合理（非过密/过疏）")
        elif med < 1.0:
            score += 5
            reasons.append(f"网格线过密（间距中位{med:.2f}%），档位冗余")
        else:
            score += 8
            reasons.append(f"网格线偏疏（间距中位{med:.2f}%），机会少")
    else:
        score -= 10
        reasons.append("无有效网格线")

    # 5) 筹码结构
    if chip:
        pp = chip["profit_pct"]
        if 20 <= pp <= 80:
            score += 10
            reasons.append(f"获利盘{pp:.0f}%，多空筹码均衡")
        elif pp > 80:
            score += 3
            reasons.append(f"获利盘{pp:.0f}%偏高，上方获利了结压力")
        else:
            score += 3
            reasons.append(f"获利盘{pp:.0f}%偏低，上方套牢盘重、反弹遇阻")

    # 6) 上下档均衡
    n_buy = sum(1 for g in lines if g["dir"] == "买")
    n_sell = sum(1 for g in lines if g["dir"] == "卖")
    if n_buy >= 2 and n_sell >= 2:
        score += 10
        reasons.append(f"上下档较均衡（买{n_buy}档/卖{n_sell}档）")
    elif n_buy >= 1 and n_sell >= 1:
        score += 4
        reasons.append(f"有买卖档但不对称（买{n_buy}档/卖{n_sell}档）")
    else:
        score -= 5
        reasons.append("上下档缺失一侧（现价已在支撑/压力之外）")

    score = max(0, min(100, score))
    # 闸门：趋势市最多「不适合」，弱箱体最多「观望」，非明确箱体不给「适合」
    if r == "趋势市（不利网格）":
        score = min(score, 49)
    elif r == "弱箱体/方向未明":
        score = min(score, 59)
    if score >= 70:
        verdict = "✅ 适合"
    elif score >= 50:
        verdict = "⚠️ 观望"
    else:
        verdict = "❌ 不适合"
    return verdict, score, reasons


# ---------------------------------------------------------------- 解析参数

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 120
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(30, min(int(a), 250))
        elif a:
            name = a
    return code, name, days


# ---------------------------------------------------------------- 主流程

def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/adaptive-grid/analyze_grid.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)
    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    market = _detect_market(code)

    # ---- 1. 实时行情（新浪）----
    quote = None
    try:
        items = [WatchItem(code=code, market=market, type="ETF" if code.startswith(("51", "56", "58", "15", "16", "18")) else "stock")]
        quotes = fetch_quotes(items)
        if quotes:
            quote = quotes[0]
    except Exception:
        quote = None

    price = quote.price if quote and quote.price and quote.price > 0 else None
    change_pct = quote.change_pct if quote else None
    amplitude = quote.amplitude if quote else None
    live_name = (quote.name if quote and quote.name else "") or name

    # ---- 2. 日K（本地 DuckDB 优先，含成交额；缺则新浪）----
    bars = _read_local_bars(code, market, days)
    amount_based = bool(bars)
    if not bars:
        klines_raw = fetch_historical_kline(code, market, days=days, scale=240)
        bars = [
            {"date": k.date, "open": k.open, "high": k.high, "low": k.low,
             "close": k.close, "volume": k.volume, "amount": None}
            for k in klines_raw
        ]
    if not bars:
        print("❌ 未能获取 K 线数据（本地 DuckDB + 新浪均失败），请检查代码或稍后重试。")
        return 1

    klines = [
        KlineData(date=b["date"], open=b["open"], high=b["high"],
                  low=b["low"], close=b["close"], volume=b["volume"])
        for b in bars
    ]
    closes = [b["close"] for b in bars if b.get("close") is not None]
    if not closes:
        print("❌ K 线数据为空。")
        return 1

    if price is None:
        price = closes[-1]

    # ---- 指标计算 ----
    ma5, ma10, ma20, ma60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    sr = calc_support_resistance(klines, lookback=60)
    box = detect_box_regime(klines, price)
    chip = _chip_distribution(bars, price)
    atr = sr.atr
    lines = _build_grid(sr, chip, price, atr, box.lower, box.upper)
    verdict, score, reasons = _score_suitability(box, chip, lines, price)

    # ============================================================ 输出
    def f(x, n=2):
        return f"{x:.{n}f}" if x is not None else "--"

    def fp(x, n=2):
        return f"{x:+.{n}f}" if x is not None else "--"

    print("=" * 72)
    print(f"【1. 基本信息】{code} {live_name or ''}")
    print("=" * 72)
    print(f"  最新价 {f(price)}  涨跌幅 {fp(change_pct)}%  振幅 {f(amplitude)}%"
          f"  |  K线 {len(bars)} 根（{'本地库·成交额' if amount_based else '新浪·成交量'}）")

    print()
    print("=" * 72)
    print("【2. 技术位与箱体阶段】")
    print("=" * 72)
    print(f"  收盘 {f(closes[-1])}  |  MA5 {f(ma5)}  MA10 {f(ma10)}  MA20 {f(ma20)}  MA60 {f(ma60)}")
    print(f"  近60日箱体 [{f(box.lower)} ~ {f(box.upper)}]  现价位置 {box.pos_pct:.0f}%（0%下沿/100%上沿）")
    print(f"  ATR(14) {f(atr)}  |  ATR% {f(box.atr_pct)}%  |  近20日日均振幅 {f(box.avg_amp)}%")
    print(f"  箱体阶段: {box.regime}")
    for r in box.reasons:
        print(f"     · {r}")

    print()
    print("=" * 72)
    print("【3. 支撑 / 压力位明细】")
    print("=" * 72)
    below = [s for s in (sr.swing_supports or []) if s < price]
    above = [s for s in (sr.swing_resistances or []) if s > price]
    if below:
        print(f"  摆动支撑(现价下方): {', '.join(f(x,3) for x in below)}")
    if above:
        print(f"  摆动压力(现价上方): {', '.join(f(x,3) for x in above)}")
    if sr.pivot_supports:
        print(f"  枢轴支撑 S1/S2/S3: {', '.join(f(x,3) for x in sr.pivot_supports)}")
    if sr.pivot_resistances:
        print(f"  枢轴压力 R1/R2/R3: {', '.join(f(x,3) for x in sr.pivot_resistances)}")
    if sr.volume_clusters:
        print(f"  成交密集区: {', '.join(f(x,3) for x in sr.volume_clusters)}")

    print()
    print("=" * 72)
    print("【4. 筹码分布（成交额·时间衰减 成本分布近似）】")
    print("=" * 72)
    if chip:
        src = "成交额" if chip["amount_based"] else "成交量"
        print(f"  获利盘比例 {f(chip['profit_pct'],1)}%  |  平均成本 {f(chip['avg_cost'])}"
              f"  （现价{'高于' if price >= chip['avg_cost'] else '低于'}成本 {f(abs(price-chip['avg_cost'])/chip['avg_cost']*100,1)}%）")
        print(f"  90%成本区间 [{f(chip['cost90_low'])} ~ {f(chip['cost90_high'])}]"
              f"  集中度 {f(chip['conc90'],1)}%")
        print(f"  70%成本区间 [{f(chip['cost70_low'])} ~ {f(chip['cost70_high'])}]")
        if chip["peaks"]:
            pk = "  ".join(f"{p:.3f}(峰)" for p, _m in chip["peaks"])
            print(f"  筹码峰: {pk}   （依据: {src}）")
    else:
        print("  ⚠️ 筹码分布无法计算（K线不足 20 根）")

    print()
    print("=" * 72)
    print("【5. 自适应网格（非等距，落在支撑/压力/筹码峰上）】")
    print("=" * 72)
    if lines:
        print(f"  {'档位':<6}{'价格':>10}{'方向':>6}{'距现价%':>9}{'强度':>6}  来源")
        print("  " + "-" * 68)
        for g in lines:
            dist = (g["price"] - price) / price * 100
            print(f"  {'':<6}{g['price']:>10.3f}{g['dir']:>6}{dist:>+8.1f}%"
                  f"{_strength_label(g['weight']):>6}  {g['sources']}")
        print(f"\n  说明: 方向=相对现价（买=下方支撑档 / 卖=上方压力档）；"
              f"摆动高点跌破后是支撑、摆动低点涨破后是压力；"
              f"「·」表示多来源共振（强度累加）。")
        # 等距网格参考（对照）
        if box.grid_step:
            print(f"\n  对照·传统等距网格: 间距 {f(box.grid_step)}（约{box.grid_step_pct}%）"
                  f"  → 约 {box.grid_grids} 档")
    else:
        print("  ⚠️ 现价上下方没有足够的支撑/压力/筹码峰，无法生成非等距网格")

    print()
    print("=" * 72)
    print("【6. 是否适合网格】")
    print("=" * 72)
    print(f"  结论: {verdict}  （评分 {score}/100）")
    for r in reasons:
        print(f"     · {r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
