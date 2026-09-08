"""板块/行业 → 成分股池解析（供左右侧选股扫描共用）

输入一个板块/行业（创业板 / 科创板 / 沪深300 / 中证500 / 中证1000 / 半导体 / 芯片 /
化工 ...），解析出成分股池 ``{code: {"name": ..., "industry": ..., "source": ...}}``。

数据源（按优先级）：
1. 指数成分：akshare ``index_stock_cons``（沪深300/中证500/中证1000/上证50/中证红利 ...）
2. 板（创业板/科创板/沪深主板/深主板）：东方财富 clist 全市场快照按 ``fs`` 过滤
3. 精确关键字优先映射（``KEY_BOARD_PRIORITY``）：白酒/化工/光伏/军工/芯片/有色/医药/新能源
   → 精确东财板块名（``b:BKxxxx``），避免模糊匹配命中「非白酒」「磷肥及磷化工」等子板块/反义词。
4. 行业/概念板块：东方财富 clist 板块列表（``m:90 t:2`` 行业 / ``t:3`` 概念）按名称模糊匹配兜底
   → 板块成分（``b:BKxxxx``）。绕开 akshare ``stock_board_industry_*_em`` 的 push2 易断连接口。

核心不依赖 MX_APIKEY。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

# 定位项目根目录（app/board_pool.py 的上一级）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 抑制 akshare 的 WARNING 噪音
logging.disable(logging.WARNING)


# 指数别名 → akshare index_stock_cons 的 symbol
INDEX_ALIASES: dict[str, str] = {
    "沪深300": "000300",
    "中证500": "000905",
    "中证1000": "000852",
    "上证50": "000016",
    "中证红利": "000922",
    "中证800": "000906",
    "深证100": "399330",
    "科创50": "000688",
    "创业板指": "399006",
    "中证A50": "930050",
    "上证180": "000010",
}

# 板 → 东方财富 clist fs 过滤串（按代码前缀归属）
BOARD_FS: dict[str, str] = {
    "创业板": "m:0 t:80",
    "科创板": "m:1 t:23",
    "深主板": "m:0 t:6",
    "沪主板": "m:1 t:2",
    "北交所": "m:0 t:81 s:2048",
}

# 精确关键字 → 优先命中的东财板块名（精确匹配，先行业后概念）。
# 用「精确名」而非模糊匹配，避免「白酒」命中「非白酒」、「化工」命中「磷肥及磷化工」、
# 「光伏」命中「光伏辅材」、「军工」命中「军工电子Ⅲ」等反义词/子板块误匹配。
# 值为多个板块名 = 合并成一个池（如新能源）。
KEY_BOARD_PRIORITY: dict[str, list[str]] = {
    "半导体": ["半导体"],
    "芯片": ["半导体"],
    "化工": ["基础化工"],
    "白酒": ["白酒Ⅱ"],
    "光伏": ["光伏设备"],
    "军工": ["国防军工"],
    "有色": ["有色金属"],
    "医药": ["医药生物"],
    "新能源": ["电池", "光伏设备", "风电设备", "电网设备"],
}


def _norm_code(c) -> str:
    """清洗股票代码为 6 位数字字符串（None/非数字 → 空串）。"""
    s = re.sub(r"\D", "", str(c or ""))
    return s.zfill(6) if s else ""


def _iter_rows(df):
    """安全迭代 DataFrame 行（None/空 → 空迭代器）。"""
    if df is None or getattr(df, "empty", True):
        return iter(())
    return df.iterrows()


# ---------------------------------------------------------------- 指数成分

def _pool_from_index(symbol: str, label: str) -> dict[str, dict]:
    """指数成分（akshare index_stock_cons）。"""
    try:
        import akshare as ak

        df = ak.index_stock_cons(symbol=symbol)
    except Exception:
        return {}
    pool: dict[str, dict] = {}
    for _, r in _iter_rows(df):
        code = _norm_code(r.get("品种代码"))
        if not code or code == "000000":
            continue
        pool[code] = {
            "name": str(r.get("品种名称", "")),
            "industry": label,
            "source": label,
        }
    return pool


# ---------------------------------------------------------------- 板（代码前缀）

def _pool_from_board(fs: str, label: str) -> dict[str, dict]:
    """板（创业板/科创板/主板）成分：东财 clist 全市场快照按 fs 过滤。"""
    try:
        from app.data_fetcher import _fetch_em_clist
    except Exception:
        return {}
    items = _fetch_em_clist(fs=fs, fields="f12,f14,f100", fid="f12")
    pool: dict[str, dict] = {}
    for it in items:
        code = _norm_code(it.get("f12"))
        if not code:
            continue
        pool[code] = {
            "name": str(it.get("f14", "")),
            "industry": str(it.get("f100", "") or label),
            "source": label,
        }
    return pool


# ---------------------------------------------------------------- 行业/概念板块

def _list_boards(sector_type: str) -> list[tuple[str, str]]:
    """拉板块列表 [(board_code, board_name)]。sector_type: 2=行业 3=概念 1=地域。"""
    try:
        from app.data_fetcher import _fetch_em_clist
    except Exception:
        return []
    items = _fetch_em_clist(fs=f"m:90 t:{sector_type}", fields="f12,f14", fid="f3", max_pages=8)
    out: list[tuple[str, str]] = []
    for it in items:
        code = str(it.get("f12", "") or "").strip()
        name = str(it.get("f14", "") or "").strip()
        if code and name:
            out.append((code, name))
    return out


def _pool_from_board_code(board_code: str, label: str) -> dict[str, dict]:
    """板块成分（东财 clist b:板块代码）。"""
    try:
        from app.data_fetcher import _fetch_em_clist
    except Exception:
        return {}
    items = _fetch_em_clist(fs=f"b:{board_code}", fields="f12,f14,f100", fid="f12", max_pages=8)
    pool: dict[str, dict] = {}
    for it in items:
        code = _norm_code(it.get("f12"))
        if not code:
            continue
        pool[code] = {
            "name": str(it.get("f14", "")),
            "industry": str(it.get("f100", "") or label),
            "source": label,
        }
    return pool


def _match_board(key: str, sector_type: str) -> tuple[str, str] | None:
    """在板块列表里找最佳匹配：精确 → 前缀 → 包含，返回 (board_code, board_name)。

    不用双向包含（`name in key`）——那会让「白酒」命中「非白酒」。
    """
    boards = _list_boards(sector_type)
    if not boards:
        return None
    # 精确
    for code, name in boards:
        if name == key:
            return code, name
    # 前缀（「白酒」→「白酒Ⅱ」，但「非白酒」startswith「白酒」为假，不会误命中）
    for code, name in boards:
        if name.startswith(key):
            return code, name
    # 包含（最后兜底：「磷肥及磷化工」含「化工」）
    for code, name in boards:
        if key in name:
            return code, name
    return None


def _pool_from_board_name(key: str) -> dict[str, dict]:
    """行业/概念板块名 → 成分股池。先行业(t:2)后概念(t:3)。"""
    for sector_type in ("2", "3"):
        hit = _match_board(key, sector_type)
        if hit:
            code, name = hit
            pool = _pool_from_board_code(code, name)
            if pool:
                return pool
    return {}


def _pool_from_priority(key: str) -> dict[str, dict]:
    """精确关键字 → 东财板块成分（按 KEY_BOARD_PRIORITY 精确板块名匹配，可合并多板块）。"""
    names = KEY_BOARD_PRIORITY.get(key)
    if not names:
        return {}
    # 一次拉全 行业+概念 板块列表，构建 名→代码 索引（避免逐名重复拉列表）
    name_to_code: dict[str, str] = {}
    for sector_type in ("2", "3"):
        for code, name in _list_boards(sector_type):
            name_to_code.setdefault(name, code)
    pool: dict[str, dict] = {}
    for name in names:
        code = name_to_code.get(name)
        if code:
            sub = _pool_from_board_code(code, name)
            if sub:
                pool.update(sub)
    return pool


# ---------------------------------------------------------------- 入口

def resolve_board_pool(query: str) -> dict[str, dict]:
    """解析板块/行业 → 成分股池 {code: {name, industry, source}}。

    支持：指数名/代码（沪深300/中证500/000300 ...）、板名（创业板/科创板/主板）、
    行业名（半导体/芯片/化工 ...）。解析失败返回空 dict。
    """
    q = (query or "").strip()
    if not q:
        return {}

    # 纯 6 位指数代码
    if re.fullmatch(r"\d{6}", q):
        pool = _pool_from_index(q, q)
        return pool if pool else {}

    # 归一化：去尾部「板块/行业/概念/指数/成分股」后缀
    key = re.sub(r"(板块|行业|概念|指数|成分股?|ETF)$", "", q).strip() or q

    # 1) 指数成分（精确）
    for alias, sym in INDEX_ALIASES.items():
        if key == alias or key == sym:
            return _filter_scannable(_pool_from_index(sym, alias))

    # 2) 板（精确）
    for bname, fs in BOARD_FS.items():
        if key == bname:
            return _filter_scannable(_pool_from_board(fs, bname))

    # 3) 精确关键字优先映射（白酒/化工/光伏/军工/芯片/有色/医药/新能源… 精确板块名）
    pool = _pool_from_priority(key)
    if pool:
        return _filter_scannable(pool)

    # 4) 行业/概念板块名模糊匹配（精确→前缀→包含，仅作兜底）
    pool = _pool_from_board_name(key)
    if pool:
        return _filter_scannable(pool)

    return {}


# 沪深可扫代码前缀：60/68（沪主板/科创）、00/30（深主板/创业）。
# 北交所(43/83/87/92)、B股(200/900)等新浪 K 线不可靠，直接剔除。
_SCANNABLE_RE = re.compile(r"^(60|68|00|30)")


def _filter_scannable(pool: dict[str, dict]) -> dict[str, dict]:
    """只保留沪深可扫代码（新浪 K 线可用）。"""
    return {c: s for c, s in pool.items() if _SCANNABLE_RE.match(c)}
