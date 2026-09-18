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

数据源: 成分股池（app.board_pool：指数成分 akshare + 东财 clist 板块/行业 + 同花顺概念
板块）+ 本地 duckdb 日 K 线（前复权，缺该标的才回退新浪）。top 候选估值/基本面用同花顺（ROE/净利
同比/当前估值，需 HITHINK_FINANCE_API_KEY）+ 妙想 PB 历史分位（可选）。核心不依赖 MX_APIKEY。
"""
import logging
import os
import re
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

# 定位项目根目录（skills/left-side 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块的 WARNING 噪音（个别 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.board_pool import resolve_board_pool
from app.helpers import _detect_market
from app.data_fetcher import fetch_turnover_map
from app.technical import (
    calc_sma,
    calc_rsi,
    calc_macd,
    calc_kdj,
)
from app.kline_local import fetch_daily_local_first


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


def _score_candidate(code: str, stock: dict, klines, turnover: float | None = None) -> dict | None:
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

    # 已反弹到位（超买）= 不再是「超跌埋伏」标的，交还给右侧扫描
    # 左侧要的是「跌得差不多 + 有止跌苗头」，不是已经涨回去的票。
    if (rsi is not None and rsi > 60) or (kdj is not None and kdj.j is not None and kdj.j > 85):
        return None

    above_ma5 = ma5 is not None and price >= ma5
    above_ma10 = ma10 is not None and price >= ma10
    above_ma20 = ma20 is not None and price >= ma20
    above_ma120 = ma120 is None or price >= ma120
    above_ma250 = ma250 is None or price >= ma250

    # ---- 打分 0~100 ----
    score = 0

    # 超跌深度 20（回测：浅跌 12~30% 反弹最佳，深跌≥40% 多为暴雷、fut20 最差）
    if dd >= 45:
        score += 4    # 深度暴跌，暴雷风险
    elif dd >= 40:
        score += 8
    elif dd >= 30:
        score += 12
    elif dd >= 20:
        score += 16
    else:
        score += 20   # 12~20% 温和超跌

    # 缩量抛压衰竭 20（回测：缩量<0.6 略优，信号偏弱，降权）
    if vol_ratio is not None:
        if vol_ratio < 0.6:
            score += 20
        elif vol_ratio < 0.8:
            score += 14
        elif vol_ratio < 1.0:
            score += 8
        # >= 1.0 放量（疑似出货）不加分

    # 换手率确认（±5）：低换手=地量确认真缩量；高换手=抛压未尽/仍有人活跃交易，缩量存疑
    if turnover is not None:
        if turnover < 1.0:
            score += 5      # 地量，缩量可信
        elif turnover < 2.0:
            score += 3      # 低换手，抛压衰减
        elif turnover >= 5.0 and vol_ratio is not None and vol_ratio < 1.0:
            score -= 3      # 高换手却缩量比低 = 仍有资金活跃，缩量存疑

    # 超卖 15（回测：超卖 RSI≤30 略优，信号偏弱，降权）
    if rsi is not None:
        if rsi <= 30:
            score += 5
        elif rsi <= 40:
            score += 2
    if kdj is not None and kdj.j is not None:
        if kdj.j <= 0:
            score += 4
        elif kdj.signal in ("超卖", "金叉"):
            score += 2
    if macd is not None:
        if macd.signal == "金叉":
            score += 4
        elif (macd.histogram or 0) > 0:
            score += 2

    # 企稳 25（回测：止跌形态是最强单信号 fut20 +4.8%/胜率66%，大幅上调）
    if pattern:
        score += 15
    if above_ma5:
        score += 5
    if above_ma10:
        score += 5

    # 未破长期趋势 20（回测：站上 MA120 是最强信号之一，fut20 +2.6% vs 跌破 -2.5%）
    if above_ma120:
        score += 12
    if above_ma250:
        score += 8

    return {
        "code": code,
        "name": stock["name"],
        "source": stock.get("source", ""),
        "price": price,
        "dd": dd,
        "gap": gap,
        "vol_ratio": vol_ratio,
        "turnover": turnover,
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


def _local_fundamental(code: str) -> dict:
    """本地 market.duckdb：估值历史分位 + 三张报表基本面，字段不足为 None。"""
    out = {"pb_pct": None, "pe_pct": None, "roe": None, "profit_growth": None,
           "revenue_growth": None, "pb": None, "pe": None}
    try:
        from app import fundamental_local
        v = fundamental_local.valuation_percentile(code)
        if v:
            out["pb_pct"] = v.get("pb_pct")
            out["pe_pct"] = v.get("pe_pct")
        fs = fundamental_local.financial_snapshot(code)
        if fs:
            out["roe"] = fs.get("roe")
            out["profit_growth"] = fs.get("profit_growth")
            out["revenue_growth"] = fs.get("revenue_growth")
    except Exception:
        pass
    return out


def _hithink_fundamental(code: str, local: dict | None = None) -> dict:
    """同花顺当前估值（PE/PB）+ 财务指标（本地三张报表优先、同花顺兜底），不依赖 MX_APIKEY。"""
    local = local or {}
    f = {"pe": None, "pb": None, "roe": local.get("roe"),
         "profit_growth": local.get("profit_growth"),
         "revenue_growth": local.get("revenue_growth")}
    try:
        from app import hithink
        vals = hithink.fetch_valuations_snapshot([code])
        if vals:
            f["pe"] = vals[0].get("pe_ttm")
            f["pb"] = vals[0].get("pb_mrq")
    except Exception:
        pass
    # 本地缺失的财务指标回退同花顺
    if f["roe"] is None or f["profit_growth"] is None or f["revenue_growth"] is None:
        try:
            from app import hithink
            ind = hithink.fetch_financial_indicators(code)
            if f["roe"] is None:
                f["roe"] = _num(ind.get("profitability", {}).get("index_weighted_avg_roe"))
            if f["profit_growth"] is None:
                f["profit_growth"] = _num(ind.get("growth", {}).get("calculate_parent_holder_net_profit_yoy_growth_ratio"))
            if f["revenue_growth"] is None:
                f["revenue_growth"] = _num(ind.get("growth", {}).get("calculate_operating_income_yoy_growth_ratio"))
        except Exception:
            pass
    return f


def _fetch_fundamental(mx, name: str, code: str) -> dict:
    """本地（market.duckdb 估值分位 + 三张报表，主源）→ 同花顺（当前估值/财报，兜底）→ 妙想（分位，兜底）。

    mx 为 None（无 MX_APIKEY）时 pb_pct 走本地分位、缺历史则显示 --。
    """
    f = {"pb_pct": None, "roe": None, "profit_growth": None, "pb": None, "pe": None}
    local = _local_fundamental(code)
    f.update(_hithink_fundamental(code, local))
    f["pb_pct"] = local["pb_pct"]
    if f["pb_pct"] is None and mx is not None:
        try:
            tables = mx.query_structured(f"{name} {code} 市净率 历史分位")
            f["pb_pct"] = _find_col_value(tables, "市净率", "百分位")
        except Exception:
            pass
    return f


def _fund_gate(f: dict) -> tuple[str, str]:
    """确定性门槛：技术面初筛通过后再做基本面否决。并列列出所有命中的问题。"""
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


def _fundamental_check(results, top: int) -> None:
    """对 top 候选跑估值分位 + 净利同比 + ROE 确定性门槛。"""
    try:
        from app.config import Config
        from app.utils import load_env
    except Exception:
        return
    mx = None
    try:
        load_env(_ROOT)
        config = Config(_ROOT / "watchlist_config.json")
        keys = config.mx_apikeys
        if keys:
            from app.miaoxiang import MXClient
            mx = MXClient(keys)
    except Exception:
        mx = None

    n_fund = min(top, len(results), 10)
    print()
    print("=" * 72)
    print(f"基本面/估值确定性门槛（前 {n_fund} 名，本地库 + 同花顺 + 妙想分位）")
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
    print("  （ROE/净利同比/当前估值由本地库+同花顺直供；PB 历史分位优先本地逐日累积、不足回退妙想，缺失时显示 --）")
    print("  仅 ✅通过 才值得进一步看买点；❌/⚠️/待查 建议排除或人工补查后观望。")


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
    if not argv[1:]:
        return None, 20, False, False
    query = argv[1].strip()
    top = 20
    sync = False
    full = False
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            top = max(5, min(int(a), 50))
        elif a == "--sync":
            sync = True
        elif a in ("--full", "--no-prescreen"):
            full = True
    return query, top, sync, full


def main():
    query, top, sync, full = _parse_args(sys.argv)
    if not query:
        print(__doc__)
        return 2

    print("=" * 72)
    print(f"左侧选股扫描（板块/行业: {query}）")
    print("=" * 72)

    if sync:
        _sync_local_db()

    pool = resolve_board_pool(query)
    if not pool:
        print(f"❌ 无法解析「{query}」的成分股池（板块/行业名未匹配或数据源不可达）")
        print("   支持: 指数(沪深300/中证500/中证1000/上证50) / 板(创业板/科创板) / 行业(半导体/化工/白酒...)")
        return 1
    print(f"  成分股池: {len(pool)} 只（已剔除北交所/B股等无新浪K线的标的）")

    if not full:
        pool, prescreen_note = _prescreen(pool, ["down", "vol-down"])
        if prescreen_note:
            print(prescreen_note)

    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 1~4 分钟）...")
    turnover_map = fetch_turnover_map(list(pool.keys()))
    results = []
    deep_results = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_daily_local_first(code, market, days=250)
            if not klines or len(klines) < 60:
                continue
            r = _score_candidate(code, stock, klines, turnover_map.get(code))
            if r is not None:
                (deep_results if r.get("overdeep") else results).append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results and not deep_results:
        print("\n❌ 当前该板块/行业无符合条件的左侧机会候选（多处于「未明显回调」或「已破位走坏」状态）")
        return 0

    results.sort(key=lambda x: -x["score"])
    # 深坑股按「缩量优先 → 回撤更深」排序（缩量更像错杀，放量更像暴雷）
    deep_results.sort(key=lambda x: (
        not (x.get("vol_ratio") is not None and x["vol_ratio"] < 0.8),
        -x["dd"],
    ))

    print()
    print("=" * 72)
    print(f"左侧机会候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    header = (f"  {'代码':<8}{'名称':<10}{'板块':<12}{'现价':>8}{'回撤%':>7}{'缩量比':>7}{'换手%':>6}"
              f"{'RSI':>6}{'J值':>7}{'MACD':>6}{'站MA20':>7}{'止跌':>5}{'分':>4}  分级")
    print(header)
    print("  " + "-" * 110)
    for r in results[:top]:
        vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
        tr_txt = f"{r['turnover']:.1f}" if r["turnover"] is not None else "  --"
        rsi_txt = f"{r['rsi']:.0f}" if r["rsi"] is not None else "  --"
        j_txt = f"{r['j']:.0f}" if r["j"] is not None else "  --"
        print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<12}{r['price']:>8.2f}{r['dd']:>7.1f}"
              f"{vol_txt:>7}{tr_txt:>6}{rsi_txt:>6}{j_txt:>7}{r['macd_sig']:>6}"
              f"{('✅' if r['above_ma20'] else '❌'):>7}{('✅' if r['pattern'] else '❌'):>5}"
              f"{r['score']:>4}  {_verdict(r['score'])}")

    print()
    print("  说明:")
    print("    - 板块=成分归属（指数名/行业名）；回撤%=现价距近120日高点跌幅；缩量比=回调段均量/高点前20日均量（<0.6 抛压衰竭）")
    print("    - 换手%=今日换手率（<1% 地量确认真缩量 / ≥5% 高换手则缩量存疑）")
    print("    - RSI<30 / J值≤0 / MACD金叉 = 超卖反转信号；站MA20=初步企稳；止跌=长下影/锤子/看涨吞没")
    print("    - 回测：深跌≠好事——回撤≥40% 多为暴雷（fut20 最差），12~30% 温和超跌反弹最佳；止跌形态+站上 MA120 权重最高")
    print("    - 已超买（RSI>60 或 J>85）= 已反弹到位，不作为「超跌埋伏」列入")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 左侧是赌反转的埋伏仓，需分批、留补仓空间；确认买点/止损对单只运行 analyze_golden_pit.py 或 stock-analysis")

    # ---- 深度超阈值（>45%）单独列出，供人工判断基本面 ----
    if deep_results:
        n_deep = min(len(deep_results), top)
        print()
        print("=" * 72)
        print(f"❌ 深度超阈值候选（回撤 >45%，大概率非「短期错杀」而是暴雷，共 {len(deep_results)} 只，显示前 {n_deep}）")
        print("=" * 72)
        print("  跌得过深，务必先查基本面是「深度错杀」还是「暴雷」，再决定是否左侧埋伏。")
        print()
        dheader = (f"  {'代码':<8}{'名称':<10}{'板块':<12}{'现价':>8}{'回撤%':>7}"
                   f"{'缩量比':>7}{'站半年线':>8}{'RSI':>6}{'止跌':>5}")
        print(dheader)
        print("  " + "-" * 90)
        for r in deep_results[:n_deep]:
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            rsi_txt = f"{r['rsi']:.0f}" if r["rsi"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<12}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{('✅' if r['above_ma120'] else '❌'):>8}{rsi_txt:>6}"
                  f"{('✅' if r['pattern'] else '❌'):>5}")
        print()
        print("  说明: 站半年线(MA120)✅ + 缩量比<1.0 = 更可能是「深度错杀」，值得进一步查基本面。")

    _fundamental_check(results, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
