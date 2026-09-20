#!/usr/bin/env python3
"""右侧选股批量扫描：输入一个板块/行业（创业板/科创板/沪深300/中证500/中证1000/
半导体/芯片/化工 ...），解析成分股池，逐股按「均线多头→放量突破→MACD动量→涨幅强度」
检测打分，输出潜在的右侧机会候选排序表。

右侧 = 底部右侧买入（突破确认后顺势追），标的已站上关键均线、趋势转多、放量突破。
靠「趋势确认 + 突破 + 量价齐升」取胜。与 left-side（左侧：超跌埋伏、赌反转）互为镜像。

本脚本只做「初筛 + 打分排序 + 妙想资金快查」；确认单只买点/止损，再用 stock-analysis
或 intraday-signal 细看。

用法:
    py .claude/skills/right-side/scan_right_side.py <板块/行业> [输出数量]

参数:
    板块/行业  板块或行业名（如 创业板 / 科创板 / 沪深300 / 中证500 / 半导体 / 化工）
    输出数量    输出候选数量上限（可选，默认 20）

数据源: 成分股池（app.board_pool：指数成分 akshare + 东财 clist 板块/行业，核心不依赖
MX_APIKEY）+ 本地 duckdb 日 K 线（前复权，缺该标的才回退新浪）。妙想（MX_APIKEY）可选，用于 top
候选主力资金快查。
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

# 定位项目根目录（skills/right-side 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块的 WARNING 噪音
logging.disable(logging.WARNING)

from app.board_pool import resolve_board_pool
from app.helpers import _detect_market
from app.data_fetcher import fetch_turnover_map
from app.technical import (
    calc_sma,
    calc_macd,
)
from app.kline_local import fetch_daily_local_first


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 右侧检测

def _mean_vol(klines) -> float | None:
    vols = [k.volume for k in klines if k.volume is not None]
    return sum(vols) / len(vols) if vols else None


def _score_candidate(code: str, stock: dict, klines, turnover: float | None = None) -> dict | None:
    """对单只候选股做右侧机会检测 + 打分（0~100）。不满足「站上MA20 + 至少一个走强确认」直接排除。"""
    n = len(klines)
    if n < 60:
        return None
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-30:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # 排除 ST / 退市风险股（名称含 ST）
    name = stock.get("name", "")
    if "ST" in name.upper() or "退" in name:
        return None

    # 均线
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma60 = calc_sma(closes, 60)[-1] if len(closes) >= 60 else None

    above_ma20 = ma20 is not None and price >= ma20
    if not above_ma20:
        return None  # 未站上 MA20 = 趋势尚未修复

    # 突破：近 20 / 60 日高点（排除今日）
    highs20 = [k.high for k in klines[-21:-1] if k.high is not None]
    highs60 = [k.high for k in klines[-61:-1] if k.high is not None]
    high20 = max(highs20) if highs20 else None
    high60 = max(highs60) if highs60 else None
    broke20 = high20 is not None and price > high20
    broke60 = high60 is not None and price > high60

    # 放量：量比 = 今日量 / 前5日均量
    vols = [k.volume for k in klines if k.volume is not None]
    vol_ratio = None
    if len(vols) >= 6 and vols[-1] is not None:
        prev5 = vols[-6:-1]
        avg5 = sum(prev5) / len(prev5) if prev5 else None
        vol_ratio = (vols[-1] / avg5) if (avg5 and avg5 > 0) else None

    # MACD
    try:
        macd = calc_macd(closes)
    except Exception:
        macd = None
    macd_bullish = macd is not None and macd.signal in ("金叉", "多头")

    # 短期均线多头
    ma5_gt_ma10 = ma5 is not None and ma10 is not None and ma5 > ma10
    bull_align = (ma5 is not None and ma10 is not None and ma20 is not None and ma5 > ma10 > ma20)
    full_align = bull_align and (ma60 is not None and ma20 > ma60)

    # 5 日涨幅强度
    gain5 = None
    if len(closes) >= 6 and closes[-6] and closes[-6] > 0:
        gain5 = (price - closes[-6]) / closes[-6] * 100

    # ---- 硬门槛：至少一个走强确认 ----
    has_confirm = broke20 or broke60 or bull_align or macd_bullish or (vol_ratio is not None and vol_ratio >= 1.2)
    if not has_confirm:
        return None

    # ---- 打分 0~100 ----
    score = 0

    # 均线多头 30
    if full_align:
        score += 30
    elif bull_align:
        score += 22
    elif ma5_gt_ma10:
        score += 14
    else:
        score += 8

    # 突破 25（放量突破才算有效突破；无量创新高 = 假突破嫌疑，降级）
    vol_confirm = vol_ratio is not None and vol_ratio >= 1.2
    if broke20:
        score += 25 if vol_confirm else 12
    elif broke60:
        score += 20 if vol_confirm else 10
    elif high20 is not None and price >= high20 * 0.98:
        score += 12
    elif ma60 is not None and price >= ma60:
        score += 8

    # 放量 20（回测：极端放量≥2.0 为追涨衰竭、fut20 最差；温和放量 1.2~1.5 相对最好）
    if vol_ratio is not None:
        if vol_ratio >= 2.0:
            score += 6
        elif vol_ratio >= 1.5:
            score += 12
        elif vol_ratio >= 1.2:
            score += 20

    # 换手率确认（±5）：量比是相对值，大盘股量比可能虚高；换手率给绝对流动性校准
    if turnover is not None:
        if turnover >= 5.0:
            score += 5      # 高换手，放量可信（资金真在交易）
        elif turnover >= 2.0:
            score += 3      # 换手正常，放量有效
        elif turnover < 1.0 and vol_ratio is not None and vol_ratio >= 1.2:
            score -= 3      # 低换手却量比高 = 大盘股量比虚高，放量存疑

    # 涨幅强度 15（回测：5日已涨≥10% 为追高衰竭、fut20 最差；健康启动 3~6% 相对最好）
    if gain5 is not None:
        if gain5 >= 10:
            score += 4
        elif gain5 >= 6:
            score += 8
        elif gain5 >= 3:
            score += 15
        elif gain5 >= 1:
            score += 10

    # MACD 动量 10
    if macd is not None:
        if macd.signal == "金叉":
            score += 10
        elif macd.signal == "多头":
            score += 8

    align_txt = ("多头排列" if full_align else
                 "MA5>10>20" if bull_align else
                 "MA5>10" if ma5_gt_ma10 else "站MA20")
    _no_vol = "（无量）" if (broke20 or broke60) and not vol_confirm else ""
    breakout_txt = ("创20日新高" if broke20 else "创60日新高" if broke60 else ("逼近高点" if (high20 is not None and price >= high20 * 0.98) else "站MA60" if (ma60 is not None and price >= ma60) else "—")) + _no_vol

    return {
        "code": code,
        "name": name,
        "source": stock.get("source", ""),
        "price": price,
        "gain5": gain5,
        "vol_ratio": vol_ratio,
        "turnover": turnover,
        "align": align_txt,
        "macd_sig": macd.signal if macd else "",
        "breakout": breakout_txt,
        "score": score,
    }


def _score_pullback(code: str, stock: dict, klines, turnover: float | None = None) -> dict | None:
    """右侧「回踩买点」检测：趋势多头 + 近期创过新高 + 现价回踩缩量 + 未破 MA20。

    与 _score_candidate（突破日追入）互补：突破日是最延伸处、追高易接盘；回踩
    MA5/MA10/突破位附近是趋势内的优质赔率位。返回 dict 或 None（不满足条件）。

    核心条件（全部满足才返回）：
      1. 站上 MA20 且 MA20 走平向上（趋势未破）
      2. 近 5 日内创过 20 日新高（突破发生在近期，而非早已走远/已走坏）
      3. 现价从近期高点回撤 2%~10%（正在回踩，而非创新高追高）
      4. 回踩缩量（今日量比 < 1.2，放量回调 = 出货嫌疑，排除）
    """
    n = len(klines)
    if n < 60:
        return None
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-30:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    name = stock.get("name", "")
    if "ST" in name.upper() or "退" in name:
        return None

    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma20_prev = calc_sma(closes, 20)[-6] if len(closes) >= 26 else None

    # 1. 站上 MA20 且 MA20 走平向上（趋势未破）
    if ma20 is None or price < ma20:
        return None
    ma20_rising = ma20_prev is not None and ma20 >= ma20_prev
    if not ma20_rising:
        return None

    # 2. 近 5 日内创过 20 日新高
    highs20 = [k.high for k in klines[-21:-1] if k.high is not None]
    recent5 = [k.high for k in klines[-6:-1] if k.high is not None]
    if len(highs20) < 5 or not recent5:
        return None
    high20 = max(highs20)
    recent_high = max(recent5)
    if recent_high < high20 * 0.99:
        return None

    # 3. 回撤幅度 2%~10%
    if recent_high <= 0:
        return None
    drawdown = (recent_high - price) / recent_high * 100
    if drawdown < 2.0 or drawdown > 10.0:
        return None

    # 4. 回踩缩量：今日量比 < 1.2
    vols = [k.volume for k in klines if k.volume is not None]
    vol_ratio = None
    if len(vols) >= 6 and vols[-1] is not None:
        prev5 = vols[-6:-1]
        avg5 = sum(prev5) / len(prev5) if prev5 else None
        vol_ratio = (vols[-1] / avg5) if (avg5 and avg5 > 0) else None
    if vol_ratio is None or vol_ratio >= 1.2:
        return None

    # ---- 打分 0~100 ----
    score = 40  # 核心条件满足的基础分

    # 回撤深度（3~6% 健康回踩最优；太浅=没调够，太深=可能转弱）
    if 3.0 <= drawdown <= 6.0:
        score += 15
    elif drawdown < 3.0:
        score += 8
    else:
        score += 6

    # 缩量程度（回踩量越小越健康）
    if vol_ratio < 0.8:
        score += 15
    elif vol_ratio < 1.0:
        score += 10
    else:
        score += 5

    # 支撑贴近度：回踩到 MA10/MA20 附近加分
    dist_ma10 = abs(price - ma10) / ma10 * 100 if ma10 else 999.0
    dist_ma20 = abs(price - ma20) / ma20 * 100 if ma20 else 999.0
    if dist_ma10 <= 2.0:
        score += 15
        align_txt = "回踩MA10"
    elif dist_ma20 <= 2.0:
        score += 10
        align_txt = "回踩MA20"
    elif ma10 is not None and price >= ma10:
        score += 8
        align_txt = "MA10上方"
    else:
        score += 4
        align_txt = "MA10下方"

    # 换手率（低换手回踩更健康）
    if turnover is not None:
        if 1.0 <= turnover < 4.0:
            score += 5
        elif turnover < 1.0:
            score += 3

    return {
        "code": code,
        "name": name,
        "source": stock.get("source", ""),
        "price": price,
        "drawdown": round(drawdown, 1),
        "vol_ratio": round(vol_ratio, 2),
        "turnover": turnover,
        "dist_ma10": round(dist_ma10, 1),
        "align": align_txt,
        "recent_high": round(recent_high, 2),
        "score": score,
    }


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


# ---------------------------------------------------------------- 妙想主力资金

def _num_with_unit(value) -> float | None:
    """提取带「万/亿」单位的数字并换算为元（"505.8万"→5.058e6、"3.2亿"→3.2e8）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group()) * mult if m else None


def _fmt_flow(v: float | None) -> str:
    """元 → 短字符串（亿/万）。"""
    if v is None:
        return "  --"
    if abs(v) >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:+.0f}万"
    return f"{v:+.0f}元"


def _fetch_flow5d(mx, name: str, code: str) -> float | None:
    """妙想 → 近5日主力资金净流入（累计，元）。失败返回 None。"""
    try:
        tables = mx.query_structured(f"{name} {code} 近5日主力资金净流入")
    except Exception:
        return None
    # 取行数最多的表（每日序列，而非单行「当前」表），累加「主力净流入」列
    best = None
    for t in tables or []:
        rows = t.get("rows") or []
        if best is None or len(rows) > len(best.get("rows") or []):
            best = t
    if not best:
        return None
    total, found = 0.0, False
    for col in best.get("columns") or []:
        if "主力" in col and "净流入" in col and "占比" not in col and "比例" not in col:
            for r in best.get("rows") or []:
                v = _num_with_unit(r.get(col))
                if v is not None:
                    total += v
                    found = True
            break  # 只取第一个金额列，避免「净流入/占比」重复累加
    return total if found else None


def _flow_check(results, top: int) -> None:
    """对 top 候选查近5日主力资金，判断资金是否在买。"""
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
            print("\n  （未配置 MX_APIKEY，跳过主力资金快查；可对单只运行 fund-flow 看资金）")
            return
        mx = MXClient(keys)
    except Exception:
        return

    n_fund = min(top, len(results), 10)
    print()
    print("=" * 72)
    print(f"主力资金确认（前 {n_fund} 名，近5日累计，妙想）")
    print("=" * 72)
    print(f"  {'代码':<8}{'名称':<10}{'近5日主力净流入':>16}  判定")
    print("  " + "-" * 52)
    for r in results[:n_fund]:
        v = _fetch_flow5d(mx, r["name"], r["code"])
        r["flow5d"] = v
        tag = "-- 未查到" if v is None else ("✅ 净流入" if v > 0 else "⚠️ 净流出")
        print(f"  {r['code']:<8}{r['name']:<10}{_fmt_flow(v):>16}  {tag}")
    print()
    print("  说明: 右侧追的是「资金 + 趋势」共振，主力近5日净流入更稳；净流出则追高风险大，谨慎。")


# ---------------------------------------------------------------- 本地库 streaks 初筛

def _prescreen(pool: dict, kinds: list, min_pool: int = 40) -> tuple[dict, str]:
    """本地 marketdb streaks 初筛（实现见 tools.marketdb_local.prescreen_pool）。"""
    from tools.marketdb_local import prescreen_pool
    return prescreen_pool(pool, kinds, min_pool=min_pool, label="量价齐升/创新高")


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
    print(f"右侧选股扫描（板块/行业: {query}）")
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
        pool, prescreen_note = _prescreen(pool, ["vol-price-up", "new-high"])
        if prescreen_note:
            print(prescreen_note)

    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 1~3 分钟）...")
    turnover_map = fetch_turnover_map(list(pool.keys()))
    results = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_daily_local_first(code, market, days=120)
            if not klines or len(klines) < 60:
                continue
            r = _score_candidate(code, stock, klines, turnover_map.get(code))
            if r is not None:
                results.append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results:
        print("\n❌ 当前该板块/行业无符合条件的右侧机会候选（多处于「未站上MA20」或「无量无突破」状态）")
        return 0

    results.sort(key=lambda x: -x["score"])
    strong = [r for r in results if r["score"] >= 75]
    mid = [r for r in results if 55 <= r["score"] < 75]
    weak_n = len(results) - len(strong) - len(mid)

    header = (f"  {'代码':<8}{'名称':<10}{'板块':<12}{'现价':>8}{'5日涨%':>7}{'量比':>6}{'换手%':>6}"
              f"{'均线':<10}{'MACD':>6}{'突破':<10}{'分':>4}  分级")

    def _dump(rows, limit):
        for r in rows[:limit]:
            gain_txt = f"{r['gain5']:.1f}" if r["gain5"] is not None else "  --"
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            tr_txt = f"{r['turnover']:.1f}" if r["turnover"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<12}{r['price']:>8.2f}{gain_txt:>7}"
                  f"{vol_txt:>6}{tr_txt:>6}{r['align']:<10}{r['macd_sig']:>6}{r['breakout']:<10}"
                  f"{r['score']:>4}  {_verdict(r['score'])}")

    print()
    print("=" * 72)
    print(f"右侧机会候选（共 {len(results)} 只；强档 ≥75 为主，中档 55~74 观察，弱档 <55 不显示）")
    print("=" * 72)

    if strong:
        print(f"\n  【✅ 强信号 · 可重点看】 {len(strong)} 只，显示前 {min(top, len(strong))}")
        print(header)
        print("  " + "-" * 98)
        _dump(strong, top)
    else:
        print("\n  ❌ 无 score≥75 的强信号候选（该板块当前多为中弱档，趋势确认不足）")

    if mid:
        show_mid = min(len(mid), 10)
        print(f"\n  【⚠️ 观察 · 中档 55~74 · 仅跟踪、不急于买】 {len(mid)} 只，显示前 {show_mid}")
        print(header)
        print("  " + "-" * 98)
        _dump(mid, show_mid)

    if weak_n:
        print(f"\n  （另有 {weak_n} 只 score<55 弱档未显示——只跟随大盘、无增量信息）")

    print()
    print("  说明:")
    print("    - 板块=成分归属；5日涨%=近5个交易日涨幅；量比=今日量/前5日均量（≥1.2 放量，≥2 显著放量）")
    print("    - 换手%=今日换手率（<1% 地量 / 1~3% 正常 / 3~5% 活跃 / ≥5% 高换手），高换手确认真放量、低换手则量比可能虚高存疑")
    print("    - 均线：多头排列(MA5>10>20>60 最强) / MA5>10>20 / MA5>10 / 站MA20；突破=创20/60日新高或逼近高点")
    print("    - 分级：✅强(≥75 主信号) / ⚠️中(55~74 观察) / 🔸弱(<55 不显示)")
    print("    - ⚠️ 右侧是「趋势确认」信号，不是「选出来就买」：候选≠买点，买点=回踩 MA5/MA10 或突破位不破再进，止损=跌破 MA20 收盘确认即走")
    print("      回测（3 年 × 沪深300/中证500/中证1000）显示右侧确认信号净 edge 仅 ±0.2%，本质上跟随大盘；追高被套多发生在震荡/弱势市")
    print("      建议配合 market-heat 判断市场情绪，弱势市右侧信号降级或仅轻仓试错。")

    _flow_check(results, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
