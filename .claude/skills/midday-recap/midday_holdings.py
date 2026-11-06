#!/usr/bin/env python3
"""中午复盘数据包：读 holdings.csv 里 show_flow=1 的标的，逐只拉「上午快照 + 技术面 + 上午资金流 + 下午操作(加减仓+做T)」，
输出上午总结块 + 下午操作建议，供 AI 生成下午操作结论并写入 memory（覆盖 holdings-closing-recap.md，见 SKILL.md）。

与收盘复盘(closing-recap)的三处差异：
1. 数据口径 = 上午半日（11:30 上午收盘价 + 上午累计资金流），非全天/15:00 定盘；
2. 多一层【大盘环境】（核心指数 + 全市场广度），下午操作更依赖大盘氛围；
3. 多一段【下午操作】：加减仓信号(evaluate_position_signal) + 做T信号(evaluate_t0_measure)，
   直接给下午的加/减/做T挂单价。

轻量版：不拉基本面/估值分位/研报/筹码。复用 closing-recap 的解析/取数函数 + intraday-signal 的评估引擎，
避免三套脚本逻辑漂移。

用法:
    py .claude/skills/midday-recap/midday_holdings.py

午间休市（11:30–13:00）跑。上午收盘价用实时快照(新浪/腾讯)，上午主力用本地 fund_flow_intraday 最后一条
（须覆盖到 11:15 之后，否则回退妙想），近5日/连续天数/ETF净申购走妙想兜底。
"""
import sys
from datetime import date, datetime
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/midday-recap 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 复用 closing-recap 的解析/取数函数（避免两套脚本逻辑漂移）
_CR_DIR = _ROOT / ".claude" / "skills" / "closing-recap"
sys.path.insert(0, str(_CR_DIR))
import recap_holdings as CR

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient
from app.models import WatchItem, Quote
from app.helpers import _detect_market
from app.data_fetcher import fetch_quotes, fetch_major_indices, _fetch_em_clist
from app import technical as T
from app.technical import fetch_historical_kline
from app.t0_monitor import evaluate_position_signal, evaluate_t0_measure

# 大盘环境层常量（与 intraday-signal 一致）
_FS_ALL_A = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"   # 东财 clist 全 A 股
_EM_BREADTH_FIELDS = "f12,f14,f3,f6"               # f3=涨跌幅 f6=成交额
_ENV_INDICES = ("上证指数", "深证成指", "创业板指", "沪深300")

# 上午主力快照须覆盖到该时刻之后（上午收盘 11:30 前 15 分钟，与收盘复盘 14:45 对称），
# 否则东财反爬致部分标的中午停更、上午累计失真，自动回退妙想口径
_MORNING_FLOW_GATE_MIN = 11 * 60 + 15


# ---------------------------------------------------------------- 大盘环境

def _market_stance(idx_chgs, up_ratio):
    """由核心指数平均涨跌 + 上涨占比，给环境强弱标签（过滤仓位用，非硬信号）。"""
    if not idx_chgs and up_ratio is None:
        return "数据不足"
    avg = sum(idx_chgs) / len(idx_chgs) if idx_chgs else None
    parts = []
    if avg is not None:
        if avg >= 0.5:
            parts.append("指数偏强")
        elif avg <= -0.5:
            parts.append("指数偏弱")
        else:
            parts.append("指数平盘")
    if up_ratio is not None:
        if up_ratio >= 60:
            parts.append("普涨")
        elif up_ratio <= 35:
            parts.append("普跌")
        else:
            parts.append("分化")
    return " / ".join(parts) if parts else "数据不足"


def _print_market_env():
    """【0 大盘环境】核心指数涨跌 + 全市场广度（涨跌家数/成交额）。失败静默降级，不阻塞个股。"""
    print()
    print("=" * 60)
    print("【0. 大盘环境（环境过滤层，非买卖信号）】")
    print("=" * 60)
    idx_chgs, idx_parts = [], []
    try:
        for q in fetch_major_indices():
            if q.name in _ENV_INDICES:
                idx_parts.append(f"{q.name} {q.change_pct:+.2f}%" if q.change_pct is not None else f"{q.name} --")
                if q.change_pct is not None:
                    idx_chgs.append(q.change_pct)
    except Exception:
        idx_parts = []

    up = down = flat = 0
    total_amount = 0.0
    try:
        for it in _fetch_em_clist(_FS_ALL_A, _EM_BREADTH_FIELDS, fid="f3"):
            chg = CR._num(it.get("f3"))
            if chg is None:
                continue
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
            else:
                flat += 1
            total_amount += CR._num(it.get("f6")) or 0.0
    except Exception:
        pass

    if not idx_parts and not (up + down + flat):
        print("  ⚠️ 大盘环境数据不可达（跳过环境层）")
        return
    if idx_parts:
        print("  指数: " + "  ".join(idx_parts))
    total = up + down + flat
    if total:
        if total < 4500:
            print(f"  ⚠️ 广度数据不完整（仅 {total} 只，分页截断），上涨占比不可用")
        else:
            ratio = up / total * 100
            print(f"  广度: 上涨 {up} / 下跌 {down} / 平盘 {flat}  （上涨占比 {ratio:.0f}%）"
                  f"  成交额 {total_amount / 1e8:.0f} 亿")
            print(f"  环境强弱: {_market_stance(idx_chgs, ratio)}")


# ---------------------------------------------------------------- 取数

def _ts_datetime(ts):
    """duckdb 的 ts 字段归一化为 datetime（datetime/date/str 兼容）。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, date):
        return datetime.combine(ts, datetime.min.time())
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _fetch_daily(code, market, days=60):
    """日K：本地 duckdb + 远端补当日缺口（hybrid），失败回退 local_first。"""
    try:
        from app import kline_local
        out = kline_local.fetch_daily_hybrid(code, market, days=days)
        if out:
            return out
    except Exception:
        pass
    return CR._fetch_klines(code, market, days)


# ---------------------------------------------------------------- 输出

def _print_block(it, board, is_etf, q, klines, ts, box, stage, flow, pos_sig, t0):
    """打印单标复盘块：上午总结 + 上午资金 + 下午操作。"""
    print("─" * 60)
    hold = f"持仓 {it['amount']}@{it['cost']}" if it["amount"] else "空仓观察"
    kind = "ETF" if is_etf else board
    price = q.price if q else None
    chg = q.change_pct if q else None
    price_s = CR._fmt_price(price) if price is not None else "  --"
    chg_s = f" ({chg:+.2f}%)" if chg is not None else ""
    print(f"{it['name']} {it['code']} | {kind} | {hold} | 上午收 {price_s}{chg_s}")

    if klines:
        closes = [k.close for k in klines if k.close is not None]
        ma_vals = {}
        for p in (5, 10, 20, 60):
            sma = T.calc_sma(closes, p)
            ma_vals[p] = round(sma[-1], 3) if sma and sma[-1] is not None else None
        ma_txt = " ".join(f"MA{p}={CR._fmt_price(ma_vals[p])}" for p in (5, 10, 20, 60))
        ma_line = ts.ma_alignment if (ts and ts.ma_alignment and ts.ma_alignment != "数据不足") else "均线数据不足"
        print(f"  {ma_line} | {ma_txt}")
        if ts:
            rsi = f"RSI{CR._fmt_price(ts.rsi)}{ts.rsi_signal or ''}"
            macd = f"MACD{ts.macd_signal or ''}"
            kdj = f"KDJ{ts.kdj_signal or ''}"
            print(f"  {rsi} {macd} {kdj} | 支撑{CR._fmt_price(ts.support)}/压力{CR._fmt_price(ts.resistance)}")
        if box and box.regime and box.regime != "数据不足":
            pos = f"现价{box.pos_pct:.0f}%" if box.pos_pct is not None else ""
            print(f"  箱体[{CR._fmt_price(box.lower)},{CR._fmt_price(box.upper)}]{pos}")
        if stage and stage.stage != "数据不足":
            print(f"  阶段:{stage.stage}({stage.confidence}%) 趋势{stage.trend_axis} 量能{stage.volume_axis} 动量{stage.momentum_axis}")
            if stage.reasons:
                print(f"    判据:" + "；".join(stage.reasons))
    else:
        print("  ⚠️ 无 K 线数据（技术面缺失）")

    # 上午资金流
    today_main = flow.get("today_main")
    cum5 = flow.get("cum5")
    consec = flow.get("consec", 0)
    sign = flow.get("consec_sign", 0)
    main_ts = flow.get("main_ts")
    main_src = flow.get("main_src")
    src_txt = ""
    if main_src == "本地" and main_ts is not None:
        try:
            src_txt = f"·截至{main_ts:%H:%M}"
        except (TypeError, ValueError):
            src_txt = "·本地"
    elif main_src == "本地":
        src_txt = "·本地"
    if is_etf:
        sub = flow.get("net_subscribe")
        print(f"  资金: 上午主力 {CR._fmt_amt(today_main)}{src_txt} | 净申购 {CR._fmt_amt(sub)}")
    else:
        parts = [f"上午主力 {CR._fmt_amt(today_main)}{src_txt}"]
        if cum5:
            parts.append(f"近5日 {CR._fmt_amt(cum5)}")
        if consec:
            parts.append(f"连{'流入' if sign == 1 else '流出'}{consec}日")
        line = "  资金: " + " | ".join(parts)
        if today_main is not None and chg is not None:
            if today_main > 0 and chg < 0:
                line += " ⚠️价跌主力吸筹"
            elif today_main < 0 and chg > 0:
                line += " ⚠️价涨主力出货"
        print(line)

    # 下午操作（加减仓 + 做T）
    if pos_sig is not None:
        sugg = ""
        if pos_sig.suggested_high > pos_sig.suggested_low > 0 and (pos_sig.suggested_high - pos_sig.suggested_low) >= 0.005:
            sugg = f" 挂单 {pos_sig.suggested_low:.2f}~{pos_sig.suggested_high:.2f}"
        elif pos_sig.suggested_price > 0:
            sugg = f" 挂单 {pos_sig.suggested_price:.2f}"
        reg = f" [{pos_sig.regime}]" if getattr(pos_sig, "regime", "") else ""
        print(f"  [下午] 加减仓: {pos_sig.action_label} 阶段[{pos_sig.stage}]{reg} 置信{pos_sig.confidence_label}({pos_sig.confidence}%){sugg}")
        if pos_sig.reasons:
            print(f"      └─ {'；'.join(pos_sig.reasons)}")
    else:
        print("  [下午] 加减仓: 无信号（主升浪/震荡市/数据不足，持股为主）")
    if t0 is not None:
        if t0.get("suitable"):
            print(f"  [下午] 做T: ✅ 适合  买 {t0['buy_price']:.3f} / 卖 {t0['sell_price']:.3f}"
                  f"（振幅{t0['amp']:.2f}% {t0.get('ma_align') or ''}）")
        else:
            print(f"  [下午] 做T: ❌ 不适合（{'；'.join(t0.get('reasons') or ['无'])}）")
    else:
        print("  [下午] 做T: 无数据（缺 5 分钟 K 线）")
    print()


def main():
    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("⚠️ 未配置 MX_APIKEY：近5日/连续天数/ETF净申购缺失，其余（快照/技术面/上午主力/加减仓/做T）仍可跑")

    items = CR.load_holdings(_ROOT / "holdings.csv")
    if not items:
        print("⚠️ holdings.csv 中无 show_flow=1 的标的")
        return 1

    mx = MXClient(api_keys) if api_keys else None
    print(f"中午复盘：holdings.csv show_flow=1 共 {len(items)} 个标的")
    print(f"数据日期：{date.today().strftime('%Y-%m-%d')}（上午半日口径；日K基于本地 duckdb，可能不含当日 bar）")

    _print_market_env()

    # 批量拉实时快照（新浪+腾讯），作为上午收盘价 + 加减仓/做T 的输入
    watch = []
    for it in items:
        market = _detect_market(it["code"])
        is_etf = CR._is_etf(it["code"])
        watch.append(WatchItem(name=it["name"], code=it["code"], market=market,
                               type="ETF" if is_etf else "个股"))
    quotes = fetch_quotes(watch) or []
    quote_map = {q.code: q for q in quotes}

    summary = []
    for it, item in zip(items, watch):
        code, name = it["code"], it["name"]
        market = _detect_market(code)
        is_etf = CR._is_etf(code)
        board = CR._board_desc(code)
        q = quote_map.get(code)

        # 日K + 5分钟K
        klines = _fetch_daily(code, market, 60)
        min_klines = None
        if q is not None and (q.price or 0) > 0:
            try:
                min_klines = fetch_historical_kline(code, market, days=2, scale=5)
            except Exception:
                min_klines = None

        # 技术面（阶段/箱体用上午实时价，非昨日收盘）
        ts = box = stage = None
        if klines:
            closes = [k.close for k in klines if k.close is not None]
            if closes:
                cur_price = q.price if (q and q.price) else closes[-1]
                quote = Quote(code=code, name=name, price=cur_price, open=klines[-1].open)
                try:
                    ts = T.get_technical_summary(quote, klines)
                except Exception:
                    ts = None
                try:
                    box = T.detect_box_regime(klines, cur_price)
                except Exception:
                    box = None

        # 上午主力净流入：本地 fund_flow_intraday 最后一条优先（东财源，上午累计），须覆盖到 11:15 之后
        local_flow = CR._local_flow_last(code)
        local_main = None
        main_src, main_ts = "妙想", None
        if local_flow and local_flow.get("main_net") is not None:
            _ts = _ts_datetime(local_flow.get("ts"))
            if _ts is not None and _ts.date() == date.today() and _ts.hour * 60 + _ts.minute >= _MORNING_FLOW_GATE_MIN:
                local_main = local_flow["main_net"]
                main_src, main_ts = "本地", _ts

        # 资金流（个股/ETF 分口径）
        if is_etf:
            main_net, sub = (mx and CR._fetch_etf_flow(mx, code, name)) or (None, None)
            if local_main is not None:
                main_net = local_main
            flow = {"today_main": main_net, "net_subscribe": sub, "cum5": None, "consec": 0,
                    "consec_sign": 0, "main_src": main_src, "main_ts": main_ts}
            main_inflow_1d = main_net / 1e8 if main_net is not None else None
            main_inflow_5d = None
        else:
            flow_rows = mx and CR._fetch_stock_flow(mx, code, name) or []
            today_main, cum5, consec, sign = CR._flow_summary(flow_rows)
            if local_main is not None:
                today_main = local_main
            flow = {"today_main": today_main, "cum5": cum5, "consec": consec, "consec_sign": sign,
                    "main_src": main_src, "main_ts": main_ts}
            main_inflow_1d = today_main / 1e8 if today_main is not None else None
            main_inflow_5d = cum5 / 1e8 if cum5 else None

        # 阶段定位（含资金轴）
        if klines and ts:
            try:
                stage = T.detect_stage(
                    klines, ts,
                    main_inflow_1d=main_inflow_1d,
                    main_inflow_5d=main_inflow_5d,
                    price_change_1d=q.change_pct if q else None,
                )
            except Exception:
                stage = None

        # 下午操作：加减仓 + 做T（复用 intraday-signal 引擎）
        pos_sig = t0 = None
        if q is not None and (q.price or 0) > 0:
            try:
                pos_sig = evaluate_position_signal(item, q, klines, min_klines)
            except Exception:
                pos_sig = None
            if min_klines:
                try:
                    t0 = evaluate_t0_measure(q, min_klines)
                except Exception:
                    t0 = None

        _print_block(it, board, is_etf, q, klines, ts, box, stage, flow, pos_sig, t0)

        stage_txt = stage.stage if (stage and stage.stage != "数据不足") else "—"
        pos_txt = pos_sig.action_label if pos_sig else "—"
        summary.append((code, name, "ETF" if is_etf else board, q.change_pct if q else None,
                        flow.get("today_main"), stage_txt, pos_txt))

    # 汇总表
    print("=" * 60)
    print("汇总：代码 名称 | 类型 | 上午涨跌 | 上午主力 | 阶段 | 加减仓")
    print("=" * 60)
    for code, name, kind, chg, today_main, stage_txt, pos_txt in summary:
        chg_s = f"{chg:+.2f}%" if chg is not None else "  --"
        print(f"{code} {name} | {kind} | {chg_s} | {CR._fmt_amt(today_main)} | {stage_txt} | {pos_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
