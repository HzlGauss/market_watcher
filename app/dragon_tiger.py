"""
龙虎榜数据抓取与分析模块 —— AKShare (东方财富数据源)

抓取每日龙虎榜数据，分析大资金动向：
1. 机构席位买卖方向 → 关注/风险个股
2. 沪深股通席位动向 → 外资偏好
3. 知名游资追踪 → 短线情绪指标
4. 板块资金汇总 → 板块轮动信号
5. 买卖力量对比 → 个股多空判断
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from app.models import DragonTigerRecord, DragonTigerSummary
from app.utils import log

# ============================================================
# 常量
# ============================================================

# 净买入额阈值（元）—— 超过此值视为重点关注
BUY_THRESHOLD = 10_000_000  # 1000万

# 净卖出额阈值（元）—— 超过此值视为风险
SELL_THRESHOLD = -10_000_000  # -1000万


# ============================================================
# 数据抓取
# ============================================================

def fetch_dragon_tiger_list(max_count: int = 30) -> list[DragonTigerRecord]:
    """获取每日龙虎榜上榜个股列表

    通过 AKShare 调用东方财富龙虎榜详情接口，
    获取当日全部上榜个股及其交易数据。

    Args:
        max_count: 最大返回条数

    Returns:
        DragonTigerRecord 列表
    """
    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        log.debug("AKShare 未安装，无法获取龙虎榜数据")
        return []

    today = datetime.now().strftime("%Y%m%d")
    # 回看 10 天，覆盖周末/节假日，取最近一个交易日的数据
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

    try:
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=today)
    except Exception as e:
        log.warning(f"龙虎榜数据获取失败: {e}")
        return []

    if df is None or df.empty:
        log.info("龙虎榜无数据（最近10日无上榜记录）")
        return []

    # 取最近一个交易日的数据（适配周末/节假日跑）
    date_col = next(
        (c for c in ("上榜日", "上榜日期", "交易日期", "日期") if c in df.columns), None
    )
    if date_col is not None:
        latest = df[date_col].max()
        df = df[df[date_col] == latest]
        log.info(f"龙虎榜取最新交易日数据: {latest}")

    records = []
    for _, row in df.iterrows():
        try:
            record = DragonTigerRecord(
                code=str(row.get("代码", "")),
                name=str(row.get("名称", "")),
                change_pct=_parse_float(row.get("涨跌幅")),
                total_buy=float(row.get("龙虎榜买入额", 0) or 0),
                total_sell=float(row.get("龙虎榜卖出额", 0) or 0),
                net_buy=float(row.get("龙虎榜净买额", 0) or 0),
                total_trade=float(row.get("龙虎榜成交额", 0) or 0),
                turnover_rate=_parse_float(row.get("换手率")),
                reason=str(row.get("上榜原因", "")),
            )
            records.append(record)
        except Exception:
            continue

        if len(records) >= max_count:
            break

    log.info(f"龙虎榜数据获取成功: {len(records)} 条")
    return records


def _parse_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
        return v
    except (ValueError, TypeError):
        return None


from app.data_fetcher import fetch_stock_industry_map as _get_stock_industry_map


# ============================================================
# 分析逻辑
# ============================================================

def _classify_capital_flow(records: list[DragonTigerRecord]) -> tuple[list[dict], list[dict]]:
    """从龙虎榜数据中识别资金流向

    基于龙虎榜净买入额 + 买卖金额比，综合判断资金态度。

    Returns:
        (focus_list, risk_list)
        focus_list: 值得关注的个股列表（资金净买入，按净额排序）
        risk_list: 有风险的个股列表（资金净卖出，按净额排序）
    """
    focus_list = []
    risk_list = []

    for record in records:
        net_buy = record.net_buy
        buy_sell_ratio = record.buy_sell_ratio

        item = {
            "code": record.code,
            "name": record.name,
            "net_buy": net_buy,
            "buy_sell_ratio": buy_sell_ratio,
            "change_pct": record.change_pct,
            "turnover_rate": record.turnover_rate,
            "total_trade": record.total_trade,
            "reason": record.reason,
        }

        if net_buy > BUY_THRESHOLD and (buy_sell_ratio or 0) > 1:
            focus_list.append(item)
        elif net_buy < SELL_THRESHOLD and (buy_sell_ratio or 1) < 0.8:
            risk_list.append(item)

    focus_list.sort(key=lambda x: x["net_buy"], reverse=True)
    risk_list.sort(key=lambda x: x["net_buy"])

    return focus_list, risk_list


def _track_hot_money(records: list[DragonTigerRecord], seat_data: dict[str, dict] | None = None) -> list[dict]:
    """追踪短线资金（游资）动向

    基于换手率和涨跌幅判断游资行为，优先使用席位数据分析：
    - 席位数据可用时：优先以 hot_money_net 确认游资参与
    - 回退模式：涨停 + 高换手 → 游资拉升；跌停 + 高换手 → 游资出逃

    Args:
        records: 去重后的龙虎榜记录
        seat_data: {code: {hot_money_net, hot_money_count, ...}} 席位分析汇总

    Returns:
        游资动向列表
    """
    tracking = []

    for record in records:
        if record.turnover_rate is None or record.change_pct is None:
            continue

        seat_info = seat_data.get(record.code, {}) if seat_data else {}
        hm_net = seat_info.get("hot_money_net", 0)
        hm_count = seat_info.get("hot_money_count", 0)

        if record.turnover_rate > 15:
            if _is_near_limit_up(record.change_pct, record.code):
                item = {
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资拉升",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "hot_money_count": hm_count,
                    "hot_money_net": hm_net,
                }
                if hm_count > 0:
                    item["detail"] = f"涨停+高换手({record.turnover_rate:.1f}%)，{hm_count}个游资席位净买{hm_net/1e4:+.0f}万，游资主导拉升"
                else:
                    item["detail"] = f"涨停+高换手({record.turnover_rate:.1f}%)，游资主导拉升"
                tracking.append(item)
            elif record.change_pct > 7:
                # 未到涨停但强势拉升+高换手（如20cm股票涨13%但未封板）
                item = {
                    "code": record.code,
                    "name": record.name,
                    "signal": "强势异动",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "hot_money_count": hm_count,
                    "hot_money_net": hm_net,
                }
                limit_pct = _get_limit_pct(record.code) * 100
                if hm_count > 0:
                    item["detail"] = f"强势拉升+高换手({record.turnover_rate:.1f}%, 未封{limit_pct:.0f}%板)，{hm_count}个游资席位净买{hm_net/1e4:+.0f}万"
                else:
                    item["detail"] = f"强势拉升+高换手({record.turnover_rate:.1f}%, 未封{limit_pct:.0f}%板)，资金活跃"
                tracking.append(item)
            elif _is_near_limit_down(record.change_pct, record.code):
                item = {
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资出逃",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "hot_money_count": hm_count,
                    "hot_money_net": hm_net,
                }
                if hm_count > 0:
                    item["detail"] = f"近跌停+高换手({record.turnover_rate:.1f}%)，{hm_count}个游资席位净卖{abs(hm_net)/1e4:.0f}万，游资出逃⚠️"
                else:
                    item["detail"] = f"近跌停+高换手({record.turnover_rate:.1f}%)，游资出逃⚠️"
                tracking.append(item)
            elif record.change_pct < -7:
                # 未到跌停但暴跌+高换手
                item = {
                    "code": record.code,
                    "name": record.name,
                    "signal": "恐慌抛售",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "hot_money_count": hm_count,
                    "hot_money_net": hm_net,
                }
                limit_pct = _get_limit_pct(record.code) * 100
                if hm_count > 0:
                    item["detail"] = f"暴跌+高换手({record.turnover_rate:.1f}%, 未封{limit_pct:.0f}%跌停)，{hm_count}个游资席位净卖{abs(hm_net)/1e4:.0f}万"
                else:
                    item["detail"] = f"暴跌+高换手({record.turnover_rate:.1f}%, 未封{limit_pct:.0f}%跌停)，恐慌抛售"
                tracking.append(item)
            elif abs(record.net_buy) < record.total_trade * 0.05 and record.total_trade > 0:
                tracking.append({
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资对倒",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "hot_money_count": hm_count,
                    "hot_money_net": hm_net,
                    "detail": f"高换手({record.turnover_rate:.1f}%)但净买入仅{record.net_buy/1e4:.0f}万，游资对倒",
                })

    return tracking


def _aggregate_by_reason(records: list[DragonTigerRecord]) -> list[dict]:
    """按上榜原因汇总

    分析不同类型上榜原因的分布，判断市场焦点。

    Returns:
        上榜原因汇总列表
    """
    reason_map: dict[str, dict] = {}

    for record in records:
        reason = record.reason or "未知"
        if reason not in reason_map:
            reason_map[reason] = {
                "reason": reason,
                "count": 0,
                "total_net_buy": 0.0,
                "stocks": [],
            }

        info = reason_map[reason]
        info["count"] += 1
        info["total_net_buy"] += record.net_buy
        info["stocks"].append(record.name)

    result = sorted(reason_map.values(), key=lambda x: x["count"], reverse=True)
    for info in result:
        info.pop("stocks", None)
        # 清理上榜原因前缀
        info["reason"] = _clean_reason(info["reason"])

    return result


def _aggregate_sector_flow(records: list[DragonTigerRecord], industry_map: dict[str, str] | None = None) -> list[dict]:
    """汇总真实行业板块资金流向

    优先使用 industry_map（从东方财富获取的真实行业分类），
    若不可用则回退到从上榜原因中推断。

    Args:
        records: 龙虎榜记录列表
        industry_map: {code: industry} 映射，由 _get_stock_industry_map 提供

    Returns:
        行业板块资金流向列表（按净流入排序，只返回上榜数 >= 2 的板块）
    """
    sector_map: dict[str, dict] = {}

    for record in records:
        # 优先用真实行业，回退到上榜原因推断
        if industry_map and record.code in industry_map:
            industry = industry_map[record.code] or "其他"
        else:
            industry = _extract_sector_from_reason(record.reason or "")

        if industry not in sector_map:
            sector_map[industry] = {
                "industry": industry,
                "total_net_buy": 0.0,
                "total_trade": 0.0,
                "stock_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "top_buyer": ("", 0.0),
                "top_seller": ("", 0.0),
            }

        info = sector_map[industry]
        info["total_net_buy"] += record.net_buy
        info["total_trade"] += record.total_trade
        info["stock_count"] += 1
        if record.net_buy >= 0:
            info["positive_count"] += 1
        else:
            info["negative_count"] += 1
        # 跟踪板块内最大买/卖家
        if record.net_buy > info["top_buyer"][1]:
            info["top_buyer"] = (record.name, record.net_buy)
        if record.net_buy < info["top_seller"][1]:
            info["top_seller"] = (record.name, record.net_buy)

    # 只保留上榜数 >= 2 的板块，单只个股不构成板块信号
    result = [v for v in sector_map.values() if v["stock_count"] >= 2]
    result.sort(key=lambda x: x["total_net_buy"], reverse=True)
    return result


def _extract_sector_from_reason(reason: str) -> str:
    """从上榜原因中提取板块/行业关键词（仅用于回退）"""
    keywords = {
        "ST": "ST板块",
        "新股": "新股",
        "无价格涨跌幅限制": "新股",
        "连续三个交易日": "连板股",
    }
    for kw, sector in keywords.items():
        if kw in reason:
            return sector
    return "主板"


def merge_duplicate_records(records: list[DragonTigerRecord]) -> list[DragonTigerRecord]:
    """合并同一股票代码的重复记录

    同一只股票可能因多个上榜原因（如"涨幅偏离值"+"换手率异常"）
    分别出现在龙虎榜中。这些记录代表不同席位/不同交易日的汇总，
    需要合并处理以获得完整的资金流向。

    Args:
        records: 原始龙虎榜记录列表（可能含重复 code）

    Returns:
        去重合并后的记录列表
    """
    if not records:
        return []

    grouped: dict[str, list[DragonTigerRecord]] = {}
    for r in records:
        if r.code not in grouped:
            grouped[r.code] = []
        grouped[r.code].append(r)

    merged = []
    for code, items in grouped.items():
        if len(items) == 1:
            merged.append(items[0])
        else:
            # 合并：金额累加，属性取第一条有效值，原因拼接
            first = items[0]
            reasons = []
            total_buy = 0.0
            total_sell = 0.0
            net_buy = 0.0
            total_trade = 0.0
            for item in items:
                total_buy += item.total_buy
                total_sell += item.total_sell
                net_buy += item.net_buy
                total_trade += item.total_trade
                if item.reason and item.reason not in reasons:
                    reasons.append(item.reason)

            merged_record = DragonTigerRecord(
                code=code,
                name=first.name,
                change_pct=first.change_pct,
                total_buy=total_buy,
                total_sell=total_sell,
                net_buy=net_buy,
                total_trade=total_trade,
                turnover_rate=first.turnover_rate,
                reason="；".join(reasons),
                industry=first.industry,
                sector=first.sector,
                buy_seats=first.buy_seats,
                sell_seats=first.sell_seats,
                main_net_inflow=first.main_net_inflow,
            )
            merged.append(merged_record)

    if len(merged) < len(records):
        log.info(f"龙虎榜去重: {len(records)}条 → {len(merged)}只个股")

    return merged


def _get_limit_pct(code: str) -> float:
    """根据股票代码前缀返回涨跌停幅度（小数，如 0.10 表示 10%）

    主板(000/001/002/003/600/601/603/605): 10%
    创业板(300/301): 20%
    科创板(688): 20%
    北交所(8xx): 30%
    新三板(4xx): 30%
    """
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def _is_near_limit_up(change_pct: float, code: str) -> bool:
    """判断涨跌幅是否接近涨停（在涨停价的 95% 以内）"""
    limit = _get_limit_pct(code)
    return change_pct >= limit * 0.95 * 100


def _is_near_limit_down(change_pct: float, code: str) -> bool:
    """判断涨跌幅是否接近跌停（在跌停价的 70% 以内）"""
    limit = _get_limit_pct(code)
    return change_pct <= -limit * 0.70 * 100


def _detect_abnormal_patterns(records: list[DragonTigerRecord]) -> list[dict]:
    """检测龙虎榜中的异常交易形态

    识别以下形态：
    - 涨停板出货：涨停但龙虎榜呈净卖出，买卖比低（诱多出货）
    - 跌停接筹：大跌但龙虎榜呈净买入（恐慌中接筹）
    - 封板缩量：涨停 + 换手率极低（筹码锁定，次日高溢价概率高）
    - 放量烂板：涨停 + 高换手 + 低买卖比（封板质量差）
    - 机构对倒：成交额大但净买卖接近零（疑似对倒）

    Args:
        records: 去重后的龙虎榜记录

    Returns:
        异常形态列表
    """
    patterns = []

    for r in records:
        change = r.change_pct or 0
        ratio = r.buy_sell_ratio
        turnover = r.turnover_rate or 0
        net = r.net_buy
        trade = r.total_trade

        # 1. 涨停板出货：近涨停 + 净卖出 + 买卖比 < 0.5
        if _is_near_limit_up(change, r.code) and net < SELL_THRESHOLD and ratio is not None and ratio < 0.5:
            patterns.append({
                "code": r.code,
                "name": r.name,
                "pattern_type": "limit_up_distribution",
                "pattern_label": "🚨涨停板出货",
                "change_pct": change,
                "net_buy": net,
                "buy_sell_ratio": ratio,
                "turnover_rate": turnover,
                "detail": f"近涨停(封板换手{turnover:.1f}%)但龙虎榜净卖出{abs(net)/1e8:.2f}亿，买卖比仅{ratio:.2f}，"
                          f"典型诱多出货形态，次日大概率高开低走",
            })

        # 2. 跌停接筹：近跌停 + 净买入 > 1000万
        if _is_near_limit_down(change, r.code) and net > BUY_THRESHOLD:
            patterns.append({
                "code": r.code,
                "name": r.name,
                "pattern_type": "limit_down_accumulation",
                "pattern_label": "💰跌停接筹",
                "change_pct": change,
                "net_buy": net,
                "buy_sell_ratio": ratio,
                "turnover_rate": turnover,
                "detail": f"近跌停({change:+.1f}%)但龙虎榜净买入{net/1e8:.2f}亿，换手{turnover:.1f}%，"
                          f"资金在恐慌中接筹。关注止跌确认后的反弹机会",
            })

        # 3. 封板缩量：近涨停 + 换手率 < 5% + 买卖比 > 1.5
        if _is_near_limit_up(change, r.code) and turnover < 5 and ratio is not None and ratio > 1.5:
            patterns.append({
                "code": r.code,
                "name": r.name,
                "pattern_type": "tight_lockup",
                "pattern_label": "🔒封板缩量",
                "change_pct": change,
                "net_buy": net,
                "buy_sell_ratio": ratio,
                "turnover_rate": turnover,
                "detail": f"近涨停封板换手仅{turnover:.1f}%，买卖比{ratio:.2f}，"
                          f"筹码高度锁定，次日大概率有溢价",
            })

        # 4. 放量烂板：近涨停 + 高换手(>15%) + 买卖比 < 1.5
        if _is_near_limit_up(change, r.code) and turnover > 15 and ratio is not None and ratio < 1.5:
            patterns.append({
                "code": r.code,
                "name": r.name,
                "pattern_type": "weak_lockup",
                "pattern_label": "⚠️放量烂板",
                "change_pct": change,
                "net_buy": net,
                "buy_sell_ratio": ratio,
                "turnover_rate": turnover,
                "detail": f"近涨停但换手{turnover:.1f}%、买卖比仅{ratio:.2f}，"
                          f"封板质量差，次日容易大幅分化",
            })

        # 5. 机构对倒：成交额 > 1亿 且 净买入/成交额 < 5%
        if trade > 1e8 and abs(net) < trade * 0.05 and ratio is not None and 0.9 < ratio < 1.1:
            patterns.append({
                "code": r.code,
                "name": r.name,
                "pattern_type": "wash_trade",
                "pattern_label": "🔄机构对倒",
                "change_pct": change,
                "net_buy": net,
                "buy_sell_ratio": ratio,
                "turnover_rate": turnover,
                "detail": f"成交额{trade/1e8:.2f}亿但净买卖仅{net/1e4:+.0f}万，"
                          f"买卖比{ratio:.2f}接近1:1，疑似对倒行为",
            })

    # 合并同一只个股的多个异常形态
    return _merge_patterns(patterns)


def _merge_patterns(patterns: list[dict]) -> list[dict]:
    """合并同一只个股的多个异常形态到一行

    优先保留最关键的形态标签，用 / 连接多个标签。
    详情取最靠前的非对倒形态，对倒形态优先级最低。
    """
    if not patterns:
        return []

    # 形态优先级：涨停板出货 > 跌停接筹 > 封板缩量 = 放量烂板 > 机构对倒
    _PRIORITY = {
        "limit_up_distribution": 5,
        "limit_down_accumulation": 4,
        "tight_lockup": 3,
        "weak_lockup": 3,
        "wash_trade": 1,
    }

    grouped: dict[str, list[dict]] = {}
    for p in patterns:
        code = p["code"]
        if code not in grouped:
            grouped[code] = []
        grouped[code].append(p)

    merged = []
    for code, items in grouped.items():
        if len(items) == 1:
            merged.append(items[0])
        else:
            # 按优先级排序，取最高优先级的为主
            items.sort(key=lambda x: _PRIORITY.get(x["pattern_type"], 0), reverse=True)
            primary = items[0]
            labels = [item["pattern_label"] for item in items]
            # 去重标签
            seen = set()
            unique_labels = []
            for lb in labels:
                if lb not in seen:
                    seen.add(lb)
                    unique_labels.append(lb)
            primary["pattern_label"] = " / ".join(unique_labels)
            # 取第一条非对倒的 detail 作为主 detail
            non_wash = [item for item in items if item["pattern_type"] != "wash_trade"]
            primary["detail"] = (non_wash[0]["detail"] if non_wash else items[0]["detail"])
            merged.append(primary)

    return merged


def _clean_reason(reason: str) -> str:
    """清理上榜原因中的冗余前缀（如 "非S证券"）"""
    prefixes_to_strip = ["非S证券", "非ST证券", "非*ST证券"]
    for prefix in prefixes_to_strip:
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


def _score_tomorrow_watch(
    records: list[DragonTigerRecord],
    seat_data: dict[str, dict],
    abnormal_patterns: list[dict],
    consecutive_listings: list[dict],
) -> list[dict]:
    """综合多维度评分，推荐明日最值得关注的个股

    评分维度：
      +3: 机构主导买入 + 封板缩量（筹码锁定）
      +3: 机构主导买入 + 龙虎榜大额净买入（>3000万）
      +2: 跌停接筹 + 买卖比 > 1.5（资金接盘意愿强）
      +2: 连续上榜 + 同向加仓（趋势确认）
      +1: 游资抱团 + 非连续上榜（首次博弈机会）
      -2: 散户接盘 / 机构大额出逃
      -3: 涨停板出货
      -1: 机构对倒

    Returns:
        推荐列表（按得分降序），每项含 code/name/score/reasons
    """
    # 建立辅助索引
    abnormal_map: dict[str, dict] = {p["code"]: p for p in abnormal_patterns}
    consecutive_map: dict[str, dict] = {c["code"]: c for c in consecutive_listings}
    results: list[dict] = []

    for r in records:
        score = 0
        reasons: list[str] = []
        code = r.code
        sd = seat_data.get(code, {})
        ab = abnormal_map.get(code, {})
        pat_type = ab.get("pattern_type", "")
        pat_types = pat_type.split("/") if pat_type else []

        # +3: 机构主导 + 封板缩量
        if sd.get("quality") == "高质" and "tight_lockup" in pat_types:
            score += 3
            reasons.append("机构主导+封板缩量")
        # +3: 机构主导 + 大额净买
        elif sd.get("quality") == "高质" and r.net_buy > 30_000_000:
            score += 3
            reasons.append("机构主导大额买入")
        # +2: 机构主导（无风险）
        elif sd.get("quality") == "高质" and not sd.get("risk_flags"):
            score += 2
            reasons.append("机构主导买入")

        # +2: 跌停接筹 + 强买盘
        if "limit_down_accumulation" in pat_types:
            ratio = r.buy_sell_ratio or 1
            if ratio > 1.5:
                score += 2
                reasons.append("跌停接筹(强买盘)")
            else:
                score += 1
                reasons.append("跌停接筹")

        # +2: 连续上榜 + 同向加仓
        if code in consecutive_map:
            cs = consecutive_map[code]
            if not cs.get("is_relay"):
                score += 2
                reasons.append(f"连续{cs['consecutive_days']}天上榜(同向)")

        # +1: 游资抱团 + 非连续上榜
        if sd.get("quality") == "活跃" and code not in consecutive_map:
            score += 1
            reasons.append("游资抱团(首次)")

        # -3: 涨停板出货
        if "limit_up_distribution" in pat_types:
            score -= 3
            reasons.append("涨停板出货")

        # -2: 风险
        risk_flags = sd.get("risk_flags", [])
        if "散户接盘" in risk_flags or "机构大额出逃" in risk_flags:
            score -= 2
            reasons.append(f"风险:{'/'.join(risk_flags[:2])}")

        # -1: 机构对倒
        if "wash_trade" in pat_types:
            score -= 1
            reasons.append("机构对倒")

        if score > 0:
            results.append({
                "code": code,
                "name": r.name,
                "score": score,
                "change_pct": r.change_pct,
                "net_buy": r.net_buy,
                "buy_sell_ratio": r.buy_sell_ratio,
                "reasons": reasons,
                "quality": sd.get("quality", "普通"),
            })

    # 按得分降序，得分相同按净买入降序
    results.sort(key=lambda x: (x["score"], x["net_buy"]), reverse=True)
    return results[:6]
    """清理上榜原因中的冗余前缀（如 "非S证券"）"""
    prefixes_to_strip = ["非S证券", "非ST证券", "非*ST证券"]
    for prefix in prefixes_to_strip:
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


def _detect_sector_divergence(records: list[DragonTigerRecord], industry_map: dict[str, str] | None = None) -> list[dict]:
    """检测板块内部分化

    当同一行业/分类内同时存在大额净买入和大额净卖出的个股时，
    说明该板块内部正在发生资金切换。

    Args:
        records: 去重后的龙虎榜记录
        industry_map: {code: industry} 映射

    Returns:
        内部分化板块列表
    """
    # 按行业分类
    sector_stocks: dict[str, list[dict]] = {}
    for r in records:
        if industry_map and r.code in industry_map:
            sector = industry_map[r.code] or "其他"
        else:
            sector = _extract_sector_from_reason(r.reason or "")
        if sector not in sector_stocks:
            sector_stocks[sector] = []
        sector_stocks[sector].append({
            "code": r.code,
            "name": r.name,
            "net_buy": r.net_buy,
            "change_pct": r.change_pct,
        })

    divergences = []
    for sector, stocks in sector_stocks.items():
        if len(stocks) < 3:
            continue

        buyers = [s for s in stocks if s["net_buy"] > BUY_THRESHOLD]
        sellers = [s for s in stocks if s["net_buy"] < SELL_THRESHOLD]

        # 既有大额买入又有大额卖出 → 分化
        if buyers and sellers:
            buyers.sort(key=lambda x: x["net_buy"], reverse=True)
            sellers.sort(key=lambda x: x["net_buy"])

            buy_leaders = [f"{s['name']}(+{s['net_buy']/1e8:.2f}亿)" for s in buyers[:3]]
            sell_laggards = [f"{s['name']}({s['net_buy']/1e8:.2f}亿)" for s in sellers[:3]]

            note = f"{' > '.join(buy_leaders[:2])} vs {' / '.join(sell_laggards[:2])} — 资金内部分化"

            divergences.append({
                "sector": sector,
                "buy_leaders": buy_leaders,
                "sell_laggards": sell_laggards,
                "note": note,
            })

    return divergences


def analyze_dragon_tiger(
    records: list[DragonTigerRecord],
    seat_analyses: list | None = None,
) -> DragonTigerSummary:
    """综合分析龙虎榜数据

    对龙虎榜数据进行多维度分析，输出结构化的分析结果。
    自动合并同一股票代码的重复记录，检测异常形态和板块分化。

    Args:
        records: 龙虎榜记录列表（可能含重复 code）
        seat_analyses: 外部预取的席位分析结果（SeatAnalysis 列表），用于游资追踪和资金结构，
                       传入后内部不再重复调用席位 API。调用方（__main__.py）负责统一拉取一次。

    Returns:
        DragonTigerSummary 综合分析结果
    """
    if not records:
        return DragonTigerSummary(
            date=datetime.now().strftime("%Y-%m-%d"),
            total_count=0,
            overall_assessment="今日无龙虎榜数据（可能非交易日）",
        )

    # 0. 去重合并
    merged = merge_duplicate_records(records)
    original_count = len(records)

    # 0.5 获取真实行业分类（用于板块分析和分化检测）
    merged_codes = [r.code for r in merged]
    industry_map = _get_stock_industry_map(merged_codes)
    if industry_map:
        log.info(f"获取到 {len(industry_map)} 只个股的行业分类")

    # 0.6 从外部席位数据构建 seat_data（不再内部调用 API）
    seat_data: dict[str, dict] = {}
    if seat_analyses:
        for sa in seat_analyses:
            seat_data[sa.code] = {
                "institution_net": sa.institution_net,
                "hsgt_net": sa.hsgt_net,
                "hot_money_net": sa.hot_money_net,
                "hot_money_count": sum(1 for s in sa.seats if s.seat_type == "知名游资"),
                "retail_net": sa.retail_net,
                "quality": sa.quality_label,
                "risk_flags": sa.risk_flags,
            }
        log.info(f"席位数据整合: {len(seat_data)} 只个股（外部传入）")

    # 1. 各项分析
    focus_list, risk_list = _classify_capital_flow(merged)
    hot_money = _track_hot_money(merged, seat_data if seat_data else None)
    sector_flow = _aggregate_sector_flow(merged, industry_map if industry_map else None)
    reason_summary = _aggregate_by_reason(merged)
    abnormal_patterns = _detect_abnormal_patterns(merged)
    sector_divergences = _detect_sector_divergence(merged, industry_map if industry_map else None)

    # 2. 连续上榜追踪
    consecutive_listings: list[dict] = []
    try:
        from app.dragon_seat import detect_consecutive_listings
        consecutive_listings = detect_consecutive_listings(merged)
    except Exception as e:
        log.debug(f"连续上榜检测跳过: {e}")

    # 2.5. 明日关注推荐评分
    tomorrow_watch = _score_tomorrow_watch(
        merged, seat_data, abnormal_patterns, consecutive_listings
    )

    # 3. 计算全市场净买入总额
    total_net_buy = sum(r.net_buy for r in merged)

    # 4. 如果有席位数据，汇总机构/游资/北向净流向
    total_inst_net = sum(sd.get("institution_net", 0) for sd in seat_data.values())
    total_hm_net = sum(sd.get("hot_money_net", 0) for sd in seat_data.values())
    total_hsgt_net = sum(sd.get("hsgt_net", 0) for sd in seat_data.values())

    top_focus = focus_list[:8]
    top_risk = risk_list[:5]
    top_sectors = sector_flow[:8]

    # 5. 构建整体研判
    assessment_parts = []

    # 全市场净流向
    if abs(total_net_buy) >= 1e8:
        direction = "净流入" if total_net_buy > 0 else "净流出"
        assessment_parts.append(f"龙虎榜全市场{direction}{abs(total_net_buy)/1e8:.2f}亿")

    if top_focus:
        focus_names = "、".join(f"{s['name']}" for s in top_focus[:3])
        top_net = top_focus[0]["net_buy"]
        net_str = f"{top_net/1e8:.2f}亿" if abs(top_net) >= 1e8 else f"{top_net/1e4:.0f}万"
        assessment_parts.append(f"资金净买入: {focus_names} 等, 最大净买{net_str}")

    if top_risk:
        risk_names = "、".join(s["name"] for s in top_risk[:3])
        top_net = top_risk[0]["net_buy"]
        net_str = f"{abs(top_net)/1e8:.2f}亿" if abs(top_net) >= 1e8 else f"{abs(top_net)/1e4:.0f}万"
        assessment_parts.append(f"资金净卖出: {risk_names} 等, 最大净卖{net_str}")

    # 主力资金结构（如果有席位数据）
    if seat_data:
        fund_parts = []
        if abs(total_inst_net) >= 5e7:
            fund_parts.append(f"机构{'净买' if total_inst_net > 0 else '净卖'}{abs(total_inst_net)/1e8:.2f}亿")
        if abs(total_hsgt_net) >= 5e7:
            fund_parts.append(f"北向{'净买' if total_hsgt_net > 0 else '净卖'}{abs(total_hsgt_net)/1e8:.2f}亿")
        if abs(total_hm_net) >= 3e7:
            fund_parts.append(f"游资{'净买' if total_hm_net > 0 else '净卖'}{abs(total_hm_net)/1e8:.2f}亿")
        if fund_parts:
            assessment_parts.append("主力结构: " + "，".join(fund_parts))

    if hot_money:
        lift_count = sum(1 for h in hot_money if h["signal"] in ("游资拉升", "强势异动"))
        escape_count = sum(1 for h in hot_money if h["signal"] in ("游资出逃", "恐慌抛售"))
        if lift_count > escape_count:
            assessment_parts.append(f"短线情绪偏暖（做多{lift_count}只 > 做空{escape_count}只）")
        elif escape_count > lift_count:
            assessment_parts.append(f"短线情绪偏冷（做空{escape_count}只 > 做多{lift_count}只）")
        else:
            assessment_parts.append(f"游资活跃（做多{lift_count}只, 做空{escape_count}只）")

    if reason_summary:
        top_reason = reason_summary[0]
        # strip "非S证券" 等前缀
        clean_reason = _clean_reason(top_reason['reason'])
        assessment_parts.append(f"主要上榜类型: {clean_reason[:25]} ({top_reason['count']}只)")

    # 异常形态信号
    if abnormal_patterns:
        limit_up_dist = [p for p in abnormal_patterns if p["pattern_type"] == "limit_up_distribution"]
        limit_down_acc = [p for p in abnormal_patterns if p["pattern_type"] == "limit_down_accumulation"]
        if limit_up_dist:
            names = "、".join(p["name"] for p in limit_up_dist[:3])
            assessment_parts.append(f"涨停板出货: {names}等{len(limit_up_dist)}只")
        if limit_down_acc:
            names = "、".join(p["name"] for p in limit_down_acc[:3])
            assessment_parts.append(f"跌停接筹: {names}等{len(limit_down_acc)}只")

    if sector_divergences:
        divergent_sectors = [d["sector"] for d in sector_divergences[:3]]
        assessment_parts.append(f"板块分化: {'、'.join(divergent_sectors)}等{len(sector_divergences)}个板块内部分化")

    if consecutive_listings:
        relay_count = sum(1 for c in consecutive_listings if c.get("is_relay"))
        assessment_parts.append(f"连续上榜: {len(consecutive_listings)}只(含{relay_count}只方向转换)")

    overall = "；".join(assessment_parts) if assessment_parts else "龙虎榜资金面无明显方向性信号"

    summary = DragonTigerSummary(
        date=datetime.now().strftime("%Y-%m-%d"),
        total_count=len(merged),
        records=merged[:30],
        institutional_focus=top_focus,
        institutional_risk=top_risk,
        hot_money_track=hot_money[:10],
        sector_flow=top_sectors,
        overall_assessment=overall,
        abnormal_patterns=abnormal_patterns,
        sector_divergence=sector_divergences,
        reason_summary=reason_summary,
        industry_flow=top_sectors,
        consecutive_listings=consecutive_listings,
        tomorrow_watch=tomorrow_watch,
        total_net_buy=total_net_buy,
    )

    # 日志输出去重情况
    if original_count != len(merged):
        log.info(f"龙虎榜分析: 原始{original_count}条 → 去重{len(merged)}只个股")

    return summary


def format_dragon_tiger_report(summary: DragonTigerSummary) -> str:
    """将龙虎榜分析结果格式化为可读的报告文本

    Args:
        summary: 龙虎榜综合分析结果

    Returns:
        格式化的报告文本
    """
    lines = []
    lines.append(f"## 龙虎榜资金分析（{summary.date}）")
    lines.append(f"上榜个股: {summary.total_count} 只")
    lines.append("")

    if summary.overall_assessment:
        lines.append(f"**整体研判**: {summary.overall_assessment}")
        lines.append("")

    if summary.tomorrow_watch:
        lines.append("### 🎯 明日重点关注")
        lines.append("")
        lines.append("| 个股 | 评分 | 涨跌幅 | 净买入 | 买卖比 | 关注理由 |")
        lines.append("|------|------|--------|--------|--------|----------|")
        for w in summary.tomorrow_watch[:5]:
            score_bar = "⭐" * w["score"]
            chg_str = f"{w['change_pct']:+.1f}%" if w['change_pct'] is not None else "--"
            net_str = _format_money(w["net_buy"])
            ratio_str = f"{w['buy_sell_ratio']:.2f}" if w['buy_sell_ratio'] else "--"
            reasons = " + ".join(w["reasons"][:3])
            lines.append(f"| {w['name']}({w['code']}) | {score_bar}({w['score']}分) | {chg_str} | {net_str} | {ratio_str} | {reasons} |")
        lines.append("")

    if summary.institutional_focus:
        lines.append("### 🟢 资金关注（龙虎榜净买入）")
        lines.append("| 个股 | 龙虎榜净买入 | 买卖比 | 涨跌幅 | 换手率 |")
        lines.append("|------|-------------|--------|--------|--------|")
        for s in summary.institutional_focus[:6]:
            net_str = _format_money(s["net_buy"])
            ratio_str = f"{s['buy_sell_ratio']:.2f}" if s['buy_sell_ratio'] else "--"
            chg_str = f"{s['change_pct']:+.2f}%" if s['change_pct'] else "--"
            tr_str = f"{s['turnover_rate']:.1f}%" if s['turnover_rate'] else "--"
            lines.append(f"| {s['name']}({s['code']}) | {net_str} | {ratio_str} | {chg_str} | {tr_str} |")
        lines.append("")

    if summary.institutional_risk:
        lines.append("### 🔴 资金流出（龙虎榜净卖出）")
        lines.append("| 个股 | 龙虎榜净卖出 | 买卖比 | 涨跌幅 | 换手率 |")
        lines.append("|------|-------------|--------|--------|--------|")
        for s in summary.institutional_risk[:5]:
            net_str = _format_money(abs(s["net_buy"]))
            ratio_str = f"{s['buy_sell_ratio']:.2f}" if s['buy_sell_ratio'] else "--"
            chg_str = f"{s['change_pct']:+.2f}%" if s['change_pct'] else "--"
            tr_str = f"{s['turnover_rate']:.1f}%" if s['turnover_rate'] else "--"
            lines.append(f"| {s['name']}({s['code']}) | {net_str} | {ratio_str} | {chg_str} | {tr_str} |")
        lines.append("")

    if summary.hot_money_track:
        lines.append("### 🐉 游资动向（高换手个股）")
        for h in summary.hot_money_track[:6]:
            if h["signal"] == "游资拉升":
                emoji = "🟢"
            elif h["signal"] == "强势异动":
                emoji = "🟡"
            elif h["signal"] == "游资出逃":
                emoji = "🔴"
            elif h["signal"] == "恐慌抛售":
                emoji = "🟠"
            else:
                emoji = "⚪"
            lines.append(f"  {emoji} {h['name']}({h['code']}): {h['detail']}")
        lines.append("")

    # 行业资金流向（使用真实行业分类）
    if summary.industry_flow:
        lines.append("### 📊 行业资金流向")
        lines.append("| 行业 | 净买入合计 | 上榜个数 | 资金方向 | 龙头股 |")
        lines.append("|------|-----------|---------|---------|--------|")
        for s in summary.industry_flow[:8]:
            net_str = _format_money(s["total_net_buy"])
            direction = "🟢 净流入" if s["total_net_buy"] > 0 else "🔴 净流出"
            leader = s.get("top_buyer", ("--", 0))[0] or "--"
            lines.append(f"| {s['industry']} | {net_str} | {s['stock_count']}只 | {direction} | {leader} |")
        lines.append("")

    # 上榜原因分布
    if summary.reason_summary:
        lines.append("### 📋 上榜原因分布")
        lines.append("| 原因 | 数量 | 净买入合计 |")
        lines.append("|------|------|-----------|")
        for s in summary.reason_summary[:8]:
            clean_reason = _clean_reason(s["reason"])
            net_str = _format_money(s["total_net_buy"])
            lines.append(f"| {clean_reason} | {s['count']}只 | {net_str} |")
        lines.append("")

    if summary.consecutive_listings:
        lines.append("### 🔁 连续上榜追踪")
        lines.append("")
        for c in summary.consecutive_listings[:10]:
            days = c["consecutive_days"]
            relay = c.get("relay_note", "")
            prev_info = " → ".join(
                f"{e['date']}({e['net_buy']/1e8:+.2f}亿)" for e in c.get("prev_entries", [])[:3]
            )
            lines.append(f"- **{c['name']}**({c['code']}): 连续{days}天上榜 {relay}")
            if prev_info:
                lines.append(f"  历史: {prev_info}")
        lines.append("")

    if summary.abnormal_patterns:
        lines.append("### ⚠️ 异常形态警示")
        lines.append("")
        lines.append("| 个股 | 形态 | 涨跌幅 | 龙虎榜净买入 | 买卖比 | 详情 |")
        lines.append("|------|------|--------|-------------|--------|------|")
        for p in summary.abnormal_patterns[:10]:
            net_str = _format_money(p["net_buy"])
            ratio_str = f"{p['buy_sell_ratio']:.2f}" if p['buy_sell_ratio'] else "--"
            chg_str = f"{p['change_pct']:+.2f}%" if p['change_pct'] is not None else "--"
            detail_short = p["detail"][:60]
            lines.append(f"| {p['name']}({p['code']}) | {p['pattern_label']} | {chg_str} | {net_str} | {ratio_str} | {detail_short} |")
        lines.append("")

    if summary.sector_divergence:
        lines.append("### 🔀 板块内部分化")
        lines.append("")
        lines.append("以下行业/板块内部资金方向分化，可能在发生资金切换：")
        lines.append("")
        for d in summary.sector_divergence[:5]:
            lines.append(f"- **{d['sector']}**: {d['note']}")
        lines.append("")

    return "\n".join(lines)


def _format_money(amount: float) -> str:
    if abs(amount) >= 1e8:
        return f"{amount/1e8:.2f}亿"
    elif abs(amount) >= 1e4:
        return f"{amount/1e4:.0f}万"
    return f"{amount:.0f}元"


def build_llm_context(summary: DragonTigerSummary) -> str:
    """生成适用于 LLM prompt 的龙虎榜数据摘要

    Args:
        summary: 龙虎榜综合分析结果

    Returns:
        紧凑的文本摘要
    """
    if summary.total_count == 0:
        return ""

    lines = []
    lines.append("[龙虎榜资金分析]")

    if summary.overall_assessment:
        lines.append(f"  整体: {summary.overall_assessment}")

    if summary.institutional_focus:
        focus_short = []
        for s in summary.institutional_focus[:5]:
            focus_short.append(f"{s['name']}({_format_money(s['net_buy'])})")
        lines.append(f"  净买入前5: {' '.join(focus_short)}")

    if summary.institutional_risk:
        risk_short = []
        for s in summary.institutional_risk[:3]:
            risk_short.append(f"{s['name']}({_format_money(s['net_buy'])})")
        lines.append(f"  净卖出前3: {' '.join(risk_short)}")

    if summary.hot_money_track:
        hot_short = []
        for h in summary.hot_money_track[:4]:
            hm_info = h["signal"]
            if h.get("hot_money_count", 0) > 0:
                hm_info += f"({h['hot_money_count']}席位)"
            hot_short.append(f"{h['name']}({hm_info})")
        lines.append(f"  游资异动: {' '.join(hot_short)}")

    if summary.industry_flow:
        industry_short = []
        for s in summary.industry_flow[:3]:
            direction = "+" if s["total_net_buy"] > 0 else ""
            industry_short.append(f"{s['industry']}({direction}{_format_money(s['total_net_buy'])})")
        lines.append(f"  行业: {' '.join(industry_short)}")

    if summary.abnormal_patterns:
        pattern_short = []
        for p in summary.abnormal_patterns[:4]:
            pattern_short.append(f"{p['name']}({p['pattern_label']})")
        lines.append(f"  异常形态: {' '.join(pattern_short)}")

    if summary.sector_divergence:
        div_short = []
        for d in summary.sector_divergence[:3]:
            div_short.append(d['note'])
        lines.append(f"  板块分化: {'; '.join(div_short)}")

    if summary.consecutive_listings:
        consec_short = []
        for c in summary.consecutive_listings[:4]:
            relay = "🔄" if c.get("is_relay") else "→"
            consec_short.append(f"{c['name']}({c['consecutive_days']}天{relay})")
        lines.append(f"  连续上榜: {' '.join(consec_short)}")

    return "\n".join(lines)


# ============================================================
# LLM 龙虎榜深度解读
# ============================================================

def analyze_dragon_tiger_llm(
    summary: DragonTigerSummary,
    config,
    seat_analyses: list | None = None,
    holdings_alerts: list[dict] | None = None,
) -> str | None:
    """调用 LLM 生成龙虎榜深度解读

    将汇总报告、席位分析和持仓联动数据整合成一个结构化 prompt，
    让 LLM 输出资金意图推断、博弈格局分析和次日操作预判。

    Args:
        summary: 龙虎榜综合分析结果
        config: Config 对象（需含 llm_enabled、deepseek_key）
        seat_analyses: SeatAnalysis 列表（来自 dragon_seat.analyze_dragon_tiger_seats）
        holdings_alerts: 持仓联动预警列表（来自 dragon_seat.check_holdings_dragon_tiger）

    Returns:
        LLM 分析文本，调用失败或 LLM 未启用时返回 None
    """
    if not config.dragon_tiger_llm_enabled or not config.deepseek_key:
        return None

    if summary.total_count == 0:
        return None

    # ---- 构建完整数据区（Markdown 格式，LLM 能高效解析）----
    data_sections: list[str] = []

    # 1. 汇总报告（全市场数据 + 行业 + 异常形态 + 连续上榜）
    report_md = format_dragon_tiger_report(summary)
    data_sections.append(report_md)

    # 2. 席位级报告（如有）
    if seat_analyses:
        try:
            from app.dragon_seat import generate_seat_report
            seat_md = generate_seat_report(seat_analyses)
            data_sections.append(seat_md)
        except Exception:
            pass

    # 3. 持仓联动（如有）
    if holdings_alerts:
        ha_lines = ["### 🔔 持仓上榜预警", ""]
        for a in holdings_alerts:
            risk = f"⚠️ {', '.join(a.get('risk_flags', []))}" if a.get('risk_flags') else ""
            inst = f"机构{a.get('institution_net', 0)/1e4:+.0f}万" if a.get('institution_net') else ""
            hm = f"游资{a.get('hot_money_net', 0)/1e4:+.0f}万" if a.get('hot_money_net') else ""
            ha_lines.append(
                f"- **[{a.get('quality', '普通')}] {a['name']}({a['code']})** "
                f"{a.get('change_pct', 0):+.1f}% {inst} {hm} {risk}"
            )
            if a.get('suggestions'):
                for sug in a['suggestions']:
                    ha_lines.append(f"  {sug}")
        data_sections.append("\n".join(ha_lines))

    data_block = "\n\n---\n\n".join(data_sections)

    # ---- 构建 user prompt ----
    prompt = f"""以下是今日({summary.date})A股龙虎榜完整数据，请基于这些数据输出专业的龙虎榜深度解读。

{data_block}

---
请按以下结构输出分析（约 600-800 字，Markdown 格式）：

### 一、💰 资金面全景判断
- 今日龙虎榜整体多空力量判断（偏多/偏空/分歧），标注置信度[高/中/低]
- 主力资金结构：机构、北向、游资各自的态度和操作方向
- 如有连续上榜数据，对比近期趋势变化

### 二、🐉 重点席位博弈（选3-5只资金博弈最激烈的个股）
对每只个股输出：
- **个股名(代码)**: 谁在买 vs 谁在卖（机构/游资/量化/散户）
- 封板质量 / 接筹动机 / 出货嫌疑的判断
- 次日大概率走势预判 + 验证标准（如"次日开盘30分钟不破XX价位则确认强势"）

### 三、🔄 板块轮动信号
- 资金集中流入和流出的行业
- 是否有板块内部资金切换（从哪切到哪）
- 次日可能轮动的方向

### 四、⚠️ 风险与机会
- 异常形态中最值得警惕的 1-3 个信号
- 游资活跃度 + 明日涨停板溢价概率判断
- 如有持仓上榜，给出具体操作建议（具体触发条件）

---
**要求**：
- 每一个判断必须标注置信度[高/中/低]
- 不做复述数据的描述，做推断和预判
- 次日预判必须给出可验证标准，不写"可能涨也可能跌"
- 数据不足时直接说"数据不足以判断"，不强行给结论"""

    # ---- 调用 LLM ----
    try:
        from app.llm_client import get_llm_client, SYSTEM_PROMPTS
        llm = get_llm_client(config)
        system_prompt = SYSTEM_PROMPTS.get("dragon_tiger_analyst", "")
        response = llm.chat(
            prompt,
            system_prompt=system_prompt,
            max_tokens=2000,
            temperature=0.4,
            timeout=120,
        )
        if response:
            log.info("龙虎榜 LLM 分析生成成功")
        return response
    except Exception as e:
        log.error(f"龙虎榜 LLM 分析失败: {e}")
        return None
