#!/usr/bin/env python3
"""左侧选股批量扫描：输入一个板块/行业（创业板/科创板/沪深300/中证500/中证1000/
半导体/芯片/化工 ...），解析成分股池，逐股按「超跌→缩量→超卖→企稳→未破长期趋势」
检测打分，输出潜在的左侧机会候选排序表。

左侧 = 底部左侧买入（赌反转/埋伏），标的仍在下跌或磨底、尚未确认反转，靠
「深度超跌 + 抛压衰竭 + 超卖 + 早期企稳」取胜。与 right-side（右侧：突破确认后顺势）
互为镜像。

本脚本只做「初筛 + 打分排序 + 妙想基本面快查」；确认单只买点/止损，再用单股版
analyze_golden_pit.py（白马）或 stock-analysis（通用）细看。

用法:
    py .claude/skills/left-side/scan_left_side.py <板块/行业> [输出数量]

参数:
    板块/行业  板块或行业名（如 创业板 / 科创板 / 沪深300 / 中证500 / 半导体 / 化工）
    输出数量    输出候选数量上限（可选，默认 20）

数据源: 成分股池（app.board_pool：指数成分 akshare + 东财 clist 板块/行业，核心不依赖
MX_APIKEY）+ 新浪日 K 线（fetch_historical_kline）。妙想（MX_APIKEY）可选，用于 top
候选估值/基本面快查。
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

# 定位项目根目录（skills/left-side 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块的 WARNING 噪音（个别 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.board_pool import resolve_board_pool
from app.helpers import _detect_market
from app.technical import (
    fetch_historical_kline,
    calc_sma,
    calc_rsi,
    calc_macd,
    calc_kdj,
)


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 左侧检测

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


def _score_candidate(code: str, stock: dict, klines) -> dict | None:
    """对单只候选股做左侧机会检测 + 打分（0~100）。太浅(<12%)/刚见顶直接排除。"""
    n = len(klines)
    if n < 60:
        return None
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-60:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # 近期高点（回看 120 根，识别下跌起点）
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    if peak is None or peak <= 0:
        return None
    dd = (peak - price) / peak * 100
    gap = n - 1 - hi

    # ---- 硬门槛 ----
    if dd < 12:              # 回调太浅，不算左侧机会
        return None
    if gap < 3:              # 距高点不足 3 日 = 仍在下跌初期
        return None

    # 均线
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma120 = calc_sma(closes, 120)[-1] if len(closes) >= 120 else None
    ma250 = calc_sma(closes, 250)[-1] if len(closes) >= 250 else None

    # 缩量：回调段(高点之后)均量 vs 高点前 20 日均量
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    vol_ratio = (pit_vol / pre_vol) if (pit_vol and pre_vol) else None

    # 超卖指标
    rsi = calc_rsi(closes)
    try:
        kdj = calc_kdj([k.high for k in klines], [k.low for k in klines], closes)
    except Exception:
        kdj = None
    try:
        macd = calc_macd(closes)
    except Exception:
        macd = None
    pattern = _detect_reversal_pattern(klines)

    above_ma5 = ma5 is not None and price >= ma5
    above_ma10 = ma10 is not None and price >= ma10
    above_ma20 = ma20 is not None and price >= ma20
    above_ma120 = ma120 is None or price >= ma120
    above_ma250 = ma250 is None or price >= ma250

    # ---- 打分 0~100 ----
    score = 0

    # 超跌深度 30（跌得越深左侧空间越大，但 >45% 疑似暴雷会标记）
    if dd >= 40:
        score += 30
    elif dd >= 30:
        score += 26
    elif dd >= 20:
        score += 20
    else:
        score += 12

    # 缩量抛压衰竭 25
    if vol_ratio is not None:
        if vol_ratio < 0.6:
            score += 25
        elif vol_ratio < 0.8:
            score += 18
        elif vol_ratio < 1.0:
            score += 10
        # >= 1.0 放量（疑似出货）不加分

    # 超卖 20
    if rsi is not None:
        if rsi <= 30:
            score += 8
        elif rsi <= 40:
            score += 4
    if kdj is not None and kdj.j is not None:
        if kdj.j <= 0:
            score += 6
        elif kdj.signal in ("超卖", "金叉"):
            score += 4
    if macd is not None:
        if macd.signal == "金叉":
            score += 6
        elif (macd.histogram or 0) > 0:
            score += 4

    # 企稳 15
    if pattern:
        score += 5
    if above_ma5:
        score += 5
    if above_ma10:
        score += 5

    # 未破长期趋势 10
    if above_ma120:
        score += 5
    if above_ma250:
        score += 5

    return {
        "code": code,
        "name": stock["name"],
        "source": stock.get("source", ""),
        "price": price,
        "dd": dd,
        "gap": gap,
        "vol_ratio": vol_ratio,
        "rsi": rsi,
        "j": kdj.j if kdj else None,
        "macd_sig": macd.signal if macd else "",
        "above_ma20": above_ma20,
        "above_ma120": above_ma120,
        "pattern": pattern,
        "overdeep": dd > 45,
        "score": score,
    }


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


# ---------------------------------------------------------------- 妙想基本面/估值

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
    """妙想结构化查询 → {pb_pct, roe, profit_growth}，失败字段为 None。"""
    f = {"pb_pct": None, "roe": None, "profit_growth": None}
    try:
        tables = mx.query_structured(f"{name} {code} 市净率 历史分位")
        f["pb_pct"] = _find_col_value(tables, "市净率", "百分位")
    except Exception:
        pass
    time.sleep(0.4)
    try:
        tables = mx.query_structured(f"{name} {code} 最新财报 净利润 同比增长 ROE")
        f["profit_growth"] = _find_col_value(tables, "净利润", "同比")
        f["roe"] = _find_col_value(tables, "ROE")
    except Exception:
        pass
    return f


def _fund_gate(f: dict) -> tuple[str, str]:
    """确定性门槛：技术面初筛通过后再做基本面否决。"""
    if all(v is None for v in f.values()):
        return "--", "未查到（缺 key/查询失败）"
    if f["profit_growth"] is not None and f["profit_growth"] < 0:
        return "❌ 真跌", f"净利同比 {f['profit_growth']:.1f}%"
    if f["pb_pct"] is not None and f["pb_pct"] > 70:
        return "⚠️ 估值高", f"PB分位 {f['pb_pct']:.1f}%"
    if f["roe"] is not None and f["roe"] < 8:
        return "⚠️ 成色弱", f"ROE {f['roe']:.1f}%"
    if f["pb_pct"] is None:
        return "-- 待查", "估值分位缺失，需人工确认 PB 分位后再定"
    return "✅ 通过", "估值+基本面 双过"


def _fundamental_check(results, top: int) -> None:
    """对 top 候选跑估值分位 + 净利同比 + ROE 确定性门槛。"""
    try:
        from app.config import Config
        from app.utils import load_env
        from app.miaoxiang import MXClient
    except Exception:
        return
    try:
        load_env(_ROOT)
        config = Config(_ROOT / "watchlist_config.json")
        keys = config.mx_apikeys
        if not keys:
            print("\n  （未配置 MX_APIKEY，跳过基本面/估值门槛；可对单只运行 analyze_golden_pit.py 细看）")
            return
        mx = MXClient(keys)
    except Exception:
        return

    n_fund = min(top, len(results), 10)
    print()
    print("=" * 72)
    print(f"基本面/估值确定性门槛（前 {n_fund} 名，妙想）")
    print("=" * 72)
    print(f"  {'代码':<8}{'名称':<10}{'PB分位':>8}{'ROE':>8}{'净利同比':>10}  门槛判定")
    print("  " + "-" * 68)
    for r in results[:n_fund]:
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
    print("  仅 ✅通过 才值得进一步看买点；❌/⚠️/待查 建议排除或人工补查后观望。")


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
    print(f"左侧选股扫描（板块/行业: {query}）")
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
            r = _score_candidate(code, stock, klines)
            if r is not None:
                results.append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results:
        print("\n❌ 当前该板块/行业无符合条件的左侧机会候选（多处于「未明显回调」或「已破位走坏」状态）")
        return 0

    results.sort(key=lambda x: -x["score"])

    print()
    print("=" * 72)
    print(f"左侧机会候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    header = (f"  {'代码':<8}{'名称':<10}{'板块':<12}{'现价':>8}{'回撤%':>7}{'缩量比':>7}"
              f"{'RSI':>6}{'J值':>7}{'MACD':>6}{'站MA20':>7}{'止跌':>5}{'分':>4}  分级")
    print(header)
    print("  " + "-" * 104)
    for r in results[:top]:
        vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
        rsi_txt = f"{r['rsi']:.0f}" if r["rsi"] is not None else "  --"
        j_txt = f"{r['j']:.0f}" if r["j"] is not None else "  --"
        deep_mark = " ⚠️深" if r["overdeep"] else ""
        print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<12}{r['price']:>8.2f}{r['dd']:>7.1f}"
              f"{vol_txt:>7}{rsi_txt:>6}{j_txt:>7}{r['macd_sig']:>6}"
              f"{('✅' if r['above_ma20'] else '❌'):>7}{('✅' if r['pattern'] else '❌'):>5}"
              f"{r['score']:>4}  {_verdict(r['score'])}{deep_mark}")

    print()
    print("  说明:")
    print("    - 板块=成分归属（指数名/行业名）；回撤%=现价距近120日高点跌幅；缩量比=回调段均量/高点前20日均量（<0.6 抛压衰竭）")
    print("    - RSI<30 / J值≤0 / MACD金叉 = 超卖反转信号；站MA20=初步企稳；止跌=长下影/锤子/看涨吞没")
    print("    - ⚠️深 = 回撤>45%，跌得过深，大概率已非「短期错杀」，务必先查基本面是否暴雷")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 左侧是赌反转的埋伏仓，需分批、留补仓空间；确认买点/止损对单只运行 analyze_golden_pit.py 或 stock-analysis")

    _fundamental_check(results, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
