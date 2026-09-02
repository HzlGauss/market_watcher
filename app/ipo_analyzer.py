"""
打新申购分析器 —— 新股(IPO) / 新债(可转债) 申购价值与破发概率

数据源（akshare 可选依赖，缺失时降级为提示）：
- 新股：ak.stock_ipo_ths()  — 同花顺新股，含发行价/发行市盈率/行业市盈率/申购日期/上市日期/中签率
- 新债：ak.bond_zh_cov()    — 集思录可转债，含正股/转股价/转股价值/转股溢价率/评级/发行规模
- 市场环境：新浪指数行情（上证/深证/创业板）
- 质地增强（可选）：妙想 MXClient 查公司/正股 主营/行业/营收净利

核心：规则引擎打分（0~100）+ 破发概率区间估算 + 申购结论标签。
新股破发主因子 = 板块（主板 23 倍 PE 红线几乎不破发）+ 发行市盈率 vs 行业市盈率；
新债破发主因子 = 转股价值（债底保护）+ 债券评级 + 发行规模。
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Optional

from app.http_client import sina_client
from app.utils import log


# ============================================================
# 字段/数值处理
# ============================================================

def _num(v) -> Optional[float]:
    """把 akshare 字段值安全转 float，兼容 '-'/'--'/None/NaN/带逗号字符串。"""
    if v is None:
        return None
    # numpy 标量（np.int64/np.float64）先转成原生类型
    try:
        if hasattr(v, "item"):
            v = v.item()
    except Exception:
        pass
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "None", "nan", "NaN", "NaT"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s) -> Optional[date]:
    """解析 'YYYY-MM-DD' 格式日期，其他格式返回 None。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(s or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _board_from_code(code: str) -> str:
    """按代码前缀识别板块（A 股规则）。"""
    c = str(code or "").strip()
    if c.startswith("68"):
        return "科创板"
    if c.startswith("60"):
        return "沪主板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith("00"):
        return "深主板"
    if c.startswith(("43", "83", "87", "92")):
        return "北交所"
    return "其他"


# ============================================================
# 数据获取（akshare 可选，懒加载 + 降级）
# ============================================================

def _akshare():
    """懒加载 akshare，不可用时返回 None（调用方降级）。"""
    try:
        import akshare as ak
        return ak
    except Exception as e:
        log.warning(f"akshare 不可用（pip install akshare）: {e}")
        return None


def _fetch_stock_ipos():
    """同花顺新股列表（含待申购 + 已上市）。"""
    ak = _akshare()
    if ak is None:
        return None
    try:
        return ak.stock_ipo_ths()
    except Exception as e:
        log.warning(f"新股列表获取失败: {e}")
        return None


def _fetch_bond_covs():
    """可转债列表（集思录数据）。"""
    ak = _akshare()
    if ak is None:
        return None
    try:
        return ak.bond_zh_cov()
    except Exception as e:
        log.warning(f"可转债列表获取失败: {e}")
        return None


def _find_row(df, code: str, name: str) -> Optional[dict]:
    """在 DataFrame 中按代码（精确）/名称（模糊）找一行，返回 dict。"""
    if df is None or len(df) == 0:
        return None
    code = str(code or "").strip()
    name = (name or "").strip()

    for code_col in ("股票代码", "债券代码", "代码"):
        if code_col not in df.columns:
            continue
        for _, row in df.iterrows():
            if str(row[code_col]).strip().zfill(6) == code.zfill(6):
                return row.to_dict()

    if name:
        for name_col in ("股票简称", "债券简称", "证券简称", "名称"):
            if name_col not in df.columns:
                continue
            for _, row in df.iterrows():
                if name in str(row[name_col]):
                    return row.to_dict()
    return None


def _fetch_market_context() -> dict[str, str]:
    """新浪指数行情（上证/深证/创业板），用于市场环境。"""
    indices = {"上证指数": "sh000001", "深证成指": "sz399001", "创业板指": "sz399006"}
    try:
        resp = sina_client.get(f"http://hq.sinajs.cn/list={','.join(indices.values())}")
        if not resp:
            return {}
        resp.encoding = "gbk"
        result: dict[str, str] = {}
        for line in resp.text.split("\n"):
            m = re.search(r'hq_str_(\w+)="([^"]*)"', line)
            if not m:
                continue
            fields = m.group(2).split(",")
            if len(fields) < 4 or not fields[0]:
                continue
            price = _num(fields[3])
            prev = _num(fields[2])
            if price is not None and prev:
                result[fields[0]] = f"{price:.2f} ({(price - prev) / prev * 100:+.2f}%)"
            else:
                result[fields[0]] = fields[3]
        return result
    except Exception as e:
        log.warning(f"市场环境获取失败: {e}")
        return {}


def _mx_query(query: str, config) -> Optional[str]:
    """妙想深度数据查询（复用 app.miaoxiang，多 key 轮询，无 key 返回 None）。"""
    if config is None or not getattr(config, "mx_apikeys", None):
        return None
    try:
        from app.miaoxiang import get_mx_client
        return get_mx_client(config).query_as_text(query) or None
    except Exception as e:
        log.warning(f"妙想质地查询失败: {e}")
        return None


# ============================================================
# 新股分析（破发概率 + 评分）
# ============================================================

def _analyze_stock(row: dict) -> dict:
    """规则引擎：新股申购价值与破发概率。

    主逻辑：
    - 沪/深主板受 23 倍发行 PE 红线约束，历史破发率极低 → 几乎无脑打。
    - 注册制板块（科创/创业/北交）看 发行PE vs 行业PE：
      折价/平价 → 破发风险低；大幅溢价或亏损发行 → 破发风险高。
    """
    code = str(row.get("股票代码", "")).strip()
    name = str(row.get("股票简称", "")).strip()
    board = _board_from_code(code)
    issue_price = _num(row.get("发行价格"))
    issue_pe = _num(row.get("发行市盈率"))
    ind_pe = _num(row.get("行业市盈率"))
    total_shares = _num(row.get("发行总数（万股）"))
    sub_date = str(row.get("申购日期", "") or "").strip()
    list_date = str(row.get("上市日期", "") or "").strip()
    win_rate = _num(row.get("中签率（%）"))

    status = "待上市（申购窗口内）" if list_date in ("", "-", "None", "nan", "NaT") else f"已上市（{list_date}）"

    factors: list[str] = []

    if board in ("沪主板", "深主板"):
        break_prob = "低（<5%）"
        verdict = "建议申购"
        score = 88
        factors.append("主板发行，受 23 倍发行市盈率红线约束，历史破发率极低")
    else:
        if issue_pe is None:
            factors.append(
                f"发行市盈率未披露（{('行业PE ' + str(ind_pe)) if ind_pe else '可能为未盈利企业或待定价'}）"
            )
            if board == "北交所":
                break_prob = "中（20~40%）"
                verdict = "谨慎申购"
                score = 55
            else:
                break_prob = "中高（30~50%）"
                verdict = "谨慎申购"
                score = 48
        elif ind_pe is None or ind_pe <= 0:
            factors.append(f"发行市盈率 {issue_pe:.2f}，行业市盈率缺失")
            break_prob = "中（20~40%）"
            verdict = "谨慎申购"
            score = 55
        else:
            ratio = issue_pe / ind_pe
            factors.append(f"发行PE {issue_pe:.2f} vs 行业PE {ind_pe:.2f}（溢价 {ratio - 1:+.0%}）")
            if ratio <= 0.8:
                break_prob = "低（5~15%）"
                verdict = "建议申购"
                score = 80
            elif ratio <= 1.0:
                break_prob = "中低（15~25%）"
                verdict = "建议申购"
                score = 70
            elif ratio <= 1.3:
                break_prob = "中（25~35%）"
                verdict = "谨慎申购"
                score = 55
            else:
                break_prob = "高（>40%）"
                verdict = "放弃申购"
                score = 35

    if issue_price is None:
        factors.append("发行价未确定（待定价）")
        score -= 10
    if total_shares and total_shares > 100000:  # 万股 → >10 亿股视为大盘
        factors.append(f"发行规模大（{total_shares / 10000:.1f} 亿股），首日表现或承压")
        score -= 5

    score = max(0, min(100, score))

    return {
        "kind": "stock", "code": code, "name": name, "board": board,
        "issue_price": issue_price, "issue_pe": issue_pe, "industry_pe": ind_pe,
        "total_shares_wan": total_shares, "sub_date": sub_date, "list_date": list_date,
        "win_rate": win_rate, "status": status,
        "score": score, "break_prob": break_prob, "verdict": verdict,
        "factors": factors,
    }


# ============================================================
# 新债（可转债）分析（破发概率 + 评分）
# ============================================================

def _analyze_bond(row: dict) -> dict:
    """规则引擎：可转债申购价值与破发概率。

    主逻辑：可转债有债底保护，破发概率远低于新股。
    核心因子：转股价值（=100×正股价/转股价）、债券评级、发行规模、转股溢价率。
    """
    code = str(row.get("债券代码", "")).strip()
    name = str(row.get("债券简称", "")).strip()
    stock_code = str(row.get("正股代码", "") or "").strip()
    stock_name = str(row.get("正股简称", "") or "").strip()
    stock_price = _num(row.get("正股价"))
    conv_price = _num(row.get("转股价"))
    conv_value = _num(row.get("转股价值"))
    premium = _num(row.get("转股溢价率"))
    rating = str(row.get("信用评级", "") or "").strip()
    scale = _num(row.get("发行规模"))  # 亿元
    sub_date = str(row.get("申购日期", "") or "").strip()
    list_time = str(row.get("上市时间", "") or "").strip()
    win_rate = _num(row.get("中签率"))

    status = "待上市（申购窗口内）" if list_time in ("", "-", "None", "nan", "NaT") else f"已上市（{list_time}）"

    factors: list[str] = []

    if conv_value is None:
        break_prob = "无法估算"
        verdict = "谨慎申购"
        factors.append("转股价值缺失")
    else:
        factors.append(f"转股价值 {conv_value:.2f}（正股 {stock_name} {stock_price} / 转股价 {conv_price}）")
        if conv_value >= 100:
            break_prob = "低（<3%）"
            verdict = "建议申购"
        elif conv_value >= 95:
            break_prob = "低（3~8%）"
            verdict = "建议申购"
        elif conv_value >= 90:
            break_prob = "中低（8~15%）"
            verdict = "建议申购"
        elif conv_value >= 85:
            break_prob = "中（15~25%）"
            verdict = "谨慎申购"
        else:
            break_prob = "中高（25~35%）"
            verdict = "谨慎申购"

    # 评分（0~100）
    score = 0
    if conv_value is not None:
        score += max(0, min(40, int(conv_value) - 60))  # 100→40 / 90→30 / 85→25 / ≤60→0
    if premium is not None:
        factors.append(f"转股溢价率 {premium:+.2f}%")
        if premium <= 0:
            score += 15
        elif premium <= 10:
            score += 12
        elif premium <= 20:
            score += 8
        else:
            score += 4
    rating_score = {"AAA": 25, "AA+": 22, "AA": 20, "AA-": 16, "A+": 10, "A": 8, "A-": 6}.get(rating, 12)
    score += rating_score
    factors.append(f"债券评级 {rating}")
    if scale is not None:
        factors.append(f"发行规模 {scale:.2f} 亿")
        if scale >= 10:
            score += 10
        elif scale >= 5:
            score += 8
        elif scale >= 3:
            score += 6
        else:
            score += 4
    else:
        score += 7

    score = max(0, min(100, score))

    # 低评级 + 转股价值不足 → 强制降级
    if rating in ("A+", "A", "A-", "") and conv_value is not None and conv_value < 95:
        verdict = "谨慎申购"
        factors.append("评级偏低且转股价值不足，破发风险上升")

    return {
        "kind": "bond", "code": code, "name": name,
        "stock_code": stock_code, "stock_name": stock_name,
        "stock_price": stock_price, "conv_price": conv_price,
        "conv_value": conv_value, "premium": premium, "rating": rating,
        "scale": scale, "sub_date": sub_date, "list_time": list_time,
        "win_rate": win_rate, "status": status,
        "score": score, "break_prob": break_prob, "verdict": verdict,
        "factors": factors,
    }


# ============================================================
# 类型识别 + 分析入口
# ============================================================

def detect_and_analyze(code: str, name: str = "") -> dict:
    """识别输入是新股还是新债，返回分析结果 dict（kind: stock/bond/not_found）。"""
    code = str(code or "").strip()
    name = (name or "").strip()
    is_bond_hint = code.startswith(("11", "12")) or "转债" in name

    first, second = (("bond", "stock") if is_bond_hint else ("stock", "bond"))
    for kind in (first, second):
        if kind == "bond":
            row = _find_row(_fetch_bond_covs(), code, name)
            if row:
                return _analyze_bond(row)
        else:
            row = _find_row(_fetch_stock_ipos(), code, name)
            if row:
                return _analyze_stock(row)

    return {"kind": "not_found", "code": code, "name": name}


def list_upcoming(limit: int = 10) -> dict:
    """列出待申购的新股/新债清单（打新日历）。"""
    stocks: list[dict] = []
    df = _fetch_stock_ipos()
    if df is not None:
        for _, row in df.iterrows():
            d = row.to_dict()
            if str(d.get("上市日期", "") or "").strip() in ("", "-", "None", "nan", "NaT"):
                stocks.append(d)

    bonds: list[dict] = []
    df2 = _fetch_bond_covs()
    if df2 is not None:
        today = date.today()
        for _, row in df2.iterrows():
            d = row.to_dict()
            code = str(d.get("债券代码", "") or "").strip()
            name = str(d.get("债券简称", "") or "").strip()
            # 排除退市债（40 开头 / 简称含"退"）
            if code.startswith("40") or "退" in name:
                continue
            # 未上市
            if str(d.get("上市时间", "") or "").strip() not in ("", "-", "None", "nan", "NaT"):
                continue
            # 申购日期须在近期窗口内（排除历史遗留的退市债/停发债）
            sub = _parse_date(d.get("申购日期"))
            if sub is None or not (today - timedelta(days=45) <= sub <= today + timedelta(days=30)):
                continue
            bonds.append(d)

    return {"stocks": stocks[:limit], "bonds": bonds[:limit]}
