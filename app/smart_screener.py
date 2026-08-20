"""
智能选股管线 —— 妙想 + LLM + 主力资金持续低吸

策略：稳健 / 低位埋伏。核心逻辑是「先用东财板块资金流排名锁定『主力净流入多 +
涨幅小』的资金潜伏板块，再在这些板块内找低位滞涨 + 主力持续低吸 + 尚未启动的个股」
——板块有资金悄悄流入但还没涨（潜伏），个股在低位横盘、主力持续吸筹，等板块启动时补涨。

五阶段管线：
  Stage 0  数据驱动板块选择：东财板块资金流排名（5日）→ 筛「主力净流入多 + 涨幅小」潜伏板块
           （东财失败时回退 LLM 热点板块判断 + 妙想新闻题材/龙虎榜背景）
  Stage 1  妙想 stock_screen 执行 → 候选池（跨条件去重 + 共振加分）
  Stage 2  主力资金持续低吸评分 + 多维过滤（黑名单板块/概念 + ST/退市 + 新股/次新股，核心）
  Stage 3  LLM 综合排序解读 → 强关注/关注/风险分级
  Stage 4  报告生成 + 保存 + ServerChan 推送

入口：run_smart_screening(config) -> ScreeningReport（随时手动触发，菜单 S 键）
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import Config
from app.helpers import _detect_market
from app.models import (
    AccumulationScore,
    FundFlowDaily,
    ScreeningCandidate,
    ScreeningCondition,
    ScreeningReport,
    SectorFundFlow,
)
from app.utils import log

NEWS_WINDOW_HOURS = 24  # 背景新闻回看窗口（小时）


# ============================================================
# 吸筹评分器（核心信号）
# ============================================================

def analyze_accumulation(
    code: str,
    name: str,
    flow_history: list[FundFlowDaily],
    price_change_pct: Optional[float],
) -> AccumulationScore:
    """主力资金「持续低吸」评分（0-100）

    核心思想：主力资金连续净流入（低吸）但股价横盘/微跌（背离），
    意味着筹码在低位悄悄集中，行情随时可能启动。

    评分维度（合计 100 分）：
      - 净流入天数占比   0-25 分  （窗口内主力净流入天数越多越强）
      - 最近连续净流入   0-15 分  （近期仍在持续吸筹加分）
      - 背离度（灵魂）   0-35 分  （资金净流入 + 股价滞涨 = 背离，涨幅越小分越高）
      - 累计净流入规模   0-15 分  （净流入金额越大越强）
      - 主力占比趋势     0-10 分  （近 5 日主力净占比较前 5 日上升 = 吸筹深化）

    Args:
        code: 股票代码
        name: 股票名称
        flow_history: 多日资金流序列（按日期升序）
        price_change_pct: 窗口股价涨跌幅（%），用于算背离度

    Returns:
        AccumulationScore
    """
    notes: list[str] = []
    days = len(flow_history)
    if days == 0:
        return AccumulationScore(
            code=code, name=name, score=0.0, label="无数据",
            notes=["无资金流数据"],
        )

    main_nets = [d.main_net or 0.0 for d in flow_history]
    inflow_days = sum(1 for n in main_nets if n > 0)
    total_net = sum(main_nets)

    # 最近连续净流入天数
    consecutive_days = 0
    for n in reversed(main_nets):
        if n > 0:
            consecutive_days += 1
        else:
            break

    # 1. 净流入天数占比
    inflow_score = (inflow_days / max(days, 1)) * 25.0

    # 2. 最近连续净流入
    consecutive_score = (min(consecutive_days, 5) / 5.0) * 15.0

    # 3. 背离度（灵魂）：资金净流入但股价滞涨
    divergence: Optional[float] = None
    if total_net > 0 and price_change_pct is not None:
        pc = price_change_pct
        if pc <= 0:
            divergence = 1.0  # 资金净流入 + 股价下跌 = 最强背离
        elif pc < 5:
            divergence = 1.0 - (pc / 5.0) * 0.5  # 0~5% 之间线性衰减到 0.5
        else:
            divergence = max(0.0, 0.5 - (pc - 5.0) / 30.0)  # 5%+ 继续衰减，约 20% 归零
        divergence = max(0.0, min(1.0, divergence))
    divergence_score = (divergence or 0.0) * 35.0

    # 4. 累计净流入规模（粗略：每 5000 万加 1 分，封顶 15）
    if total_net > 0:
        net_score = min(15.0, 8.0 + abs(total_net) / 5e7)
    else:
        net_score = 0.0

    # 5. 主力净占比趋势（近 5 日 vs 前 5 日）
    large_ratio_rising = _main_pct_rising(flow_history)
    large_ratio_score = 10.0 if large_ratio_rising else 0.0

    score = round(inflow_score + consecutive_score + divergence_score + net_score + large_ratio_score, 1)
    score = max(0.0, min(100.0, score))

    # 标签
    if total_net < 0:
        label = "出货"
    elif score >= 70:
        label = "强吸筹"
    elif score >= 50:
        label = "吸筹"
    else:
        label = "中性"

    # 判定说明
    notes.append(f"窗口{inflow_days}/{days}日净流入，连续{consecutive_days}日")
    if total_net > 0:
        notes.append(f"累计净流入 {total_net / 1e8:.2f} 亿")
    else:
        notes.append(f"累计净流出 {abs(total_net) / 1e8:.2f} 亿")
    if price_change_pct is not None:
        notes.append(f"窗口涨幅 {price_change_pct:+.1f}%")
    if divergence is not None:
        notes.append(f"背离度 {divergence:.2f}")
    if large_ratio_rising:
        notes.append("主力占比近5日上升")

    return AccumulationScore(
        code=code, name=name, score=score, label=label,
        inflow_days=inflow_days, consecutive_days=consecutive_days,
        total_net=total_net, price_change_10d=price_change_pct,
        divergence=divergence, large_ratio_rising=large_ratio_rising,
        notes=notes,
    )


def _main_pct_rising(flow_history: list[FundFlowDaily]) -> bool:
    """主力净占比近 5 日是否较前 5 日上升（机构/大户吸筹深化）"""
    pcts = [d.main_pct for d in flow_history if d.main_pct is not None]
    if len(pcts) < 10:
        return False
    recent = pcts[-5:]
    prior = pcts[-10:-5]
    if len(recent) != 5 or len(prior) != 5:
        return False
    return (sum(recent) / 5.0) > (sum(prior) / 5.0)


# ============================================================
# Stage 0：市场背景采集
# ============================================================

def _gather_market_context(config: Config) -> str:
    """采集市场背景：妙想财经新闻(最近24小时) + 龙虎榜板块流

    注意：妙想 query 接口拿不到「行业资金净流入排名」（实测仅返回全A聚合），
    故热点板块数据源改用「新闻题材 + 龙虎榜板块流」，由 Stage 1 的 LLM 做语义归一。

    Returns:
        拼接后的背景摘要（≤2000 字），任一块失败静默置空
    """
    parts: list[str] = []

    # 1. 妙想新闻题材（×3）
    if config.mx_apikeys:
        from app import miaoxiang
        client = miaoxiang.get_mx_client(config)
        for q in (
            f"最近{NEWS_WINDOW_HOURS}小时A股热点板块财经消息",
            f"最近{NEWS_WINDOW_HOURS}小时政策利好行业",
            f"最近{NEWS_WINDOW_HOURS}小时题材轮动方向",
        ):
            try:
                text = client.fin_search_as_text(q, hours=NEWS_WINDOW_HOURS)
                if text:
                    parts.append(text[:600])
            except Exception as e:
                log.debug(f"妙想资讯失败 [{q}]: {e}")
            time.sleep(0.4)  # 规避 112 频率限制

    # 2. 龙虎榜板块流 + 低位接筹形态
    try:
        from app import dragon_tiger
        records = dragon_tiger.fetch_dragon_tiger_list(max_count=30)
        if records:
            summary = dragon_tiger.analyze_dragon_tiger(records)
            ctx = dragon_tiger.build_llm_context(summary)
            if ctx:
                parts.append(ctx)
    except Exception as e:
        log.debug(f"龙虎榜背景采集失败: {e}")

    return "\n\n".join(parts)[:2000]


# ============================================================
# Stage 0：数据驱动板块选择 → 资金潜伏板块（主力净流入多 + 涨幅小）
# ============================================================

def _find_accumulating_sectors(config: Config, limit: int) -> list[SectorFundFlow]:
    """东财板块资金流排名 → 筛「主力资金净流入为正」的板块

    条件：板块5日主力净流入 > 0（资金流入为正），排除黑名单板块，
    按净流入金额降序取前 limit 个。东财行业板块约 86 个、正常交易日净流入为正的
    常有 40+ 个，故必须截断，否则妙想逐板块查询会过慢且易触发限流。
    """
    from app.data_fetcher import fetch_sector_fund_flow_rank

    sectors = fetch_sector_fund_flow_rank("5日")
    blacklist = config.screening_blacklist_sectors
    picked: list[SectorFundFlow] = []
    for s in sectors:
        if not s.name or _hits_blacklist(s.name, blacklist):
            continue
        if not s.main_net or s.main_net <= 0:
            continue
        picked.append(s)

    picked.sort(key=lambda s: (s.main_net or 0.0), reverse=True)
    return picked[:limit]


def _build_conditions_from_sectors(sectors: list[SectorFundFlow]) -> list[ScreeningCondition]:
    """从资金潜伏板块生成妙想选股条件（模板化，无需 LLM）"""
    conds: list[ScreeningCondition] = []
    for s in sectors:
        net = f"{s.main_net / 1e8:.2f}亿" if s.main_net is not None else "—"
        gain = f"{s.change_pct:+.1f}%" if s.change_pct is not None else "—"
        conds.append(ScreeningCondition(
            sector=s.name,
            condition=f"{s.name}板块中，股价处于低位且主力资金连续5日净流入的股票",
            intent=f"板块5日主力净流入{net}、涨幅{gain}，资金潜伏待启动，埋伏低位补涨标的",
            risk_note="",
        ))
    return conds


# ============================================================
# Stage 1（回退）：LLM 综合判断 → 热点板块 + 选股条件
# ============================================================

# function calling 工具定义：强制 LLM 输出结构化选股计划
SCREENING_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_screening_plan",
        "description": "提交当前热点板块与对应的妙想智能选股条件",
        "parameters": {
            "type": "object",
            "properties": {
                "hot_sectors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "今日热点板块名列表（3~5个，已排除黑名单，用市场通用简称）",
                },
                "conditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sector": {"type": "string", "description": "所属热点板块名"},
                            "condition": {
                                "type": "string",
                                "description": "妙想可执行的自然语言选股条件（带板块限定，低位埋伏方向，与当前时点一致的语义）",
                            },
                            "intent": {"type": "string", "description": "策略意图"},
                            "risk_note": {"type": "string", "description": "风险提示"},
                        },
                        "required": ["sector", "condition"],
                    },
                },
            },
            "required": ["conditions"],
        },
    },
}


def _build_stage1_prompt(context: str, blacklist: list[str], count: int) -> str:
    lines = [
        f"今天是 {datetime.now().strftime('%Y年%m月%d日')}。你是一名A股低位埋伏策略师。请根据以下市场背景，判断当前热点板块并给出妙想智能选股条件。",
        "",
        "【板块黑名单 —— 严禁选为热点、严禁出现在任何条件中】",
        "、".join(blacklist) if blacklist else "（无）",
        "",
        f"【要求】输出 {count} 条选股条件，覆盖 3~5 个热点板块；条件必须带板块限定、使用与当前时点一致的语义。",
        "",
        "【市场背景】",
        context if context else "（背景数据获取失败，请基于常识谨慎判断；若无法判断热点，hot_sectors 可留空、conditions 可给通用低位埋伏条件）",
        "",
        "请调用 submit_screening_plan 工具提交结果。",
    ]
    return "\n".join(lines)


def _generate_hot_sectors_and_conditions(
    config: Config, context: str
) -> tuple[list[str], list[ScreeningCondition]]:
    """LLM（persona=stock_screener）综合背景 → 热点板块 + 选股条件"""
    if not (config.llm_enabled and config.deepseek_key):
        log.warning("LLM 未启用，跳过热点板块判断")
        return [], []

    blacklist = config.screening_blacklist_sectors
    count = config.screening_condition_count
    prompt = _build_stage1_prompt(context, blacklist, count)

    try:
        from app.llm_client import get_llm_client, SYSTEM_PROMPTS
        llm = get_llm_client(config)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPTS.get("stock_screener", "")},
            {"role": "user", "content": prompt},
        ]
        resp = llm.chat_with_tools(
            messages=messages,
            tools=[SCREENING_TOOL],
            max_tokens=1500,
            temperature=0.3,
            timeout=120,
            tool_choice="required",
        )
    except Exception as e:
        log.warning(f"Stage 1 LLM 调用失败: {e}")
        return [], []

    return _parse_screening_plan(resp)


def _parse_screening_plan(resp) -> tuple[list[str], list[ScreeningCondition]]:
    """从 function calling 返回解析热点板块 + 选股条件"""
    hot_sectors: list[str] = []
    conditions: list[ScreeningCondition] = []
    for tc in (getattr(resp, "tool_calls", None) or []):
        args = tc.arguments or {}
        for s in args.get("hot_sectors") or []:
            s = str(s).strip()
            if s and s not in hot_sectors:
                hot_sectors.append(s)
        for c in args.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            sector = str(c.get("sector") or "").strip()
            condition = str(c.get("condition") or "").strip()
            if not sector or not condition:
                continue
            conditions.append(ScreeningCondition(
                sector=sector,
                condition=condition,
                intent=str(c.get("intent") or "").strip(),
                risk_note=str(c.get("risk_note") or "").strip(),
            ))
    return hot_sectors, conditions


# ============================================================
# Stage 2：妙想 stock_screen 执行 → 候选池
# ============================================================

def _run_mx_screening(
    config: Config, conditions: list[ScreeningCondition]
) -> list[ScreeningCandidate]:
    """逐条条件调用妙想 stock_screen_structured，跨条件去重 + 共振加分"""
    if not config.mx_apikeys or not conditions:
        return []

    from app import miaoxiang
    client = miaoxiang.get_mx_client(config)

    pool: dict[str, ScreeningCandidate] = {}
    for cond in conditions:
        try:
            rows = client.stock_screen_structured(cond.condition, page_size=20)
        except Exception as e:
            log.debug(f"妙想选股失败 [{cond.sector}]: {e}")
            rows = []

        for row in rows:
            code = row.get("code", "")
            if not code:
                continue
            cand = pool.get(code)
            if cand is None:
                cand = ScreeningCandidate(
                    code=code,
                    name=row.get("name", ""),
                    market=row.get("market", "") or _detect_market(code),
                    price=row.get("price"),
                    change_pct=row.get("change_pct"),
                    industry=row.get("industry", ""),
                    concept=row.get("concept", ""),
                )
                pool[code] = cand
            # 妙想自带多日主力净额（「连续N日净流入」条件下返回），保留最长序列
            fd = [
                FundFlowDaily(date=d.get("date", ""), main_net=d.get("main_net"))
                for d in (row.get("flow_days") or [])
                if isinstance(d, dict) and d.get("main_net") is not None
            ]
            if len(fd) > len(cand.flow_days):
                cand.flow_days = fd
            if cond.sector and cond.sector not in cand.hot_sectors:
                cand.hot_sectors.append(cond.sector)
            if cond.condition and cond.condition not in cand.hit_conditions:
                cand.hit_conditions.append(cond.condition)
            cand.resonance = len(cand.hit_conditions)

        time.sleep(0.4)  # 串行 + sleep，规避 112 频率限制

    return list(pool.values())


# ============================================================
# Stage 3：主力资金持续低吸评分 + 板块黑名单硬过滤（核心）
# ============================================================

def _hits_blacklist(text: Optional[str], blacklist: list[str]) -> bool:
    """文本是否命中黑名单关键词（包含匹配）"""
    if not text or not blacklist:
        return False
    t = str(text)
    return any(kw in t for kw in blacklist)


def _is_st(name: Optional[str]) -> bool:
    """是否为 ST/*ST/SST 或退市整理等风险股（名称含 ST 或 退）"""
    if not name:
        return False
    n = str(name).upper()
    return "ST" in n or "退" in n


def _is_sub_new(list_date: Optional[str], days: int) -> bool:
    """上市日期是否在最近 days 个自然日内（新股/次新股判定）"""
    if not list_date or days <= 0:
        return False
    try:
        ld = datetime.strptime(str(list_date), "%Y%m%d")
    except (ValueError, TypeError):
        return False
    return (datetime.now() - ld).days < days


def _exclusion_reason(
    cand: ScreeningCandidate,
    industry_map: dict[str, str],
    listing_map: dict[str, str],
    blacklist_sectors: list[str],
    blacklist_concepts: list[str],
    exclude_st: bool,
    sub_new_days: int,
) -> Optional[str]:
    """判断候选是否命中任一排除规则，命中返回原因字符串，未命中返回 None

    排除顺序（前序命中即返回，省去后续判断）：
      1. 行业黑名单（妙想行业总分类 → AKShare 行业映射兜底）
      2. 概念黑名单（板块黑名单 ∪ 概念黑名单，命中概念题材）
      3. ST/退市风险股
      4. 新股/次新股（上市不满 N 自然日）
    """
    # 1. 行业黑名单
    if blacklist_sectors:
        if _hits_blacklist(cand.industry, blacklist_sectors):
            return "行业黑名单"
        if _hits_blacklist(industry_map.get(cand.code, ""), blacklist_sectors):
            return "行业黑名单"
    # 2. 概念黑名单（板块关键词 + 独立概念关键词都查）
    concept_blacklist = list(dict.fromkeys(blacklist_sectors + blacklist_concepts))
    if _hits_blacklist(cand.concept, concept_blacklist):
        return "概念黑名单"
    # 3. ST/退市
    if exclude_st and _is_st(cand.name):
        return "ST/退市"
    # 4. 新股/次新股
    if sub_new_days > 0 and _is_sub_new(listing_map.get(cand.code, ""), sub_new_days):
        return "次新股"
    return None


def _score_accumulation(
    config: Config, candidates: list[ScreeningCandidate]
) -> list[ScreeningCandidate]:
    """对候选做资金流持续低吸评分，并执行多维排除过滤 + 截断到上限"""
    if not candidates:
        return []

    days = config.screening_fund_flow_days
    limit = config.screening_candidate_limit
    blacklist_sectors = config.screening_blacklist_sectors
    blacklist_concepts = config.screening_blacklist_concepts
    exclude_st = config.screening_exclude_st
    sub_new_days = config.screening_sub_new_days

    # 1. 行业映射 + 上市日期映射（均为批量、日级缓存，成本与候选数无关）
    industry_map: dict[str, str] = {}
    listing_map: dict[str, str] = {}
    try:
        from app import data_fetcher
        industry_map = data_fetcher.fetch_stock_industry_map([c.code for c in candidates])
    except Exception as e:
        log.debug(f"行业映射获取失败: {e}")
    if sub_new_days > 0:
        try:
            from app import data_fetcher
            listing_map = data_fetcher.fetch_stock_listing_date_map([c.code for c in candidates])
        except Exception as e:
            log.debug(f"上市日期映射获取失败: {e}")

    # 2. 多维排除硬过滤（行业/概念/ST/次新股，全部在昂贵取数之前）
    kept: list[ScreeningCandidate] = []
    removed: dict[str, list[str]] = {}
    for c in candidates:
        reason = _exclusion_reason(
            c, industry_map, listing_map,
            blacklist_sectors, blacklist_concepts, exclude_st, sub_new_days,
        )
        if reason:
            c.blacklisted = True
            removed.setdefault(reason, []).append(c.name)
        else:
            kept.append(c)
    if removed:
        for reason, names in removed.items():
            log.info(f"过滤[{reason}] {len(names)} 只: {names}")

    # 3. 截断到上限（先按共振分降序，减少后续 API 调用）
    kept.sort(key=lambda c: c.resonance, reverse=True)
    kept = kept[:limit]

    # 3b. 妙想未返回行业时，用 AKShare 行业映射回填（报告展示真实行业用）
    for c in kept:
        if not c.industry and industry_map.get(c.code):
            c.industry = industry_map[c.code]

    # 4a. 资金流：优先用妙想选股自带的多日主力净额（省去东财 daykline 取数，稳定且不耗配额），
    #     不足 3 日时回退东财（对并发极敏感，串行 + sleep 规避）
    from app import data_fetcher
    flow_map: dict[str, list] = {}
    for c in kept:
        if len(c.flow_days) >= 3:
            flow_map[c.code] = c.flow_days
            continue
        c.market = c.market or _detect_market(c.code)
        try:
            flow_map[c.code] = data_fetcher.fetch_fund_flow_history(c.code, c.market, days=days)
        except Exception:
            flow_map[c.code] = []
        time.sleep(0.5)

    # 4b. 串行拉K线（AKShare 个股限流约 1 次/秒）+ 计算背离度 + 评分
    from app import technical
    for c in kept:
        flow = flow_map.get(c.code, [])
        window = len(flow) if flow else days  # 与资金流窗口对齐（妙想 5 日 / 东财 days 日）
        price_chg: Optional[float] = None
        try:
            # 取更长K线（至少60日）用于 MA20/支撑压力，背离度仍只在资金流窗口上计算
            klines = technical.fetch_historical_kline(c.code, c.market, days=max(window, 60))
            if klines and len(klines) >= 2:
                last = klines[-1].close
                seg = klines[-window:] if window else klines
                base = seg[0].close
                if base and last:
                    price_chg = (last / base - 1.0) * 100.0
                c.last_price = last
                c.ma20 = technical.calc_ma_alignment(klines).ma20
                sr = technical.calc_support_resistance(klines)
                c.support = sr.support
                c.resistance = sr.resistance
        except Exception as e:
            log.debug(f"K线获取失败 {c.code}: {e}")

        c.accumulation = analyze_accumulation(c.code, c.name, flow, price_chg)
        time.sleep(0.7)

    return kept


# ============================================================
# Stage 4：LLM 综合排序解读
# ============================================================

def _finalize_ranking(candidates: list[ScreeningCandidate]) -> list[ScreeningCandidate]:
    """按吸筹分排序 + 确定性分级 + 排名（供报告与 __main__ 摘要用）"""
    scored = [c for c in candidates if c.accumulation is not None]
    scored.sort(key=lambda c: c.accumulation.score, reverse=True)
    for i, c in enumerate(scored, 1):
        c.rank = i
        s = c.accumulation.score
        if s >= 70:
            c.grade = "强关注"
        elif s >= 50:
            c.grade = "关注"
        elif s >= 30:
            c.grade = "观察"
        else:
            c.grade = "风险"
    return scored


def _industry_label(c: ScreeningCandidate) -> str:
    """取候选真实行业（东财行业分类，取最细分的一级）"""
    raw = (c.industry or "").strip()
    if not raw:
        return "—"
    for sep in ("-", "/", ">", "、"):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            if parts:
                raw = parts[-1]
            break
    return raw or "—"


def _build_stage4_prompt(top: list[ScreeningCandidate], sectors: list[SectorFundFlow]) -> str:
    lines = [
        f"今天是 {datetime.now().strftime('%Y年%m月%d日')}。以下是机器筛出的「低位吸筹」候选（已按吸筹分排序）。"
        "请结合所属板块资金潜伏强度、技术位置、资金持续低吸强度，做综合排序与分级。"
        "（输出标题与日期时请使用今天日期，不要臆造。）",
        "",
    ]
    if sectors:
        lines.append("【资金潜伏板块】（5日主力净流入多、涨幅小）")
        for s in sectors:
            net = f"{s.main_net / 1e8:.2f}亿" if s.main_net is not None else "—"
            pct = f"{s.main_pct:.2f}%" if s.main_pct is not None else "—"
            gain = f"{s.change_pct:+.1f}%" if s.change_pct is not None else "—"
            lines.append(f"- {s.name}：主力净流入{net}（净占比{pct}），5日涨幅{gain}")
        lines.append("")
    lines.append("【候选清单】")
    for c in top:
        acc = c.accumulation
        hot = "、".join(c.hot_sectors) or "—"
        industry = _industry_label(c)
        div = f"{acc.divergence:.2f}" if acc and acc.divergence is not None else "—"
        pchg = f"{acc.price_change_10d:+.1f}%" if acc and acc.price_change_10d is not None else "—"
        tech = "、".join(c.tech_signals) if c.tech_signals else "—"
        price = f"{c.last_price:.2f}元" if c.last_price else "—"
        ma20 = f"{c.ma20:.2f}元" if c.ma20 else "—"
        sup = f"{c.support:.2f}元" if c.support else "—"
        res = f"{c.resistance:.2f}元" if c.resistance else "—"
        lines.append(
            f"- {c.name}({c.code}) 行业={industry} 潜伏板块={hot} 吸筹分={acc.score:.1f} "
            f"净流入{acc.inflow_days}天 连续{acc.consecutive_days}天 背离度={div} "
            f"窗口涨幅={pchg} 现价={price} MA20={ma20} 支撑={sup} 压力={res} 技术={tech}"
        )
    lines.append("")
    lines.append(
        "请输出「强关注 / 关注 / 风险」三级清单，每只附一句理由和一个可量化的下一交易日验证标准。"
        "验证标准中的价格必须严格引用上面给出的现价/MA20/支撑/压力数值，禁止臆造任何价格或均线位置。"
    )
    return "\n".join(lines)


def _analyze_and_rank(
    config: Config, candidates: list[ScreeningCandidate], sectors: list[SectorFundFlow]
) -> str:
    """LLM（persona=screening_analyst）对 top 候选做综合排序解读"""
    if not candidates:
        return ""
    if not (config.llm_enabled and config.deepseek_key):
        return ""

    top = candidates[:15]  # 只传吸筹分 top 15，控制 prompt 大小
    prompt = _build_stage4_prompt(top, sectors)
    try:
        from app.llm_client import get_llm_client, SYSTEM_PROMPTS
        llm = get_llm_client(config)
        resp = llm.chat(
            prompt,
            system_prompt=SYSTEM_PROMPTS.get("screening_analyst", ""),
            max_tokens=2000,
            temperature=0.3,
            timeout=120,
        )
        return resp or ""
    except Exception as e:
        log.warning(f"Stage 4 LLM 调用失败: {e}")
        return ""


# ============================================================
# 降级兜底：妙想不可用 → 技术面底部反转筛选
# ============================================================

def _fallback_technical_screen(config: Config) -> list[ScreeningCandidate]:
    """妙想不可用/无候选时，回退到现有纯技术面底部反转筛选"""
    try:
        from app import stock_screener
        raw = stock_screener.screen_stock_bottom_reversal(
            max_candidates=config.screening_candidate_limit,
            max_kline_fetch=60,
        )
    except Exception as e:
        log.warning(f"技术面兜底筛选失败: {e}")
        return []

    return [
        ScreeningCandidate(
            code=c.code, name=c.name,
            market=_detect_market(c.code),
            price=c.price, change_pct=c.change_pct,
            tech_signals=list(c.signals or []),
            resonance=1,
        )
        for c in raw
    ]


# ============================================================
# Stage 5：报告生成 + 保存 + 推送
# ============================================================

def _build_markdown(report: ScreeningReport) -> str:
    lines = [
        "# 🎯 智能选股（资金潜伏板块 · 低位埋伏 · 主力持续低吸）",
        "",
        f"**日期**: {report.date}",
        "**策略**: 先锁「主力净流入多 + 涨幅小」的资金潜伏板块 → 板块内找「低位滞涨 + 主力持续低吸」的补涨标的",
    ]
    if report.degraded:
        lines.append("**模式**: ⚠️ 降级（妙想/LLM 不可用，已回退技术面筛选）")
    lines += ["", "---", ""]

    if report.accumulating_sectors:
        lines.append("## 💰 资金潜伏板块（5日主力净流入多 · 涨幅小）")
        lines.append("")
        lines.append("| 板块 | 5日主力净流入 | 净占比 | 5日涨幅 |")
        lines.append("|------|-------------|--------|---------|")
        for s in report.accumulating_sectors:
            net = f"{s.main_net / 1e8:.2f}亿" if s.main_net is not None else "—"
            pct = f"{s.main_pct:.2f}%" if s.main_pct is not None else "—"
            gain = f"{s.change_pct:+.1f}%" if s.change_pct is not None else "—"
            lines.append(f"| {s.name} | {net} | {pct} | {gain} |")
        lines.append("")
    elif report.hot_sectors:
        lines.append("## 🔥 热点板块")
        lines.append("")
        for s in report.hot_sectors:
            lines.append(f"- {s}")
        lines.append("")

    if report.conditions:
        lines.append("## 🧭 选股条件")
        lines.append("")
        lines.append("| 板块 | 选股条件 | 意图 |")
        lines.append("|------|---------|------|")
        for c in report.conditions:
            lines.append(f"| {c.sector} | {c.condition} | {c.intent} |")
        lines.append("")

    lines.append("## 📈 候选标的（按吸筹分排序）")
    lines.append("")
    if not report.candidates:
        lines.append("> 无候选。今日可能非交易日，或妙想/技术面筛选均未命中。")
    else:
        lines.append("| 排名 | 代码 | 名称 | 行业 | 潜伏板块 | 吸筹分 | 评级 | 背离度 | 净流入天数 |")
        lines.append("|------|------|------|------|---------|--------|------|--------|-----------|")
        for c in report.candidates:
            acc = c.accumulation
            score = f"{acc.score:.1f}" if acc else "—"
            label = acc.label if acc else "—"
            div = f"{acc.divergence:.2f}" if acc and acc.divergence is not None else "—"
            inflow = f"{acc.inflow_days}" if acc else "—"
            industry = _industry_label(c)
            hot = "、".join(c.hot_sectors) or "—"
            lines.append(f"| {c.rank} | {c.code} | {c.name} | {industry} | {hot} | {score} | {label} | {div} | {inflow} |")
        lines.append("")

    if report.llm_analysis:
        lines += ["---", "", "## 🤖 AI 综合研判", "", report.llm_analysis, ""]

    return "\n".join(lines)


def run_smart_screening(config: Config) -> ScreeningReport:
    """智能选股主入口（随时手动触发）

    Returns:
        ScreeningReport（含热点板块、条件、候选、LLM 解读），报告已落盘并推送
    """
    log.info("========== 智能选股（资金潜伏板块 · 低位埋伏 · 主力持续低吸） ==========")
    report = ScreeningReport(date=datetime.now().strftime("%Y-%m-%d"))
    context = ""

    # Stage 0：数据驱动板块选择（东财板块资金流 → 资金潜伏板块），失败回退 LLM 热点判断
    acc_sectors = _find_accumulating_sectors(config, config.screening_condition_count)
    if acc_sectors:
        hot_sectors = [s.name for s in acc_sectors]
        conditions = _build_conditions_from_sectors(acc_sectors)
        log.info(f"Stage 0 资金潜伏板块: {hot_sectors} | 条件数: {len(conditions)}")
    else:
        log.warning("东财板块资金流不可用，回退 LLM 热点板块判断")
        context = _gather_market_context(config)
        hot_sectors, conditions = _generate_hot_sectors_and_conditions(config, context)
        acc_sectors = [SectorFundFlow(name=n) for n in hot_sectors]
    report.hot_sectors = hot_sectors
    report.accumulating_sectors = acc_sectors
    report.conditions = conditions

    # Stage 1：妙想选股
    candidates = _run_mx_screening(config, conditions)
    log.info(f"Stage 1 妙想选股: {len(candidates)} 只候选")

    # 降级兜底
    if not candidates:
        report.degraded = True
        log.warning("妙想无候选，回退技术面底部反转筛选")
        candidates = _fallback_technical_screen(config)
        log.info(f"技术面兜底: {len(candidates)} 只候选")

    # Stage 2：吸筹评分 + 黑名单过滤
    candidates = _score_accumulation(config, candidates)
    candidates = _finalize_ranking(candidates)
    report.candidates = candidates
    log.info(f"Stage 2 评分完成: {len(candidates)} 只（已过滤黑名单/ST/次新股）")

    # Stage 3：LLM 综合解读
    report.llm_analysis = _analyze_and_rank(config, candidates, acc_sectors)

    # 非交易日提示
    if not context and not conditions and not candidates:
        report.error = "今日可能非交易日，或各数据源均无有效数据。"

    # Stage 4：报告落盘 + 推送
    content = _build_markdown(report)
    try:
        from app import reporter
        save_dir = Path(config.config_path).parent / "smart_screening"
        saved = reporter._save_report(content, "智能选股", save_dir)
        log.info(f"报告已保存: {saved}")
        if config.push_enabled and config.sct_sendkey:
            reporter._push_report("智能选股（低位吸筹）", content, config)
    except Exception as e:
        log.warning(f"报告保存/推送失败: {e}")

    return report
