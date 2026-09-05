#!/usr/bin/env python3
"""黄金坑检测数据包：对单个标的判断「是否处于/即将进入黄金坑（白马深坑后的缩量企稳）」。

核心思路（六层过滤链）：
    白马成色 → 深度挖坑 → 坑底缩量 → 估值低位 → 企稳反转 → 未破长期趋势。

本脚本只负责「取数 + 算指标 + 硬门槛预判」，输出 6 段结构化数据包；
「是否构成黄金坑 / 能否参与 / 买点与止损」由 AI 依据 SKILL.md 的分析框架生成。

用法:
    py .claude/skills/golden-pit/analyze_golden_pit.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股代码（必填）
    名称   股票名称（可选，提升妙想查询精度）
    天数   拉取的日 K 线根数（可选，默认 250；≥120 才能算 MA120，250 才算 MA250 年线）

数据源（按优先级）:
    - 实时快照: 新浪（价格/高低开收）
    - 日 K 线: 新浪（AKShare 兜底）→ 挖坑/缩量/企稳/均线
    - 基本面 + 估值: 妙想 Miaoxiang（ROE/营收/净利/负债率 + PE/PB 历史分位/股息率）
    - 指数成分归属: AKShare（判断是否沪深300/上证50/中证红利成分，可选）

输出: 分 6 段——白马成色 / 挖坑幅度 / 坑底缩量 / 估值低位 / 企稳反转 / 结论聚合。
"""
import os
import re
import sys
import time
from pathlib import Path

# 禁用 akshare/tqdm 进度条，避免污染 skill 输出
os.environ.setdefault("TQDM_DISABLE", "1")

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/golden-pit 上三级：golden-pit -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.models import WatchItem
from app.helpers import _detect_market
from app.data_fetcher import fetch_quotes
from app.technical import (
    fetch_historical_kline,
    calc_sma,
    calc_rsi,
    calc_macd,
    calc_kdj,
    calc_support_resistance,
)

INDICES = [("000016", "上证50"), ("000300", "沪深300"), ("000922", "中证红利")]


def _f(x, nd=2) -> str:
    return f"{x:.{nd}f}" if x is not None else "  --"


def _fpct(x) -> str:
    """百分比 -> 字符串（None 显示 --）。"""
    return f"{x:.1f}%" if x is not None else "  --"


def _mean_vol(klines) -> float | None:
    vols = [k.volume for k in klines if k.volume is not None]
    return sum(vols) / len(vols) if vols else None


def _detect_reversal_pattern(klines) -> tuple[bool, str]:
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


# ---------------------------------------------------------------- 妙想表格渲染

def _pad(s, width):
    w = sum(2 if ord(c) > 0x1100 else 1 for c in str(s))
    return str(s) + " " * max(0, width - w)


def _render_tables(tables) -> str:
    lines = []
    for t in tables:
        title = t.get("title") or t.get("entity_name") or "数据"
        if t.get("entity_name") and t.get("entity_name") != title:
            title = f"{title}（{t['entity_name']}）"
        lines.append(f"  {title}")
        columns = t.get("columns") or []
        rows = t.get("rows") or []
        if not columns:
            continue
        widths = []
        for c in columns:
            w = sum(2 if ord(ch) > 0x1100 else 1 for ch in str(c))
            for r in rows[:12]:
                w = max(w, sum(2 if ord(ch) > 0x1100 else 1 for ch in str(r.get(c, ""))))
            widths.append(min(w, 20))
        lines.append("  " + " | ".join(_pad(c, widths[i]) for i, c in enumerate(columns)))
        lines.append("  " + "-+-".join("-" * w for w in widths))
        for r in rows[:12]:
            cells = [_pad(str(r.get(c, "")).strip(), widths[i]) for i, c in enumerate(columns)]
            lines.append("  " + " | ".join(cells))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 基本面/估值解析

def _num(value) -> float | None:
    """从带后缀字符串（"33.98%"/"4.281倍"/"-34.11%"）提取首个带符号数字。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group()) if m else None


def _find_col_value(tables, *keywords, exclude=None) -> float | None:
    """在 query_structured 表格里，找列名同时含所有 keywords 且不含 exclude 的列，返回最新一行的数值。"""
    for t in tables:
        for col in (t.get("columns") or []):
            if all(k in col for k in keywords):
                if exclude and any(e in col for e in exclude):
                    continue
                for r in (t.get("rows") or []):
                    v = _num(r.get(col))
                    if v is not None:
                        return v
    return None




# ---------------------------------------------------------------- 打印段

def _print_identity(code: str, name: str, board: str):
    """【1】白马成色：板块 + 指数成分归属。"""
    print("=" * 72)
    print(f"【1. 白马成色】{code} {name}（{board}）")
    print("=" * 72)
    membership = []
    try:
        import akshare as ak
        for idx_code, idx_name in INDICES:
            df = ak.index_stock_cons(symbol=idx_code)
            if df is not None and not getattr(df, "empty", True):
                if any(str(r.get("品种代码", "")).zfill(6) == code for _, r in df.iterrows()):
                    membership.append(idx_name)
    except Exception:
        membership = []
    if membership:
        print(f"  指数成分: {' / '.join(membership)}（{'核心白马蓝筹' if '上证50' in membership else '白马/蓝筹'}）")
    else:
        print("  指数成分: 不在 沪深300/上证50/中证红利 中（是否「白马」需看下方基本面）")
    print("  （基本面质地见【4】，估值分位见【4】）")


def _print_pit(klines, price: float | None):
    """【2】挖坑幅度：近期高点→现价，回撤深度。"""
    print()
    print("=" * 72)
    print("【2. 挖坑幅度】")
    print("=" * 72)
    n = len(klines)
    if n < 60:
        print("  ⚠️ K 线不足 60 根，无法识别挖坑结构")
        return
    closes = [k.close for k in klines]
    price = price or closes[-1]
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    if peak is None or peak <= 0:
        print("  ⚠️ 无有效高点")
        return
    gap = n - 1 - hi
    dd = (peak - price) / peak * 100
    # 坑底最低点（高点之后）
    pit_lows = [k.low for k in klines[hi:] if k.low is not None]
    pit_low = min(pit_lows) if pit_lows else None
    deep_retrace = (peak - pit_low) / peak * 100 if pit_low else None

    print(f"  坑沿高点 {klines[hi].date} {_f(peak)}  →  现价 {_f(price)}")
    print(f"  现价回撤 {_f(dd, 2)}%  |  坑底最低 {_f(pit_low)}（最深回撤 {_f(deep_retrace, 2)}%）")
    print(f"  距高点 {gap} 个交易日")
    if dd < 20:
        band = "回调 <20%，尚未构成「黄金坑」（普通回调，未到错杀级别）"
    elif dd <= 35:
        band = "回撤 20%~35%，标准黄金坑深度（风险充分释放、又未破位）"
    elif dd <= 45:
        band = "回撤 35%~45%，深坑（错杀嫌疑大，但需警惕基本面是否恶化）"
    else:
        band = "回撤 >45%，深度超阈值（大概率趋势性下跌/基本面暴雷，但须确认是否「深度错杀」）"
    print(f"  深度分级: {band}")


def _print_volume(klines):
    """【3】坑底缩量：坑段均量 vs 高点前均量。"""
    print()
    print("=" * 72)
    print("【3. 坑底缩量】")
    print("=" * 72)
    n = len(klines)
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    if pit_vol and pre_vol:
        ratio = pit_vol / pre_vol
        if ratio < 0.6:
            print(f"  坑段均量 {pit_vol:,.0f} vs 高点前均量 {pre_vol:,.0f}  →  缩量比 {ratio:.2f}（✅ 抛压衰竭，主力未出逃）")
        elif ratio < 1.0:
            print(f"  坑段均量 {pit_vol:,.0f} vs 高点前均量 {pre_vol:,.0f}  →  缩量比 {ratio:.2f}（⚠️ 缩量不明显）")
        else:
            print(f"  坑段均量 {pit_vol:,.0f} vs 高点前均量 {pre_vol:,.0f}  →  缩量比 {ratio:.2f}（❌ 放量下跌，疑似出货/暴雷）")
    else:
        print("  ⚠️ 量能数据不足")


def _print_fundamental(mx, code: str, name: str) -> dict:
    """【4】估值低位 + 基本面：渲染妙想表格 + 抽取关键数字，返回 dict。"""
    print()
    print("=" * 72)
    print("【4. 估值低位 + 基本面（妙想）】")
    print("=" * 72)
    f = {"pb_pct": None, "pe_pct": None, "pe": None, "pb": None,
         "roe": None, "profit_growth": None, "revenue_growth": None}
    if mx is None:
        print("  ⚠️ 未配置 MX_APIKEY，跳过估值/基本面（可配置后重跑）")
        return f
    try:
        tables = mx.query_structured(f"{name} {code} 市盈率 历史分位 市净率 历史分位 股息率")
        if tables:
            print(_render_tables(tables))
            f["pb_pct"] = _find_col_value(tables, "市净率", "百分位")
            f["pe_pct"] = _find_col_value(tables, "市盈率", "百分位")
            f["pe"] = _find_col_value(tables, "市盈率PE", exclude=("百分位", "分位"))
            f["pb"] = _find_col_value(tables, "市净率PB", exclude=("百分位", "分位"))
    except Exception:
        pass
    time.sleep(0.4)
    try:
        tables = mx.query_structured(f"{name} {code} 最新财报 净利润 营业收入 同比增长 ROE 资产负债率")
        if tables:
            print(_render_tables(tables))
            f["roe"] = _find_col_value(tables, "ROE")
            f["profit_growth"] = _find_col_value(tables, "净利润", "同比")
            f["revenue_growth"] = _find_col_value(tables, "营业收入", "同比")
    except Exception:
        pass
    print("  关键数字:")
    print(f"    PB三年分位 {_fpct(f['pb_pct'])}  |  PE(TTM) {_f(f['pe'])}  |  PB {_f(f['pb'])}")
    print(f"    净利同比 {_fpct(f['profit_growth'])}  |  营收同比 {_fpct(f['revenue_growth'])}  |  ROE {_fpct(f['roe'])}")
    return f


def _print_stabilize(klines, price: float | None, ma20: float | None, ma60: float | None):
    """【5】企稳反转：RSI/KDJ/MACD/站上MA20/止跌形态。"""
    print()
    print("=" * 72)
    print("【5. 企稳反转】")
    print("=" * 72)
    closes = [k.close for k in klines if k.close is not None]
    price = price or (closes[-1] if closes else None)

    above_ma20 = ma20 is not None and price is not None and price >= ma20
    above_ma60 = ma60 is not None and price is not None and price >= ma60
    print(f"  站上 MA20: {'✅' if above_ma20 else '❌'}  （现价 {_f(price)} vs MA20 {_f(ma20)}）")
    print(f"  站上 MA60: {'✅' if above_ma60 else '❌'}  （MA60 {_f(ma60)}）")

    rsi = calc_rsi(closes) if len(closes) >= 14 else None
    print(f"  RSI {_f(rsi, 0)}", end="")
    if rsi is not None and rsi < 30:
        print("  （⚠️ 超卖，跌得急但超卖≠见底，需等止跌确认）")
    elif rsi is not None and rsi > 70:
        print("  （已回升至高位，追高需谨慎）")
    else:
        print()

    try:
        macd = calc_macd(closes)
        print(f"  MACD {macd.signal}（DIF {_f(macd.dif, 3)} / DEA {_f(macd.dea, 3)} / 柱 {_f(macd.histogram, 3)}）")
    except Exception:
        pass
    try:
        kdj = calc_kdj([k.high for k in klines], [k.low for k in klines], closes)
        kd = "金叉" if (kdj.k is not None and kdj.d is not None and kdj.k > kdj.d) else "死叉/弱势"
        print(f"  KDJ {kd}（K {_f(kdj.k, 0)} / D {_f(kdj.d, 0)} / J {_f(kdj.j, 0)}）")
    except Exception:
        pass
    pattern, detail = _detect_reversal_pattern(klines)
    print(f"  止跌 K 线形态: {'✅' if pattern else '❌'}  {detail}")


def _print_verdict(klines, price: float | None, ma20, ma60, ma120, ma250, tech, fund: dict) -> None:
    """【6】结论聚合：未破长期趋势 + 技术/基本面 checklist + 确定性门槛 + 买点/止损参考。"""
    print()
    print("=" * 72)
    print("【6. 结论聚合（硬门槛预判，AI 据此出最终结论）】")
    print("=" * 72)
    n = len(klines)
    closes = [k.close for k in klines if k.close is not None]
    price = price or (closes[-1] if closes else None)
    if n < 60 or price is None:
        print("  ❌ 数据不足，无法判定黄金坑")
        return

    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    gap = n - 1 - hi
    dd = (peak - price) / peak * 100 if peak else None
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    vol_ratio = (pit_vol / pre_vol) if (pit_vol and pre_vol) else None
    pit_low = min([k.low for k in klines[hi:] if k.low is not None], default=None)

    pattern, _ = _detect_reversal_pattern(klines)
    above_ma20 = ma20 is not None and price >= ma20
    macd_ok = False
    try:
        macd = calc_macd(closes)
        macd_ok = macd.signal in ("金叉", "多头") and (macd.histogram or 0) >= 0
    except Exception:
        pass
    kdj_ok = False
    try:
        kdj = calc_kdj([k.high for k in klines], [k.low for k in klines], closes)
        kdj_ok = kdj.k is not None and kdj.d is not None and kdj.k > kdj.d
    except Exception:
        pass

    # ---- 长期趋势（未破位）----
    trend_txt = []
    if ma120 is not None:
        trend_txt.append(f"MA120 {_f(ma120)} {'✓站上' if price >= ma120 else '✗跌破'}")
    if ma250 is not None:
        trend_txt.append(f"MA250(年线) {_f(ma250)} {'✓站上' if price >= ma250 else '✗跌破'}")
    print(f"  长期趋势: {'；'.join(trend_txt) if trend_txt else '  数据不足'}")

    # ---- 六层 checklist ----
    checks = []
    checks.append(("深坑幅度 20%~45%", dd is not None and 20 <= dd <= 45, f"现价回撤 {dd:.0f}%" if dd else "--"))
    checks.append(("坑底缩量(比<1.0)", vol_ratio is not None and vol_ratio < 1.0,
                   f"缩量比 {vol_ratio:.2f}" if vol_ratio else "--"))
    checks.append(("未破半年线 MA120", ma120 is None or price >= ma120,
                   "站上" if ma120 is None or price >= ma120 else "跌破"))
    checks.append(("站上 MA20", above_ma20, "现价 ≥ MA20"))
    checks.append(("企稳信号(MACD/KDJ/止跌)", macd_ok or kdj_ok or pattern,
                   "MACD金叉/KDJ金叉/止跌形态 任一"))
    checks.append(("未放量下跌(<1.2)", vol_ratio is not None and vol_ratio < 1.2,
                   f"缩量比 {vol_ratio:.2f}" if vol_ratio else "--"))

    print("  信号 checklist:")
    for label, ok, note in checks:
        print(f"    {'✅' if ok else '❌'} {label}: {note}")

    # ---- 基本面/估值 checklist（确定性门槛，非仅参考）----
    fund = fund or {}
    profit_growth = fund.get("profit_growth")
    pb_pct = fund.get("pb_pct")
    roe = fund.get("roe")
    fund_has_data = any(v is not None for v in (profit_growth, pb_pct, roe))

    print("  基本面/估值 checklist（确定性门槛）:")
    if not fund_has_data:
        print("    -- 未取到妙想数据，基本面/估值门槛跳过（缺 MX_APIKEY 或查询失败）")
    else:
        profit_ok = profit_growth is None or profit_growth >= 0
        print(f"    {'✅' if profit_ok else '❌'} 净利同比≥0（非业绩下滑）: "
              f"{_fpct(profit_growth) if profit_growth is not None else '--'}")
        pb_ok = pb_pct is None or pb_pct <= 70
        print(f"    {'✅' if pb_ok else '❌'} PB历史分位≤70（非高位）: "
              f"{_fpct(pb_pct) if pb_pct is not None else '--'}")
        roe_ok = roe is None or roe >= 8
        print(f"    {'✅' if roe_ok else '❌'} ROE≥8（白马成色）: "
              f"{_fpct(roe) if roe is not None else '--'}")

    # ---- 聚合分级 ----
    dd_ok = checks[0][1]
    vol_ok = checks[1][1]
    trend_ok = checks[2][1]
    above_ma20_ok = checks[3][1]
    stab_ok = checks[4][1]
    no_fangliang = checks[5][1]
    tech_pass = (dd_ok and trend_ok and no_fangliang and vol_ok and stab_ok)

    if dd is not None and dd < 20:
        verdict = "❌ 尚未构成黄金坑（回撤 <20%，只是普通回调，未到错杀级别）"
    elif dd is not None and dd > 45:
        verdict = "❌ 深度超阈值（回撤 >45%，超出黄金坑常规范畴）——默认按趋势性下跌/暴雷对待；须结合【4】确认基本面是「深度错杀」还是「暴雷」"
    elif not trend_ok:
        verdict = "❌ 不构成黄金坑（跌破半年线，长期趋势走坏，属趋势性下跌而非错杀挖坑）"
    elif not no_fangliang:
        verdict = "❌ 不构成黄金坑（坑段放量下跌，疑似出货/暴雷，非缩量挖坑）"
    elif not vol_ok:
        verdict = "⚠️ 形态接近但坑底缩量不明显，需进一步观察是否缩量企稳"
    elif not stab_ok:
        verdict = "⚠️ 已深坑缩量未破位，但尚无企稳反转确认；可等待右侧信号（站上MA20/MACD金叉）"
    else:
        # 技术面全过 → 叠加基本面/估值确定性门槛
        if not fund_has_data:
            verdict = "✅ 具备黄金坑形态（技术面全过）；基本面/估值未查（缺 MX_APIKEY），确认估值低位后再参与"
        elif profit_growth is not None and profit_growth < 0:
            verdict = f"❌ 技术形态达标但业绩下滑（净利同比 {_fpct(profit_growth)}），属「真跌」而非「错杀」，排除"
        elif pb_pct is not None and pb_pct > 70:
            verdict = f"⚠️ 技术形态达标但估值分位偏高（PB分位 {_fpct(pb_pct)}），非典型错杀、安全垫不足，观察等回落"
        elif roe is not None and roe < 8:
            verdict = f"⚠️ 技术形态达标但 ROE 偏低（{_fpct(roe)}），白马成色不足，谨慎"
        elif pb_pct is None:
            verdict = "⚠️ 技术形态达标、业绩/ROE 未见异常，但估值分位未查到，需人工确认 PB 分位后再定是否参与"
        else:
            verdict = "✅ 构成黄金坑（技术形态 + 基本面/估值 双过，错杀概率高）"

    print(f"  → 信号分级: {verdict}")

    # ---- 买点 / 止损参考 ----
    print()
    print("  参考关键位:")
    if peak:
        print(f"    坑沿高点（修复目标/压力）: {_f(peak)}")
    if pit_low:
        print(f"    坑底低点（止损参考/强支撑）: {_f(pit_low)}")
    if ma60 is not None:
        print(f"    MA20 {_f(ma20)} / MA60 {_f(ma60)} / MA120 {_f(ma120)} / MA250 {_f(ma250)}")
    if tech.support is not None:
        print(f"    技术支撑 {_f(tech.support)} / 压力 {_f(tech.resistance)}")


# ---------------------------------------------------------------- 参数与主流程

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 250
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(120, min(int(a), 400))
        elif a:
            name = a
    return code, name, days


def _board_desc(code: str) -> str:
    c = (code or "").strip()
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith("60"):
        return "沪市主板"
    if c.startswith(("300", "301", "302")):
        return "创业板"
    if c.startswith("00"):
        return "深市主板"
    return "其他"


def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/golden-pit/analyze_golden_pit.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)
    market = _detect_market(code)
    board = _board_desc(code)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")

    # ---- 妙想（可选）----
    mx = None
    if config.mx_apikeys:
        try:
            from app.miaoxiang import MXClient
            mx = MXClient(config.mx_apikeys)
        except Exception:
            mx = None

    # ---- 实时快照 ----
    price = None
    try:
        item = WatchItem(name=name, code=code, market=market, type="个股")
        quotes = fetch_quotes([item])
        if quotes and quotes[0].price:
            price = quotes[0].price
    except Exception:
        price = None

    # ---- 日 K 线（核心数据源）----
    klines = fetch_historical_kline(code, market, days=days, scale=240)
    if not klines or len(klines) < 60:
        print(f"❌ 未查到 {code} 足够日 K 线数据（需 ≥60 根）")
        return 1

    closes = [k.close for k in klines if k.close is not None]
    price = price or (closes[-1] if closes else None)
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma60 = calc_sma(closes, 60)[-1] if len(closes) >= 60 else None
    ma120 = calc_sma(closes, 120)[-1] if len(closes) >= 120 else None
    ma250 = calc_sma(closes, 250)[-1] if len(closes) >= 250 else None
    tech = calc_support_resistance(klines, lookback=20)

    # ---- 输出 6 段 ----
    _print_identity(code, name, board)
    _print_pit(klines, price)
    _print_volume(klines)
    fund = _print_fundamental(mx, code, name)
    _print_stabilize(klines, price, ma20, ma60)
    _print_verdict(klines, price, ma20, ma60, ma120, ma250, tech, fund)

    return 0


if __name__ == "__main__":
    sys.exit(main())
