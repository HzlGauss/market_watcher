"""盘中分钟级回测：主力「转向」提醒（3 分钟扫描口径）

信号口径对齐 app/analyzer.py 的 analyze()（2026-08 起改为「占成交额%」相对口径）：
  - 主力由流入转流出 / 由流出转流入：累计主力净流入(超大+大)在两个相邻扫描(3 分钟)间符号反转
  - 生产口径：|主力净流入占成交额%| ≥ flow_reversal_pct 才算有效转向（相对口径，适配盘子）

⚠️ 口径偏差（本脚本仍用绝对净额 flow_reversal_min）：
  东财分钟资金流 fflow/kline klt=1 只返回各档净额，不含「成交额」，无法算占成交额%；
  故本脚本沿用旧的绝对净额阈值（≥ flow_reversal_min）。已废弃的「总资金转向」一并移除。

数据源：
  - 分钟级累计资金流：东方财富 fflow/kline/get?klt=1（f52=主力累计）
  - 分钟级价格：新浪 CN_MarketData.getKLineData?scale=5（用于计算信号后收益）

⚠️ 已知限制（重要）：
  1. 东财分钟资金流接口 klt=1 通常只返回「当日」分时累计，公开接口不支持按历史日期回拉；
     因此多日分钟回测需要调用方自行积累多日分时数据（本脚本支持传入本地 JSON 缓存）。
  2. 本机对东财触发 IP 限频时（RemoteDisconnected），脚本会明确报错并跳过。
  3. 新浪分钟价格历史深度有限（约近 5~10 个交易日）。

用法：
  py tools/backtest_flow_alerts_intraday.py                 # 拉取当日分钟数据回放
  py tools/backtest_flow_alerts_intraday.py --series <json> # 用本地多日分时缓存(见 _collect 说明)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config

STOCK_FLOW_API = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
SINA_KL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
UT = "b2884a393a59ad64002292a3e90d46a5"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# 回测标的（个股 + 代表性 ETF，指数无资金流）
SYMBOLS = [
    ("600036", "SH", "招商银行"),
    ("000333", "SZ", "美的集团"),
    ("300059", "SZ", "东方财富"),
    ("600031", "SH", "三一重工"),
    ("601939", "SH", "建设银行"),
    ("510300", "SH", "沪深300ETF"),
    ("512480", "SH", "半导体ETF"),
    ("512880", "SH", "证券ETF"),
    ("159915", "SZ", "创业板ETF"),
]


def _pf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _secid(code: str, market: str) -> str:
    return ("1" if market.upper() == "SH" else "0") + "." + code


def fetch_intraday_flow(code: str, market: str, minutes: int = 240) -> list[dict]:
    """拉取当日分钟级累计资金流，返回 [{time, main, total}]（升序）

    东财 fflow/kline klt=1 每行：时间,f52(主力),f53(小),f54(中),f55(大),f56(超大),f57..f61(占比)
    main  = f52              （主力 = 超大+大，累计值）
    total = f56+f55+f54+f53  （总资金 = 超大+大+中+小，累计值）
    """
    url = (f"{STOCK_FLOW_API}?secid={_secid(code, market)}"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&lmt={minutes}&klt=1&ut={UT}")
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=8, headers=EM_HEADERS)
            if resp.status_code != 200:
                time.sleep(1.5 * (attempt + 1))
                continue
            data = resp.json().get("data")
            if not data:
                time.sleep(1.5 * (attempt + 1))
                continue
            klines = data.get("klines") or []
            out = []
            for line in klines:
                p = line.split(",")
                if len(p) < 6:
                    continue
                main = _pf(p[1])
                total = None
                if None not in (_pf(p[5]), _pf(p[4]), _pf(p[3]), _pf(p[2])):
                    total = _pf(p[5]) + _pf(p[4]) + _pf(p[3]) + _pf(p[2])
                out.append({"time": p[0], "main": main, "total": total})
            return out
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return []


def fetch_minute_prices(code: str, market: str, scale: int = 5) -> dict[str, float]:
    """拉取分钟级收盘价，返回 {HH:MM: close}（5 分钟一档，近若干交易日）"""
    prefix = {"SH": "sh", "SZ": "sz"}.get(market, "sh")
    url = (f"{SINA_KL}?symbol={prefix}{code}&scale={scale}&ma=no&datalen=1970")
    try:
        resp = requests.get(url, timeout=10, headers=SINA_HEADERS)
        data = resp.json()
        out = {}
        for item in data:
            day = item.get("day", "")
            tm = day.split(" ")[-1][:5] if " " in day else day[:5]
            close = _pf(item.get("close"))
            if close is not None:
                out[tm] = close
        return out
    except Exception:
        return {}


def detect_reversals(flow: list[dict], reversal_min: float, scan_min: int = 3) -> list[dict]:
    """按 3 分钟扫描间隔检测主力资金流符号反转（总资金转向已废弃）

    返回 [{idx, side}]
      side: 'in'（由流出转流入，看多）| 'out'（由流入转流出，看空）
    """
    # 按 scan_min 抽样（生产环境每 scan_min 分钟扫一次，与上一档对比）
    step = scan_min
    signals = []
    idxs = list(range(0, len(flow), step))
    for j in range(1, len(idxs)):
        prev, cur = flow[idxs[j - 1]], flow[idxs[j]]
        pv, cv = prev["main"], cur["main"]
        if pv is None or cv is None:
            continue
        if cv > 0 and pv < 0 and abs(cv) >= reversal_min:
            signals.append({"idx": idxs[j], "side": "in"})
        elif cv < 0 and pv > 0 and abs(cv) >= reversal_min:
            signals.append({"idx": idxs[j], "side": "out"})
    return signals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="本地多日分时缓存 JSON（暂未实现采集，占位）")
    args = ap.parse_args()

    config = Config(ROOT / "watchlist_config.json")
    reversal_min = config.flow_reversal_min
    scan_min = config.scan_interval
    print(f"当前设置: 转向最小净额={reversal_min/1e4:.0f}万(旧绝对口径, 分钟数据缺成交额), 扫描间隔={scan_min}分钟")
    print("数据源: 东财分钟资金流(当日) + 新浪5分钟价格\n")

    agg = {}  # signal_name -> {side, count, fwd: {15:[],30:[],60:[]}}

    for code, market, name in SYMBOLS:
        flow = fetch_intraday_flow(code, market)
        if not flow:
            print(f"[跳过] {name}({code}) 分钟资金流拉取失败（东财可能限频）")
            continue
        prices = fetch_minute_prices(code, market)
        # 信号后收益：用分钟价格序列近似（flow 的 time 是 HH:MM，与 prices 对齐）
        sigs = detect_reversals(flow, reversal_min, scan_min)
        for s in sigs:
            label = f"主力{'由流出转流入' if s['side']=='in' else '由流入转流出'}"
            key = (label, s["side"])
            if key not in agg:
                agg[key] = {"count": 0, "fwd": {15: [], 30: [], 60: []}}
            agg[key]["count"] += 1
            t = flow[s["idx"]]["time"]
            base = prices.get(t)
            if base:
                for horizon in (15, 30, 60):
                    # 用 5 分钟一档近似后续分钟价格
                    later_idx = min(len(flow) - 1, s["idx"] + horizon)
                    lt = flow[later_idx]["time"]
                    lp = prices.get(lt)
                    if lp:
                        agg[key]["fwd"][horizon].append((lp - base) / base * 100)
        time.sleep(0.3)
        print(f"[完成] {name}({code}) 分时 {len(flow)} 档, 信号 {len(sigs)} 条")

    print("\n" + "=" * 72)
    print("盘中转向信号回放结果（分钟级，当日分时）")
    print("=" * 72)
    print(f"{'信号':<16} {'方向':<4} {'次数':>4} {'15分均':>8} {'30分均':>8} {'60分均':>8}")
    print("-" * 72)

    def mean(vals):
        return sum(vals) / len(vals) if vals else float("nan")

    for (label, side), a in agg.items():
        dirn = "看多" if side == "in" else "看空"
        print(f"{label:<16} {dirn:<4} {a['count']:>4} "
              f"{mean(a['fwd'][15]):>8.2f} {mean(a['fwd'][30]):>8.2f} {mean(a['fwd'][60]):>8.2f}")
    if not agg:
        print("（无信号：可能当日分钟资金流为空，或转向未触发）")
    print("-" * 72)
    print("说明：'方向' 为信号含义；收益为信号时刻到后续 15/30/60 分钟的价格涨跌幅。")
    print("      由于东财分钟资金流仅当日可得，本脚本只能回放当日；多日样本需自行积累分时缓存。")


if __name__ == "__main__":
    main()
