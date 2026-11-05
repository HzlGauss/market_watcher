#!/usr/bin/env python3
"""收盘复盘数据包：读 holdings.csv 里 show_flow=1 的标的，逐只拉「今日快照 + 技术面 + 资金流」，
输出紧凑复盘块，供 AI 生成短期操作结论并写入 memory（见 SKILL.md）。

轻量版：不拉基本面/估值分位/研报/筹码（那些留给 stock-analysis 单只深挖），
聚焦收盘复盘真正需要的三件事——今日涨跌、趋势阶段、主力资金方向。
个股走「主力=超大单+大单」口径；ETF 走「主力净流入 + 净申购额」申赎口径。

用法:
    py .claude/skills/closing-recap/recap_holdings.py
"""
import csv
import re
import sys
from datetime import date
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/closing-recap 上三级：closing-recap -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient
from app.models import Quote
from app.helpers import _detect_market
from app import technical as T

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- 解析工具

def _num(value):
    """从带后缀的字符串（"89.66元"/"-3.716%"/"608万股"）中提取首个数字。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group()) if m else None


def _parse_amount(value):
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


def _row_main_net(r):
    """主力净流入：优先「主力净流入资金」字段，缺省用 超大单+大单 合成。"""
    main = _parse_amount(r.get("主力净流入资金"))
    if main is not None:
        return main
    sup = _parse_amount(r.get("超大单净流入资金"))
    lrg = _parse_amount(r.get("大单净流入资金"))
    if sup is None and lrg is None:
        return None
    return (sup or 0.0) + (lrg or 0.0)


def _norm_date(s):
    """日期归一化："2026-08-21(日)" / "2026-08-24 13:07" -> "2026-08-21" """
    m = _DATE_RE.search(str(s or ""))
    return m.group(1) if m else ""


def _fmt_amt(v):
    """金额自适应格式化（亿/万/元），None -> --。"""
    if v is None:
        return "  --"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:+.0f}万"
    return f"{v:+.0f}元"


def _fmt_price(v):
    """价格 -> 两位小数字符串（None -> --）。"""
    return f"{v:.2f}" if v is not None else "  --"


def _is_etf(code):
    """按代码前缀识别场内 ETF/LOF（沪 51/56/58、深 15/16/18）。"""
    c = (code or "").strip()
    return len(c) == 6 and c.startswith(("51", "56", "58", "15", "16", "18"))


def _board_desc(code):
    """按代码前缀识别板块。"""
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


def _first_val(tables, *keys):
    """从 query_structured 返回的多张表里，找首个列名含 keys 之一的非空数值。"""
    for t in tables:
        cols = t.get("columns") or []
        rows = t.get("rows") or []
        for col in cols:
            if any(k in col for k in keys):
                for r in rows:
                    v = _num(r.get(col))
                    if v is not None:
                        return v
    return None


# ---------------------------------------------------------------- 取数

def load_holdings(path):
    """读 holdings.csv，返回 show_flow=1 的标的列表（name/code/amount/cost）。"""
    items = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if str(row.get("show_flow", "0")).strip() != "1":
                    continue
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                items.append({
                    "name": (row.get("name") or "").strip(),
                    "code": code,
                    "amount": (row.get("amount") or "").strip(),
                    "cost": (row.get("cost") or "").strip(),
                })
    except FileNotFoundError:
        print(f"⚠️ 未找到 {path}")
    return items


def _local_close_drift(code):
    """本地收盘快照：state/close_drift_latest.json（close-drift skill 收盘后落库）。
    返回 (收盘价 c1500, 收盘涨跌幅 day_pct%)，未命中返回 (None, None)。"""
    p = _ROOT / "state" / "close_drift_latest.json"
    if not p.exists():
        return None, None
    try:
        import json
        row = (json.loads(p.read_text(encoding="utf-8")).get("rows") or {}).get(code)
        if not row:
            return None, None
        return row.get("c1500"), row.get("day_pct")
    except Exception:
        return None, None


def _local_flow_last(code):
    """本地当日资金流最后一条快照：data/fund_flow_intraday.duckdb（东财源）。
    返回 dict（main_net/…/price/change_pct/ts/source），未命中返回 None。
    注意是盘中累计到某时刻（盯盘线程最后落库），非全天口径。"""
    try:
        import duckdb
        con = duckdb.connect(str(_ROOT / "data" / "fund_flow_intraday.duckdb"), read_only=True)
        try:
            row = con.execute(
                "SELECT main_net, super_large_net, large_net, medium_net, small_net, "
                "price, change_pct, pre_close, ts, source "
                "FROM fund_flow_intraday WHERE code = ? ORDER BY ts DESC LIMIT 1",
                [code],
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        cols = ("main_net", "super_large_net", "large_net", "medium_net", "small_net",
                "price", "change_pct", "pre_close", "ts", "source")
        return dict(zip(cols, row))
    except Exception:
        return None


def _today_snapshot(mx, code, name):
    """今日收盘价 + 涨跌幅：本地 close_drift(真收盘) → 本地资金流快照(盘中) → 妙想。
    返回 (price, chg, source)。"""
    price, chg = _local_close_drift(code)
    if price is not None and chg is not None:
        return price, chg, "close_drift"
    fl = _local_flow_last(code)
    if fl and fl.get("price") is not None and fl.get("change_pct") is not None:
        return fl["price"], fl["change_pct"], "fundflow"
    tables = mx.query_structured(f"{code} {name} 最新价 涨跌幅".strip())
    price = _first_val(tables, "最新价", "收盘价")
    chg = _first_val(tables, "涨跌幅")
    return price, chg, "miaoxiang"


def _fetch_klines(code, market, days=60):
    """本地前复权日K（本地 duckdb 优先，缺则远端兜底），失败返回 None。"""
    try:
        from app.kline_local import fetch_daily_local_first
        return fetch_daily_local_first(code, market, days) or None
    except Exception:
        return None


def _fetch_stock_flow(mx, code, name):
    """个股近 30 日 5 档资金流 → [(日期降序, row_dict)]，失败返回 []。"""
    q = (f"{code} {name} 近45日 资金流向 "
         f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入").strip()
    _TIER_KEYS = ("主力净流入资金", "超大单净流入资金", "大单净流入资金", "中单净流入资金", "小单净流入资金")
    merged = {}
    for t in mx.query_structured(q):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            if not d:
                continue
            row = merged.setdefault(d, {})
            for k, v in r.items():
                k = str(k)
                if k.startswith("(") and ")" in k:  # 剥「(区间)」前缀
                    k = k.split(")", 1)[1]
                if k in _TIER_KEYS and row.get(k) is None:
                    row[k] = v
    flow_rows = []
    for d, row in merged.items():
        main = _row_main_net(row)
        if main is None:
            continue
        row.setdefault("主力净流入资金", main)
        flow_rows.append((d, row))
    flow_rows.sort(key=lambda x: x[0], reverse=True)
    return flow_rows[:30]


def _flow_summary(flow_rows):
    """个股资金流摘要：今日主力(元) / 近5日累计(元) / 连续天数+方向。"""
    if not flow_rows:
        return None, None, 0, 0
    today_main = _parse_amount(flow_rows[0][1].get("主力净流入资金"))
    cum5 = sum(_parse_amount(r.get("主力净流入资金")) or 0 for _, r in flow_rows[:5])
    sign = consec = 0
    for _, r in flow_rows:
        m = _parse_amount(r.get("主力净流入资金"))
        s = 1 if (m and m > 0) else (-1 if (m and m < 0) else 0)
        if s == 0:
            break
        if sign == 0:
            sign, consec = s, 1
        elif s == sign:
            consec += 1
        else:
            break
    return today_main, cum5, consec, sign


def _fetch_etf_flow(mx, code, name):
    """ETF 资金流：主力净流入 + 净申购额（申赎口径），返回 (main_net, net_subscribe)。"""
    tables = mx.query_structured(f"{code} {name} 主力资金净流入 净申购额".strip())
    main_net = sub = None
    for t in tables:
        cols = t.get("columns") or []
        rows = t.get("rows") or []
        if not rows:
            continue
        main_col = next((c for c in cols if "主力净流入" in c), None)
        sub_col = next((c for c in cols if "净申购" in c or "申赎" in c), None)
        row = rows[0]
        if main_col and main_net is None:
            main_net = _parse_amount(row.get(main_col))
        if sub_col and sub is None:
            sub = _parse_amount(row.get(sub_col))
    return main_net, sub


# ---------------------------------------------------------------- 输出

def _print_block(it, board, is_etf, price, chg, snap_src, klines, ts, box, stage, flow):
    """打印单标复盘块（紧凑）。"""
    print("─" * 60)
    hold = f"持仓 {it['amount']}@{it['cost']}" if it["amount"] else "空仓观察"
    kind = "ETF" if is_etf else board
    price_s = _fmt_price(price) if price is not None else "  --"
    chg_s = f" ({chg:+.2f}%)" if chg is not None else ""
    if snap_src == "fundflow":
        chg_s += "·盘中"
    print(f"{it['name']} {it['code']} | {kind} | {hold} | 收 {price_s}{chg_s}")

    if klines:
        closes = [k.close for k in klines if k.close is not None]
        ma_vals = {}
        for p in (5, 10, 20, 60):
            sma = T.calc_sma(closes, p)
            ma_vals[p] = round(sma[-1], 3) if sma and sma[-1] is not None else None
        ma_txt = " ".join(f"MA{p}={_fmt_price(ma_vals[p])}" for p in (5, 10, 20, 60))
        if ts and ts.ma_alignment and ts.ma_alignment != "数据不足":
            ma_line = f"{ts.ma_alignment}"
        else:
            ma_line = "均线数据不足"
        print(f"  {ma_line} | {ma_txt}")
        if ts:
            rsi = f"RSI{_fmt_price(ts.rsi)}{ts.rsi_signal or ''}"
            macd = f"MACD{ts.macd_signal or ''}"
            kdj = f"KDJ{ts.kdj_signal or ''}"
            print(f"  {rsi} {macd} {kdj} | 支撑{_fmt_price(ts.support)}/压力{_fmt_price(ts.resistance)}")
        if box and box.regime and box.regime != "数据不足":
            pos = f"现价{box.pos_pct:.0f}%" if box.pos_pct is not None else ""
            print(f"  箱体[{_fmt_price(box.lower)},{_fmt_price(box.upper)}]{pos}")
        if stage and stage.stage != "数据不足":
            print(f"  阶段:{stage.stage}({stage.confidence}%) 趋势{stage.trend_axis} 量能{stage.volume_axis} 动量{stage.momentum_axis}")
            if stage.reasons:
                print(f"    判据:" + "；".join(stage.reasons))
    else:
        print("  ⚠️ 无 K 线数据（技术面缺失）")

    # 资金流
    today_main, cum5, consec, sign = flow.get("today_main"), flow.get("cum5"), flow.get("consec", 0), flow.get("consec_sign", 0)
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
        print(f"  资金: 主力 {_fmt_amt(today_main)}{src_txt} | 净申购 {_fmt_amt(sub)}")
    else:
        parts = [f"今日主力 {_fmt_amt(today_main)}{src_txt}"]
        if cum5:
            parts.append(f"近5日 {_fmt_amt(cum5)}")
        if consec:
            parts.append(f"连{'流入' if sign == 1 else '流出'}{consec}日")
        line = "  资金: " + " | ".join(parts)
        # 背离：今日价 vs 今日主力
        if today_main is not None and chg is not None:
            if today_main > 0 and chg < 0:
                line += " ⚠️价跌主力吸筹"
            elif today_main < 0 and chg > 0:
                line += " ⚠️价涨主力出货"
        print(line)
    print()


def main():
    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    items = load_holdings(_ROOT / "holdings.csv")
    if not items:
        print("⚠️ holdings.csv 中无 show_flow=1 的标的")
        return 1

    mx = MXClient(api_keys)
    print(f"收盘复盘：holdings.csv show_flow=1 共 {len(items)} 个标的")
    print(f"数据日期：{date.today().strftime('%Y-%m-%d')}（技术面日K基于本地 duckdb，可能不含当日 bar）")
    print()

    summary = []
    for it in items:
        code, name = it["code"], it["name"]
        market = _detect_market(code)
        is_etf = _is_etf(code)
        board = _board_desc(code)

        price, chg, snap_src = _today_snapshot(mx, code, name)

        klines = _fetch_klines(code, market, 60)
        ts = box = stage = None
        last_close = None
        if klines:
            closes = [k.close for k in klines if k.close is not None]
            if closes:
                last_close = closes[-1]
                quote = Quote(code=code, name=name, price=last_close, open=klines[-1].open)
                try:
                    ts = T.get_technical_summary(quote, klines)
                except Exception:
                    ts = None
                try:
                    box = T.detect_box_regime(klines, last_close)
                except Exception:
                    box = None

        # 今日主力净流入：本地 fund_flow_intraday 最后一条优先（东财源，盘中累计），缺则妙想
        local_flow = _local_flow_last(code)
        local_main = None
        if local_flow and local_flow.get("main_net") is not None:
            _ts = local_flow.get("ts")
            # 收盘复盘口径：本地快照须覆盖到尾盘（≥14:45），否则盘中累计失真（东财反爬致部分标的中午停更），回退妙想全天口径
            if _ts is not None and _ts.hour * 60 + _ts.minute >= 14 * 60 + 45:
                local_main = local_flow["main_net"]
        # 资金流（个股/ETF 分口径）
        if is_etf:
            main_net, sub = _fetch_etf_flow(mx, code, name)
            main_src, main_ts = "妙想", None
            if local_main is not None:
                main_net, main_src, main_ts = local_main, "本地", local_flow.get("ts")
            flow = {"today_main": main_net, "net_subscribe": sub, "cum5": None, "consec": 0,
                    "consec_sign": 0, "main_src": main_src, "main_ts": main_ts}
            main_inflow_1d = main_net / 1e8 if main_net is not None else None
            main_inflow_5d = None
        else:
            flow_rows = _fetch_stock_flow(mx, code, name)
            today_main, cum5, consec, sign = _flow_summary(flow_rows)
            main_src, main_ts = "妙想", None
            if local_main is not None:
                today_main, main_src, main_ts = local_main, "本地", local_flow.get("ts")
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
                    price_change_1d=chg,
                )
            except Exception:
                stage = None

        _print_block(it, board, is_etf, price, chg, snap_src, klines, ts, box, stage, flow)

        stage_txt = stage.stage if (stage and stage.stage != "数据不足") else "—"
        summary.append((code, name, "ETF" if is_etf else board, chg, flow.get("today_main"), stage_txt))

    # 汇总表
    print("=" * 60)
    print("汇总：代码 名称 | 类型 | 今涨跌 | 今日主力 | 阶段")
    print("=" * 60)
    for code, name, kind, chg, today_main, stage_txt in summary:
        chg_s = f"{chg:+.2f}%" if chg is not None else "  --"
        print(f"{code} {name} | {kind} | {chg_s} | {_fmt_amt(today_main)} | {stage_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
