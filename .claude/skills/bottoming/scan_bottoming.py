#!/usr/bin/env python3
"""磨底（止跌横盘缩量打底）批量扫描：输入一个板块/行业（创业板/科创板/沪深300/中证500/
中证1000/半导体/化工 ...）或本地白名单/CSV（白马蓝筹/蓝筹白马/白名单，或 .csv 文件路径），
解析成分股池，逐股按「止跌→横盘窄幅→缩量→磨底时长→企稳→未启动」六层检测打分，输出
「已经止跌、正在底部横盘缩量打底」的候选排序表。

磨底 = 底部左侧买入中最安全的一段：股价已不再创新低（止跌确认），在低位窄幅横盘、
成交量萎缩（抛压衰竭），但尚未放量突破（那是 right-side）。区别于：
  - left-side：标的可能仍在下跌（接飞刀），未见底确认；
  - right-side：已放量突破启动，顺势追；
  - golden-pit：磨底的白马+深坑（回撤≥20%）特例。

本脚本只做「初筛 + 打分排序」；确认单只买点/止损/基本面，用 stock-analysis 或
golden-pit/left-side 细看。白名单/CSV 输入时，输出表附其自带的 ROE/净利/PB分位 快照。

用法:
    py .claude/skills/bottoming/scan_bottoming.py <板块/行业|白名单/CSV> [输出数量] [--sync] [--full]

参数:
    板块/行业  板块或行业名（如 创业板 / 科创板 / 沪深300 / 中证500 / 半导体 / 化工）
    白名单/CSV 本地白名单关键词（白马蓝筹/蓝筹白马/白名单 → data/bluechip_whitelist.csv）
               或任意 .csv 文件路径（相对项目根目录，须含「代码」列，可选「名称/层级」列）
    输出数量    输出候选数量上限（可选，默认 20）
    --sync    先增量同步本地 marketdb 到最新（可能触发下载，遇 429 限流会失败）
    --full    跳过本地库 streaks 初筛，全池逐股拉 K 线（更慢但召回最全）

数据源: 成分股池（app.board_pool）+ 本地 duckdb 日 K 线（前复权，缺该标的才回退新浪）。
纯技术维度，不依赖 MX_APIKEY，不做远端基本面门槛。
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

# 定位项目根目录（skills/bottoming 上三级）
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
from app.technical import calc_sma, calc_rsi, detect_stage
from app.kline_local import fetch_daily_local_first
from app.regime import gate_side


# ---------------------------------------------------------------- 阈值（初版，回测后校准）

DIST_LOW_MAX = 8.0        # 距近60日最低收盘 ≤8% 才算「止跌未涨离」
DAYS_SINCE_LOW_MIN = 10   # 低点距今 ≥10 交易日（止跌横盘够久，排除下跌中继）
BOX_WIDTH_MAX = 20.0      # 近60日箱宽（高低/下沿）≤20% = 窄幅横盘
VOL_RATIO_MAX = 0.8       # 近20日均量 / 近120日均量 <0.8 = 缩量打底
RSI_CEIL = 65.0           # RSI 上限，超过视为已反弹到位、不算磨底


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# ---------------------------------------------------------------- 检测 + 打分

def _score_bottoming(code: str, stock: dict, klines) -> dict | None:
    """对单只候选股做「磨底」检测打分（0~100）。

    六层：止跌（不创新低+贴底）→ 横盘窄幅（箱体压缩）→ 缩量 → 磨底时长 → 企稳 → 未启动。
    硬门槛不满足直接排除。
    """
    n = len(klines)
    if n < 60:
        return None
    closes = [k.close for k in klines]
    lows = [k.low for k in klines]
    highs = [k.high for k in klines]
    if (any(c is None for c in closes[-60:])
            or any(l is None for l in lows[-60:])
            or any(h is None for h in highs[-60:])):
        return None
    price = closes[-1]
    if price is None or price <= 0:
        return None

    # ---- 止跌：近60日最低收盘 = 磨底锚点，算距低点% + 磨底时长 ----
    anchor = min(closes[-60:])
    if anchor is None or anchor <= 0:
        return None
    # 取低点「首次」出现位置算磨底时长：低点形成越久、越久未创新低 = 止跌越确认。
    # （用首次而非末次：横盘打底的票会反复触低，末次会把时长清零、误判成下跌中继。）
    anchor_idx = min(i for i in range(n - 60, n) if closes[i] == anchor)
    days_since_low = n - 1 - anchor_idx
    dist_low = (price - anchor) / anchor * 100  # 恒 ≥0（现价在窗口内）；0%=正贴底 / >0%=已从底部抬升

    # ---- 箱体（近60日真实高低区间）+ 箱宽 + 位置 ----
    lo60 = min(lows[-60:])
    hi60 = max(highs[-60:])
    if hi60 <= lo60:
        return None
    box_w = (hi60 - lo60) / lo60 * 100
    pos_pct = (price - lo60) / (hi60 - lo60) * 100

    # ---- 缩量：近20日均量 / 近120日均量（当前是否地量打底）----
    vols = [k.volume for k in klines if k.volume is not None]
    if len(vols) < 120:
        return None
    avg120 = sum(vols[-120:]) / 120
    if avg120 <= 0:
        return None
    vol_ratio = (sum(vols[-20:]) / 20) / avg120

    # ---- 硬门槛 ----
    if dist_low > DIST_LOW_MAX:
        return None   # 已涨离底部（已启动），交还 right-side
    if days_since_low < DAYS_SINCE_LOW_MIN:
        return None   # 60日低点形成不久（近10日仍在创新低）= 下跌中继，非磨底
    if box_w > BOX_WIDTH_MAX:
        return None   # 箱体过宽 = 趋势，非窄幅磨底
    if vol_ratio >= VOL_RATIO_MAX:
        return None   # 未缩量 = 抛压未尽

    # ---- 阶段（detect_stage）+ 动量 ----
    try:
        stage = detect_stage(klines)
    except Exception:
        stage = None
    stage_name = stage.stage if stage else ""
    if stage_name in ("启动期", "主升浪", "赶顶期"):
        return None   # 已启动，交还 right-side
    rsi = calc_rsi(closes)
    if rsi is not None and rsi > RSI_CEIL:
        return None   # 已反弹到位，不再是「磨底埋伏」标的

    # ---- 均线企稳 ----
    ma5 = calc_sma(closes, 5)[-1] if len(closes) >= 5 else None
    ma10 = calc_sma(closes, 10)[-1] if len(closes) >= 10 else None
    ma20 = calc_sma(closes, 20)[-1] if len(closes) >= 20 else None
    above_ma5 = ma5 is not None and price >= ma5
    above_ma10 = ma10 is not None and price >= ma10
    above_ma20 = ma20 is not None and price >= ma20

    # ---- 打分 0~100 ----
    score = 0

    # 止跌贴底 20：越贴底（未涨离）越高分
    if dist_low <= 2:
        score += 20
    elif dist_low <= 4:
        score += 16
    elif dist_low <= 6:
        score += 12
    else:
        score += 8

    # 箱体压缩 20：越窄越像磨底尾声
    if box_w <= 10:
        score += 20
    elif box_w <= 15:
        score += 16
    else:
        score += 12

    # 缩量 20：越缩越健康
    if vol_ratio < 0.6:
        score += 20
    else:
        score += 14

    # 磨底时长 15：止跌横盘越久越充分（但过久也非最优，此处只奖充分不罚过长）
    if days_since_low >= 40:
        score += 15
    elif days_since_low >= 30:
        score += 13
    elif days_since_low >= 20:
        score += 10
    else:
        score += 6

    # 企稳 15：站回中短期均线 = 止跌确认
    if above_ma20:
        score += 8
    if above_ma10:
        score += 4
    if above_ma5:
        score += 3

    # 阶段确认 10：detect_stage 判「磨底期」+ 均线缠绕
    if stage_name == "磨底期":
        score += 6
    trend = stage.trend_axis if stage else ""
    if trend.startswith("缠绕"):
        score += 4

    return {
        "code": code,
        "name": stock["name"],
        "source": stock.get("source", ""),
        "price": price,
        "dist_low": dist_low,
        "days_since_low": days_since_low,
        "box_w": box_w,
        "pos_pct": pos_pct,
        "vol_ratio": vol_ratio,
        "rsi": rsi,
        "stage": stage_name,
        "above_ma20": above_ma20,
        "score": score,
        "fund_csv": stock.get("fund_csv"),
    }


def _verdict(score: int) -> str:
    if score >= 75:
        return "✅ 强"
    if score >= 55:
        return "⚠️ 中"
    return "🔸 弱"


# ---------------------------------------------------------------- 白名单/CSV 基本面快照

def _fund_csv_table(results, top: int) -> None:
    """白名单/CSV 输入时，附其自带的 ROE今%/净利同比%/PB分位% 快照（免远端拉取）。

    板块/行业输入无 fund_csv 快照，跳过本段（磨底是纯技术阶段，基本面另查）。
    """
    rows = [r for r in results[:top] if r.get("fund_csv")]
    if not rows:
        return
    print()
    print("=" * 72)
    print(f"白名单/CSV 自带基本面快照（前 {len(rows)} 名，筛选当日快照，非实时）")
    print("=" * 72)
    print(f"  {'代码':<8}{'名称':<10}{'ROE今%':>9}{'净利同比%':>10}{'PB分位%':>9}")
    print("  " + "-" * 56)
    for r in rows:
        fc = r["fund_csv"]
        roe = fc.get("roe")
        pg = fc.get("profit_growth")
        pb = fc.get("pb_pct")
        roe_txt = f"{roe:.1f}" if roe is not None else " --"
        pg_txt = f"{pg:.1f}" if pg is not None else " --"
        pb_txt = f"{pb:.1f}" if pb is not None else " --"
        print(f"  {r['code']:<8}{r['name']:<10}{roe_txt:>9}{pg_txt:>10}{pb_txt:>9}")
    print("  快照规则: 净利同比<0 = 真跌 / PB分位>70 = 估值高 / ROE<8 = 成色弱，命中须警惕价值陷阱")


# ---------------------------------------------------------------- 本地库 streaks 初筛

def _prescreen(pool: dict, kinds: list, min_pool: int = 40) -> tuple[dict, str]:
    """本地 marketdb streaks 初筛（实现见 tools.marketdb_local.prescreen_pool）。"""
    from tools.marketdb_local import prescreen_pool
    return prescreen_pool(pool, kinds, min_pool=min_pool, label="连续缩量")


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
    print(f"磨底扫描（止跌横盘缩量打底，板块/行业: {query}）")
    print("=" * 72)

    if sync:
        _sync_local_db()

    pool = resolve_board_pool(query)
    if not pool:
        print(f"❌ 无法解析「{query}」的成分股池（板块/行业名未匹配或数据源不可达）")
        print("   支持: 指数(沪深300/中证500/中证1000/上证50) / 板(创业板/科创板) / 行业(半导体/化工/白酒...) / 白马蓝筹 / CSV路径")
        return 1
    print(f"  成分股池: {len(pool)} 只（已剔除北交所/B股等无新浪K线的标的）")

    if not full:
        pool, prescreen_note = _prescreen(pool, ["vol-down"])
        if prescreen_note:
            print(prescreen_note)

    print()
    print(f"  逐股检测中（{len(pool)} 只，约需 1~4 分钟）...")
    results = []
    suppressed = 0
    done = 0
    for code, stock in pool.items():
        try:
            market = _detect_market(code)
            klines = fetch_daily_local_first(code, market, days=250)
            if not klines or len(klines) < 60:
                continue
            r = _score_bottoming(code, stock, klines)
            if r is not None:
                ok, note = gate_side(code, "left")
                if not ok:
                    suppressed += 1
                    continue
                results.append(r)
        except Exception:
            pass
        done += 1
        if done % 50 == 0:
            print(f"    已检测 {done}/{len(pool)} ...", file=sys.stderr)

    if not results:
        msg = "当前该板块/行业无符合条件的磨底候选（多处于「仍在下跌」「箱体过宽趋势」「未缩量」或「已启动」状态）"
        if suppressed:
            msg += f"；另有 {suppressed} 只因板块广度 regime 不符（牛市禁左侧埋伏）被抑制"
        print(f"\n❌ {msg}")
        return 0

    results.sort(key=lambda x: -x["score"])

    print()
    print("=" * 72)
    print(f"磨底候选（按信号分降序，共 {len(results)} 只，显示前 {min(top, len(results))}）")
    print("=" * 72)
    header = (f"  {'代码':<8}{'名称':<10}{'板块':<12}{'现价':>8}{'距低点%':>8}{'磨底天数':>8}"
              f"{'箱宽%':>7}{'缩量比':>7}{'RSI':>6}{'站MA20':>7}{'分':>4}  分级")
    print(header)
    print("  " + "-" * 108)
    for r in results[:top]:
        vol_txt = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "  --"
        rsi_txt = f"{r['rsi']:.0f}" if r["rsi"] is not None else "  --"
        print(f"  {r['code']:<8}{r['name']:<10}{r['source']:<12}{r['price']:>8.2f}{r['dist_low']:>8.1f}"
              f"{r['days_since_low']:>8}{r['box_w']:>7.1f}{vol_txt:>7}{rsi_txt:>6}"
              f"{('✅' if r['above_ma20'] else '❌'):>7}{r['score']:>4}  {_verdict(r['score'])}")

    print()
    if suppressed:
        print(f"  ⚠️ 已按板块广度 regime 抑制 {suppressed} 只（牛市禁左侧埋伏——广度高时超跌是真弱）")
    print("  说明:")
    print("    - 板块=成分归属（指数名/行业名）；距低点%=现价距近60日最低收盘（0%=正贴底，≤8%才算止跌未涨离）")
    print("    - 磨底天数=距该低点的交易日数（≥10 才算磨底，排除下跌中继）")
    print("    - 箱宽%=近60日真实高低区间宽度（≤20% = 窄幅横盘）")
    print("    - 缩量比=近20日均量/近120日均量（<0.8 缩量打底，<0.6 地量更佳）")
    print("    - RSI>65 或已进入启动/主升/赶顶期 = 已启动，不作为磨底列入")
    print("    - 分级：✅强(≥75) / ⚠️中(55~74) / 🔸弱(<55)，仅供初筛")
    print("    - 磨底是「止跌后的左侧埋伏」，仍可能破位补跌；买点/止损/基本面对单只运行 stock-analysis 或 golden-pit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
