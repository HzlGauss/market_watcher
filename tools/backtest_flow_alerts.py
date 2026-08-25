"""回测资金流提醒信号：主力转向 + 价量背离，在近期日线数据上的表现

信号口径对齐 app/analyzer.py 的 analyze()（2026-08 起改为「占成交额%」相对口径）：
  1. 主力资金由流入转流出 / 由流出转流入（跨日符号反转，|主力占成交额%| ≥ flow_reversal_pct）
  2. 价升资金流出（涨幅 > flow_diverge_pct 且 主力净流出占比 ≥ 卖出阈值）
  3. 价跌资金流入（跌幅 > flow_diverge_pct 且 主力净流入占比 ≥ 背离阈值）

已废弃口径（总资金转向/总资金背离）：主力(超大+大)与散户(小单)天然反向，总资金≈0 是噪音。

数据源：新浪 MoneyFlow.ssl_qsfx_lscjfb（东方财富 fflow 历史接口当日被限频，
新浪提供 超大/大/中/小单 完整分类，且自带收盘价与涨跌幅，无需二次取价）。

字段映射（对齐东财口径）：
  主力 net = r0_net(超大) + r1_net(大)
  总资金 net = r0_net + r1_net + r2_net(中) + r3_net(小)  (= netamount)
  涨跌幅%  = changeratio * 100

用「日线」粒度近似生产环境的「扫描周期」粒度——生产是 3 分钟一档的盘中累计，
这里是收盘后当日累计，转向信号的含义更偏「跨日」，仅供评估信号方向的有效性。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config

SINA_FLOW_API = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_lscjfb"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# 回测标的：个股 + 代表性 ETF（指数无资金流，跳过）
SYMBOLS = [
    ("600036", "SH", "招商银行", "stock"),
    ("000333", "SZ", "美的集团", "stock"),
    ("300059", "SZ", "东方财富", "stock"),
    ("600031", "SH", "三一重工", "stock"),
    ("601939", "SH", "建设银行", "stock"),
    ("600143", "SH", "金发科技", "stock"),
    ("510300", "SH", "沪深300ETF", "etf"),
    ("512480", "SH", "半导体ETF", "etf"),
    ("512880", "SH", "证券ETF", "etf"),
    ("159915", "SZ", "创业板ETF", "etf"),
    ("513180", "SH", "恒生科技ETF", "etf"),
    ("512100", "SH", "中证1000ETF", "etf"),
]

DIVERGE_THRESHOLD = {"stock": 5, "etf": 4, "index": 3}  # 价跌+主力流入的净占比阈值，对齐 analyze()
SELL_THRESHOLD = {"stock": 10, "etf": 7, "index": 5}  # 价升+主力流出的净占比阈值，对齐 analyze()


def _pf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_flow(code: str, market: str, days: int = 100) -> list[dict]:
    """拉取新浪资金流历史，返回按日期升序的原始记录列表"""
    prefix = {"SH": "sh", "SZ": "sz"}.get(market, "sh")
    daima = f"{prefix}{code}"
    url = (f"{SINA_FLOW_API}?daima={daima}"
           f"&page=1&num={days}&sort=opendate&asc=0")
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=10, headers=HEADERS)
            if resp.status_code != 200:
                time.sleep(1.5)
                continue
            data = resp.json()
            if not isinstance(data, list) or not data:
                time.sleep(1.5)
                continue
            return list(reversed(data))  # asc=0 返回最新在前，反转为升序
        except Exception:
            time.sleep(1.5)
    return []


def main() -> None:
    config = Config(ROOT / "watchlist_config.json")
    reversal_pct = config.flow_reversal_pct
    diverge_price = config.flow_diverge_pct
    print(f"当前设置: 资金流转向最小占比={reversal_pct:.1f}%, 资金背离价格阈值={diverge_price:.1f}%")
    print(f"数据源: 新浪 MoneyFlow 历史成交分布（超大/大/中/小单）\n")

    # 信号聚合: name -> {bull, count, fwd: {1:[],3:[],5:[]}}
    agg: dict[str, dict] = {}
    # 基准样本：全部对齐日期的未来收益
    base1, base5 = [], []

    def record(name: str, bull: bool, i: int, days_list: list[dict]) -> None:
        if name not in agg:
            agg[name] = {"bull": bull, "count": 0, "fwd": {1: [], 3: [], 5: []}}
        agg[name]["count"] += 1
        for k in (1, 3, 5):
            if i + k < len(days_list):
                c0 = days_list[i]["close"]
                ck = days_list[i + k]["close"]
                if c0:
                    agg[name]["fwd"][k].append((ck - c0) / c0 * 100)

    for code, market, name, typ in SYMBOLS:
        raw = fetch_flow(code, market, days=100)
        if not raw:
            print(f"[跳过] {name}({code}) 数据不足")
            continue

        days_list = []
        for rec in raw:
            r0 = _pf(rec.get("r0")); r1 = _pf(rec.get("r1"))
            r2 = _pf(rec.get("r2")); r3 = _pf(rec.get("r3"))
            r0n = _pf(rec.get("r0_net")); r1n = _pf(rec.get("r1_net"))
            r2n = _pf(rec.get("r2_net")); r3n = _pf(rec.get("r3_net"))
            # 主力 = 超大 + 大
            main_net = (r0n + r1n) if (r0n is not None and r1n is not None) else None
            # 总资金 = 超大 + 大 + 中 + 小（对齐 FundFlowDetail.total_net）
            total_net = (r0n + r1n + r2n + r3n) if None not in (r0n, r1n, r2n, r3n) else None
            # 成交额 = 四类成交额之和
            amount = (r0 + r1 + r2 + r3) if None not in (r0, r1, r2, r3) else None
            # 主力净占比 = 主力净额 / 成交额 * 100
            main_pct = (main_net / amount * 100) if (main_net is not None and amount) else None
            chg = _pf(rec.get("changeratio"))
            days_list.append({
                "date": rec.get("opendate"),
                "close": _pf(rec.get("trade")),
                "chg": chg * 100 if chg is not None else None,
                "main": main_net, "total": total_net, "main_pct": main_pct,
            })

        div_thr = DIVERGE_THRESHOLD.get(typ, 5)
        sell_thr = SELL_THRESHOLD.get(typ, 10)
        for i in range(1, len(days_list)):
            prev, cur = days_list[i - 1], days_list[i]
            # 基准（无筛选）
            for k, bucket in ((1, base1), (5, base5)):
                if i + k < len(days_list) and cur["close"]:
                    bucket.append((days_list[i + k]["close"] - cur["close"]) / cur["close"] * 100)
            # 主力转向（相对口径：|主力占成交额%| ≥ reversal_pct）
            if prev["main"] is not None and cur["main"] is not None and cur["main_pct"] is not None:
                if cur["main"] > 0 and prev["main"] < 0 and cur["main_pct"] >= reversal_pct:
                    record("主力由流出转流入", True, i, days_list)
                elif cur["main"] < 0 and prev["main"] > 0 and abs(cur["main_pct"]) >= reversal_pct:
                    record("主力由流入转流出", False, i, days_list)
            # 价量背离（仅主力口径，占成交额%）
            chg = cur["chg"]
            if chg is None:
                continue
            if chg > diverge_price:
                out = (cur["main"] is not None and cur["main"] < 0
                       and cur["main_pct"] is not None and abs(cur["main_pct"]) >= sell_thr)
                if out:
                    record("价升资金流出", False, i, days_list)
            elif chg < -diverge_price:
                main_in = (cur["main"] is not None and cur["main"] > 0
                           and cur["main_pct"] is not None and cur["main_pct"] >= div_thr)
                if main_in:
                    record("价跌资金流入", True, i, days_list)

        time.sleep(0.3)
        print(f"[完成] {name}({code}) 对齐 {len(days_list)} 个交易日")

    # ---- 汇总输出 ----
    print("\n" + "=" * 92)
    print("回测结果（日线粒度，近期约 100 个交易日）")
    print("=" * 92)
    print(f"{'信号':<14} {'方向':<4} {'次数':>4} {'1日均':>8} {'3日均':>8} {'5日均':>8} {'胜率(1日)':>10} {'胜率(5日)':>10}")
    print("-" * 92)
    order = ["主力由流入转流出", "主力由流出转流入",
             "价升资金流出", "价跌资金流入"]
    for name in order:
        a = agg.get(name)
        if not a:
            continue
        bull = a["bull"]

        def mean(vals):
            return sum(vals) / len(vals) if vals else float("nan")

        def hit(vals):
            if not vals:
                return float("nan")
            if bull:
                return sum(1 for v in vals if v > 0) / len(vals) * 100
            return sum(1 for v in vals if v < 0) / len(vals) * 100

        f1, f3, f5 = a["fwd"][1], a["fwd"][3], a["fwd"][5]
        dirn = "看多" if bull else "看空"
        print(f"{name:<14} {dirn:<4} {a['count']:>4} "
              f"{mean(f1):>8.2f} {mean(f3):>8.2f} {mean(f5):>8.2f} "
              f"{hit(f1):>9.1f}% {hit(f5):>9.1f}%")
    print("-" * 92)
    print("说明：'胜率' = 未来收益方向与信号方向一致的比例（看多→正收益，看空→负收益）。")
    print("      日线粒度为收盘口径，生产环境的转向信号是盘中 3 分钟一档，方向有效性可参考、幅度需打折。")

    if base1 and base5:
        print(f"\n[基准对照] 全样本 {len(base1)} 个交易日的平均未来收益（无筛选）:")
        print(f"  1 日均收益 {sum(base1)/len(base1):+.3f}% | 5 日均收益 {sum(base5)/len(base5):+.3f}%")


if __name__ == "__main__":
    main()
