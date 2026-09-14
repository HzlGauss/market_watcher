#!/usr/bin/env python3
"""全市场 ETF 多维度筛选（规模+流动性 / 动量+趋势 / 估值分位 / 资金流+份额）

借鉴「AI量化+ETF」研报的三信号择时思想，落地成可复用的 ETF 筛选 skill：
  - 免费层（akshare + 东财，无需 key）：全市场清单 + 流动性/规模硬门槛 + 动量趋势打分
  - key 层（妙想 MX_APIKEY，仅对 top 候选）：跟踪指数估值分位 + 净申购额（申赎口径）

用法:
    py screen_etf.py [类型|关键词] [输出数量]

参数:
    类型|关键词  可选。类型=宽基/行业/主题/债券/跨境/商品/货币；或具体跟踪方向
                （半导体/医药/新能源/沪深300/恒生/黄金/纳指...）。不传 = 全市场扫。
    输出数量     可选，默认 15。

示例:
    py screen_etf.py                  # 全市场 ETF 筛选
    py screen_etf.py 半导体           # 只筛半导体方向
    py screen_etf.py 沪深300 10       # 宽基沪深300，输出前10
    py screen_etf.py 债券 8           # 只筛债券类

输出: 按综合分降序的候选表。综合分 = 动量趋势(0~40) + 估值分位(0~30) + 资金流份额(0~30)。
      无 MX_APIKEY 时只出免费层（动量排序），估值/资金流列标注「待补」。
"""
import sys
import time
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/etf-screen 上三级：etf-screen -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 加载 .env（MX_APIKEY 等）
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

from app.data_fetcher import fetch_etf_spot_snapshot, _etf_name_to_industry
from app.technical import calc_rsi, calc_macd, calc_ma_alignment, fetch_historical_kline
from app.helpers import _detect_market

# ============================================================
# 常量
# ============================================================

# 流动性 / 规模硬门槛
MIN_AMOUNT = 1e7    # 日成交额 ≥ 1000 万
MIN_MKTCAP = 2e8    # 规模（总市值）≥ 2 亿

# 深度分析数量上限（K 线逐只拉，控制 API 调用量）
MAX_KLINE_FETCH = 60

# key 层（妙想）只对 top 候选跑，控制额度
MAX_KEY_QUERY = 15

# ETF 类型识别关键词（按优先级匹配，先命中先归）
_TYPE_RULES = (
    ("债券", ("债", "短融", "国开", "政金", "可转债", "城投", "国债")),
    ("货币", ("货币", "现金", "保证金", "理财金", "日利", "添益")),
    ("商品", ("黄金", "白银", "豆粕", "原油", "能源化工", "商品", "螺纹")),
    ("跨境", ("纳指", "标普", "道琼斯", "日经", "德国", "法国", "恒生", "港股",
              "中概", "海外", "QDII", "东南亚", "越南", "印度", "亚太", "全球", "美国")),
    ("宽基", ("沪深300", "中证500", "中证1000", "中证2000", "上证50", "科创50",
              "科创100", "创业板", "双创", "中证A50", "中证A500", "中证800",
              "中证红利", "上证180", "深证100", "深证50", "中证100")),
)
_TYPE_KEYS = {"宽基", "行业", "主题", "债券", "跨境", "商品", "货币"}

# 估值分位可识别的跟踪指数（名称子串 → 指数名，用于妙想查询）
_INDEX_KEYS = (
    "沪深300", "中证500", "中证1000", "中证2000", "上证50", "科创50", "科创100",
    "创业板指", "创业板50", "中证红利", "中证A50", "中证A500", "中证800",
    "恒生科技", "恒生指数", "证券公司", "中证全指证券", "中证银行", "中证军工",
    "国证芯片", "中华半导体", "中证全指半导体", "中证全指医药", "中证医药",
)

# 行业方向 → 可被妙想识别的具体指数名（行业 ETF 名称里没有具体指数时用）
_VALUATION_INDEX_MAP = {
    "半导体": "国证半导体芯片指数",
    "券商": "中证全指证券公司指数",
    "银行": "中证银行指数",
    "军工": "中证军工指数",
    "医药": "中证医药卫生指数",
    "酿酒": "中证白酒指数",
    "新能源": "中证新能源汽车指数",
    "有色": "中证有色金属指数",
    "煤炭": "中证煤炭指数",
    "钢铁": "中证钢铁指数",
    "化工": "中证细分化工产业主题指数",
    "电力": "中证全指电力公用事业指数",
    "通信": "中证全指通信设备指数",
    "计算机": "中证计算机主题指数",
    "传媒": "中证传媒指数",
    "房地产": "中证全指房地产指数",
    "消费": "中证主要消费指数",
    "食品饮料": "中证食品饮料指数",
    "汽车": "中证全指汽车指数",
    "红利": "中证红利指数",
    "农牧": "中证畜牧养殖指数",
    "人工智能": "中证人工智能主题指数",
}


# ============================================================
# 工具函数
# ============================================================

def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _pad(s, width):
    """按显示宽度左对齐填充（中文按 2 宽度计）"""
    w = sum(2 if ord(c) > 0x1100 else 1 for c in s)
    return s + " " * max(0, width - w)


def _fmt_yi(v):
    """金额转亿元（None 返回 —）"""
    if v is None:
        return "—"
    return f"{v / 1e8:.2f}"


def _fmt_pct(v, signed=False):
    if v is None:
        return "—"
    if signed:
        sign = "+" if v >= 0 else "-"
        return f"{sign}{abs(v):.2f}%"
    return f"{v:.2f}%"


def _classify_etf(name):
    """按名称归类 ETF 类型（宽基/行业/主题/债券/跨境/商品/货币）"""
    for etype, keys in _TYPE_RULES:
        if any(k in name for k in keys):
            return etype
    if _etf_name_to_industry(name):
        return "行业"
    return "主题"


def _tracking_index(name):
    """从 ETF 名称推断跟踪方向/指数名（用于展示）"""
    for k in _INDEX_KEYS:
        if k in name:
            return k
    return _etf_name_to_industry(name) or ""


def _valuation_index(name):
    """推断可被妙想识别的估值指数名（优先名称子串，其次行业映射）"""
    for k in _INDEX_KEYS:
        if k in name:
            return k
    industry = _etf_name_to_industry(name)
    return _VALUATION_INDEX_MAP.get(industry, "")


# ============================================================
# 动量 + 趋势打分（免费层，0~40）
# ============================================================

def _score_momentum(klines):
    """近期涨幅 + 均线排列 + MACD + RSI 位置，0~40 分。

    偏向「趋势已走强」的动量（追势）视角，与 left-side（超跌埋伏）互补：
    上涨的、均线多头的、MACD 多头的、RSI 处于健康强势区间的加分。
    """
    if len(klines) < 30:
        return 0, {}

    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < 26:
        return 0, {}

    score = 0
    detail = {}

    # 近 20 日涨幅（0~12）
    chg20 = 0.0
    if closes[-21] and closes[-21] > 0:
        chg20 = (closes[-1] - closes[-21]) / closes[-21] * 100
    detail["chg20"] = round(chg20, 1)
    if chg20 > 10:
        score += 12
    elif chg20 > 5:
        score += 9
    elif chg20 > 0:
        score += 6
    elif chg20 > -5:
        score += 3

    # 均线排列（0~12）
    ma = calc_ma_alignment(klines)
    detail["ma"] = ma.alignment
    if ma.alignment == "多头排列":
        score += 12
    elif ma.alignment == "多头回调":
        score += 9
    elif ma.alignment == "缠绕" and "偏多" in (ma.detail or ""):
        score += 5

    # MACD（0~8）
    macd = calc_macd(closes)
    detail["macd"] = macd.signal
    if macd.signal == "金叉":
        score += 8
    elif macd.signal == "多头":
        score += 6
    elif macd.signal == "空头" and macd.histogram and macd.histogram > 0:
        score += 3

    # RSI 位置（0~8）：50~70 健康强势区最高，超卖/超买都扣
    rsi = calc_rsi(closes)
    detail["rsi"] = round(rsi, 0) if rsi is not None else None
    if rsi is not None:
        if 50 <= rsi <= 70:
            score += 8
        elif 40 <= rsi < 50:
            score += 5
        elif 30 <= rsi < 40:
            score += 3
        elif rsi < 30:
            score += 1  # 超卖，可能有反转但非「动量」

    return min(score, 40), detail


# ============================================================
# key 层：估值分位 + 净申购额（妙想）
# ============================================================

def _load_mx():
    """加载妙想客户端（无 key 返回 None）"""
    try:
        from app.config import Config
        from app.miaoxiang import MXClient
        config = Config(_ROOT / "watchlist_config.json")
        if not config.mx_apikeys:
            return None
        return MXClient(config.mx_apikeys)
    except Exception:
        return None


_VAL_CACHE: dict = {}


def _fetch_valuation_pct(mx, index_name):
    """查跟踪指数的 PE/PB 历史分位（取最新一行的估值相关分位均值）

    同一跟踪指数多次查询会命中缓存（避免对沪深300这类同指数多只 ETF 重复问妙想，
    也规避连发导致的结果抖动）。"""
    if not index_name:
        return None
    if index_name in _VAL_CACHE:
        return _VAL_CACHE[index_name]
    result = None
    try:
        tables = mx.query_structured(f"{index_name} 指数 市盈率 市净率 历史百分位")
        from app.miaoxiang import MXClient
        vals = []
        for t in tables:
            cols = t.get("columns") or []
            rows = t.get("rows") or []
            if not rows:
                continue
            row = rows[0]  # 最新交易日在前
            for c in cols:
                cname = str(c)
                if "分位" not in cname and "百分位" not in cname:
                    continue
                # 只取估值相关分位，过滤股息率/风险溢价等干扰项
                if not any(k in cname for k in ("市盈率", "市净率", "PE", "PB", "估值", "盈利")):
                    continue
                v = MXClient._parse_amount(row.get(c))
                if v is not None and 0 <= v <= 100:
                    vals.append(v)
        if vals:
            result = round(sum(vals) / len(vals), 1)
    except Exception:
        result = None
    _VAL_CACHE[index_name] = result
    return result


def _score_valuation(pct):
    """估值分位 → 0~30 分，分位越低分越高"""
    if pct is None:
        return 0
    if pct <= 10:
        return 30
    if pct <= 30:
        return 22
    if pct <= 50:
        return 14
    if pct <= 70:
        return 7
    return 0


def _fetch_net_subscribe(mx, candidates):
    """并发查 top 候选的净申购额（申赎口径，妙想）→ {code: net_subscribe}"""
    from concurrent.futures import ThreadPoolExecutor
    from app.miaoxiang import MXClient

    def _query_one(c):
        try:
            tables = mx.query_structured(
                f"{c['name']} {c['code']} 主力资金净流入 净申购额"
            )
            for t in tables:
                cols = t.get("columns") or []
                rows = t.get("rows") or []
                sub_col = next((x for x in cols if "净申购" in x or "申赎" in x), None)
                if not sub_col or not rows:
                    continue
                # 今日行净申购额常为「-」，逐行找第一个有效值（往前取最近交易日）
                for row in rows:
                    v = MXClient._parse_amount(row.get(sub_col))
                    if v is not None:
                        return c["code"], v
            return c["code"], None
        except Exception:
            return c["code"], None

    result = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_query_one, c) for c in candidates]
        for fut in futures:
            try:
                code, v = fut.result(timeout=30)
                result[code] = v
            except Exception:
                pass
    return result


def _score_fundflow(main_pct, net_subscribe, mktcap):
    """资金流+份额 0~30：主力净占比（免费 0~10）+ 净申购额（妙想 0~20）"""
    score = 0
    if main_pct is not None:
        if main_pct > 5:
            score += 10
        elif main_pct > 2:
            score += 7
        elif main_pct > 0:
            score += 4
    if net_subscribe is not None and mktcap and mktcap > 0:
        ratio = net_subscribe / mktcap
        if ratio > 0.01:
            score += 20
        elif ratio > 0.003:
            score += 14
        elif ratio > 0:
            score += 8
    return min(score, 30)


# ============================================================
# 主流程
# ============================================================

def _parse_args(argv):
    keyword, top = None, 15
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--top":
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                top = max(1, int(argv[i + 1]))
                i += 1
        elif a.isdigit():
            top = max(1, int(a))
        elif keyword is None:
            keyword = a
        i += 1
    return keyword, top


def _match_keyword(etf, keyword):
    """按关键词过滤：类型名匹配类型，否则匹配名称/跟踪方向子串"""
    if keyword in _TYPE_KEYS:
        return etf["etype"] == keyword
    return (
        keyword in etf["name"]
        or keyword in etf["industry"]
        or keyword in etf["tracking"]
    )


def main():
    argv = sys.argv[1:]
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 0

    keyword, top = _parse_args(argv)
    print("=" * 92)
    scope = f"范围: {keyword}" if keyword else "范围: 全市场"
    print(f"ETF 多维度筛选（{scope} | 输出前 {top} | 流动性≥1000万 & 规模≥2亿）")
    print("=" * 92)

    # 1. 全市场快照（规模/折溢价/份额/单日资金流一次拿全）
    snapshots = fetch_etf_spot_snapshot()
    if not snapshots:
        print("❌ 未获取到 ETF 快照（东财/新浪接口均不可达）")
        return 1

    # 2. 补充类型 / 跟踪方向，然后按关键词过滤
    for s in snapshots:
        s["industry"] = _etf_name_to_industry(s["name"])
        s["etype"] = _classify_etf(s["name"])
        s["tracking"] = _tracking_index(s["name"])

    pool = [s for s in snapshots if not keyword or _match_keyword(s, keyword)]
    if not pool:
        print(f"❌ 未匹配到与「{keyword}」相关的 ETF")
        return 1

    # 3. 硬门槛：流动性 + 规模
    passed = []
    for s in pool:
        amount = s.get("amount") or 0
        mktcap = s.get("mktcap")
        if amount < MIN_AMOUNT:
            continue
        if mktcap is not None and mktcap < MIN_MKTCAP:
            continue
        passed.append(s)
    passed.sort(key=lambda x: x.get("amount") or 0, reverse=True)
    print(f"  快照 {len(snapshots)} 只 → 关键词过滤 {len(pool)} 只 → 门槛后 {len(passed)} 只")
    print(f"  深度分析前 {min(MAX_KLINE_FETCH, len(passed))} 只（拉日K线算动量，约需 1~2 分钟）...\n")

    # 4. 逐只拉 K 线，算动量分
    candidates = []
    checked = 0
    for s in passed[:MAX_KLINE_FETCH]:
        checked += 1
        klines = fetch_historical_kline(
            s["code"], _detect_market(s["code"]), days=60, scale=240
        )
        mscore, detail = _score_momentum(klines) if klines else (0, {})
        if klines:
            candidates.append({
                **s,
                "mscore": mscore,
                "chg20": detail.get("chg20"),
                "ma": detail.get("ma", ""),
                "macd": detail.get("macd", ""),
                "rsi": detail.get("rsi"),
                "val_pct": None,
                "val_score": 0,
                "net_subscribe": None,
                "ff_score": _score_fundflow(s.get("main_pct"), None, s.get("mktcap")),
            })
        if checked % 20 == 0:
            print(f"  已分析 {checked}/{min(MAX_KLINE_FETCH, len(passed))} ...")
        time.sleep(0.3)

    if not candidates:
        print("❌ 未拉取到任何候选 ETF 的 K 线")
        return 1

    candidates.sort(key=lambda x: x["mscore"], reverse=True)
    candidates = candidates[:top]

    # 5. key 层：估值分位 + 净申购额（仅 top 候选，需 MX_APIKEY）
    mx = _load_mx()
    key_note = ""
    if mx is not None:
        key_n = min(MAX_KEY_QUERY, len(candidates))
        print(f"  妙想 key 层：对前 {key_n} 只查估值分位 + 净申购额（较慢，请稍候）...\n")
        sub_map = _fetch_net_subscribe(mx, candidates[:key_n])
        for c in candidates[:key_n]:
            c["val_pct"] = _fetch_valuation_pct(mx, _valuation_index(c["name"]))
            c["val_score"] = _score_valuation(c["val_pct"])
            c["net_subscribe"] = sub_map.get(c["code"])
            c["ff_score"] = _score_fundflow(
                c.get("main_pct"), c["net_subscribe"], c.get("mktcap")
            )
        key_note = "（含估值分位 + 净申购额）"
    else:
        key_note = "（未配 MX_APIKEY，估值/净申购待补，仅免费层动量+主力净占比）"

    # 6. 综合分排序
    for c in candidates:
        c["composite"] = c["mscore"] + c["val_score"] + c["ff_score"]
    candidates.sort(key=lambda x: x["composite"], reverse=True)

    # 7. 输出表格
    print()
    print("代码      名称                    类型  现价     涨跌幅   成交额  规模    跟踪方向     动量  估值   主力净占  净申购   综合")
    print("-" * 92)
    for c in candidates:
        name = _pad(c["name"], 18)
        etype = _pad(c["etype"], 4)
        price = f"{c['price']:.3f}" if c["price"] is not None else "—"
        chg = _fmt_pct(c["change_pct"], signed=True)
        amt = _fmt_yi(c["amount"])
        mcap = _fmt_yi(c["mktcap"])
        tracking = _pad(c["tracking"] or "-", 9)
        val_txt = f"{c['val_pct']:.1f}%" if c["val_pct"] is not None else "待补"
        mpct = _fmt_pct(c.get("main_pct"), signed=True)
        sub = _fmt_yi(c["net_subscribe"]) if c["net_subscribe"] is not None else "待补"
        if c["net_subscribe"] is not None and c["net_subscribe"] > 0:
            sub = "+" + sub
        print(
            f"{c['code']:<8}  {name}  {etype}  {price:>7}  {chg:>8}  {amt:>5}  {mcap:>5}  "
            f"{tracking}  {c['mscore']:>3}   {val_txt:>6}  {mpct:>8}  {sub:>7}  {c['composite']:>3}"
        )

    print()
    print("说明:")
    print("  - 综合分 = 动量趋势(0~40) + 估值分位(0~30) + 资金流份额(0~30)，满分 100")
    print("  - 动量分：近20日涨幅 + 均线排列 + MACD + RSI 位置，偏「追势」，与 left-side（超跌埋伏）互补")
    print("  - 估值分位：跟踪指数 PE/PB 历史分位（越低分越高），待补=指数名未被识别或无 key")
    print("  - 主力净占=单日主力净流入占比(免费)；净申购=申赎口径净申购额(妙想)，单位亿元")
    print(f"  - 硬门槛：成交额≥1000万 & 规模≥2亿；{key_note}")
    print("  - 仅初筛，确认买点请对单只运行 etf-grid / stock-analysis；不构成投资建议")
    return 0


if __name__ == "__main__":
    sys.exit(main())
