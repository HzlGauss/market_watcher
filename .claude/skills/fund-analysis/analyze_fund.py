#!/usr/bin/env python3
"""主动基金诊基（场外开放式基金，同花顺 + 东财净值 + 妙想兜底）

用法:
    py analyze_fund.py <代码> [名称]

参数:
    代码   6 位场外基金代码（如 005827 易方达蓝筹精选、110011 易方达中小盘）
    名称   基金名称（可选，提升查询精度）

示例:
    py analyze_fund.py 005827 易方达蓝筹精选
    py analyze_fund.py 110011
    py analyze_fund.py 005827

输出数据包：
    ① 基本信息（全称/成立日/规模/最新净值/费率/管理公司，同花顺 + 东财净值兜底）
    ② 净值绩效指标（年化收益/年化波动/夏普/最大回撤/卡玛/胜率/盈亏比，东财净值计算）
    ③ 相对基准（业绩比较基准 + 超额收益/信息比率/Beta/Alpha）
    ④ 区间收益 + 同类排名（近月/季/半年/年/三年/五年/今年/成立以来，同花顺）
    ⑤ 重仓股 + 行业配置（前十大持仓/行业占比/集中度，同花顺）
    ⑥ 持有人 + 分红 + 诊断（机构占比/户数/分红次数/诊断评分，同花顺）
    ⑦ 基金经理（任职回报/年化/历史业绩/投资风格/简历，同花顺）
    ⑧ 妙想深度数据（晨星/银河评级等兜底，无同花顺 key 时的替代源）
"""
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/fund-analysis 上三级：fund-analysis -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.fund_analyzer import (
    _fetch_nav,
    _calc_fund_metrics,
    _fetch_risk_free_rate,
    _fetch_fund_benchmark,
    _fetch_benchmark_nav,
    _calc_benchmark_metrics,
    _mx_query,
)


def _parse_args(argv):
    """解析命令行参数，返回 (code, name)"""
    if not argv:
        return None, ""
    code = argv[0].strip()
    name = ""
    for a in argv[1:]:
        a = a.strip()
        if a and not a.isdigit():
            name = a
    return code, name


def _fmt_metrics(m):
    """把 _calc_fund_metrics 结果渲染为文本"""
    if not m:
        return "  (净值数据不足，无法计算指标)\n"
    quality = "⚠️ 数据不足一年" if m.get("data_quality") != "sufficient" else ""
    lines = [
        f"  年化收益率:   {m['annual_return']:+.2f}%",
        f"  年化波动率:   {m['annual_volatility']:.2f}%",
        f"  夏普比率:     {m['sharpe_ratio']:.2f}",
        f"  最大回撤:     {m['max_drawdown']:.2f}%",
        f"  卡玛比率:     {m['calmar_ratio']:.2f}",
        f"  胜率:         {m['win_rate']:.2f}%",
        f"  盈亏比:       {m['profit_factor']:.2f}",
        f"  数据点数:     {m['data_points']} 个交易日 {quality}",
    ]
    return "\n".join(lines) + "\n"


def _fmt_benchmark(bm):
    """把 _calc_benchmark_metrics 结果渲染为文本"""
    if not bm:
        return "  (无基准对比数据)\n"
    lines = [
        f"  超额收益:     {bm['excess_return']:+.2f}%",
        f"  跟踪误差:     {bm['tracking_error']:.2f}%",
        f"  信息比率:     {bm['info_ratio']:.2f}",
        f"  Beta:         {bm['beta']:.2f}",
        f"  Alpha:        {bm['alpha']:+.2f}%",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 同花顺基金数据

def _ms_ymd(ms) -> str:
    """毫秒时间戳 -> YYYY-MM-DD（None/0 -> --）。"""
    if not ms:
        return "--"
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "--"


def _fnum(v, nd=2) -> str:
    """float -> 定宽字符串（None -> --）。"""
    if v is None:
        return "  --"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "  --"


def _fpct(v, plus=False) -> str:
    """百分数原值 -> 带符号字符串（None -> --）。"""
    if v is None:
        return "   --"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "   --"
    return f"{v:+.2f}%" if plus else f"{v:.2f}%"


def _fyi(v) -> str:
    """元 -> 亿/万 短字符串（None -> --）。"""
    if v is None:
        return "--"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    return f"{v / 1e4:.2f}万"


def _hithink_profile(code: str):
    """同花顺基金基本资料（首条 item），失败返回 None。"""
    try:
        from app import hithink
        items = hithink.fetch_fund_profile(code)
        return items[0] if items else None
    except Exception:
        return None


def _rank_str(r, total):
    """名次/总数 -> "名次/总数"（缺任一 -> --）。"""
    if r is None or total is None:
        return "--"
    try:
        return f"{int(r)}/{int(total)}"
    except (TypeError, ValueError):
        return "--"


def _print_hithink_returns(code: str):
    """④ 区间收益 + 同类排名（同花顺）。"""
    try:
        from app import hithink
        items = hithink.fetch_fund_returns(code)
    except Exception:
        items = []
    if not items:
        print("【④ 区间收益 + 同类排名】⚠️ 无数据（无 key 或接口不可达）")
        return
    r = items[0]
    periods = [
        ("近1月", "return_month", "rank_month", "rank_total_month"),
        ("近3月", "return_tmonth", "rank_tmonth", "rank_total_tmonth"),
        ("近半年", "return_hyear", "rank_hyear", "rank_total_hyear"),
        ("近1年", "return_year", "rank_year", "rank_total_year"),
        ("近3年", "return_tyear", "rank_tyear", "rank_total_tyear"),
        ("近5年", "return_fyear", "rank_fyear", "rank_total_fyear"),
        ("今年以来", "return_nowyear", None, None),
        ("成立以来", "return_now", None, None),
    ]
    print("【④ 区间收益 + 同类排名（同花顺）】")
    print(f"  {'区间':<8}{'收益':>10}{'同类平均':>10}{'同类排名':>10}")
    print("  " + "-" * 42)
    for label, ret, rk, rtot in periods:
        ret_s = _fpct(r.get(ret), plus=True)
        # 同类平均字段名 = return_ 前缀替换为 peer_average_
        peer_key = ret.replace("return_", "peer_average_") if rk else None
        peer_s = _fpct(r.get(peer_key), plus=True) if peer_key else "       --"
        rank_s = _rank_str(r.get(rk), r.get(rtot)) if rk else "       --"
        print(f"  {label:<8}{ret_s:>10}{peer_s:>10}{rank_s:>10}")


def _print_hithink_holdings(code: str):
    """⑤ 重仓股 + 行业配置（同花顺）。"""
    try:
        from app import hithink
        holdings = hithink.fetch_fund_holdings(code)
        industry = hithink.fetch_fund_industry_allocation(code)
    except Exception:
        holdings, industry = {}, []
    print("【⑤ 重仓股 + 行业配置（同花顺，定期披露口径）】")
    conc = holdings.get("concentration_ratio")
    conc_s = _fpct((conc * 100) if isinstance(conc, (int, float)) else None)
    top10 = holdings.get("total_stock_ratio_pct")
    hdr = (f"  股票仓位 {_fpct(holdings.get('stock_ratio_pct'))}  "
           f"前十大合计 {_fpct(top10)}  集中度(占股票仓位) {conc_s}  "
           f"主力行业 {holdings.get('main_industry') or '--'}")
    print(hdr)
    items = holdings.get("item") or []
    if items:
        print("  ── 前十大重仓（按披露市值占比）──")
        for it in items[:10]:
            name = it.get("stock_name", "")
            ratio = _fpct(it.get("hold_ratio"))
            cap = _fyi(it.get("position_capital"))
            print(f"    {it.get('ticker', ''):<8}{name:<12} 占比 {ratio}  市值 {cap}")
    if industry:
        # 只取最新报告期，并按行业名去重（取最大占比）
        latest = max((x.get("report_period", "") for x in industry), default="")
        rows = [x for x in industry if x.get("report_period") == latest]
        best = {}
        for x in rows:
            k = x.get("industry_name") or "--"
            v = x.get("ratio_pct") or 0
            if k not in best or v > best[k]:
                best[k] = v
        print(f"  ── 行业配置（{latest}）──")
        nonzero = [(k, v) for k, v in best.items() if v and v > 0]
        for name_, ratio in sorted(nonzero, key=lambda kv: -kv[1])[:8]:
            print(f"    {name_:<14}{_fpct(ratio)}")
    if not items and not industry:
        print("  ⚠️ 无持仓/行业披露数据")


def _print_hithink_holders_diag(code: str):
    """⑥ 持有人结构 + 分红 + 诊断（同花顺）。"""
    try:
        from app import hithink
        holders = hithink.fetch_fund_holders(code)
        div = hithink.fetch_fund_dividends(code)
        diag = hithink.fetch_fund_diagnostics(code)
    except Exception:
        holders, div, diag = [], {}, []
    print("【⑥ 持有人 + 分红 + 诊断（同花顺）】")
    if holders:
        h = holders[0]
        print(f"  持有人: 机构占比 {_fpct(h.get('ins_position'))}  个人占比 {_fpct(h.get('psnl_rate'))}  "
              f"户数 {_fnum(h.get('holder_amount'), 0)}  员工持有 {_fpct(h.get('mgmt_staff_hold_rate'))}")
    cnt = div.get("dividend_count")
    if cnt:
        print(f"  分红: {cnt} 次  累计每份 {_fnum(div.get('dividend_total'))} 元")
    if diag:
        d = diag[0]
        # dimensions 为 [{year, ..._score}]，取最新一年综合评分
        dims = d.get("dimensions") or []
        print("  诊断（同花顺）: ", end="")
        if dims:
            latest = dims[0]
            print(f"综合 {_fnum(latest.get('integrate_score'), 0)}  "
                  f"业绩 {_fnum(latest.get('performance_capability_score'), 0)}  "
                  f"抗风险 {_fnum(latest.get('anti_risk_score'), 0)}  "
                  f"经理 {_fnum(latest.get('manager_score'), 0)}  "
                  f"公司 {_fnum(latest.get('company_score'), 0)}")
        else:
            print("无维度评分")
    if not holders and not cnt and not diag:
        print("  ⚠️ 无持有人/分红/诊断数据")


def _print_hithink_manager(code: str):
    """⑦ 基金经理（同花顺）。"""
    try:
        from app import hithink
        prof = hithink.fetch_fund_profile(code)
    except Exception:
        return
    if not prof:
        print("【⑦ 基金经理】⚠️ 无数据")
        return
    managers = prof[0].get("manager_info") or []
    print("【⑦ 基金经理（同花顺）】")
    for m in managers[:3]:
        mid = m.get("manager_id")
        name = m.get("manager_name") or "--"
        tenure = _fpct(m.get("tenure_return_pct"), plus=True)
        days = m.get("tenure_days")
        days_s = f"{days} 天" if days is not None else "--"
        print(f"  ── {name}（任职 {days_s}，任职回报 {tenure}）──")
        if not mid:
            continue
        try:
            from app import hithink
            detail = hithink.fetch_fund_manager_detail(mid)
            if detail:
                d = detail[0]
                extra = []
                if d.get("annual_return_pct") is not None:
                    extra.append(f"年化 {_fpct(d.get('annual_return_pct'), plus=True)}")
                if d.get("maximum_return_pct") is not None:
                    extra.append(f"最大回报 {_fpct(d.get('maximum_return_pct'), plus=True)}")
                if extra:
                    print(f"      {'  '.join(extra)}")
            style = hithink.fetch_fund_manager_style(mid)
            if style and style[0].get("investment_idea"):
                idea = str(style[0].get("investment_idea"))
                print(f"      风格: {idea[:80]}")
        except Exception:
            pass


def _fetch_name(code: str, name: str, mx) -> str:
    """通过妙想获取基金全称（已有名称则直接用）"""
    if name:
        return name
    try:
        text = mx.query_as_text(f"{code} 基金全称")
        # 从结果里粗略提取名称行，兜底返回空
        for line in text.split("\n"):
            if code in line and ("基金" in line or "(" in line):
                return line.strip().split("(")[0].strip()[:20]
    except Exception:
        pass
    return ""


def main():
    code, name = _parse_args(sys.argv[1:])
    if not code:
        print(__doc__)
        return 2

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys

    print(f"=== 基金诊基: {code} {name} ===\n")

    # 1. 净值 + 指标（东财，不依赖妙想）
    nav = _fetch_nav(code)
    if nav is None:
        print("❌ 未获取到基金净值数据（代码无效 / 非场外开放式基金）")
        return 1

    rf = _fetch_risk_free_rate()
    nav_series = nav.get("nav_series") or []
    metrics = _calc_fund_metrics(nav_series, rf)

    # 2. 基准对比（东财）
    bench_code = _fetch_fund_benchmark(code)
    bench_metrics = {}
    if bench_code and nav_series:
        bench_nav = _fetch_benchmark_nav(bench_code)
        if bench_nav:
            fund_returns = [
                nav_series[i] / nav_series[i - 1] - 1
                for i in range(1, len(nav_series))
                if nav_series[i - 1] > 0
            ]
            bench_returns = [
                (bench_nav[i] - bench_nav[i - 1]) / bench_nav[i - 1]
                for i in range(1, len(bench_nav))
                if bench_nav[i - 1] > 0
            ]
            bench_metrics = _calc_benchmark_metrics(fund_returns, bench_returns, rf)

    # 3. 同花顺基金基本资料（有 key 时作主源；无 key 回退妙想全称）
    prof = _hithink_profile(code)
    if prof:
        if not name:
            name = prof.get("fund_name") or ""
        hithink_ok = True
    else:
        hithink_ok = False
        if not name and api_keys:
            from app.miaoxiang import MXClient
            name = _fetch_name(code, name, MXClient(api_keys)) or name

    # 输出①基本信息（同花顺主源 + 东财净值兜底）
    print("【① 基本信息】")
    print(f"  基金代码:     {code}")
    print(f"  基金名称:     {name or prof.get('fund_name') or '--'}")
    if prof:
        print(f"  基金公司:     {prof.get('mgmt_name') or '--'}")
        print(f"  现任经理:     {prof.get('manager_name') or '--'}")
        print(f"  成立日期:     {_ms_ymd(prof.get('estab_date'))}")
        print(f"  基金规模:     {_fyi(prof.get('fund_scale'))}")
        print(f"  单位净值:     {_fnum(prof.get('unit_nav'), 4)}  (同花顺)")
        rate = (prof.get("rate_info") or [])
        by_type = {}
        for r in rate:
            by_type.setdefault(r.get("rate_type"), r.get("standard_rate"))
        fee_parts = []
        for label, key in (("管理费", "management"), ("托管费", "custody"), ("申购费", "purchase")):
            if by_type.get(key):
                fee_parts.append(f"{label} {by_type[key]}")
        if fee_parts:
            print(f"  费率:         {' / '.join(fee_parts)}")
    print(f"  最新净值:     {nav.get('nav')}  ({nav.get('date', '')})")
    print(f"  日涨跌幅:     {nav.get('daily_change', '--')}%")
    print(f"  跟踪区间:     {nav.get('start_date', '')} ~ {nav.get('date', '')}（{nav.get('days', 0)} 个交易日）")
    print()

    # 输出②净值绩效指标
    print("【② 净值绩效指标（近1年滚动）】")
    print(_fmt_metrics(metrics))

    # 输出③相对基准
    print(f"【③ 相对基准（{bench_code or '--'}）】")
    print(_fmt_benchmark(bench_metrics))

    # 输出④~⑦ 同花顺基金深度数据（区间收益/持仓/持有人诊断/经理）
    _print_hithink_returns(code)
    print()
    _print_hithink_holdings(code)
    print()
    _print_hithink_holders_diag(code)
    print()
    _print_hithink_manager(code)
    print()

    # 输出⑧妙想深度数据（权威评级兜底 + 补充；无 key 或无同花顺时作替代源）
    if api_keys:
        from app.miaoxiang import MXClient
        mx = MXClient(api_keys)
    else:
        mx = None

    print("【⑧ 妙想深度数据】")
    if mx is None:
        print("  ⚠️ 未配置 MX_APIKEY，跳过（同花顺 + 东财净值已覆盖主体数据）")
        return 0

    queries = [
        ("权威评级", f"{code} {name} 晨星评级 银河评级"),
        ("规模配置", f"{code} {name} 基金规模 资产配置"),
    ]
    # 无同花顺数据时，妙想补全持仓/阶段涨幅/经理
    if not hithink_ok:
        queries += [
            ("前十大持仓", f"{code} {name} 前十大持仓 行业分布"),
            ("阶段涨幅排名", f"{code} {name} 阶段涨幅 四分位排名"),
            ("基金经理", f"{code} {name} 基金经理 任职回报"),
        ]
    for label, q in queries:
        text = _mx_query(q, config)
        print(f"  —— {label} ——")
        if text:
            print(text)
        else:
            print("  (无数据)")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
