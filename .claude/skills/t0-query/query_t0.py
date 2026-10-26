#!/usr/bin/env python3
"""查询当前做T信号快照（state/t0_signals.json）。

读取 T0MonitorThread 每轮扫描后覆盖写的最新信号快照，输出：
  - 无参数：列出当前所有做T信号（买/卖，含挂单价 + 支撑/压力）
  - <代码>：查单个标的的完整信号详情（买/卖挂单价 + 支撑压力 + RR + 触发时间）

数据源: state/t0_signals.json（由 app/t0_monitor.py 的 _write_snapshot 覆盖写）。
注意: 快照是「最新信号缓存」，信号超过 10 分钟未更新会被清理（age_sec 越大越旧）。
"""
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = _ROOT / "state" / "t0_signals.json"

_TYPE_LABEL = {"buy": "买入", "sell": "卖出", "none": "无"}
_BB_SHORT = {"触及上轨": "上轨", "触及下轨": "下轨", "偏强": "偏强", "偏弱": "偏弱", "中性": "中"}


def _load():
    if not SNAPSHOT.exists():
        return None
    try:
        with open(SNAPSHOT, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fmt(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "--"


def _show_one(snap, code):
    sig = snap.get("signals", {}).get(code)
    if not sig:
        hits = [c for c, s in snap.get("signals", {}).items()
                if code in c or code in str(s.get("name", ""))]
        if len(hits) == 1:
            sig = snap["signals"][hits[0]]
            code = hits[0]
        elif len(hits) > 1:
            print(f"匹配到多个代码: {', '.join(sorted(hits))}，请指定完整代码")
            return
        else:
            print(f"快照中无 {code} 的做T信号（可能尚未触发，或信号已过期被清理）")
            return

    t = _TYPE_LABEL.get(sig.get("signal_type"), sig.get("signal_type"))
    age = sig.get("age_sec", 0)
    print(f"\n  {t}信号  {sig.get('name')}({code})  "
          f"[快照更新于 {snap.get('updated_at', '')}，信号 {age} 秒前]")
    print(f"    现价:           {_fmt(sig.get('price'))}")
    print(f"    建议买入挂单:   {_fmt(sig.get('buy_price'))}")
    print(f"    建议卖出挂单:   {_fmt(sig.get('sell_price'))}")
    print(f"    支撑: {_fmt(sig.get('support'))}    压力: {_fmt(sig.get('resistance'))}")
    # 布林带（5分钟K线 period=20）
    bb_upper, bb_middle, bb_lower = sig.get("bb_upper"), sig.get("bb_middle"), sig.get("bb_lower")
    if bb_upper or bb_middle or bb_lower:
        print(f"    布林带: 上轨 {_fmt(bb_upper)} / 中轨 {_fmt(bb_middle)} / 下轨 {_fmt(bb_lower)}")
        bw = sig.get("bb_width")
        bs = sig.get("bb_signal")
        extra = []
        if bw is not None:
            extra.append(f"带宽 {bw:.1f}%")
        if bs:
            extra.append(f"位置 {bs}")
        if sig.get("bb_squeeze"):
            extra.append(f"挤压({sig.get('bb_squeeze_note') or '变盘临近'})")
        if extra:
            print(f"               {' / '.join(extra)}")
    rr = sig.get("risk_reward", 0)
    print(f"    盈亏比 RR:      {rr:.1f}x" if rr else "    盈亏比 RR:      --")
    reason = sig.get("reason", "")
    if reason:
        print(f"    触发条件: {reason}")
    print()


def _show_all(snap):
    signals = snap.get("signals", {})
    if not signals:
        print(f"当前无做T信号（快照更新于 {snap.get('updated_at', '')}，可能尚未触发）")
        return
    print(f"\n做T信号快照（更新于 {snap.get('updated_at', '')}，共 {len(signals)} 个）\n")
    print(f"  {'代码':<8}{'名称':<10}{'方向':<6}{'现价':>9}{'买挂单':>9}"
          f"{'卖挂单':>9}{'支撑':>9}{'压力':>9}{'上/下轨':>16}{'布林':>6}{'RR':>6}{'龄(秒)':>7}")
    print("  " + "─" * 106)
    for code, s in sorted(signals.items(), key=lambda kv: -kv[1].get("age_sec", 0)):
        t = _TYPE_LABEL.get(s.get("signal_type"), s.get("signal_type"))
        rr = s.get("risk_reward", 0)
        rr_s = f"{rr:.1f}" if rr else "--"
        bb = _BB_SHORT.get(s.get("bb_signal"), "—")
        bb_up, bb_lo = s.get("bb_upper"), s.get("bb_lower")
        band = "--" if bb_up is None and bb_lo is None else f"{_fmt(bb_up)}/{_fmt(bb_lo)}"
        print(f"  {code:<8}{str(s.get('name', ''))[:8]:<10}{t:<6}"
              f"{_fmt(s.get('price')):>9}{_fmt(s.get('buy_price')):>9}"
              f"{_fmt(s.get('sell_price')):>9}{_fmt(s.get('support')):>9}"
              f"{_fmt(s.get('resistance')):>9}{band:>16}{bb:>6}{rr_s:>6}{s.get('age_sec', 0):>7}")
    print()


def main():
    snap = _load()
    if snap is None:
        print("未找到 state/t0_signals.json —— 做T监控线程可能尚未运行，或尚未触发任何信号。")
        print("启动监控（python __main__.py 选项 4，开启做T监控）后会自动生成。")
        return
    args = [a for a in sys.argv[1:] if a.strip()]
    if args:
        _show_one(snap, args[0].strip())
    else:
        _show_all(snap)


if __name__ == "__main__":
    main()
