"""
资金强势选股管线 —— 热门板块主力资金流入 + 板块内资金流入前 N% 强势标的

与 smart_screener（低位埋伏低吸）思路相反：本管线偏向「资金已大幅流入」的强势股。
核心逻辑分两层：
  1. 板块层：东财板块资金流排名 → 锁定「主力净流入最多」的热门板块（排除黑名单）
  2. 个股层：对每个热门板块，用东财 clist 拉板块成分股，按主力净流入降序取「前 N%」
     （至少 N_min 只，不足全取），跨板块去重后截断到候选上限

随后逐股采集资金面/量能/股价/估值/业绩/资讯，交 LLM 综合评价（护城河/风险点/评级/操作建议）。
采集前先做「筛选周期内个股走势强于所属板块」硬过滤，弱于板块的标的直接剔除。

入口：run_strong_screening(config) -> StrongScreeningReport（菜单 G 键）
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import Config
from app.helpers import _detect_market
from app.models import SectorFundFlow, StrongCandidate, StrongScreeningReport
from app.smart_screener import _exclusion_reason, _hits_blacklist
from app.utils import log


def _strength(net: Optional[float], float_mcap: Optional[float]) -> float:
    """主力净流入占流通市值比例 —— 归一化后的资金强度，跨市值可比。

    直接用净流入绝对额排序会让大盘股霸榜；除以流通市值后，衡量的是
    「相对盘子大小的资金进攻强度」，小市值强势股也能排到前面。
    """
    return (net / float_mcap) if (net and float_mcap) else 0.0


def _blend_score(main_net: Optional[float], float_mcap: Optional[float]) -> float:
    """绝对额(主) + 占流通强度(辅) 混合评分（0-100），避免「大盘霸榜」与「微盘霸榜」两个极端

    规模分 0-70：主力净流入绝对额对数映射（0.05亿→0、50亿→70 封顶），
    让「真金白银」规模主导排序，同时 log 压缩避免大盘股靠绝对额无限霸榜。
    强度分 0-30：净流入占流通市值比例，每 1% 给 3 分、10% 封顶。
    """
    net = main_net or 0.0
    size_score = 0.0
    if net > 0:
        size_score = (math.log10(net / 1e8) - math.log10(0.05)) / (
            math.log10(50.0) - math.log10(0.05)
        ) * 70.0
        size_score = max(0.0, min(70.0, size_score))
    strength_pct = _strength(main_net, float_mcap) * 100.0
    strength_score = max(0.0, min(30.0, strength_pct * 3.0))
    return size_score + strength_score


def _score_candidate(c: StrongCandidate) -> float:
    """强势候选综合评分（0-100，供排序/展示，好的排前面）"""
    return round(_blend_score(c.main_net, c.float_mcap), 1)


def _period_window(period: str) -> int:
    """资金流入周期 → K线回看交易日数（今日特殊，用当日涨跌幅，不走窗口）"""
    return {"5日": 5, "10日": 10}.get(period, 10)


def _period_label(period: str) -> str:
    """资金流入周期 → 展示用涨幅标签"""
    return {"今日": "当日", "5日": "5日", "10日": "10日"}.get(period, "10日")


# ============================================================
# Stage 0：热门板块选择（主力净流入最多）
# ============================================================

def _find_hot_sectors(config: Config) -> list[SectorFundFlow]:
    """东财板块资金流排名 → 筛「主力净流入为正」的热门板块，排除黑名单，取前 K 个"""
    from app.data_fetcher import fetch_sector_fund_flow_rank

    sectors = fetch_sector_fund_flow_rank(config.strong_flow_period)
    blacklist = config.screening_blacklist_sectors
    picked: list[SectorFundFlow] = []
    for s in sectors:
        if not s.name or not s.code:
            continue  # 需要板块代码才能查成分股
        if _hits_blacklist(s.name, blacklist):
            continue
        if not s.main_net or s.main_net <= 0:
            continue
        picked.append(s)

    picked.sort(key=lambda s: (s.main_net or 0.0), reverse=True)
    return picked[: config.strong_sector_count]


# ============================================================
# Stage 1：板块内个股（资金流入前 N%）
# ============================================================

def _pick_candidates(
    config: Config, sectors: list[SectorFundFlow]
) -> list[StrongCandidate]:
    """每个热门板块取成分股资金流入前 N%（至少 N_min 只），跨板块去重后截断到上限"""
    from app.data_fetcher import fetch_board_constituent_flow

    pct = config.strong_stock_pct
    min_n = config.strong_stock_min
    period = config.strong_flow_period

    pool: dict[str, StrongCandidate] = {}
    for s in sectors:
        try:
            rows = fetch_board_constituent_flow(s.code, period)
        except Exception as e:
            log.debug(f"板块成分股获取失败 [{s.name}]: {e}")
            rows = []
        if not rows:
            continue

        # 按「绝对额+强度」混合评分排序（绝对额为主、占流通强度为辅，避免微盘/大盘霸榜）
        rows = sorted(
            rows,
            key=lambda r: _blend_score(r.get("main_net"), r.get("float_mcap")),
            reverse=True,
        )
        take = max(min_n, math.ceil(len(rows) * pct / 100.0))
        take = min(take, len(rows))
        for r in rows[:take]:
            code = r.get("code", "")
            if not code:
                continue
            cand = pool.get(code)
            if cand is None:
                cand = StrongCandidate(
                    code=code,
                    name=r.get("name", ""),
                    market=_detect_market(code),
                    sector=s.name,
                    sector_main_net=s.main_net,
                    sector_change_pct=s.change_pct,
                    price=r.get("price"),
                    change_pct=r.get("change_pct"),
                    turnover_rate=r.get("turnover_rate"),
                    vol_ratio=r.get("vol_ratio"),
                    amount=r.get("amount"),
                    total_mcap=r.get("total_mcap"),
                    float_mcap=r.get("float_mcap"),
                    pe=r.get("pe"),
                    pb=r.get("pb"),
                    main_net=r.get("main_net"),
                )
                pool[code] = cand
            elif (s.main_net or 0.0) > (cand.sector_main_net or 0.0):
                # 同票命中多板块时，保留主力净流入更大的板块归属
                cand.sector = s.name
                cand.sector_main_net = s.main_net
                cand.sector_change_pct = s.change_pct
        time.sleep(0.3)

    cands = sorted(
        pool.values(), key=lambda c: _blend_score(c.main_net, c.float_mcap), reverse=True
    )
    return _truncate_by_sector(cands, config.strong_candidate_limit)


def _truncate_by_sector(
    candidates: list[StrongCandidate], limit: int
) -> list[StrongCandidate]:
    """候选超上限时按板块比例截断，保证每个热门板块都有机会入选。

    名额按各板块候选数占比分配（Hamilton 最大余数法），每板块至少保留 1 只，
    避免单一强势板块（主力净流入巨大）在全局排序里把其它板块全部挤出。
    """
    if not candidates or limit <= 0:
        return []
    if len(candidates) <= limit:
        return candidates

    groups: dict[str, list[StrongCandidate]] = {}
    for c in candidates:  # 已按资金强度降序，分组后组内亦降序
        groups.setdefault(c.sector or "未分类", []).append(c)

    n_sectors = len(groups)

    # 名额少于板块数：只保留资金强度最高的 top-limit 个板块，各 1 只
    if limit <= n_sectors:
        ordered = sorted(
            groups.values(), key=lambda g: -_blend_score(g[0].main_net, g[0].float_mcap)
        )
        picked = [g[0] for g in ordered[:limit]]
        return sorted(picked, key=lambda c: -_blend_score(c.main_net, c.float_mcap))

    total = len(candidates)
    quotas = {s: len(lst) * limit / total for s, lst in groups.items()}
    alloc = {s: max(1, int(quotas[s])) for s in groups}

    while sum(alloc.values()) < limit:
        best = max(groups, key=lambda s: quotas[s] - alloc[s])
        alloc[best] += 1
    while sum(alloc.values()) > limit:
        worst = min(
            (s for s in groups if alloc[s] > 1), key=lambda s: quotas[s] - alloc[s]
        )
        alloc[worst] -= 1

    picked: list[StrongCandidate] = []
    for s, lst in groups.items():
        take = min(alloc[s], len(lst))
        picked.extend(lst[:take])
        log.info(f"  板块配额[{s}]: {take}/{len(lst)} 只")

    return sorted(picked, key=lambda c: -_blend_score(c.main_net, c.float_mcap))


# ============================================================
# Stage 2：黑名单硬过滤（复用智能选股口径）
# ============================================================

def _filter_blacklist(
    config: Config, candidates: list[StrongCandidate]
) -> list[StrongCandidate]:
    """行业/概念/ST/次新股多维排除，回填真实行业"""
    if not candidates:
        return []

    from app import data_fetcher

    blacklist_sectors = config.screening_blacklist_sectors
    blacklist_concepts = config.screening_blacklist_concepts
    exclude_st = config.screening_exclude_st
    sub_new_days = config.screening_sub_new_days

    industry_map: dict[str, str] = {}
    listing_map: dict[str, str] = {}
    try:
        industry_map = data_fetcher.fetch_stock_industry_map([c.code for c in candidates])
    except Exception as e:
        log.debug(f"行业映射获取失败: {e}")
    if sub_new_days > 0:
        try:
            listing_map = data_fetcher.fetch_stock_listing_date_map([c.code for c in candidates])
        except Exception as e:
            log.debug(f"上市日期映射获取失败: {e}")

    kept: list[StrongCandidate] = []
    removed: dict[str, list[str]] = {}
    for c in candidates:
        reason = _exclusion_reason(
            c, industry_map, listing_map,
            blacklist_sectors, blacklist_concepts, exclude_st, sub_new_days,
        )
        if reason:
            removed.setdefault(reason, []).append(c.name)
            continue
        if not c.industry and industry_map.get(c.code):
            c.industry = industry_map[c.code]
        kept.append(c)

    if removed:
        for reason, names in removed.items():
            log.info(f"过滤[{reason}] {len(names)} 只: {names}")
    return kept


# ============================================================
# Stage 3：逐股深查（K线技术位 + 业绩 + 资讯 + 主营 + 机构研报）
# ============================================================

def _candidate_key_info(c: StrongCandidate, period_label: str = "10日") -> str:
    """单只候选的关键信息摘要（供 log 打印复盘）"""
    strength = (
        f"{c.main_net / c.float_mcap * 100:.2f}%"
        if c.main_net and c.float_mcap
        else "—"
    )
    turnover = f"{c.turnover_rate:.2f}%" if c.turnover_rate is not None else "—"
    volr = f"{c.vol_ratio:.2f}" if c.vol_ratio is not None else "—"
    pe = f"{c.pe:.1f}" if c.pe else "—"
    pb = f"{c.pb:.2f}" if c.pb else "—"
    price = f"{c.price:.2f}" if c.price else "—"
    ma20 = f"{c.ma20:.2f}" if c.ma20 else "—"
    sup = f"{c.support:.2f}" if c.support else "—"
    res = f"{c.resistance:.2f}" if c.resistance else "—"
    pchg = f"{c.price_change_pct:+.1f}%" if c.price_change_pct is not None else "—"
    vol = f"{c.volatility:.0f}%" if c.volatility is not None else "—"
    biz = "有" if c.business_text else "无"
    rep = "有" if c.report_text else "无"
    return (
        f"{c.name}({c.code}) 板块={c.sector} 行业={c.industry or '—'} | "
        f"主力净流入={_fmt_amount(c.main_net)} 占流通={strength} | "
        f"换手={turnover} 量比={volr} | PE={pe} PB={pb} | "
        f"现价={price} {period_label}涨幅={pchg} MA20={ma20} 支撑={sup} 压力={res} | "
        f"弹性={vol} 主营={biz} 研报={rep}"
    )


def _gather_fundamentals(
    config: Config, candidates: list[StrongCandidate]
) -> list[StrongCandidate]:
    """逐股采集：K线技术位（现价/MA20/支撑/压力/筛选周期涨幅/年化波动率）+ 妙想业绩 + 资讯 + 主营 + 机构研报

    先做「筛选周期内个股走势强于所属板块」硬过滤（K线取数廉价，可在妙想取数前剔除），
    走势弱于板块的候选直接跳过，省去昂贵的妙想查询。
    """
    from app import technical

    period = config.strong_flow_period
    window = _period_window(period)
    period_label = _period_label(period)

    mx = None
    if config.mx_apikeys:
        try:
            from app import miaoxiang
            mx = miaoxiang.get_mx_client(config)
        except Exception as e:
            log.debug(f"妙想客户端初始化失败: {e}")

    kept: list[StrongCandidate] = []
    removed: list[str] = []
    for c in candidates:
        # 1. K线技术位
        try:
            klines = technical.fetch_historical_kline(c.code, c.market, days=60)
            if klines and len(klines) >= 2:
                c.last_price = klines[-1].close
                c.ma20 = technical.calc_ma_alignment(klines).ma20
                sr = technical.calc_support_resistance(klines)
                c.support = sr.support
                c.resistance = sr.resistance
                # 筛选周期涨幅：今日直接用当日涨跌幅，其余用 K 线窗口
                if period == "今日":
                    c.price_change_pct = c.change_pct
                else:
                    seg = klines[-window:]
                    base = seg[0].close
                    if base and c.last_price:
                        c.price_change_pct = (c.last_price / base - 1.0) * 100.0
                # 弹性大小：近 60 日年化波动率（%），量化股性活跃度
                closes = [k.close for k in klines if k.close]
                if len(closes) >= 10:
                    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
                    mean = sum(rets) / len(rets)
                    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                    c.volatility = math.sqrt(var) * math.sqrt(252) * 100.0
        except Exception as e:
            log.debug(f"K线获取失败 {c.code}: {e}")

        # 1b. 硬过滤：筛选周期内个股走势必须强于所属板块
        if (
            c.price_change_pct is not None
            and c.sector_change_pct is not None
            and c.price_change_pct <= c.sector_change_pct
        ):
            removed.append(
                f"{c.name}({c.price_change_pct:+.1f}%≤板块{c.sector_change_pct:+.1f}%)"
            )
            continue

        # 2-5. 妙想四维查询（业绩/资讯/主营/研报）并发执行（max_workers=2 规避 112 限流）
        if mx:
            from concurrent.futures import ThreadPoolExecutor

            def _q_financial():
                try:
                    c.financial_text = mx.query_as_text(
                        f"{c.code} {c.name} 最新业绩 营业收入 净利润 同比增速 ROE 估值"
                    )[:1500]
                except Exception as e:
                    log.debug(f"业绩查询失败 {c.code}: {e}")

            def _q_news():
                try:
                    c.news_text = mx.fin_search_as_text(
                        f"{c.name} 最新 利好 风险 公告", hours=72
                    )[:1500]
                except Exception as e:
                    log.debug(f"资讯查询失败 {c.code}: {e}")

            def _q_business():
                try:
                    c.business_text = mx.query_as_text(
                        f"{c.code} {c.name} 主营业务 主营构成 产品"
                    )[:800]
                except Exception as e:
                    log.debug(f"主营查询失败 {c.code}: {e}")

            def _q_report():
                try:
                    reports = mx.fin_search_structured(f"{c.name} 研报 评级 目标价")
                    rating_lines = []
                    for it in reports[:6]:
                        if (
                            it.get("information_type") == "REPORT"
                            and it.get("rating")
                            and miaoxiang._belongs_to(it, c.name, c.code)
                        ):
                            ins = it.get("ins_name") or "研报"
                            rating_lines.append(f"[{it['rating']}] {ins}: {it['title'][:40]}")
                    if rating_lines:
                        c.report_text = "\n".join(rating_lines[:3])
                except Exception as e:
                    log.debug(f"研报查询失败 {c.code}: {e}")

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(fn)
                    for fn in (_q_financial, _q_news, _q_business, _q_report)
                ]
                for fut in futures:
                    try:
                        fut.result(timeout=60)
                    except Exception:
                        pass

        log.info("[强势选股] " + _candidate_key_info(c, period_label))
        time.sleep(0.3)
        kept.append(c)

    if removed:
        log.info(f"过滤[走势弱于板块] {len(removed)} 只: {', '.join(removed)}")
    return kept


# ============================================================
# Stage 4：LLM 综合评价
# ============================================================

def _fmt_amount(v: Optional[float]) -> str:
    """金额（元）转亿元字符串"""
    return f"{v / 1e8:.2f}亿" if v is not None else "—"


def _build_llm_prompt(
    candidates: list[StrongCandidate],
    sectors: list[SectorFundFlow],
    final: bool = True,
    period_label: str = "10日",
) -> str:
    lines = [
        f"今天是 {datetime.now().strftime('%Y年%m月%d日')}。以下是从「主力资金流入最多的热门板块」中、"
        "按板块内资金流入前 N% 筛出的强势候选（已过滤黑名单与走势弱于板块者）。请逐只综合评价并给操作建议。",
        "",
    ]
    if sectors:
        lines.append("【热门板块】（按主力净流入降序）")
        for s in sectors:
            gain = f"{s.change_pct:+.1f}%" if s.change_pct is not None else "—"
            lines.append(f"- {s.name}：主力净流入{_fmt_amount(s.main_net)}，{period_label}涨幅{gain}")
        lines.append("")

    lines.append("【候选清单】")
    for c in candidates:
        strength = (
            f"{c.main_net / c.float_mcap * 100:.2f}%"
            if c.main_net and c.float_mcap
            else "—"
        )
        sector_chg = f"{c.sector_change_pct:+.1f}%" if c.sector_change_pct is not None else "—"
        lines.append(f"### {c.name}({c.code})  板块={c.sector}  行业={c.industry or '—'}")
        lines.append(
            f"- 资金面: 个股主力净流入 {_fmt_amount(c.main_net)}，占流通市值 {strength}；"
            f"所属板块主力净流入 {_fmt_amount(c.sector_main_net)}，{period_label}涨幅 {sector_chg}"
        )
        turnover = f"{c.turnover_rate:.2f}%" if c.turnover_rate is not None else "—"
        volr = f"{c.vol_ratio:.2f}" if c.vol_ratio is not None else "—"
        lines.append(
            f"- 量能: 换手率 {turnover}，量比 {volr}，成交额 {_fmt_amount(c.amount)}"
        )
        price = f"{c.price:.2f}元" if c.price else "—"
        chg = f"{c.change_pct:+.1f}%" if c.change_pct is not None else "—"
        last = f"{c.last_price:.2f}元" if c.last_price else "—"
        ma20 = f"{c.ma20:.2f}元" if c.ma20 else "—"
        sup = f"{c.support:.2f}元" if c.support else "—"
        res = f"{c.resistance:.2f}元" if c.resistance else "—"
        pchg = f"{c.price_change_pct:+.1f}%" if c.price_change_pct is not None else "—"
        lines.append(
            f"- 股价: 现价 {price}（当日{chg}），{period_label}涨幅 {pchg}（板块{sector_chg}），"
            f"MA20={ma20}，支撑={sup}，压力={res}"
        )
        pe = f"{c.pe:.1f}" if c.pe else "—"
        pb = f"{c.pb:.2f}" if c.pb else "—"
        tmc = _fmt_amount(c.total_mcap)
        fmc = _fmt_amount(c.float_mcap)
        lines.append(f"- 估值: PE(动)={pe}，PB={pb}，总市值={tmc}，流通市值={fmc}")
        vol = f"{c.volatility:.0f}%" if c.volatility is not None else "—"
        lines.append(f"- 弹性: 年化波动率 {vol}")
        if c.business_text:
            lines.append(f"- 主营/业务: {c.business_text}")
        if c.financial_text:
            lines.append(f"- 业绩: {c.financial_text}")
        if c.report_text:
            lines.append(f"- 机构研报: {c.report_text}")
        if c.news_text:
            lines.append(f"- 近期资讯: {c.news_text}")
        lines.append("")

    lines.append(
        "请对每只候选逐只输出【护城河】【风险点】【上下空间】【弹性】【未来发展】【评级】【操作建议】七段："
        "评级限用「强关注 / 关注 / 观望 / 回避」四级；"
        "操作建议须给可量化的买卖参考价与止损位（价格必须严格引用上面给出的现价/MA20/支撑/压力数值，禁止臆造任何价格）；"
        "护城河与未来发展须依据给出的主营/业务、业绩与研报，弹性须依据年化波动率与换手率，不得凭空编造。"
    )
    if final:
        lines.append("最后用一句话总结整体资金进攻方向与主要风险。")
    return "\n".join(lines)


def _llm_evaluate(
    config: Config,
    candidates: list[StrongCandidate],
    sectors: list[SectorFundFlow],
) -> str:
    """LLM（persona=strong_screener_analyst）对候选做综合评价

    候选分批发给 LLM（每批 chunk_size 只）。当前模型 deepseek-v4-pro 为推理模型，
    会先消耗 token 生成 reasoning_content（思考），再输出 content（结论），
    故 max_tokens 需要给足、且对「空返回」做一次加大 token 的重试，避免
    单批被截断（finish_reason=length）或整批丢失。
    """
    if not candidates:
        return ""
    if not (config.llm_enabled and config.deepseek_key):
        return ""

    try:
        from app.llm_client import SYSTEM_PROMPTS, get_llm_client
        llm = get_llm_client(config)
    except Exception as e:
        log.warning(f"LLM 客户端初始化失败: {e}")
        return ""

    system_prompt = SYSTEM_PROMPTS.get("strong_screener_analyst", "")
    period_label = _period_label(config.strong_flow_period)
    chunk_size = 4
    max_tokens_attempts = (16000, 32000)  # 空返回时第二次加倍 token 重试
    parts: list[str] = []
    for i in range(0, len(candidates), chunk_size):
        chunk = candidates[i : i + chunk_size]
        is_final = i + chunk_size >= len(candidates)
        prompt = _build_llm_prompt(
            chunk, sectors if i == 0 else [], final=is_final, period_label=period_label
        )
        resp = ""
        for attempt, mt in enumerate(max_tokens_attempts, 1):
            try:
                resp = llm.chat(
                    prompt,
                    system_prompt=system_prompt,
                    max_tokens=mt,
                    temperature=0.3,
                    timeout=600,
                ) or ""
            except Exception as e:
                log.warning(f"LLM 综合评价失败 (第{i // chunk_size + 1}批第{attempt}次): {e}")
            if resp.strip():
                break
            log.warning(f"LLM 第{i // chunk_size + 1}批第{attempt}次返回空，重试")
        if resp:
            parts.append(resp)
        log.info(
            f"LLM 综合评价 第{i // chunk_size + 1}批完成: {len(chunk)} 只，输出 {len(resp)} 字"
        )

    return "\n\n".join(parts)


# ============================================================
# Stage 5：报告生成 + 保存 + 推送
# ============================================================

def _build_markdown(report: StrongScreeningReport) -> str:
    lines = [
        "# 🚀 资金强势选股（热门板块 · 资金流入前 N%）",
        "",
        f"**日期**: {report.date}",
        "**策略**: 先选主力资金流入最多的热门板块 → 板块内资金流入前 N% 个股 → "
        "黑名单过滤 → 逐股采集资金面/量能/股价/估值/业绩 → LLM 综合评价",
    ]
    if report.error:
        lines += ["", f"**⚠️ {report.error}**"]
    lines += ["", "---", ""]

    if report.hot_sectors:
        lines.append("## 🔥 热门板块（主力净流入降序）")
        lines.append("")
        lines.append("| 板块 | 主力净流入 | 涨跌幅 |")
        lines.append("|------|-----------|--------|")
        for s in report.hot_sectors:
            gain = f"{s.change_pct:+.1f}%" if s.change_pct is not None else "—"
            lines.append(f"| {s.name} | {_fmt_amount(s.main_net)} | {gain} |")
        lines.append("")

    lines.append("## 📈 候选标的（按综合评分排序）")
    lines.append("")
    if not report.candidates:
        lines.append("> 无候选。今日可能非交易日，或各数据源均无有效数据。")
    else:
        lines.append(
            "| 排名 | 评分 | 代码 | 名称 | 板块 | 行业 | 主力净流入 | 占流通 | 换手 | 量比 | PE | PB | 现价 | MA20 | 支撑 | 压力 |"
        )
        lines.append(
            "|------|------|------|------|------|------|-----------|--------|------|------|----|----|------|------|------|------|"
        )
        for c in report.candidates:
            strength = (
                f"{c.main_net / c.float_mcap * 100:.2f}%"
                if c.main_net and c.float_mcap
                else "—"
            )
            turnover = f"{c.turnover_rate:.2f}%" if c.turnover_rate is not None else "—"
            volr = f"{c.vol_ratio:.2f}" if c.vol_ratio is not None else "—"
            pe = f"{c.pe:.1f}" if c.pe else "—"
            pb = f"{c.pb:.2f}" if c.pb else "—"
            price = f"{c.price:.2f}" if c.price else "—"
            ma20 = f"{c.ma20:.2f}" if c.ma20 else "—"
            sup = f"{c.support:.2f}" if c.support else "—"
            res = f"{c.resistance:.2f}" if c.resistance else "—"
            lines.append(
                f"| {c.rank} | {c.score:.1f} | {c.code} | {c.name} | {c.sector} | {c.industry or '—'} | "
                f"{_fmt_amount(c.main_net)} | {strength} | {turnover} | {volr} | {pe} | {pb} | "
                f"{price} | {ma20} | {sup} | {res} |"
            )
        lines.append("")

    if report.llm_analysis:
        lines += ["---", "", "## 🤖 AI 综合评价", "", report.llm_analysis, ""]

    return "\n".join(lines)


def run_strong_screening(config: Config) -> StrongScreeningReport:
    """资金强势选股主入口（菜单 G 键）

    Returns:
        StrongScreeningReport（含热门板块、候选、LLM 综合评价），报告已落盘并推送
    """
    log.info("========== 资金强势选股（热门板块 · 资金流入前 N%） ==========")
    report = StrongScreeningReport(date=datetime.now().strftime("%Y-%m-%d"))

    # Stage 0：热门板块
    sectors = _find_hot_sectors(config)
    report.hot_sectors = sectors
    log.info(
        "Stage 0 热门板块: "
        + " | ".join(f"{s.name}({_fmt_amount(s.main_net)})" for s in sectors)
    )

    # Stage 1：板块内个股资金流入前 N%
    candidates = _pick_candidates(config, sectors)
    log.info(f"Stage 1 板块内个股入选: {len(candidates)} 只")

    # Stage 2：黑名单过滤
    candidates = _filter_blacklist(config, candidates)
    log.info(f"Stage 2 黑名单过滤后: {len(candidates)} 只")

    # Stage 3：逐股深查
    candidates = _gather_fundamentals(config, candidates)

    # 打分 + 排序 + 排名（按综合评分降序，好的放前面）
    for c in candidates:
        c.score = _score_candidate(c)
    candidates.sort(key=lambda c: c.score, reverse=True)
    for i, c in enumerate(candidates, 1):
        c.rank = i
    report.candidates = candidates

    # Stage 4：LLM 综合评价
    report.llm_analysis = _llm_evaluate(config, candidates, sectors)

    if not sectors and not candidates:
        report.error = "今日可能非交易日，或各数据源均无有效数据。"

    # Stage 5：报告落盘 + 推送
    content = _build_markdown(report)
    try:
        from app import reporter
        save_dir = Path(config.config_path).parent / "strong_screening"
        saved = reporter._save_report(content, "资金强势选股", save_dir)
        log.info(f"报告已保存: {saved}")
        if config.push_enabled and config.sct_sendkey:
            reporter._push_report("资金强势选股（热门板块·资金流入前N%）", content, config)
    except Exception as e:
        log.warning(f"报告保存/推送失败: {e}")

    return report
