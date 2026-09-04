#!/usr/bin/env python3
"""龙回头检测数据包：对单个标的判断「是否构成龙回头（强势首波后的缩量回调再启动）」。

核心思路（六层过滤链）：
    龙头确认 → 首波强 + 放量 → 缩量浅回调不破关键位 → 企稳 → 二次启动 → 破位止损。

本脚本只负责「取数 + 算指标 + 硬门槛预判」，输出 6 段结构化数据包；
「是否构成龙回头 / 能否参与 / 买点与止损」由 AI 依据 SKILL.md 的分析框架生成。

用法:
    py .claude/skills/dragon-pullback/analyze_dragon_pullback.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股代码（必填）
    名称   股票名称（可选，提升妙想查询精度）
    天数   拉取的日 K 线根数（可选，默认 60，上限 120；≥60 才能算 MA60）

数据源（按优先级）:
    - 实时快照: 新浪（价格/高低开收/换手率/涨停价）+ 腾讯（量比）
    - 日 K 线: 新浪（AKShare 兜底）→ 连板/首波/回调/缩量/均线
    - 当日资金流: 东方财富分钟级 fflow（主力/超大/大/中/小 5 档）
    - 龙虎榜: AKShare（识别龙头——是否上榜/净买额/买卖比/上榜原因）
    - 近 5 日资金流: 妙想 Miaoxiang（可选，需 MX_APIKEY；无 key 时跳过）

输出: 分 6 段——龙头识别 / 首波强度 / 回踩质量 / 企稳信号 / 二次启动确认 / 结论聚合。
"""
import os
import re
import sys
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

# 定位项目根目录（skills/dragon-pullback 上三级：dragon-pullback -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.models import WatchItem, Quote, FundFlowDetail, KlineData
from app.helpers import _detect_market
from app.data_fetcher import fetch_quotes, fetch_fund_flow_detail
from app.technical import (
    fetch_historical_kline,
    calc_sma,
    calc_support_resistance,
    get_technical_summary,
)

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


# ---------------------------------------------------------------- 涨停/连板

def _limit_pct(code: str) -> float:
    """涨跌停幅度（小数，0.10/0.20/0.30）。主板 10%、创业/科创 20%、北交所/新三板 30%。"""
    c = (code or "").strip()
    if c.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if c.startswith(("8", "4")):
        return 0.30
    return 0.10


def _is_limit_up(pct: float, code: str) -> bool:
    """是否封涨停（涨幅达到涨跌停幅度，容差 0.6 个百分点吸收四舍五入）。"""
    if pct is None:
        return False
    return pct >= _limit_pct(code) * 100 - 0.6


def _consecutive_limit_ups(pcts: list[float], code: str) -> int:
    """连续涨停天数（从最新一根往回数）。"""
    n = 0
    for pct in reversed(pcts):
        if _is_limit_up(pct, code):
            n += 1
        else:
            break
    return n


def _board_desc(code: str) -> str:
    """按代码前缀识别板块（科创板/创业板/主板/北交所）。"""
    c = (code or "").strip()
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith("60"):
        return "沪市主板"
    if c.startswith(("300", "301", "302")):
        return "创业板"
    if c.startswith("00"):
        return "深市主板"
    if c.startswith(("43", "82", "83", "87", "88", "92")):
        return "北交所"
    return "其他"


# ---------------------------------------------------------------- 首波识别

def _mean_vol(klines: list[KlineData]) -> float | None:
    """区间成交量均值（过滤 None）。"""
    vols = [k.volume for k in klines if k.volume is not None]
    return sum(vols) / len(vols) if vols else None


def _find_first_wave(klines: list[KlineData], recent: int = 20) -> dict | None:
    """识别最近一波拉升段（龙回头的「首波」）。

    首波顶点 = 近 recent 个交易日内最高 high（限定近期，避免把久远的历史高点当顶点）；
    起涨点 = 顶点前（回看至窗口起点）的最低 low；顶点之后为回调段。
    返回 dict（hi/si/顶点价/起涨价/涨幅/天数），顶点之后为回调段。
    """
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


# ---------------------------------------------------------------- 企稳形态

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


# ---------------------------------------------------------------- 打印段

def _print_dragon_id(code: str, name: str, board: str, pcts: list[float],
                     closes: list[float]) -> int:
    """【1】龙头识别：板块 + 连板高度 + 龙虎榜。返回连板数。"""
    print("=" * 72)
    print(f"【1. 龙头识别】{code} {name}（{board}）")
    print("=" * 72)
    limit = _limit_pct(code) * 100
    print(f"  涨跌停幅度 ±{limit:.0f}%（{board}）")
    consec = _consecutive_limit_ups(pcts, code)
    if consec >= 2:
        print(f"  连板高度: {consec} 连板（{'当前仍在连板' if _is_limit_up(pcts[-1], code) else '已断板'}）")
    elif consec == 1:
        print("  涨停: 最近 1 个交易日涨停")
    else:
        print("  涨停/连板: 无（近期无封板，或非连板模式拉升）")

    # 龙虎榜（识别龙头：游资/机构介入、净买、上榜原因）
    print("  龙虎榜:", end=" ")
    try:
        from app.dragon_tiger import fetch_dragon_tiger_list
        records = fetch_dragon_tiger_list(max_count=200)
        hit = next((r for r in records if r.code == code), None)
    except Exception:
        hit = None
    if hit is None:
        print("未上榜（非当日龙虎榜热门，游资/机构主战场待确认）")
    else:
        ratio = hit.buy_sell_ratio
        print(f"✅ 上榜 | 净买 {_fmt_amount(hit.net_buy)} 亿 | 买卖比 {_f(ratio)} | 换手 {_f(hit.turnover_rate, 1)}%")
        if hit.reason:
            print(f"    上榜原因: {hit.reason}")
    return consec


def _print_first_wave(klines: list[KlineData], wave: dict | None):
    """【2】首波上涨强度。"""
    print()
    print("=" * 72)
    print("【2. 首波上涨强度】")
    print("=" * 72)
    if wave is None:
        print("  ⚠️ K 线不足，无法识别首波")
        return
    n = len(klines)
    si, hi = wave["si"], wave["hi"]
    rise_pct = wave["rise_pct"]
    gap = n - 1 - hi
    print(f"  起涨点 {wave['start_date']} 低点 {_f(wave['start_low'])}  →  首波高点 {wave['high_date']} {_f(wave['wave_high'])}")
    print(f"  首波涨幅 {_f(rise_pct, 2)}%  |  拉升天数 {wave['days']} 日  |  距顶点 {gap} 个交易日")
    if hi >= n - 1:
        print("  ⚠️ 顶点即最近一日：首波可能仍在进行/刚见顶，尚未进入回调（不符合「回头」前提）")
        return
    if gap > 15:
        print(f"  ⚠️ 顶点距今 {gap} 日，超出典型龙回头回调窗口（3~15 日），非近期「拉升后回调」形态")
    # 放量对比：首波段 vs 首波前
    wave_vol = _mean_vol(klines[si:hi + 1])
    pre_vol = _mean_vol(klines[max(0, si - 5):si])
    if wave_vol and pre_vol:
        ratio = wave_vol / pre_vol
        flag = "✅ 放量" if ratio >= 1.2 else ("平量" if ratio >= 0.8 else "缩量")
        print(f"  首波均量 {wave_vol:,.0f} vs 起涨前均量 {pre_vol:,.0f}  →  量比 {ratio:.2f}（{flag}）")
        if ratio < 1.2:
            print("  ⚠️ 首波未明显放量，主力进场力度存疑")
    # 强弱参考
    strength = "强（龙头级）" if (rise_pct >= 30) else ("中等" if rise_pct >= 15 else "偏弱")
    print(f"  首波强度: {strength}（龙头首波通常 ≥20%，连板更佳）")


def _print_pullback(klines: list[KlineData], wave: dict | None, price: float | None,
                    ma5: float | None, ma10: float | None, ma20: float | None):
    """【3】回踩质量：回撤幅度 + 黄金分割 + 缩量 + 破位检查。"""
    print()
    print("=" * 72)
    print("【3. 回踩质量（核心）】")
    print("=" * 72)
    if wave is None:
        print("  ⚠️ 无法识别首波，跳过回踩分析")
        return
    n = len(klines)
    hi = wave["hi"]
    if hi >= n - 1:
        print("  ⚠️ 尚无回调段（顶点即最近一日）")
        return
    pull = klines[hi + 1:]          # 回调段
    wave_high = wave["wave_high"]
    start_low = wave["start_low"]
    rise_pct = wave["rise_pct"]
    pull_lows = [k.low for k in pull if k.low is not None]
    pull_low = min(pull_lows) if pull_lows else None

    # 回撤幅度（现价=最新收盘，主判断依据；盘中最低=止损参考）
    if pull_low is not None:
        deep_retrace = (wave_high - pull_low) / wave_high * 100
        print(f"  回调低点（盘中最低，止损参考）{_f(pull_low)}（最深回撤 {_f(deep_retrace, 2)}%）")
    cur_retrace = (wave_high - price) / wave_high * 100 if price else None
    print(f"  现价 {_f(price)}（自顶点回撤 {_f(cur_retrace, 2)}%）")

    # 黄金分割（现价回撤 / 首波涨幅，避免盘中插针误判回调深度）
    if price is not None and rise_pct > 0:
        fib = (wave_high - price) / (wave_high - start_low)  # 现价回撤占首波涨幅比例
        band = ("几乎没回调(<0.2，追高)" if fib < 0.2 else
                "强势回调(0.2~0.382)" if fib < 0.382 else
                "标准回调(0.382~0.5，健康买点)" if fib < 0.5 else
                "偏深回调(0.5~0.618)" if fib < 0.618 else
                "过深(>0.618，转弱风险大)")
        print(f"  黄金分割位: 现价回撤吃掉首波涨幅的 {fib * 100:.0f}%  →  {band}")

    # 缩量判断
    wave_vol = _mean_vol(klines[wave["si"]:hi + 1])
    pull_vol = _mean_vol(pull)
    if wave_vol and pull_vol:
        ratio = pull_vol / wave_vol
        if ratio < 0.7:
            print(f"  缩量回调: ✅ 回调均量仅首波的 {ratio * 100:.0f}%（抛压衰竭，主力未出货）")
        elif ratio < 1.0:
            print(f"  缩量回调: ⚠️ 回调均量为首波的 {ratio * 100:.0f}%（缩量不明显）")
        else:
            print(f"  缩量回调: ❌ 放量回调（均量 {ratio * 100:.0f}%，疑似出货，弃）")

    # 破位检查
    print(f"  关键位: MA5 {_f(ma5)}  MA10 {_f(ma10)}  MA20 {_f(ma20)}  起涨平台 {_f(start_low)}")
    if price is not None:
        broken = []
        if ma10 is not None and price < ma10:
            broken.append("跌破 MA10")
        if ma20 is not None and price < ma20:
            broken.append("跌破 MA20")
        if price < start_low:
            broken.append("跌破起涨平台")
        if broken:
            print(f"  破位检查: ❌ {'；'.join(broken)}（龙回头失败信号）")
        elif ma5 is not None and price >= ma5:
            print("  破位检查: ✅ 现价站回 MA5 上方，未破位")
        else:
            print("  破位检查: ⚠️ 现价在 MA5 与 MA10 之间（尚未破位但偏弱）")
    print(f"  回调天数: {len(pull)} 日（3~5 日最佳，过久人气散）")


def _print_stabilize(klines: list[KlineData], tech, price: float | None, atr: float | None):
    """【4】企稳信号：K 线形态 + 技术指标。"""
    print()
    print("=" * 72)
    print("【4. 企稳信号】")
    print("=" * 72)
    pattern, detail = _detect_reversal_pattern(klines)
    print(f"  止跌 K 线形态: {'✅' if pattern else '❌'}  {detail}")
    print(f"  RSI {_f(tech.rsi, 0)}  |  KDJ J {_f(tech.kdj_j, 0)}  |  MACD 柱 {_f(tech.macd_histogram, 3)}")
    if tech.rsi is not None and tech.rsi < 30:
        print("  ⚠️ RSI 超卖（可能还有惯性下杀，需等止跌确认）")
    if pattern:
        print("  ✅ 出现止跌形态，可关注是否放量确认")


def _print_second_start(klines: list[KlineData], q: Quote, ff: FundFlowDetail | None,
                        price: float | None, ma5: float | None):
    """【5】二次启动确认：放量阳线 + 站上 MA5 + 突破回调高点 + 当日资金。"""
    print()
    print("=" * 72)
    print("【5. 二次启动确认】")
    print("=" * 72)
    if len(klines) < 2:
        print("  ⚠️ K 线不足")
        return
    last, prev = klines[-1], klines[-2]
    if None in (last.open, last.close, last.volume):
        print("  ⚠️ 最新 K 线数据不全")
        return
    is_yang = last.close > last.open
    vol_up = prev.volume and last.volume and last.volume > prev.volume * 1.2
    print(f"  最新 K 线: {'阳线' if is_yang else '阴线'}  |  {'放量' if vol_up else '未放量'}"
          f"（量 {'{:,.0f}'.format(last.volume)} vs 前日 {'{:,.0f}'.format(prev.volume) if prev.volume else '--'}）")
    above_ma5 = ma5 is not None and price is not None and price >= ma5
    print(f"  站上 MA5: {'✅' if above_ma5 else '❌'}  （现价 {_f(price)} vs MA5 {_f(ma5)}）")

    # 突破回调区间高点
    if len(klines) >= 3:
        recent_highs = [k.high for k in klines[-5:] if k.high is not None]
        if recent_highs and price is not None:
            zone_high = max(recent_highs)
            broke = price > zone_high and last.close > last.open
            print(f"  突破回踩区间高点: {'✅' if broke else '❌'}  （现价 {_f(price)} vs 近5日高 {_f(zone_high)}）")
            if broke:
                print("  ✅ 放量阳线突破，二次启动确认信号")

    # 当日主力资金
    print(f"  当日主力净流入: {_fmt_amount(ff.main_net) if ff and ff.is_valid else 'N/A'} 亿"
          f"{'（资金回流确认）' if ff and ff.is_valid and ff.main_net and ff.main_net > 0 else ''}")


def _print_verdict(code: str, name: str, board: str, consec: int, wave: dict | None,
                   klines: list[KlineData], price: float | None, ma5: float | None,
                   ma10: float | None, ma20: float | None, tech) -> None:
    """【6】结论聚合：checklist 打分 + 信号分级 + 买点/止损参考。"""
    print()
    print("=" * 72)
    print("【6. 结论聚合（硬门槛预判，AI 据此出最终结论）】")
    print("=" * 72)
    if wave is None:
        print("  ❌ 不构成龙回头：K 线不足，无法识别首波")
        return

    n = len(klines)
    hi = wave["hi"]
    si = wave["si"]
    rise_pct = wave["rise_pct"]
    start_low = wave["start_low"]
    wave_high = wave["wave_high"]
    in_pullback = hi < n - 1

    # ---- 各层 checklist ----
    checks = []
    # 1. 龙头强度（连板 ≥2 或 首波涨幅 ≥20%）
    is_dragon = consec >= 2 or rise_pct >= 20
    checks.append(("龙头强度", is_dragon, f"连板 {consec} / 首波 {rise_pct:.0f}%"))
    # 2. 首波放量
    wave_vol = _mean_vol(klines[si:hi + 1])
    pre_vol = _mean_vol(klines[max(0, si - 5):si])
    wave_fangliang = (wave_vol and pre_vol and wave_vol >= pre_vol * 1.2)
    checks.append(("首波放量", wave_fangliang, "首波量 ≥ 起涨前 1.2 倍"))
    # 3. 缩量回调
    if in_pullback:
        pull_vol = _mean_vol(klines[hi + 1:])
        suo = (wave_vol and pull_vol and pull_vol < wave_vol * 0.7)
    else:
        suo = False
    checks.append(("缩量回调", suo, "回调量 < 首波 0.7 倍"))
    # 4. 回调未过深（黄金分割 ≤0.618，用现价）
    shallow = (price is not None and (wave_high - price) <= (wave_high - start_low) * 0.618)
    checks.append(("回调未过深", shallow, "现价回撤 ≤ 首波涨幅 0.618"))
    # 回调低点（盘中最低，止损参考）
    pull_low = min([k.low for k in klines[hi + 1:] if k.low is not None], default=None) if in_pullback else None
    # 5. 不破位
    not_broken = (price is not None and start_low is not None and price >= start_low
                  and (ma20 is None or price >= ma20))
    checks.append(("不破位", not_broken, "现价 ≥ 起涨平台 / MA20"))
    # 6. 企稳 / 二次启动
    pattern, _ = _detect_reversal_pattern(klines)
    last = klines[-1]
    second_start = (last.open is not None and last.close is not None
                    and last.close > last.open
                    and ma5 is not None and price is not None and price >= ma5
                    and prev_vol_bigger(klines))
    stab_or_start = pattern or second_start
    checks.append(("企稳/二次启动", stab_or_start, "止跌形态 或 放量阳线站上 MA5"))

    print("  信号 checklist:")
    for label, ok, note in checks:
        print(f"    {'✅' if ok else '❌'} {label}: {note}")

    # ---- 聚合分级 ----
    is_dragon_ok = checks[0][1]
    suo_ok = checks[2][1]
    shallow_ok = checks[3][1]
    not_broken_ok = checks[4][1]
    stab_ok = checks[5][1]

    if not in_pullback:
        verdict = "❌ 尚不构成龙回头（首波仍在进行/刚见顶，未见「回头」）；等首次回调出现后再判断"
    elif not is_dragon_ok:
        verdict = "❌ 不构成龙回头（首波不够强/非龙头，回调后大概率直接走弱）"
    elif not shallow_ok or not not_broken_ok:
        verdict = "❌ 不构成龙回头（回调过深 / 已破位，龙头退潮而非回头）"
    elif not suo_ok:
        verdict = "⚠️ 形态接近但回调未缩量（放量回调疑似出货），仅可观察，勿左侧埋伏"
    elif not stab_ok:
        verdict = "⚠️ 已缩量浅回调未破位，但尚无企稳/二次启动确认；可等待右侧信号（放量阳线站上 MA5）"
    else:
        verdict = "✅ 具备龙回头形态（龙头 + 缩量浅回调 + 未破位 + 企稳/二次启动）"

    print(f"  → 信号分级: {verdict}")

    # ---- 买点 / 止损参考 ----
    print()
    print("  参考关键位:")
    print(f"    起涨平台（强支撑/止损底线）: {_f(start_low)}")
    print(f"    首波高点（反弹目标/压力）: {_f(wave_high)}")
    if pull_low is not None:
        print(f"    回调低点（止损参考）: {_f(pull_low)}")
    if ma5 is not None:
        print(f"    MA5 {_f(ma5)} / MA10 {_f(ma10)} / MA20 {_f(ma20)}")
    if tech.support is not None:
        print(f"    技术支撑 {_f(tech.support)} / 压力 {_f(tech.resistance)}")


def prev_vol_bigger(klines: list[KlineData]) -> bool:
    """最新一根是否放量（成交量 > 前日 1.2 倍）。"""
    if len(klines) < 2:
        return False
    last, prev = klines[-1], klines[-2]
    return (last.volume is not None and prev.volume is not None
            and last.volume > prev.volume * 1.2)


# ---------------------------------------------------------------- 参数与主流程

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 60
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(20, min(int(a), 120))
        elif a:
            name = a
    return code, name, days


def _fetch_realtime(code: str, name: str) -> Quote | None:
    """实时快照（新浪 + 腾讯）。"""
    market = _detect_market(code)
    item = WatchItem(name=name, code=code, market=market, type="个股")
    quotes = fetch_quotes([item])
    return quotes[0] if quotes else None


def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/dragon-pullback/analyze_dragon_pullback.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)
    market = _detect_market(code)
    board = _board_desc(code)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")

    # ---- 实时快照 ----
    q = _fetch_realtime(code, name)
    if q is None:
        print(f"❌ 未获取到 {code} 实时行情（代码无效 / 非交易时段 / 网络异常）")
        return 1

    # ---- 日 K 线（核心数据源）----
    klines = fetch_historical_kline(code, market, days=days, scale=240)
    if not klines:
        print(f"❌ 未查到 {code} 日 K 线数据")
        return 1
    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < 20:
        print(f"❌ 日 K 线不足 20 根（当前 {len(closes)} 根），无法做龙回头检测")
        return 1

    # 逐日涨跌幅（close 差分）
    pcts: list[float] = []
    prev_c = None
    for k in klines:
        if prev_c and k.close is not None:
            pcts.append((k.close - prev_c) / prev_c * 100)
        else:
            pcts.append(0.0)
        if k.close is not None:
            prev_c = k.close

    price = q.price or closes[-1]
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    tech = get_technical_summary(q, klines)
    sr = calc_support_resistance(klines, lookback=20)
    atr = sr.atr

    # ---- 当日资金流（东财，静默降级）----
    ff = None
    try:
        ff = fetch_fund_flow_detail(code, market)
    except Exception:
        ff = None

    wave = _find_first_wave(klines)

    # ---- 输出 6 段 ----
    consec = _print_dragon_id(code, name, board, pcts, closes)
    _print_first_wave(klines, wave)
    _print_pullback(klines, wave, price, ma5, ma10, ma20)
    _print_stabilize(klines, tech, price, atr)
    _print_second_start(klines, q, ff, price, ma5)
    _print_verdict(code, name, board, consec, wave, klines, price, ma5, ma10, ma20, tech)

    return 0


if __name__ == "__main__":
    sys.exit(main())
