#!/usr/bin/env python3
"""个股分析数据包：用妙想（Miaoxiang）拉取个股的基本信息、近 N 日 K 线、
近 5 日资金流、近 7 日资讯，并复用 technical.py 计算技术面（RSI/MACD/KDJ/BOLL/
支撑压力/均线排列/箱体），再补筹码（机构持股+股东户数）、基本面（财报+预测+分红）、
估值分位（PE/PB 三年百分位）、最近研报（评级+一致预期）。

本脚本只负责「取数 + 算指标 + 信号化」，输出一份结构化数据包；走势预测与买卖
参考由 AI 依据 SKILL.md 的分析框架在脚本输出之上生成。

用法:
    py .claude/skills/stock-analysis/analyze_stock.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股代码（必填）
    名称   股票名称（可选，提升查询精度）
    天数   拉取的日 K 线根数（可选，默认 30，上限 60，建议 60 以覆盖 MA60/箱体）

输出: 分 8 段——基本信息(含板块识别) / K线+技术面 / 资金流(含背离) /
筹码与机构持股 / 基本面(含分红) / 估值分位 / 最近研报(含一致预期) / 资讯与事件风险。
"""
import re
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/stock-analysis 上三级：stock-analysis -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient
from app.models import KlineData, Quote
from app import technical as T

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


def _fmt_price(v) -> str:
    """价格 -> 两位小数字符串（None -> --）。"""
    return f"{v:.2f}" if v is not None else "  --"


def _belongs_to_stock(item: dict, name: str, code: str) -> bool:
    """资讯是否真正关联该证券：优先 secu_list 代码/简称匹配，缺失时退化标题/全称包含。"""
    secus = item.get("secu_list") or []
    if secus:
        for s in secus:
            c = (s.get("code") or "").replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "")
            if c == code or s.get("name") == name:
                return True
        return False
    hay = (item.get("title") or "") + (item.get("entity_full_name") or "")
    return (name and name in hay) or (code and code in hay)


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


def _is_etf(code: str) -> bool:
    """按代码前缀识别场内 ETF/LOF（沪 51/56/58、深 15/16/18）。"""
    c = (code or "").strip()
    return len(c) == 6 and c.startswith(("51", "56", "58", "15", "16", "18"))


def _rating_bucket(rating: str) -> str:
    """研报评级 -> 看多/中性/看空 三档。"""
    r = rating or ""
    if any(k in r for k in ("买入", "增持", "强推", "推荐", "Buy", "Accumulate", "Outperform", "Overweight")):
        return "看多"
    if any(k in r for k in ("减持", "卖出", "Sell", "Reduce", "Underperform", "Underweight")):
        return "看空"
    if any(k in r for k in ("中性", "持有", "Neutral", "Hold", "观望", "Equal")):
        return "中性"
    return "看多"


def _query_index_name(mx: MXClient, code: str, name: str) -> str:
    """查询 ETF 的跟踪指数名称（如「沪深300」），供指数估值分位查询使用。"""
    for t in mx.query_structured(f"{code} {name} 跟踪指数名称".strip()):
        for r in t.get("rows") or []:
            v = r.get("跟踪指数名称")
            if v and str(v).strip() and str(v).strip() != "-":
                return str(v).strip()
    return ""


def _to_klines(rows: list) -> list[KlineData]:
    """妙想 K 线 rows[(日期, raw_row, close)] -> KlineData 列表（按日期升序）。"""
    klines = []
    for d, r, close in rows:
        klines.append(KlineData(
            date=d,
            open=_num(r.get("开盘价")),
            high=_num(r.get("最高价")),
            low=_num(r.get("最低价")),
            close=close,
            volume=_num(r.get("成交量")),
        ))
    return klines


# ---------------------------------------------------------------- 取数

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 30
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(5, min(int(a), 60))
        elif a:
            name = a
    return code, name, days


def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/stock-analysis/analyze_stock.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)
    board = _board_desc(code)
    is_etf = _is_etf(code)
    index_name = _query_index_name(mx, code, name) if is_etf else ""

    # ---- 1. 基本信息 ----
    print("=" * 72)
    if is_etf:
        print(f"【1. 基本信息】{code} {name}（ETF）")
    else:
        print(f"【1. 基本信息】{code} {name}（{board}）")
    print("=" * 72)
    if is_etf:
        info = mx.query_as_text(
            f"{code} {name} 最新价 涨跌幅 成交额 基金规模 跟踪指数".strip()
        )
        print(info[:1200] if info else "⚠️ 未查到基本信息")
        if index_name:
            print(f"  跟踪指数: {index_name}")
    else:
        info = mx.query_as_text(
            f"{code} {name} 股票简称 最新价 涨跌幅 市盈率 总市值 所属行业".strip()
        )
        print(info[:1200] if info else "⚠️ 未查到基本信息")

    # ---- 2. 近 N 日 K 线 + 技术面 ----
    print()
    print("=" * 72)
    print(f"【2. 近 {days} 日 K 线 + 技术面】")
    print("=" * 72)
    rows = []
    for t in mx.query_structured(
        f"{code} {name} 近{days}个交易日 日K线 开盘价 收盘价 最高价 最低价 成交量 换手率".strip()
    ):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            close = _num(r.get("收盘价"))
            if not d or close is None:
                continue
            rows.append((d, r, close))

    last_close = last_open = last_pct = None
    if rows:
        rows.sort(key=lambda x: x[0])  # 日期升序
        closes = [c for _, _, c in rows]
        last_close = closes[-1]
        last_open = _num(rows[-1][1].get("开盘价"))
        last_pct = _num(rows[-1][1].get("涨跌幅"))
        rng30 = (closes[-1] - closes[0]) / closes[0] * 100 if len(closes) > 1 and closes[0] else None

        klines = _to_klines(rows)
        quote = Quote(code=code, name=name, price=last_close, open=last_open)

        # 复用 technical.py 的一站式技术汇总 + 箱体判定
        try:
            ts = T.get_technical_summary(quote, klines)
        except Exception as e:
            ts = None
            print(f"  ⚠️ 技术指标计算失败: {e}")
        try:
            box = T.detect_box_regime(klines, last_close)
        except Exception:
            box = None

        if ts is not None:
            # MA 值独立计算：calc_ma_alignment 在 <60 根时整体返回 None，这里用 calc_sma 逐条算，
            # 保证 30 根 K 线也能拿到 MA5/10/20（仅 MA60 缺值）
            ma_vals = {}
            for p in (5, 10, 20, 60):
                sma = T.calc_sma(closes, p)
                ma_vals[p] = round(sma[-1], 3) if sma and sma[-1] is not None else None
            print(f"  收盘 {last_close:.2f}  |  MA5 {_fmt_price(ma_vals[5])}  MA10 {_fmt_price(ma_vals[10])}"
                  f"  MA20 {_fmt_price(ma_vals[20])}  MA60 {_fmt_price(ma_vals[60])}")
            if ts.ma_alignment and ts.ma_alignment != "数据不足":
                print(f"  均线排列: {ts.ma_alignment}" + (f"（{ts.ma_alignment_detail}）" if ts.ma_alignment_detail else ""))
            else:
                print(f"  均线排列: 数据不足（MA60 需 60 根 K 线，当前 {len(closes)} 根）")
            print(f"  RSI: {_fmt_price(ts.rsi)}（{ts.rsi_signal or '—'}）")
            print(f"  MACD: DIF {_fmt_price(ts.macd_dif)} / DEA {_fmt_price(ts.macd_dea)}"
                  f" / 柱 {_fmt_price(ts.macd_histogram)}（{ts.macd_signal or '—'}）")
            print(f"  KDJ: K {_fmt_price(ts.kdj_k)} / D {_fmt_price(ts.kdj_d)} / J {_fmt_price(ts.kdj_j)}（{ts.kdj_signal or '—'}）")
            print(f"  BOLL: 上 {_fmt_price(ts.bb_upper)} / 中 {_fmt_price(ts.bb_middle)} / 下 {_fmt_price(ts.bb_lower)}（{ts.bb_signal or '—'}）")
            print(f"  支撑 {_fmt_price(ts.support)} / 压力 {_fmt_price(ts.resistance)}  |  ATR {_fmt_price(ts.atr)}")
            if ts.signals:
                print("  技术信号: " + " | ".join(ts.signals))

        if box is not None and box.regime and box.regime != "数据不足":
            pos = f"  现价位置 {box.pos_pct:.0f}%" if box.pos_pct is not None else ""
            print(f"  箱体: {box.regime}  区间 [{_fmt_price(box.lower)}, {_fmt_price(box.upper)}]{pos}")

        # 涨跌幅：查询结果常缺该字段，改由相邻收盘价计算
        row_pct = {}
        prev_c = None
        for d, r, c in rows:
            if prev_c:
                row_pct[d] = (c - prev_c) / prev_c * 100
            prev_c = c

        print(f"  区间涨跌幅: {rng30:+.2f}%  |  日期 开 高 低 收 涨跌幅 换手")
        for d, r, close in rows[-min(days, len(rows)):]:
            o = _num(r.get("开盘价")); h = _num(r.get("最高价")); l = _num(r.get("最低价"))
            pct = row_pct.get(d, _num(r.get("涨跌幅"))); tr = _num(r.get("换手率"))
            print(f"    {d}  {_fmt_price(o)} {_fmt_price(h)} {_fmt_price(l)} {_fmt_price(close)} "
                  f"{pct if pct is None else f'{pct:+.2f}%'}  {tr if tr is None else f'{tr:.2f}%'}")
    else:
        print("  ⚠️ 未查到 K 线数据")

    # ---- 3. 近 5 日资金流 + 背离 ----
    print()
    print("=" * 72)
    print("【3. 近 5 日资金流（亿元，+净流入 / -净流出）】")
    print("=" * 72)
    window = max(5, int(5 * 1.5) + 3)
    q = (f"{code} {name} 近{window}日 资金流向 "
         f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入").strip()
    flow_rows, seen = [], set()
    for t in mx.query_structured(q):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            main = _parse_amount(r.get("主力净流入资金"))
            if not d or main is None or d in seen:
                continue
            seen.add(d)
            flow_rows.append((d, r))
    if flow_rows:
        flow_rows.sort(key=lambda x: x[0], reverse=True)
        print("  日期          主力     超大单   大单     中单     小单")
        for d, r in flow_rows[:5]:
            cells = [_fmt_amount(_parse_amount(r.get(k))) for k in
                     ("主力净流入资金", "超大单净流入资金", "大单净流入资金", "中单净流入资金", "小单净流入资金")]
            print(f"  {d}  " + "  ".join(cells))
        tot = sum((_parse_amount(r.get("主力净流入资金")) or 0) for _, r in flow_rows[:5])
        print(f"  → 5 日主力累计净流入: {tot / 1e8:+.2f} 亿")

        # 连续净流入/净流出天数
        sign, consec = None, 0
        for _, r in flow_rows:
            m = _parse_amount(r.get("主力净流入资金"))
            s = 1 if (m and m > 0) else (-1 if (m and m < 0) else 0)
            if s == 0:
                break
            if sign is None:
                sign, consec = s, 1
            elif s == sign:
                consec += 1
            else:
                break
        if consec:
            print(f"  → 连续{'净流入' if sign == 1 else '净流出'} {consec} 日")

        # 背离：最新日价 vs 主力方向
        latest_main = _parse_amount(flow_rows[0][1].get("主力净流入资金"))
        if latest_main is not None and last_pct is not None:
            if latest_main > 0 and last_pct < 0:
                print(f"  ⚠️ 背离：最新日价跌({last_pct:+.2f}%)但主力净流入 {latest_main / 1e8:+.2f} 亿（逆势吸筹）")
            elif latest_main < 0 and last_pct > 0:
                print(f"  ⚠️ 背离：最新日价涨({last_pct:+.2f}%)但主力净流出 {latest_main / 1e8:+.2f} 亿（拉高出货）")
    else:
        print("  ⚠️ 未查到资金流数据")

    # ---- 4. 筹码/机构持股（个股）| ETF 专属指标 ----
    print()
    print("=" * 72)
    if is_etf:
        print("【4. ETF 专属指标（折溢价率 / 份额 / 跟踪误差 / 规模）】")
        print("=" * 72)
        for t in mx.query_structured(f"{code} {name} 基金规模 最新份额 折溢价率 跟踪误差".strip()):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            if "折溢价率" in cols:
                has_share = "基金份额" in cols
                print("  折溢价率 / 基金份额（日粒度）:" if has_share else "  折溢价率（日粒度）:")
                for r in rows_[:3]:
                    line = f"    {r.get('日期', '')}: 折溢价 {r.get('折溢价率')}%"
                    if has_share and r.get("基金份额"):
                        line += f"  |  份额 {r.get('基金份额')}"
                    print(line)
            elif "跟踪误差" in cols:
                print("  跟踪误差（偏离基准越小越好）:")
                for r in rows_[:2]:
                    print(f"    {r.get('日期', '')}: {r.get('跟踪误差')}")
        for t in mx.query_structured(f"{code} {name} 基金规模 资产净值".strip()):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            if "基金资产净值" in "".join(cols):
                print("  基金规模（资产净值，报告期）:")
                for r in rows_[:4]:
                    nav = r.get("基金资产净值(合计值)") or r.get("资产净值(元)") or r.get("基金资产净值")
                    if nav:
                        print(f"    {r.get('日期', '')}: {nav}")
            elif cols and cols[0] == "日期" and len(cols) > 1:
                # 转置表：首列「日期」是行名（份额(份)/份额变动原因/资产净值(元)），其余列为报告期
                share_row = next((r for r in rows_ if "份额" in (r.get("日期") or "")), None)
                cause_row = next((r for r in rows_ if "变动原因" in (r.get("日期") or "")), None)
                if share_row:
                    print("  份额变化（报告期，申购/赎回）:")
                    for p in cols[1:5]:
                        c = f"（{cause_row.get(p)}）" if cause_row and cause_row.get(p) else ""
                        print(f"    {p}: {share_row.get(p) or '--'}{c}")
    else:
        print("【4. 筹码与机构持股（机构进出 + 股东户数）】")
        print("=" * 72)
        for t in mx.query_structured(f"{code} {name} 机构持股比例 股东户数".strip()):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            title = t.get("title") or ""
            if "机构持股" in title or any("机构持股" in c for c in cols):
                print("  机构持股比例合计（环比）:")
                prev = None
                for r in rows_[:3]:
                    d = r.get("日期", "")
                    v = _num(r.get("机构持股比例合计"))
                    chg = f"  (环比 {v - prev:+.2f}pp)" if (v is not None and prev is not None) else ""
                    print(f"    {d}: {r.get('机构持股比例合计') or '--'}{chg}")
                    if v is not None:
                        prev = v
            elif "股东户数" in title or any("股东户数" in c for c in cols):
                print("  股东户数（减少=筹码集中）:")
                nums = [(r.get("日期", ""), _parse_amount(r.get("股东户数"))) for r in rows_]
                nums = [(d, v) for d, v in nums if v is not None]
                if nums:
                    latest_d, latest_v = nums[0]
                    print(f"    最新 {latest_d}: {latest_v / 1e4:.2f} 万户")
                    if len(nums) >= 2:
                        older_d, older_v = nums[-1]
                        chg_pct = (latest_v - older_v) / older_v * 100 if older_v else None
                        if chg_pct is not None:
                            print(f"    区间 {older_d}→{latest_d}: {chg_pct:+.1f}%（户数减少=筹码趋于集中）")

    # ---- 5. 基本面 + 分红（个股）| 跟踪指数估值分位（ETF） ----
    print()
    print("=" * 72)
    if is_etf:
        print("【5. 跟踪指数估值分位（PE/PB + 历史百分位）】")
        print("=" * 72)
        idx_q = (f"{index_name} 指数 市盈率 市净率 历史百分位".strip() if index_name
                 else f"{code} {name} 跟踪指数 市盈率 市净率 历史百分位".strip())
        for t in mx.query_structured(idx_q):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            r = rows_[0]
            if "百分位" in "".join(cols):
                cells = [f"{c}={r.get(c)}" for c in cols if c != "日期" and r.get(c)]
                if cells:
                    print(f"  历史百分位: " + " | ".join(cells))
            elif "市盈率" in "".join(cols) or "市净率" in "".join(cols):
                cells = [f"{c}={r.get(c)}" for c in cols if c != "日期" and r.get(c)]
                if cells:
                    print(f"  当前估值: " + " | ".join(cells))
    else:
        print("【5. 基本面（最新财报 + 机构预测净利 + 分红）】")
        print("=" * 72)
        _FUND_COLS = ["净利润", "净利润同比增长率", "营业收入", "营业收入同比增长率",
                      "净资产收益率ROE(加权)", "销售毛利率", "资产负债率"]
        for t in mx.query_structured(
            f"{code} {name} 最新财报 净利润 营业收入 同比增长率 净资产收益率 资产负债率 毛利率".strip()
        ):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            if any("预测" in c for c in cols):
                print("  机构预测净利（中值）:")
                for r in rows_[:3]:
                    d = r.get("日期", "")
                    vals = [f"{c.split('(')[0]}={r.get(c)}" for c in cols if c != "日期" and r.get(c)]
                    print(f"    {d}: " + " | ".join(vals))
            else:
                picked = [c for c in _FUND_COLS if c in cols]
                if not picked:
                    picked = cols[:6]
                print("  报告期  " + "  ".join(picked))
                for r in rows_[:3]:
                    d = r.get("日期", "")
                    cells = [str(r.get(c)) if r.get(c) else "  --" for c in picked]
                    print(f"  {d}  " + "  ".join(cells))

        # 分红 / 股息率
        for t in mx.query_structured(f"{code} {name} 股息率 分红".strip()):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            # 分红方案表：分红方案 + 每股股利 + 分红总额 + 进度
            if "分红方案" in cols:
                print("  分红方案:")
                for r in rows_[:2]:
                    d = r.get("日期", "")
                    plan = r.get("分红方案") or "—"
                    div = _num(r.get("每股股利(税前,元)"))
                    tot = r.get("分红总额(元)")
                    progress = r.get("方案进度") or ""
                    extra = []
                    if div is not None:
                        extra.append(f"每股股利 {div:.3f} 元")
                        if last_close:
                            extra.append(f"股息率≈{div / last_close * 100:.2f}%")
                    if tot:
                        extra.append(f"总额 {tot}")
                    if progress:
                        extra.append(progress)
                    print(f"    {d}: {plan}  " + " | ".join(extra))

    # ---- 6. 估值分位（仅个股；ETF 见⑤跟踪指数分位） ----
    if not is_etf:
        print()
        print("=" * 72)
        print("【6. 估值分位（PE/PB + 3年历史百分位）】")
        print("=" * 72)
        for t in mx.query_structured(f"{code} {name} 市盈率 市净率 历史分位".strip()):
            cols = t.get("columns") or []
            rows_ = t.get("rows") or []
            if not rows_:
                continue
            r = rows_[0]
            title = t.get("title") or ""
            label = "3年历史百分位" if ("百分位" in title or any("百分位" in c for c in cols)) else "当前估值"
            cells = [f"{c}={r.get(c)}" for c in cols if c != "日期" and r.get(c)]
            if cells:
                print(f"  {label}: " + " | ".join(cells))

    # ---- 7. 最近研报 + 一致预期（仅个股；ETF 无个股研报评级） ----
    if not is_etf:
        print()
        print("=" * 72)
        print("【7. 最近研报（评级/机构 + 一致预期）】")
        print("=" * 72)
        reports = mx.fin_search_structured(f"{name or code} {code} 研报 评级 目标价".strip())
        report_lines = []
        buckets = {"看多": 0, "中性": 0, "看空": 0}
        target_prices = []
        for it in reports[:12]:
            if it.get("information_type") == "REPORT" and it.get("rating") and _belongs_to_stock(it, name, code):
                ins = it.get("ins_name") or "机构"
                rating = it.get("rating")
                title = (it.get("title") or "")[:40]
                date = (it.get("date") or "")[:10]
                report_lines.append(f"  [{rating}] {ins} ({date}): {title}")
                buckets[_rating_bucket(rating)] += 1
                m = re.search(r"目标价[:：]?\s*(\d+\.?\d*)\s*元?", (it.get("content") or "") + " " + (it.get("title") or ""))
                if m:
                    target_prices.append(m.group(1))
        if report_lines:
            dist = " | ".join(f"{k} {v}" for k, v in buckets.items() if v)
            print(f"  一致预期（评级分布）: {dist}")
            if target_prices:
                print("  目标价（研报提及）: " + " / ".join(target_prices[:5]) + " 元")
            print("\n".join(report_lines))
        else:
            print("  ⚠️ 未查到近期研报评级")

    # ---- 8. 资讯与事件风险 ----
    print()
    print("=" * 72)
    print("【8. 近 7 日资讯 + 事件风险】")
    print("=" * 72)
    news = mx.fin_search_as_text(f"{name or code} {code}", hours=24 * 7)
    print(news[:2400] if news else "⚠️ 未查到资讯")

    if is_etf:
        _EVENT_KW = ("成分股调整", "清盘", "限额", "折溢价", "分红", "份额折算", "暂停申赎")
        event_query = f"{name or code} {code} 成分股调整 清盘 限额 分红 折溢价".strip()
        event_label = "成分股调整/清盘/限额/折溢价等"
        event_empty = "（未检索到明确的成分股调整/清盘/限额/折溢价事件）"
    else:
        _EVENT_KW = ("减持", "增持", "回购", "解禁", "质押", "业绩预告", "立案", "处罚")
        event_query = f"{name or code} {code} 减持 增持 回购 解禁 质押 业绩预告".strip()
        event_label = "减持/增持/回购/解禁/质押等"
        event_empty = "（未检索到明确减持/增持/回购/解禁/质押事件）"
    events = mx.fin_search_structured(event_query)
    event_lines = []
    for it in events[:12]:
        ttl = it.get("title", "")
        if any(k in ttl for k in _EVENT_KW) and _belongs_to_stock(it, name, code):
            itype = it.get("information_type", "")
            tag = "公告" if itype == "ANNOUNCEMENT" else (itype or "资讯")
            date = (it.get("date") or "")[:10]
            event_lines.append(f"  [{tag}] ({date}) {ttl[:44]}")
    if event_lines:
        print(f"\n  事件风险（{event_label}）:")
        print("\n".join(event_lines[:8]))
    else:
        print(f"\n  {event_empty}")


if __name__ == "__main__":
    sys.exit(main())
