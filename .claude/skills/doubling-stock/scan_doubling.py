# -*- coding: utf-8 -*-
"""翻倍股启动前筛选（本地 DuckDB 纯离线）。

按回测验证过的三层闸门筛「翻倍股启动前」候选：
  ① 技术闸门(抬翻倍率)  微盘<20亿 + 距1年高点回撤>40% + 上市2-5年(可选关)
  ② 基本面闸门(压暴雷)  净利为正且同比加速 或 净利转正
  ③ 合成甜点 = ①∩②

结论来源见 memory/doubling-stock-prelaunch-signals.md。纯本地读 data/market.duckdb，
不依赖 MX_APIKEY / HITHINK，零网络。用法：py .claude/skills/doubling-stock/scan_doubling.py [数量] [--no-age] [--include-st]
"""
import argparse
import sys

import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_tables(con: duckdb.DuckDBPyConnection) -> None:
    # 1) 市值(亿) = 最新股东权益合计 × 最新 PB(MRQ)
    con.execute(
        """
        CREATE TEMP TABLE cap AS
        SELECT vb.thscode, vb.holder_equity_total * vd.pb_mrq / 1e8 AS cap_yi
        FROM (SELECT thscode, holder_equity_total FROM fin_balance
              QUALIFY row_number() OVER (PARTITION BY thscode ORDER BY period_end DESC) = 1) vb
        JOIN (SELECT thscode, pb_mrq FROM valuation_daily
              QUALIFY row_number() OVER (PARTITION BY thscode ORDER BY trade_date DESC) = 1) vd
          USING (thscode)
        WHERE vb.holder_equity_total > 0 AND vd.pb_mrq > 0
        """
    )
    # 2) 回撤(距 260 日高点) + 首根K线日(上市代理) + 交易日数
    con.execute(
        """
        CREATE TEMP TABLE dd AS
        WITH hist AS (SELECT thscode, min(date) first_date, max(date) last_date, count(*) ndays
                      FROM v_daily_qfq WHERE close > 0 GROUP BY thscode),
        last AS (SELECT thscode, close cq FROM v_daily_qfq WHERE close > 0
                 QUALIFY row_number() OVER (PARTITION BY thscode ORDER BY date DESC) = 1)
        SELECT l.thscode, l.cq, h.first_date, h.ndays,
               l.cq / (SELECT max(close) FROM v_daily_qfq WHERE thscode = l.thscode
                       AND date > h.last_date - INTERVAL 260 DAY AND close > 0) - 1 AS dd
        FROM last l JOIN hist h USING (thscode)
        """
    )
    # 3) 净利同比 + 加速：动态取每只票最新报告期，同比 = 去年同期，加速 = 同比高于上一报告期
    con.execute(
        """
        CREATE TEMP TABLE rep AS
        SELECT thscode, fiscal_year, fiscal_period, period_end, parent_holder_net_profit np
        FROM fin_income WHERE parent_holder_net_profit IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE fin AS
        WITH cur AS (SELECT thscode, fiscal_year, fiscal_period, np FROM rep
                     QUALIFY row_number() OVER (PARTITION BY thscode ORDER BY period_end DESC) = 1),
        yoy AS (SELECT c.thscode, c.np, p.np np_ly, c.np / nullif(p.np, 0) - 1 yoy
                FROM cur c LEFT JOIN rep p
                  ON p.thscode = c.thscode AND p.fiscal_year = c.fiscal_year - 1
                 AND p.fiscal_period = c.fiscal_period),
        prev AS (SELECT thscode, fiscal_year, fiscal_period, np FROM rep
                 QUALIFY row_number() OVER (PARTITION BY thscode ORDER BY period_end DESC) = 2),
        yoy_prev AS (SELECT pr.thscode, pr.np / nullif(pr2.np, 0) - 1 yoy_prev
                     FROM prev pr LEFT JOIN rep pr2
                       ON pr2.thscode = pr.thscode AND pr2.fiscal_year = pr.fiscal_year - 1
                      AND pr2.fiscal_period = pr.fiscal_period)
        SELECT y.thscode, y.np, y.np_ly, y.yoy, v.yoy_prev
        FROM yoy y LEFT JOIN yoy_prev v USING (thscode)
        """
    )
    # 4) 合并 + 分类 + ST 标记
    con.execute(
        """
        CREATE TEMP TABLE cand AS
        SELECT d.thscode, d.ticker, d.name, c.cap_yi, p.cq, p.dd, p.first_date, p.ndays,
               f.np, f.np_ly, f.yoy, f.yoy_prev,
               CASE
                 WHEN f.np IS NULL OR f.np_ly IS NULL THEN '无数据'
                 WHEN f.np <= 0 THEN '亏损'
                 WHEN f.np_ly < 0 THEN '转正'
                 WHEN f.yoy <= 0 THEN '负增长'
                 WHEN f.yoy_prev IS NOT NULL AND f.yoy > f.yoy_prev THEN '加速'
                 ELSE '正增长'
               END AS kind,
               CASE WHEN d.name LIKE '%ST%' THEN 'ST' ELSE '' END AS st
        FROM dim_symbol d
        JOIN cap c ON c.thscode = d.thscode
        JOIN dd p ON p.thscode = d.thscode
        LEFT JOIN fin f ON f.thscode = d.thscode
        WHERE d.asset_type = 'a-share'
        """
    )


def fmt_yoy(kind: str, yoy: float) -> str:
    if kind == "转正":
        return "转正"
    if kind in ("无数据", "亏损"):
        return "—"
    return f"{yoy*100:.1f}" if yoy is not None else "—"


def show(con, title, where, limit):
    rows = con.execute(
        f"""SELECT ticker, name, st, round(cap_yi,1), round(dd*100,1), kind, yoy,
                   strftime(first_date,'%Y-%m-%d') FROM cand
            WHERE {where} ORDER BY cap_yi LIMIT {limit}"""
    ).fetchall()
    n_all = con.execute(f"SELECT count(*) FROM cand WHERE {where}").fetchone()[0]
    print(f"\n### {title}  （共 {n_all} 只，列前 {min(limit, len(rows))}）")
    if not rows:
        print("  （无候选）")
        return
    print(f"{'代码':<8}{'名称':<10}{'ST':<3}{'市值亿':>7}{'回撤%':>8}{'状态':<6}{'净利同比%':>9}{'上市日':>11}")
    for tk, nm, st, cap, dd, kind, yoy, fd in rows:
        yoy_s = fmt_yoy(kind, yoy)
        flag = "低基" if (kind == "加速" and yoy is not None and yoy > 3.0) else ""
        print(f"{tk:<8}{nm:<10}{st:<3}{cap:>7.1f}{dd:>8.1f}{kind:<6}{yoy_s:>9}{fd:>11}  {flag}")


def main():
    ap = argparse.ArgumentParser(description="翻倍股启动前筛选（本地 DuckDB 纯离线）")
    ap.add_argument("limit", nargs="?", type=int, default=50, help="输出数量（默认 50）")
    ap.add_argument("--no-age", action="store_true", help="去掉「上市2-5年」门槛（Tier B，会混入 ST 老壳股）")
    ap.add_argument("--include-st", action="store_true", help="Tier A 也纳入 ST（默认剔除）")
    args = ap.parse_args()

    con = duckdb.connect("data/market.duckdb", read_only=True)
    build_tables(con)

    gate_fund = "kind IN ('加速','转正')"
    gate_tech = "cap_yi < 20 AND dd < -0.4 AND ndays >= 200"
    gate_age = "first_date BETWEEN DATE '2021-11-11' AND DATE '2024-11-11'"

    if args.include_st:
        tier_a_where = f"{gate_tech} AND {gate_age} AND {gate_fund}"
    else:
        tier_a_where = f"{gate_tech} AND {gate_age} AND {gate_fund} AND st = ''"

    show(con, "Tier A 微盘<20亿+超跌>40%+上市2-5年+盈利加速/转正", tier_a_where, args.limit)
    show(con, "Tier B 微盘<20亿+超跌>40%+盈利加速/转正（不限上市年，≥1年历史）",
         f"{gate_tech} AND {gate_fund}", args.limit)

    print("\n=== 微盘<20亿+超跌>40% 的净利状态分布（≥1年历史） ===")
    for r in con.execute(
        f"""SELECT kind, count(*) FROM cand WHERE {gate_tech} GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall():
        print(f"  {r[0]:<8}{r[1]}")
    print("\n=== 其中 ST（在 加速/转正 里） ===")
    print("  ", con.execute(
        f"SELECT count(*) FROM cand WHERE {gate_tech} AND {gate_fund} AND name LIKE '%ST%'"
    ).fetchone()[0])
    print("\n[提示] 低基 = 净利同比>300%，是去年基数近零的百分比放大，非真成长。ST 集中出现在 Tier B（老壳股保壳转正），「上市2-5年」门槛会自动剔光。名单为初筛，下手前逐只核质地。")


if __name__ == "__main__":
    main()
