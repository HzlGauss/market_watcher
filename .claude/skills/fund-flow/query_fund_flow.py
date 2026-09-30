#!/usr/bin/env python3
"""查询单个标的最近 N 个交易日的主力资金流向（妙想 Miaoxiang）

用法:
    py query_fund_flow.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股 / 场内 ETF 代码（必填）
    名称   股票名称（可选，提升查询精度）
    天数   跟踪天数（可选，默认 1）：1=今天(交易日)或最近1个交易日；N=最近N个交易日

示例:
    py query_fund_flow.py 300432 富临精工        # 今日
    py query_fund_flow.py 300432 富临精工 3      # 最近3个交易日
    py query_fund_flow.py 159801 3               # ETF，最近3个交易日

输出: 顶部「实时行情·量价参考」段（现价/涨跌幅/量比/换手率），
      下方按交易日倒序的资金流表格，单位亿元（+ 净流入 / - 净流出）。
      主力 = 超大单 + 大单；散户 ≈ 小单。
"""
import re
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

# 定位项目根目录（skills/fund-flow 上三级：fund-flow -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient
from app.helpers import _detect_market
from app.data_fetcher import fetch_fund_flow_detail, fetch_quotes
from app.models import WatchItem, Quote
from app.analyzer import detect_split_order_from_nets

# 5 档净流入的关键字段名（妙想返回的精确键名）
_TIERS = [
    ("主力", "主力净流入资金"),
    ("超大单", "超大单净流入资金"),
    ("大单", "大单净流入资金"),
    ("中单", "中单净流入资金"),
    ("小单", "小单净流入资金"),
]

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_amount(value):
    """解析金额字符串 -> 元（float）。健壮处理「万元」「亿元」「万」「亿」及纯数字。

    库里的 MXClient._parse_amount 只认以「万/亿」结尾的串，遇到「万元/亿元」会失败，
    这里在脚本内做健壮版（不改共享库，避免影响其它调用方）。
    """
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
        mult = 1e8
        s = s.replace("亿", "")
    elif "万" in s:
        mult = 1e4
        s = s.replace("万", "")
    s = s.replace("元", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _row_main_net(r):
    """主力净流入：优先取「主力净流入资金」字段，缺省用 超大单+大单 合成。

    妙想 query_structured 返回的逐日表只有 超大单/大单/中单/小单 净流入，
    没有「主力净流入资金」列（该列只在 (区间) 聚合表出现），故需合成。
    """
    main = _parse_amount(r.get("主力净流入资金"))
    if main is not None:
        return main
    sup = _parse_amount(r.get("超大单净流入资金"))
    lrg = _parse_amount(r.get("大单净流入资金"))
    if sup is None and lrg is None:
        return None
    return (sup or 0.0) + (lrg or 0.0)


def _norm_date(s) -> str:
    """日期归一化："2026-08-21(日)" / "2026-08-24 13:07" -> "2026-08-21" """
    m = _DATE_RE.match(str(s or "").strip())
    return m.group(1) if m else ""


def _fmt_amount(v) -> str:
    """元 -> 亿元字符串（保留符号，2 位小数）"""
    if v is None:
        return "   N/A"
    return f"{v / 1e8:+.2f}"


def _f(x, nd=2) -> str:
    """浮点 -> 定宽字符串（None 显示 --）。"""
    return f"{x:.{nd}f}" if x is not None else "  --"


# 场内基金（ETF/LOF）号段，与 app/hithink.py 一致
_ETF_PREFIXES = ("51", "56", "58", "15", "16", "18")


def _volume_price_label(q: Quote) -> str:
    """量价配合标签：量比 + 涨跌幅 -> 放量上涨 / 缩量下跌 等（换手率佐证活跃度）。

    量比 = 当日每分钟均量 / 近 5 日每分钟均量：≥1.5 明显放量，<0.8 缩量，0.8~1.5 平量。
    """
    if q is None:
        return ""
    vr = q.volume_ratio
    if vr is not None:
        vol = "放量" if vr >= 1.5 else ("缩量" if vr < 0.8 else "平量")
    else:
        vol = "量比N/A"
    chg = q.change_pct
    if chg is None:
        d = ""
    elif chg >= 0.5:
        d = "上涨"
    elif chg <= -0.5:
        d = "下跌"
    else:
        d = "横盘"
    return f"{vol}{d}" if d else vol


def _print_quote(code: str, name: str) -> Quote | None:
    """实时行情（现价/涨跌幅/量比/换手率），供量价配合判断，不依赖 MX_APIKEY。

    新浪主源 + 腾讯量比/换手率兜底；失败时静默跳过（不影响资金流主流程）。
    """
    market = _detect_market(code)
    is_etf = code.startswith(_ETF_PREFIXES)
    item = WatchItem(name=name, code=code, market=market, type="ETF" if is_etf else "个股")
    try:
        quotes = fetch_quotes([item])
    except Exception:
        quotes = []
    q = quotes[0] if quotes else None
    if q is None:
        return None
    print("=" * 72)
    print(f"【实时行情 · 量价参考】{q.code} {q.name}")
    print("=" * 72)
    print(f"  现价 {_f(q.price, 3)}  涨跌幅 {_f(q.change_pct, 2)}%  "
          f"量比 {_f(q.volume_ratio, 2)}  换手率 {_f(q.turnover_rate, 2)}%")
    print(f"  量价配合: {_volume_price_label(q)}（换手率口径：{'ETF·T+0' if is_etf else '个股'}）")
    return q


def _today_row_from_eastmoney(code: str):
    """东方财富实时资金流兜底（妙想实时通道失效时使用）

    通过 push2delay 子域获取当日实时 5 档净流入，返回 (日期, row dict) 或 None。
    row dict 键名与妙想结构化结果对齐，便于复用同一套打印逻辑。
    仅在能取到「当日」实时数据时返回（内部已做日期校验），非交易日返回 None。
    """
    try:
        flow = fetch_fund_flow_detail(code, _detect_market(code))
        if flow is None:
            return None
        if flow.main_net is None and flow.super_large_net is None:
            return None
        row = {
            "主力净流入资金": flow.main_net,
            "超大单净流入资金": flow.super_large_net,
            "大单净流入资金": flow.large_net,
            "中单净流入资金": flow.medium_net,
            "小单净流入资金": flow.small_net,
        }
        return date.today().strftime("%Y-%m-%d"), row
    except Exception:
        return None


def _read_intraday_series(code: str) -> list[dict]:
    """读盯盘落库的当日日内资金流时序（data/fund_flow_intraday.duckdb）。

    盯盘时 BackgroundDataCache 每 ~300s 把当日 5 档资金流 + 来源写入 duckdb，
    这里读回来补「当日盘中资金怎么走的」——妙想只给当日累计值，无盘中时序。

    返回按时间升序的快照列表；无 duckdb（系统 python3）/ 无数据 / 非当日 → []。
    """
    db_path = _ROOT / "data" / "fund_flow_intraday.duckdb"
    if not db_path.exists():
        return []
    try:
        import duckdb
    except ImportError:
        return []
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT ts, main_net, super_large_net, large_net, medium_net, small_net, source "
                "FROM fund_flow_intraday WHERE code = ? ORDER BY ts ASC",
                [code],
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        if not str(r[0]).startswith(today):
            continue
        out.append({
            "ts": r[0],
            "main_net": r[1],
            "super_large_net": r[2],
            "large_net": r[3],
            "medium_net": r[4],
            "small_net": r[5],
            "source": r[6],
        })
    return out


# --- 终端 braille 折线（把当日时序画成 Unicode 盲文字符曲线，直接在 CMD 内显示） ---

# braille 点阵：每个字符 2×4 个点，(x_sub, y_sub) -> 位掩码（x_sub∈{0,1}, y_sub∈{0..3}）
_BRAILLE_BITS = {
    (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
    (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
}
_CURVE_DOT_HEIGHT = 12   # 纵向点分辨率（12 点 = 3 行 braille 字符，曲线更舒展）
_CURVE_WIDTH = 56        # 横向 braille 字符列数（每列 2 个采样点 = 112 个横轴位置）


def _ts_epoch(ts) -> float:
    """duckdb TIMESTAMP(datetime) -> epoch 秒；其他类型尽量转 float。"""
    return ts.timestamp() if hasattr(ts, "timestamp") else float(ts)


def _interp_value(pts, t):
    """pts 已按时间升序的 (t_epoch, value) 列表；返回 t 处的线性插值。"""
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= t:
            ta, va = pts[i - 1]
            tb, vb = pts[i]
            return va + (vb - va) * (t - ta) / (tb - ta) if tb != ta else vb
    return pts[-1][1]


def _render_braille_curve(rows, field="main_net", height=_CURVE_DOT_HEIGHT, width=_CURVE_WIDTH):
    """把单一时序渲染成 braille 折线。

    rows: 含 ts(时间) 与 field(元) 的 dict 列表；按时间连续、对横轴逐点线性插值连线。
    返回 (lines, hi, lo, t0, t1)：lines 为字符串列表（每条一行）、hi/lo 为 y 上下界(元)，
    t0/t1 为起止 epoch 秒；数据不足 2 点返回 None。
    """
    pts = []
    for r in rows:
        v = r.get(field)
        ts = r.get("ts")
        if v is None or ts is None:
            continue
        pts.append((_ts_epoch(ts), v))
    if len(pts) < 2:
        return None
    pts.sort()
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    # 范围强制覆盖 0（净流入/净流出分界可见），并上下各留 5% 余量
    lo = min(lo, 0.0)
    hi = max(hi, 0.0)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    span = hi - lo
    lo -= span * 0.05
    hi += span * 0.05
    span = hi - lo

    n_char_rows = (height + 3) // 4
    grid = [[0] * width for _ in range(n_char_rows)]
    n_sub = width * 2
    for i in range(n_sub):
        t = t0 + (t1 - t0) * (i / (n_sub - 1)) if n_sub > 1 else t0
        v = _interp_value(pts, t)
        frac = (hi - v) / span                      # v=hi -> 0(顶)，v=lo -> 1(底)
        y = max(0, min(height - 1, round(frac * (height - 1))))
        x_char, x_sub = divmod(i, 2)
        grid[y // 4][x_char] |= _BRAILLE_BITS[(x_sub, y % 4)]

    lines = ["".join(chr(0x2800 + bits) for bits in row) for row in grid]
    return lines, hi, lo, t0, t1


def _print_intraday_curve(rows) -> None:
    """打印当日主力净流入 braille 折线（rows 已读回；数据不足/无数据静默跳过）。"""
    # 双源口径可能不一致，取快照较多的一方画，避免主力线无端跳变
    by_src = {}
    for r in rows:
        by_src.setdefault(r.get("source") or "eastmoney", []).append(r)
    if not by_src:
        return
    src_rows = max(by_src.values(), key=len)
    res = _render_braille_curve(src_rows)
    if res is None:
        return
    lines, hi, lo, t0, t1 = res
    src_name = "东财" if src_rows[0].get("source") == "eastmoney" else "妙想"
    top = f"{hi / 1e8:+.2f}亿"
    bot = f"{lo / 1e8:+.2f}亿"
    lw = max(len(top), len(bot))
    t0s = datetime.fromtimestamp(t0).strftime("%H:%M")
    t1s = datetime.fromtimestamp(t1).strftime("%H:%M")

    print(f"  主力净流入 当日日内走势（{src_name}源，{len(src_rows)} 个快照）  单位:亿元")
    if len(by_src) > 1:
        print("  ⚠️ 本日混用东财/妙想两渠道，曲线仅取快照较多的一方")
    print(f"  {top:>{lw}} ┤{lines[0]}")
    for ln in lines[1:-1]:
        print(f"  {'':>{lw}} │{ln}")
    print(f"  {bot:>{lw}} ┤{lines[-1]}")
    axis = f"{t0s}" + "─" * max(1, len(lines[0]) - len(t0s) - len(t1s)) + f"{t1s}"
    print(f"  {'':>{lw}}  {axis}")
    print("  （正值=净流入 · 负值=净流出）")
    print()


def _print_intraday_series(code: str) -> None:
    """打印盯盘落库的当日日内时序（无数据则静默跳过）。"""
    rows = _read_intraday_series(code)
    if not rows:
        return
    sources = {r["source"] for r in rows}
    print("=" * 72)
    print(f"【当日日内时序 · 盯盘落库】{code}  共 {len(rows)} 个快照")
    print("=" * 72)
    if len(sources) > 1:
        print("  ⚠️ 本日快照混用「东财/妙想」两个渠道，主力口径可能不一致，趋势仅参考")
    _print_intraday_curve(rows)
    print("  时间      主力      超大单   大单     中单     小单     来源")
    for r in rows:
        ts = r["ts"]
        hhmm = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)[11:16]
        src = "妙想" if r["source"] == "miaoxiang" else "东财"
        cells = [
            _fmt_amount(r["main_net"]),
            _fmt_amount(r["super_large_net"]),
            _fmt_amount(r["large_net"]),
            _fmt_amount(r["medium_net"]),
            _fmt_amount(r["small_net"]),
        ]
        print(f"  {hhmm:<8}  " + "  ".join(cells) + f"   {src}")
    print()


def _parse_args(argv):
    """解析命令行参数，返回 (code, name, days)"""
    code = argv[1].strip()
    name, days = "", 1
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(1, min(int(a), 30))  # 天数限 1..30，防止请求过宽
        elif a:
            name = a
    return code, name, days


def main():
    if len(sys.argv) < 2:
        print("用法: py query_fund_flow.py <代码> [名称] [天数]")
        print("  天数: 1=今天/最近1个交易日(默认)，N=最近N个交易日")
        return 2

    code, name, days = _parse_args(sys.argv)

    load_env(_ROOT)
    # 量比 / 换手率（不依赖 MX_APIKEY，失败静默跳过，不影响资金流主流程）
    _print_quote(code, name)
    # 当日日内时序（盯盘落库的 duckdb，无数据/无 duckdb 时静默跳过，不依赖妙想）
    _print_intraday_series(code)

    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    # 妙想「近N日」是自然日；要拿 N 个交易日，需按 1.5x + 3 多请求覆盖周末
    window = max(3, int(days * 1.5) + 3)
    query = (
        f"{code} {name} 近{window}日 资金流向 "
        f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入"
    ).strip()

    rows, seen = [], set()
    for t in mx.query_structured(query):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            main = _row_main_net(r)
            if not d or main is None or d in seen:
                continue
            seen.add(d)
            if "主力净流入资金" not in r:
                r["主力净流入资金"] = main
            rows.append((d, r))

    if not rows:
        # 回退：结构化无数据时，单日兜底 + 自然语言原始结果
        detail = mx.stock_fund_flow(code, name)
        if detail is not None:
            print(f"{code} {name or ''} 今日资金流向（单位：亿元，+净流入 / -净流出）".replace("  ", " ").strip())
            print(f"  主力净流入:     {_fmt_amount(detail.main_net)}")
            print(f"    超大单净流入: {_fmt_amount(detail.super_large_net)}")
            print(f"    大单净流入:   {_fmt_amount(detail.large_net)}")
            print(f"  中单净流入:     {_fmt_amount(detail.medium_net)}")
            print(f"  小单净流入:     {_fmt_amount(detail.small_net)}")
            return 0
        # 回退 2：东方财富实时通道（妙想结构化 + 单日接口均无数据时的兜底）
        em_row = _today_row_from_eastmoney(code)
        if em_row is not None:
            d, r = em_row
            print(f"{code} {name} 主力资金流向 · 今日/最近1个交易日（东方财富兜底，单位：亿元，+净流入 / -净流出）".replace("  ", " ").strip())
            print("  日期          主力     超大单   大单     中单     小单")
            tag = " (今日·实时)" if d == date.today().strftime("%Y-%m-%d") else ""
            cells = [_fmt_amount(_parse_amount(r.get(key))) for _, key in _TIERS]
            print(f"  {d}{tag:<9}  " + "  ".join(cells))
            return 0
        print("⚠️ 未查到资金流向数据（非交易时间 / 无数据 / 代码无效）\n")
        print(mx.query_as_text(query) or "❌ 无返回")
        return 0

    # 交易日倒序（最新在前），截取前 N 个
    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[:days]

    today_str = date.today().strftime("%Y-%m-%d")

    # 妙想实时通道失效检测：今日（交易日）数据缺失时，用东方财富 push2delay 兜底补今日实时
    if days >= 1 and (not rows or rows[0][0] < today_str):
        em_row = _today_row_from_eastmoney(code)
        if em_row is not None:
            rows = [em_row] + [r for r in rows if r[0] < today_str]
            rows = rows[:days]

    label = f"最近{days}个交易日" if days > 1 else "今日/最近1个交易日"
    print(f"{code} {name} 主力资金流向 · {label}（单位：亿元，+净流入 / -净流出）".replace("  ", " ").strip())
    print("  日期          主力     超大单   大单     中单     小单")
    for d, r in rows:
        tag = " (今日·实时)" if d == today_str else ""
        cells = [_fmt_amount(_parse_amount(r.get(key))) for _, key in _TIERS]
        print(f"  {d}{tag:<9}  " + "  ".join(cells))

    # 拆单检测（纯净额版，最新交易日）：小单单边活跃 + 大单/超大单近乎沉默 = 疑似拆单
    _, latest_r = rows[0]
    split_signal = detect_split_order_from_nets(
        _parse_amount(latest_r.get("小单净流入资金")),
        _parse_amount(latest_r.get("大单净流入资金")),
        _parse_amount(latest_r.get("超大单净流入资金")),
        None,
    )
    if split_signal:
        print(f"  {split_signal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
