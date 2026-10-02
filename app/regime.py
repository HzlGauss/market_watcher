"""分层广度 regime：离线预计算 + 盘中读取（空间换时间）。

回测发现「该股所在市值层/板块内站上MA20占比」是买卖信号的强 regime 原语
（见 ``.claude/skills/intraday-signal/backtest_buy_side.py`` / ``backtest_sell_side.py``）。
它是全市场横截面，盘中逐只算太贵 → 离线每日算一次写 ``state/regime.json``（空间），
盘中只做 JSON 查表（时间）；缺失/过期回退「中证1000 MA20/MA60」。

``update_regime()`` 属离线层（惰性 import duckdb/pandas/akshare）；``get_regime()``
纯 Python（只 import 标准库），供热路径（scan_position / left-side / right-side 等）
调用，不引入重依赖，符合项目「no pandas 热路径」约定。
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REGIME_PATH = _ROOT / "state" / "regime.json"
_SIZE_CACHE = _ROOT / "state" / "size_tier_map.json"

_SIZE_TIERS = (("000300", "沪深300"), ("000905", "中证500"), ("000852", "中证1000"))
_BOARD_PREFIX = {"60": "沪主板", "00": "深主板", "30": "创业板", "68": "科创板"}
_STALE_DAYS = 7  # asof 距今日超过 7 自然日视为过期


# ---------------------------------------------------------------- 市值分层（读缓存）

def _size_tier_map() -> dict[str, str]:
    if not _SIZE_CACHE.exists():
        _build_size_tier_map()
    try:
        return json.loads(_SIZE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_size_tier_map() -> None:
    """用中证官网权威成分建 size_tier_map.json（akshare 惰性 import，只建一次）。"""
    mapping: dict[str, str] = {}
    try:
        import akshare as ak

        for sym, label in _SIZE_TIERS:
            df = ak.index_stock_cons_csindex(symbol=sym)
            for c in df["成分券代码"]:
                code = re.sub(r"\D", "", str(c)).zfill(6)
                if code:
                    mapping[code] = label
    except Exception:
        return
    if mapping:
        try:
            _SIZE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _SIZE_CACHE.write_text(
                json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass


def get_size_tier(code: str) -> str:
    """6 位代码 → 市值分层（未命中 = 微盘）。纯 JSON 读，热路径安全。"""
    return _size_tier_map().get(str(code)[:6], "微盘")


def _board_of(code: str) -> str:
    return _BOARD_PREFIX.get(str(code)[:2], "其他")


# ---------------------------------------------------------------- 离线预计算

def _threshold(breadth: float) -> str:
    return "牛市" if breadth >= 0.6 else ("熊市" if breadth <= 0.4 else "震荡")


def update_regime(days: int = 250) -> dict:
    """离线重算分层广度 regime，写 state/regime.json，返回写入的 dict。

    只算近 ``days`` 自然日的横截面（足够 5 日平滑 + 历史缓冲），当日同步后再跑。
    """
    import duckdb
    import numpy as np
    import pandas as pd

    con = duckdb.connect(str(_ROOT / "data" / "market.duckdb"), read_only=True)
    df = con.execute(f"""
        SELECT thscode, date, close
        FROM v_daily_qfq
        WHERE substr(thscode,1,2) IN ('60','68','00','30')
          AND date >= current_date - interval {days} day
        ORDER BY thscode, date
    """).df()
    con.close()

    df["code"] = df["thscode"].str[:6]
    size_map = _size_tier_map()
    df["size"] = df["code"].map(size_map).fillna("微盘")
    df["board"] = df["code"].str[:2].map(_BOARD_PREFIX).fillna("其他")

    df["ma20"] = df.groupby("thscode", sort=False)["close"].transform(
        lambda s: s.rolling(20).mean()
    )
    above = (df["close"] > df["ma20"]).astype(float)

    def _breadth(dim: str) -> pd.Series:
        raw = above.groupby([df[dim], df["date"]]).mean()
        return raw.groupby(level=0, group_keys=False).apply(
            lambda s: s.rolling(5, min_periods=1).mean()
        )

    asof = str(df["date"].max())[:10]
    out: dict = {"asof": asof, "size": {}, "board": {}, "history": {"size": {}, "board": {}}}
    for dim, series in (("size", _breadth("size")), ("board", _breadth("board"))):
        for key in series.index.get_level_values(0).unique():
            s = series.xs(key, level=0)
            latest = float(s.iloc[-1])
            out[dim][key] = {"regime": _threshold(latest), "breadth": round(latest, 4)}
            out["history"][dim][key] = [[str(d)[:10], round(float(b), 4)] for d, b in s.items()]

    _REGIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGIME_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 盘中读取

def _load_fresh() -> dict | None:
    """读 state/regime.json；过期/缺失/解析失败返回 None。"""
    try:
        data = json.loads(_REGIME_PATH.read_text(encoding="utf-8"))
        asof = data.get("asof", "")
        try:
            fresh = (datetime.date.today() - datetime.date.fromisoformat(asof)).days <= _STALE_DAYS
        except (TypeError, ValueError):
            fresh = False
        return data if fresh else None
    except Exception:
        return None


def get_regime(code: str) -> dict | None:
    """盘中查 regime：读 state/regime.json，过期/缺失回退中证1000 MA20/MA60。

    返回 {"size_regime", "board_regime", "size_breadth", "board_breadth",
          "asof", "source"}；完全不可用返回 None（调用方回退「不区分 regime」）。
    """
    tier = get_size_tier(code)
    board = _board_of(code)
    data = _load_fresh()
    if data:
        s = data.get("size", {}).get(tier, {})
        b = data.get("board", {}).get(board, {})
        return {
            "size_regime": s.get("regime", "震荡"),
            "board_regime": b.get("regime", "震荡"),
            "size_breadth": s.get("breadth"),
            "board_breadth": b.get("breadth"),
            "asof": data.get("asof", ""),
            "source": "precomputed",
        }
    return _index_regime_fallback()


def get_board_regime(code: str) -> dict | None:
    """仅按板块广度查 regime（不触发 size tier 构建、不回退 duckdb），供热路径门控用。

    返回 {"regime", "board", "asof"}；过期/缺失返回 None（调用方不门控）。
    """
    board = _board_of(code)
    data = _load_fresh()
    if not data:
        return None
    b = data.get("board", {}).get(board, {})
    return {
        "regime": b.get("regime", "震荡"),
        "board": board,
        "asof": data.get("asof", ""),
    }


def gate_position(code: str, stage: str, action: str) -> dict:
    """按该股所在板块广度 regime 门控加减仓信号（回测结论固化，见 backtest_buy/sell_side）。

    action: 'add' / 'reduce'；stage: detect_stage 阶段名（启动期/磨底期/下跌期/赶顶期/派发期）。
    返回 {"ok", "note", "regime", "board"}；regime 不可用返回 ok=True（不门控）。

    规则（20日净edge 回测）：
    - 加仓·启动期（右侧突破/追涨）：熊市反向（接飞刀），牛市/震荡有效
    - 加仓·磨底期（左侧埋伏）：牛市反向（超跌是真弱），熊市/震荡有效
    - 加仓·下跌期（左侧接刀/抄底）：仅熊市有效
    - 减仓·赶顶期：牛市反向（动量持续别卖飞），熊市/震荡有效
    - 减仓·派发期：科创板全 regime 有效；主板仅牛市有效、熊市反向
    """
    reg = get_board_regime(code)
    out = {"ok": True, "note": "", "regime": "", "board": ""}
    if not reg:
        return out
    r = reg["regime"]
    board = reg["board"]
    out["regime"] = r
    out["board"] = board
    if action == "add":
        if stage == "启动期" and r == "熊市":
            out["ok"] = False
            out["note"] = "熊市禁右侧追涨：广度低时突破是接飞刀"
        elif stage == "磨底期" and r == "牛市":
            out["ok"] = False
            out["note"] = "牛市禁左侧埋伏：广度高时超跌是真弱"
        elif stage == "下跌期" and r != "熊市":
            out["ok"] = False
            out["note"] = "左侧接刀仅熊市有效：牛/震荡下接刀是接飞刀"
    elif action == "reduce":
        if stage == "赶顶期" and r == "牛市":
            out["ok"] = False
            out["note"] = "牛市禁赶顶减仓：动量持续，别卖飞"
        elif stage == "派发期" and board != "科创板" and r != "牛市":
            out["ok"] = False
            out["note"] = "派发减仓仅牛市有效（科创板除外）"
    return out


def gate_side(code: str, side: str) -> tuple[bool, str]:
    """按该股所在板块广度 regime 门控选股方向（回测结论固化，见 backtest_buy_side）。

    side: 'left'（左侧超跌埋伏/黄金坑）或 'right'（右侧突破追涨/龙回头）。
    返回 (允许, 说明)；regime 不可用返回 (True, "")。

    回测 20日净edge：左侧(深坑/抄底)牛市反向、熊市有效；右侧(突破/追涨)熊市反向、牛市有效。
    """
    reg = get_board_regime(code)
    if not reg:
        return True, ""
    r = reg["regime"]
    if side == "left" and r == "牛市":
        return False, "牛市禁左侧埋伏：广度高时超跌是真弱"
    if side == "right" and r == "熊市":
        return False, "熊市禁右侧追涨：广度低时突破是接飞刀"
    return True, ""


# ---------------------------------------------------------------- 组合策略 regime 标注

# 12 个组合策略（app.strategy.evaluate_all_strategies）→ (类别, 相悖警示)。
# 类别: right_buy(右侧追涨,熊市相悖) / left_buy(左侧埋伏,牛市相悖) /
#       top_escape(赶顶减仓,牛市相悖) / distribution(派发减仓,主板非牛相悖) /
#       stop_loss(MA止损,牛/震荡相悖)。「震荡套利」按 direction 拆分，不在此表。
# 注意: 「缩量洗盘」「低位放量启动」归 right_buy 是语义推断、未回测（警示带「未回测」）。
_STRATEGY_CLASS: dict[str, tuple[str, str]] = {
    "趋势启动": ("right_buy", "熊市禁右侧追涨：广度低时突破是接飞刀"),
    "放量突破确认": ("right_buy", "熊市禁右侧追涨：广度低时突破是接飞刀"),
    "均线多头回踩": ("right_buy", "熊市禁右侧追涨：广度低时突破是接飞刀"),
    "均线金叉": ("right_buy", "熊市禁右侧追涨：广度低时突破是接飞刀"),
    "缩量洗盘": ("right_buy", "熊市禁右侧追涨（未回测）"),
    "低位放量启动": ("right_buy", "熊市禁右侧追涨（未回测）"),
    "双翼齐飞": ("left_buy", "牛市禁左侧埋伏：广度高时超跌是真弱"),
    "地量地价反转": ("left_buy", "牛市禁左侧埋伏：广度高时超跌是真弱"),
    "逃顶组合": ("top_escape", "牛市禁赶顶减仓：动量持续，别卖飞"),
    "高位放量滞警": ("distribution", "派发减仓仅牛市有效（科创板除外）"),
    "均线空头反弹": ("stop_loss", "MA止损仅熊市弱有效"),
    "均线死叉": ("stop_loss", "MA止损仅熊市弱有效"),
}


def gate_strategy(code: str, strategy_name: str, direction: str) -> dict:
    """组合策略 regime 标注（标注不抑制，与 gate_position/gate_side 的硬门控不同）。

    strategy_name/direction 来自 CombinationSignal。返回 {"regime", "board", "warning"}；
    warning 非空 = 该策略与当前 regime 相悖，调用方在告警文本后附加 ⚠️ 警示。
    regime 未知返回全空（不标注）。
    """
    reg = get_board_regime(code)
    out = {"regime": "", "board": "", "warning": ""}
    if not reg:
        return out
    r = reg["regime"]
    out["regime"] = r
    out["board"] = reg["board"]

    if strategy_name == "震荡套利":
        cls = "left_buy" if direction == "buy" else "top_escape"
        warn = (
            "牛市禁左侧埋伏：广度高时超跌是真弱"
            if direction == "buy"
            else "牛市禁赶顶减仓：动量持续，别卖飞"
        )
    else:
        cls, warn = _STRATEGY_CLASS.get(strategy_name, ("", ""))

    if cls == "right_buy" and r == "熊市":
        out["warning"] = warn
    elif cls == "left_buy" and r == "牛市":
        out["warning"] = warn
    elif cls == "top_escape" and r == "牛市":
        out["warning"] = warn
    elif cls == "stop_loss" and r != "熊市":
        out["warning"] = warn
    elif cls == "distribution" and out["board"] != "科创板" and r != "牛市":
        out["warning"] = warn
    return out


def annotate_strategy_alert(code: str, strategy_name: str, direction: str, text: str) -> str:
    """给组合策略告警文本加 regime 标注（[牛/熊/震荡] + ⚠️ 相悖警示），不抑制。"""
    g = gate_strategy(code, strategy_name, direction)
    if not g["regime"]:
        return text
    suffix = f"  [{g['regime']}]"
    if g["warning"]:
        suffix += f" ⚠️ {g['warning']}"
    return text + suffix


def _index_regime_fallback() -> dict | None:
    """回退：中证1000 MA20/MA60 相对位置 → 全层同一 regime（区分度低于广度）。"""
    try:
        import duckdb

        con = duckdb.connect(str(_ROOT / "data" / "market.duckdb"), read_only=True)
        df = con.execute(
            "select date, close from v_daily_qfq where thscode='000852.SZ' order by date"
        ).df()
        con.close()
        c = df.set_index("date")["close"]
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        ratio = ma20 / ma60 - 1
        r = "牛市" if ratio > 0.02 else ("熊市" if ratio < -0.02 else "震荡")
        return {
            "size_regime": r, "board_regime": r,
            "size_breadth": None, "board_breadth": None,
            "asof": str(df["date"].max())[:10], "source": "index_fallback",
        }
    except Exception:
        return None


if __name__ == "__main__":
    if "--update" in sys.argv:
        out = update_regime()
        print(f"regime 已更新 → {_REGIME_PATH}")
        print(f"  asof={out['asof']}")
        for dim in ("size", "board"):
            print(f"  {dim}: " + "  ".join(
                f"{k}={v['regime']}({v['breadth']})" for k, v in out[dim].items()
            ))
    else:
        # 冒烟：随便查几只
        for code in ("600519", "300750", "000001", "688981"):
            print(code, get_size_tier(code), get_regime(code))
