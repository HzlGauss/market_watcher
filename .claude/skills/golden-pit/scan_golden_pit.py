#!/usr/bin/env python3
"""黄金坑批量扫描：从白马/蓝筹指数成分股（沪深300 ∪ 上证50 ∪ 中证红利）构建候选池，
逐股跑「黄金坑」六层检测打分，按信号强度输出潜在候选排序表。

黄金坑 = 基本面扎实的白马/蓝筹股被短期错杀，价格深度挖坑（回撤 20%~45%）后缩量企稳、
开始修复。候选天然来自「指数成分股」（本身就是白马蓝筹的代理定义），再用技术面判「坑」。

本脚本只做「初筛 + 打分排序 + 基本面快查」；确认单只的估值/买点/止损，再用单股版
analyze_golden_pit.py 细看。

用法:
    py .claude/skills/golden-pit/scan_golden_pit.py [输出数量]

参数:
    输出数量   输出的候选数量上限（可选，默认 20）

数据源: akshare 指数成分股（index_stock_cons，需 pip install akshare）
+ 新浪日 K 线（fetch_historical_kline）。妙想（MX_APIKEY）可选，用于 top 候选基本面快查。
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

# 定位项目根目录（skills/golden-pit 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块的 WARNING 噪音（如个别 K 线获取失败刷屏）
logging.disable(logging.WARNING)

from app.helpers import _detect_market
from app.technical import (
    fetch_historical_kline,
    calc_sma,
    calc_rsi,
    calc_macd,
    calc_kdj,
)


# 候选池指数（代码 -> 白马成色分）：上证50(超大盘) > 沪深300(大盘蓝筹) > 中证红利(高股息白马)
INDICES = [
    ("000016", "上证50", 10),
    ("000300", "沪深300", 8),
    ("000922", "中证红利", 6),
]


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 候选池

def _fetch_mktcap_map() -> dict[str, float]:
    """从新浪沪深300成分行情拉市值映射 {code: 亿元}（免费、快）。"""
    mkt: dict[str, float] = {}
    try:
        import akshare as ak
        df = ak.index_stock_cons_sina(symbol="000300")
    except Exception:
        return mkt
    if df is None or getattr(df, "empty", True) or "mktcap" not in df.columns:
        return mkt
    for _, r in df.iterrows():
        code = str(r.get("code", "")).zfill(6)
        try:
            v = float(r.get("mktcap"))
        except (TypeError, ValueError):
            continue
        if code and v and v > 0:
            mkt[code] = v / 1e4  # 万元 -> 亿元
    return mkt


def _build_pool() -> dict[str, dict]:
    """拉指数成分股并合并去重，返回 {code: {name, indices, quality, mktcap}}。"""
    pool: dict[str, dict] = {}
    try:
        import akshare as ak
    except ImportError:
        print("❌ 未安装 akshare，无法构建指数成分候选池（pip install akshare）")
        return pool
    for idx_code, idx_name, quality in INDICES:
        try:
            df = ak.index_stock_cons(symbol=idx_code)
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            code = str(r.get("品种代码", "")).zfill(6)
            if not code or code in ("nan", "000000"):
                continue
            cur = pool.setdefault(code, {"name": str(r.get("品种名称", "")),
                                        "indices": set(), "quality": 0})
            cur["indices"].add(idx_name)
            cur["quality"] = max(cur["quality"], quality)
    mkt = _fetch_mktcap_map()
    for code, cur in pool.items():
        cur["mktcap"] = mkt.get(code)
    return pool


# ---------------------------------------------------------------- 黄金坑检测

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
    """对单只候选股做黄金坑检测 + 打分。太浅(<20%)直接排除；过深(>45%)不排除，
    标记 overdeep=True 单独列出（供人工判断基本面是「深度错杀」还是「暴雷」）。"""
    n = len(klines)
    if n < 60:
        return None
    closes = [k.close for k in klines]
    if any(c is None for c in closes[-60:]):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # 近期高点（回看 120 根，识别挖坑前的「坑沿」）
    start = max(0, n - 120)
    hi = max(range(start, n), key=lambda i: klines[i].high if klines[i].high is not None else -1e9)
    peak = klines[hi].high
    if peak is None or peak <= 0:
        return None
    gap = n - 1 - hi
    dd = (peak - price) / peak * 100

    if dd < 20:              # 太浅不算「坑」
        return None
    if gap < 5:              # 距高点不足 5 日 = 仍在顶部/刚开始跌，坑尚未成形
        return None

    # 均线
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma120 = calc_sma(closes, 120)[-1] if len(closes) >= 120 else None
    ma250 = calc_sma(closes, 250)[-1] if len(closes) >= 250 else None

    # 缩量：坑段(高点之后)均量 vs 高点前 20 日均量
    pit_vol = _mean_vol(klines[hi:])
    pre_vol = _mean_vol(klines[max(0, hi - 20):hi])
    vol_ratio = (pit_vol / pre_vol) if (pit_vol and pre_vol) else None

    # 企稳信号
    above_ma20 = ma20 is not None and price >= ma20
    macd_sig, macd_ok = "", False
    try:
        macd = calc_macd(closes)
        macd_sig = macd.signal if macd else ""
        macd_ok = bool(macd) and macd.signal in ("金叉", "多头") and (macd.histogram or 0) >= 0
    except Exception:
        pass
    kdj_ok = False
    try:
        kdj = calc_kdj([k.high for k in klines], [k.low for k in klines], closes)
        kdj_ok = bool(kdj) and kdj.k is not None and kdj.d is not None and kdj.k > kdj.d
    except Exception:
        pass
    pattern = _detect_reversal_pattern(klines)

    overdeep = dd > 45
    above_ma120 = ma120 is None or price >= ma120

    base = {
        "code": code,
        "name": stock["name"],
        "indices": stock["indices"],
        "mktcap": stock.get("mktcap"),
        "dd": dd,
        "gap": gap,
        "vol_ratio": vol_ratio,
        "price": price,
        "peak": peak,
        "ma20": ma20,
        "ma120": ma120,
        "ma250": ma250,
        "above_ma20": above_ma20,
        "above_ma120": above_ma120,
        "macd_sig": macd_sig,
        "kdj_ok": kdj_ok,
        "pattern": pattern,
        "overdeep": overdeep,
    }

    # ---- 深度超阈值（>45%）：不跑半年线/放量硬门槛，标记后单独列出 ----
    if overdeep:
        base["score"] = 0
        return base

    # ---- 常规硬门槛（仅 20%~45% 区间）----
    if ma120 is not None and price < ma120:   # 跌破半年线 = 长期趋势走坏（趋势性下跌，非黄金坑）
        return None
    if vol_ratio is None:
        return None
    if vol_ratio >= 1.2:       # 放量下跌 = 疑似出货/基本面暴雷，非「缩量挖坑」
        return None

    # ---- 打分（0~100）----
    score = stock["quality"]                       # 白马成色 0~10

    # 深坑幅度 25（25%~35% 最理想：风险充分释放、又未破位）
    if 25 <= dd <= 35:
        score += 25
    elif 20 <= dd < 25 or 35 < dd <= 40:
        score += 18
    else:
        score += 10

    # 坑底缩量 25
    if vol_ratio < 0.6:
        score += 25
    elif vol_ratio < 0.8:
        score += 18
    elif vol_ratio < 1.0:
        score += 10

    # 企稳反转 25（封顶）
    stab = 0
    if above_ma20:
        stab += 8
    if macd_ok:
        stab += 6
    if kdj_ok:
        stab += 6
    if pattern:
        stab += 5
    score += min(stab, 25)

    # 未破长期趋势 15（站上年线更健康）
    if ma250 is not None and price >= ma250:
        score += 15
    else:
        score += 8

    base["score"] = score
    return base


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


def _fmt_mktcap(v) -> str:
    """市值（亿元）-> 短字符串，None 显示 --，≥1万亿 转万亿。"""
    if v is None:
        return "  --"
    if v >= 10000:
        return f"{v / 10000:.1f}万亿"
    return f"{v:.0f}亿"


# ---------------------------------------------------------------- 基本面/估值（妙想，确定性门槛）

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
    """确定性门槛：返回 (标签, 说明)。技术面初筛通过后再做基本面否决。"""
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
    """对 top 候选跑估值分位 + 净利同比 + ROE 确定性门槛，否决/降级不达标者。"""
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

    n_fund = min(top, len(results), 8)
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
    print("  仅 ✅通过 才对单只跑 analyze_golden_pit.py 细看买点/止损；❌/⚠️/待查 建议排除或人工补查后观望。")


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    top = 20
    for a in argv[1:]:
        if a.strip().isdigit():
            top = max(5, min(int(a.strip()), 50))
    return top


def main():
    top = _parse_args(sys.argv)

    print("=" * 72)
    print("黄金坑批量扫描（沪深300 ∪ 上证50 ∪ 中证红利）")
    print("=" * 72)

    pool = _build_pool()
    if not pool:
        print("❌ 未获取到指数成分候选池（akshare 未装 / 网络异常）")
        return 1
    print(f"  候选池: {len(pool)} 只白马/蓝筹成分股")

    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 2~4 分钟）...")
    results = []
    deep_results = []
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_historical_kline(code, market, days=250, scale=240)
            if not klines or len(klines) < 60:
                continue
            r = _score_candidate(code, stock, klines)
            if r is not None:
                (deep_results if r.get("overdeep") else results).append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results and not deep_results:
        print("\n❌ 当前无符合条件的黄金坑候选（成分股多处于「未明显回调」或「已破位走坏」状态）")
        return 0

    results.sort(key=lambda x: -x["score"])
    # 深坑股按「错杀嫌疑」排序：站半年线 → 缩量 → 回撤更深 优先
    deep_results.sort(key=lambda x: (
        not x.get("above_ma120"),
        not (x.get("vol_ratio") is not None and x["vol_ratio"] < 1.0),
        -x["dd"],
    ))

    print()
    print("=" * 72)
    print(f"黄金坑候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    if results:
        header = (f"  {'代码':<8}{'名称':<10}{'指数':<16}{'市值':>8}{'现价':>8}{'回撤%':>7}"
                  f"{'缩量比':>7}{'站MA20':>7}{'MACD':>6}{'KDJ':>6}{'止跌':>5}{'分':>4}  分级")
        print(header)
        print("  " + "-" * 100)
        for r in results[:top]:
            idx_txt = "/".join(sorted(r["indices"]))
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{idx_txt:<16}{_fmt_mktcap(r['mktcap']):>8}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{('✅' if r['above_ma20'] else '❌'):>7}{r['macd_sig']:>6}"
                  f"{('✅' if r['kdj_ok'] else '❌'):>6}{('✅' if r['pattern'] else '❌'):>5}"
                  f"{r['score']:>4}  {_verdict(r['score'])}")
    else:
        print("  （无 20%~45% 区间的常规黄金坑候选）")

    print()
    print("  说明:")
    print("    - 指数=候选所属指数（上证50/沪深300/中证红利）；市值=总市值（行情源未覆盖的新纳入成分显示 —）；回撤%=现价距近120日高点跌幅")
    print("    - 缩量比=坑段均量/高点前20日均量（<0.6 健康）；站MA20/MACD金叉/KDJ金叉/止跌形态=企稳信号")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 确认估值/基本面/买点止损请对单只运行: py .claude/skills/golden-pit/analyze_golden_pit.py <代码>")

    # ---- 深度超阈值（>45%）单独列出，供人工判断基本面 ----
    if deep_results:
        n_deep = min(len(deep_results), 20)
        print()
        print("=" * 72)
        print(f"❌ 深度超阈值候选（回撤 >45%，超出黄金坑常规范畴，共 {len(deep_results)} 只，显示前 {n_deep}）")
        print("=" * 72)
        print("  这些股跌得过深，大概率已非「短期错杀」而是趋势性下跌/基本面暴雷；")
        print("  但个别可能属「深度错杀」，需对单只跑 analyze_golden_pit.py 确认基本面是否未恶化。")
        print()
        dheader = (f"  {'代码':<8}{'名称':<10}{'指数':<16}{'市值':>8}{'现价':>8}{'回撤%':>7}"
                   f"{'缩量比':>7}{'站半年线':>8}{'MACD':>6}{'KDJ':>6}{'止跌':>5}")
        print(dheader)
        print("  " + "-" * 100)
        for r in deep_results[:n_deep]:
            idx_txt = "/".join(sorted(r["indices"]))
            vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
            print(f"  {r['code']:<8}{r['name']:<10}{idx_txt:<16}{_fmt_mktcap(r['mktcap']):>8}{r['price']:>8.2f}{r['dd']:>7.1f}"
                  f"{vol_txt:>7}{('✅' if r['above_ma120'] else '❌'):>8}{r['macd_sig']:>6}"
                  f"{('✅' if r['kdj_ok'] else '❌'):>6}{('✅' if r['pattern'] else '❌'):>5}")
        print()
        print("  说明: 站半年线(MA120)=长期趋势未完全破坏；缩量比<1.0=抛压衰竭。「站半年线✅且缩量」者更可能是深度错杀，值得进一步查基本面。")

    _fundamental_check(results, top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
