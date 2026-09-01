#!/usr/bin/env python3
"""主动基金诊基（场外开放式基金，妙想 + 东财净值）

用法:
    py analyze_fund.py <代码> [名称]

参数:
    代码   6 位场外基金代码（如 005827 易方达蓝筹精选、110011 易方达中小盘）
    名称   基金名称（可选，提升妙想查询精度）

示例:
    py analyze_fund.py 005827 易方达蓝筹精选
    py analyze_fund.py 110011
    py analyze_fund.py 005827

输出 4 段数据包：
    ① 基本信息（全称/类型/最新净值/成立日期，妙想）
    ② 净值绩效指标（年化收益/年化波动/夏普/最大回撤/卡玛/胜率/盈亏比，东财净值计算）
    ③ 相对基准（业绩比较基准 + 超额收益/信息比率/Beta/Alpha）
    ④ 妙想深度数据（晨星/银河评级、前十大持仓/行业分布、阶段涨幅/四分位排名、经理/任职回报、规模/资产配置）
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

    # 1. 净值 + 指标（东财，不依赖妙想）
    print(f"=== 基金诊基: {code} {name} ===\n")

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

    # 3. 妙想深度数据（有 key 时）
    mx = None
    if api_keys:
        from app.miaoxiang import MXClient
        mx = MXClient(api_keys)
        name = _fetch_name(code, name, mx) or name

    # 输出①基本信息（东财净值快照 + 妙想全称）
    print("【① 基本信息】")
    print(f"  基金代码:     {code}")
    if name:
        print(f"  基金名称:     {name}")
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

    # 输出④妙想深度数据
    if mx is None:
        print("【④ 妙想深度数据】⚠️ 未配置 MX_APIKEY，跳过（仅输出净值绩效与基准对比）")
        return 0

    print("【④ 妙想深度数据】")
    queries = [
        ("权威评级", f"{code} {name} 晨星评级 银河评级"),
        ("前十大持仓", f"{code} {name} 前十大持仓 行业分布"),
        ("阶段涨幅排名", f"{code} {name} 阶段涨幅 四分位排名"),
        ("基金经理", f"{code} {name} 基金经理 任职回报"),
        ("规模配置", f"{code} {name} 基金规模 资产配置"),
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
