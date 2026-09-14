#!/usr/bin/env python3
"""强支撑 + 箱体极低位 批量扫描：输入一个板块/行业（创业板/科创板/沪深300/中证500/
半导体/化工 ...），解析成分股池，逐股只按「箱体极低位 + 到达强支撑」这一个维度
检测打分，输出候选排序表。

与 left-side（超跌→缩量→超卖→企稳→未破趋势 六层）不同，本脚本**只聚焦一个维度**：
现价是否已跌到近期箱体的极低位（箱体位置 ≤25%），且紧贴下方强支撑位（距最近支撑 ≤8%、
下方有多重支撑共振）。不看基本面、不看缩量超卖企稳，是纯粹的「跌到位 + 有强支撑」技术筛选。

用法:
    py .claude/skills/support-box/scan_support_box.py <板块/行业> [输出数量]

参数:
    板块/行业  板块或行业名（如 创业板 / 科创板 / 沪深300 / 中证500 / 半导体 / 化工）
    输出数量    输出候选数量上限（可选，默认 20）

数据源: 成分股池（app.board_pool）+ 新浪日 K 线（fetch_historical_kline）。
核心不依赖 MX_APIKEY，纯技术维度，不做基本面门槛。
"""
import logging
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出 + 禁用 akshare/tqdm 进度条
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/support-box 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 加载 .env（API Key 等）
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

# 抑制 app 模块的 WARNING 噪音（个别 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.board_pool import resolve_board_pool
from app.helpers import _detect_market
from app.technical import (
    fetch_historical_kline,
    detect_box_regime,
    calc_support_resistance,
)


# ---------------------------------------------------------------- 阈值（适中档）

BOX_POS_MAX = 25.0     # 箱体位置 ≤25% 才算「箱体极低位」
DIST_SUPPORT_MAX = 8.0  # 现价距最近支撑位 ≤8% 才算「到达强支撑」
DIST_SUPPORT_MIN = -5.0  # 现价可略跌破支撑，但深破 >5% 视为破位失效


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 检测 + 打分

def _score_support_box(code: str, stock: dict, klines) -> dict | None:
    """对单只候选股做「强支撑 + 箱体极低位」检测打分（0~100）。

    只聚焦一个维度：现价处于近60日箱体极低位，且紧贴下方强支撑位。
    不满足硬门槛（箱体位置过中/过高、距支撑太远、已深破支撑）直接排除。
    """
    n = len(klines)
    if n < 60:
        return None
    price = klines[-1].close
    if price is None or price <= 0:
        return None

    # ---- 箱体（近60日高低 + 现价位置百分位）----
    try:
        box = detect_box_regime(klines, price)
    except Exception:
        return None
    lower, upper, pos_pct = box.lower, box.upper, box.pos_pct
    if lower is None or upper is None or upper <= lower:
        return None

    # ---- 支撑位（多来源，只保留「强支撑」：摆动低点 + 成交密集区）----
    try:
        sr = calc_support_resistance(klines, lookback=20)
    except Exception:
        return None

    # 收集「强支撑」来源：摆动低点（被反复测试的转折点）+ 成交密集区（大量换手的真实筹码区）。
    # 刻意排除两类伪支撑：
    # 1) 箱体下沿——「箱体极低位」本就≈60日低，纳入会与「贴近支撑」高度共线；
    # 2) 枢轴点 S1/S2/S3——由前一日 H/L/C 套公式得出，S1 数学上恒略低于前低，
    #    并非被测试过的强支撑，会让「贴近支撑」对任何低位股都≈0。
    raw = []
    for lst in (sr.swing_supports, sr.volume_clusters):
        if lst:
            raw.extend([s for s in lst if s is not None])

    # 现价下方的支撑位（构成「支撑区」）
    supports_below = sorted({s for s in raw if s is not None and s < price})
    if not supports_below:
        return None  # 现价已跌破所有已知支撑，不算「到达强支撑」

    nearest = supports_below[-1]  # 离现价最近的支撑位
    dist_pct = (price - nearest) / price * 100  # 正=现价在支撑上方，负=已跌破

    # ---- 硬门槛（适中档）----
    if pos_pct > BOX_POS_MAX:
        return None
    if dist_pct > DIST_SUPPORT_MAX:
        return None
    if dist_pct < DIST_SUPPORT_MIN:
        return None

    n_sup = len(supports_below)  # 支撑密集度（多法共振 = 强支撑）

    # ---- 打分 0~100 ----
    score = 0

    # 1) 箱体极低位 50 分：位置越低越贴近箱底，分数越高
    if pos_pct <= 5:
        score += 50
    elif pos_pct <= 10:
        score += 45
    elif pos_pct <= 15:
        score += 40
    elif pos_pct <= 20:
        score += 35
    else:  # 20~25
        score += 30

    # 2) 贴近支撑 30 分：现价越紧贴最近支撑位，分数越高
    if dist_pct <= 2:
        score += 30
    elif dist_pct <= 4:
        score += 25
    elif dist_pct <= 6:
        score += 20
    else:  # 6~8
        score += 15

    # 3) 支撑密集度 20 分：下方多重支撑共振 = 强支撑
    if n_sup >= 3:
        score += 20
    elif n_sup == 2:
        score += 14
    elif n_sup == 1:
        score += 8

    return {
        "code": code,
        "name": stock["name"],
        "source": stock.get("source", ""),
        "price": price,
        "box_lower": lower,
        "box_upper": upper,
        "pos_pct": pos_pct,
        "regime": box.regime,
        "nearest_support": nearest,
        "dist_pct": dist_pct,
        "n_support": n_sup,
        "score": score,
    }


def _verdict(score: int) -> str:
    if score >= 85:
        return "✅ 强"
    if score >= 70:
        return "⚠️ 中"
    return "🔸 弱"


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    if not argv[1:]:
        return None, 20
    query = argv[1].strip()
    top = 20
    for a in argv[2:]:
        if a.strip().isdigit():
            top = max(5, min(int(a.strip()), 50))
    return query, top


def main():
    query, top = _parse_args(sys.argv)
    if not query:
        print(__doc__)
        return 2

    print("=" * 72)
    print(f"强支撑 + 箱体极低位 扫描（板块/行业: {query}）")
    print("=" * 72)

    pool = resolve_board_pool(query)
    if not pool:
        print(f"❌ 无法解析「{query}」的成分股池（板块/行业名未匹配或数据源不可达）")
        print("   支持: 指数(沪深300/中证500/中证1000/上证50) / 板(创业板/科创板) / 行业(半导体/化工/白酒...)")
        return 1
    print(f"  成分股池: {len(pool)} 只（已剔除北交所/B股等无新浪K线的标的）")

    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 1~4 分钟）...")
    results = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_historical_kline(code, market, days=250, scale=240)
            if not klines or len(klines) < 60:
                continue
            r = _score_support_box(code, stock, klines)
            if r is not None:
                results.append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results:
        print("\n❌ 当前该板块/行业无符合「箱体极低位 + 强支撑」的候选（多处于箱体中高位、距支撑较远、或已深破支撑）")
        return 0

    results.sort(key=lambda x: -x["score"])

    print()
    print("=" * 72)
    print(f"强支撑 + 箱体极低位候选（按分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    header = (f"  {'代码':<8}{'名称':<10}{'板块':<10}{'现价':>8}{'箱体位置%':>8}{'最近支撑':>9}"
              f"{'距支撑%':>7}{'支撑数':>6}{'分':>4}  分级")
    print(header)
    print("  " + "-" * 96)
    for r in results[:top]:
        print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<10}{r['price']:>8.2f}{r['pos_pct']:>8.1f}"
              f"{r['nearest_support']:>9.2f}{r['dist_pct']:>7.1f}{r['n_support']:>6}"
              f"{r['score']:>4}  {_verdict(r['score'])}")

    print()
    print("  说明:")
    print("    - 箱体位置% = 现价在近60日高低区间的位置（0%=箱底 / 100%=箱顶），≤25% 才算箱体极低位")
    print("    - 最近支撑 = 现价下方最近的支撑位（摆动低点/成交密集区 中的最近者，排除箱体下沿与枢轴点伪支撑）")
    print("    - 距支撑% = 现价距最近支撑的距离（正=支撑上方 / 负=已跌破），≤8% 才算到达强支撑")
    print("    - 支撑数 = 现价下方支撑位数量（多重共振 = 强支撑）")
    print("    - 分级: ✅强(≥85) / ⚠️中(70~84) / 🔸弱(<70)，仅供初筛")
    print("    - 本脚本只做「强支撑 + 箱体极低位」单一维度筛选，不含基本面/缩量/超卖判断")
    print("    - 确认买点/止损/基本面，对单只运行 stock-analysis 或 left-side 的 analyze_golden_pit.py 细看")
    return 0


if __name__ == "__main__":
    sys.exit(main())
