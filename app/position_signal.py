#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓加减仓量化触发引擎（P2-3）

用妙想结构化数据（query_structured / fin_search_structured）对持仓个股做
规则化评分，产出确定性的加仓/持有/减仓/清仓预警信号——不依赖 LLM，与晚报
LLM 定性建议互补（LLM 给出"为什么"，规则引擎给出"触发条件是否成立"）。

五个维度 → 分数 → 动作：
  资金面：近5日主力净流入合计（正加/负减，超 1 亿加码）
  筹码：机构持股比例环比变化
  基本面：最新财报净利润同比增长率
  事件：减持/解禁（-2） vs 增持/回购（+2）
  评级：最新研报评级方向

动作映射：
  >=3 加仓 | 1~2 持有偏多 | -1~0 持有观望 | -2 减仓 | <=-3 清仓预警
"""

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import time

from app.utils import log


@dataclass
class PositionSignal:
    code: str
    name: str
    score: int
    action: str
    reasons: list = field(default_factory=list)
    confidence: str = "低"     # 高/中/低（依据数据维度覆盖度）
    dims_used: int = 0


_ACTION_ORDER = {"清仓预警": 0, "减仓": 1, "持有观望": 2, "持有偏多": 3, "加仓": 4}


def _action_for(score: int) -> str:
    if score >= 3:
        return "加仓"
    if score >= 1:
        return "持有偏多"
    if score >= -1:
        return "持有观望"
    if score >= -2:
        return "减仓"
    return "清仓预警"


def is_stock(code: str) -> bool:
    """A股代码前缀粗判个股（6/0/3 开头为个股；5/1 开头为 ETF/基金）"""
    return (code or "").startswith(("6", "0", "3"))


def _to_number(val) -> float:
    """'2.212亿元'→2.212e8 / '-3684万元'→-3.684e7 / '60.35%'→60.35 / '1.426'→1.426"""
    if val is None:
        return 0.0
    s = str(val).strip().replace(",", "")
    if not s or s in ("-", "--", "None", "nan", "—"):
        return 0.0
    neg = s.startswith("-")
    body = s.lstrip("+-").strip()
    mult = 1.0
    for suffix, m in (("亿元", 1e8), ("万元", 1e4), ("亿", 1e8), ("万", 1e4), ("%", 1.0)):
        if body.endswith(suffix):
            mult = m
            body = body[:-len(suffix)].strip()
            break
    try:
        v = float(body) * mult
    except ValueError:
        return 0.0
    return -v if neg else v


def _pick_col(columns: list, *patterns: str):
    """在列名列表中按子串匹配返回首个命中的列名，未命中返回 None"""
    for c in columns or []:
        for pat in patterns:
            if pat in c:
                return c
    return None


def _gather(client, h) -> dict:
    """采集单只持仓个股的 5 维度结构化数据，返回 dict"""
    name, code = h.name, h.code
    d = {
        "code": code, "name": name,
        "fund": None, "chip": None, "fundamental": None,
        "events": [], "ratings": [],
    }
    try:
        tables = client.query_structured(f"{name} 近5日主力资金净流入")
        d["fund"] = tables[0] if tables else None
    except Exception as e:
        log.debug(f"资金面查询失败 {name}: {e}")
    time.sleep(0.4)
    try:
        tables = client.query_structured(f"{name} 机构持股比例")
        d["chip"] = tables[0] if tables else None
    except Exception as e:
        log.debug(f"筹码查询失败 {name}: {e}")
    time.sleep(0.4)
    try:
        tables = client.query_structured(f"{name} 最新财报 净利润 营业收入 同比增长")
        d["fundamental"] = tables[0] if tables else None
    except Exception as e:
        log.debug(f"基本面查询失败 {name}: {e}")
    time.sleep(0.4)
    try:
        from app.miaoxiang import _belongs_to
        for it in client.fin_search_structured(f"{name} 减持 增持 回购 解禁"):
            if _belongs_to(it, name, code):
                d["events"].append(it)
    except Exception as e:
        log.debug(f"事件检索失败 {name}: {e}")
    time.sleep(0.4)
    try:
        from app.miaoxiang import _belongs_to
        for it in client.fin_search_structured(f"{name} 研报 评级"):
            if it.get("information_type") == "REPORT" and it.get("rating") and _belongs_to(it, name, code):
                d["ratings"].append(it)
    except Exception as e:
        log.debug(f"评级检索失败 {name}: {e}")
    return d


def _score(data: dict) -> PositionSignal:
    """规则评分，返回 PositionSignal"""
    score = 0
    reasons: list[str] = []
    dims = 0

    # 1. 资金面
    if data["fund"]:
        dims += 1
        cols = data["fund"].get("columns") or []
        rows = data["fund"].get("rows") or []
        col = _pick_col(cols, "主力净流入资金")
        if col and rows:
            flows = [_to_number(r.get(col)) for r in rows[:5]]
            total = sum(flows)
            neg_days = sum(1 for f in flows if f < 0)
            if total > 0:
                if total >= 1e8:
                    score += 2
                    reasons.append(f"5日主力净流入{total/1e8:+.2f}亿(强)")
                else:
                    score += 1
                    reasons.append(f"5日主力净流入{total/1e8:+.2f}亿")
            elif total < 0:
                if total <= -1e8:
                    score -= 2
                    reasons.append(f"5日主力净流出{total/1e8:+.2f}亿(强)")
                else:
                    score -= 1
                    reasons.append(f"5日主力净流出{total/1e8:+.2f}亿")
            if neg_days >= 4:
                score -= 1
                reasons.append(f"近5日{neg_days}日净流出")

    # 2. 筹码（机构持股比例环比）
    if data["chip"]:
        dims += 1
        cols = data["chip"].get("columns") or []
        rows = data["chip"].get("rows") or []
        col = _pick_col(cols, "机构持股比例")
        if col and len(rows) >= 2:
            latest = _to_number(rows[0].get(col))
            prev = _to_number(rows[1].get(col))
            if latest and prev:
                delta = latest - prev
                if delta > 0.5:
                    score += 1
                    reasons.append(f"机构持股环比+{delta:.1f}pct")
                elif delta < -0.5:
                    score -= 1
                    reasons.append(f"机构持股环比{delta:.1f}pct")

    # 3. 基本面（最新财报净利润同比）
    if data["fundamental"]:
        dims += 1
        cols = data["fundamental"].get("columns") or []
        rows = data["fundamental"].get("rows") or []
        col = _pick_col(cols, "净利润同比增长率")
        if col and rows:
            g = _to_number(rows[0].get(col))
            if g != 0.0:
                if g > 0:
                    score += 1
                    reasons.append(f"净利润同比+{g:.1f}%")
                else:
                    score -= 1
                    reasons.append(f"净利润同比{g:.1f}%")
                    if g <= -30:
                        score -= 1
                        reasons.append("净利润大幅下滑")

    # 4. 事件（减持/解禁 vs 增持/回购）
    if data["events"]:
        dims += 1
        bear = sum(1 for it in data["events"] if any(k in it.get("title", "") for k in ("减持", "解禁", "业绩预亏")))
        bull = sum(1 for it in data["events"] if any(k in it.get("title", "") for k in ("增持", "回购", "业绩预增")))
        if bear and not bull:
            score -= 2
            reasons.append(f"{bear}条减持/解禁事件")
        elif bull and not bear:
            score += 2
            reasons.append(f"{bull}条增持/回购事件")
        elif bear and bull:
            reasons.append("增减持事件并存(中性)")

    # 5. 评级（最新研报方向）
    if data["ratings"]:
        dims += 1
        r = data["ratings"][0]
        rating = r.get("rating") or ""
        ins = r.get("ins_name") or ""
        if any(k in rating for k in ("买入", "增持", "推荐", "跑赢", "强烈")):
            score += 1
            reasons.append(f"研报评级:{rating}({ins})")
        elif any(k in rating for k in ("减持", "卖出", "回避")):
            score -= 1
            reasons.append(f"研报评级:{rating}({ins})")
        elif any(k in rating for k in ("中性", "持有", "谨慎")):
            reasons.append(f"研报评级:{rating}(中性)")

    confidence = "高" if dims >= 4 else ("中" if dims >= 2 else "低")
    return PositionSignal(
        code=data["code"], name=data["name"], score=score,
        action=_action_for(score), reasons=reasons,
        confidence=confidence, dims_used=dims,
    )


def generate_position_signals(config, holdings, max_holdings: int = 8) -> list[PositionSignal]:
    """对持仓个股批量生成加减仓量化信号（并发采集 + 规则评分）

    返回按动作严重度排序的信号列表（清仓预警最前）。无 key / 无个股持仓返回空列表。
    """
    if not config.mx_apikeys or not holdings:
        return []

    from app.miaoxiang import get_mx_client
    client = get_mx_client(config)
    items = [h for h in holdings if getattr(h, "amount", 0) > 0 and is_stock(h.code)][:max_holdings]
    if not items:
        return []

    results: list[PositionSignal] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_gather, client, h) for h in items]
        for fut in futures:
            try:
                data = fut.result(timeout=90)
                sig = _score(data)
                if sig.dims_used > 0:
                    results.append(sig)
            except Exception as e:
                log.debug(f"信号生成失败: {e}")

    results.sort(key=lambda s: _ACTION_ORDER.get(s.action, 9))
    return results


def format_position_signals(signals: list[PositionSignal]) -> str:
    """把信号列表格式化为 Markdown 文本（供控制台/报告）"""
    if not signals:
        return ""
    emoji = {"加仓": "🟢", "持有偏多": "🔵", "持有观望": "⚪", "减仓": "🟠", "清仓预警": "🔴"}
    lines = ["**持仓加减仓量化信号（规则引擎）**\n"]
    for s in signals:
        e = emoji.get(s.action, "⚪")
        lines.append(f"- {e} **{s.name}({s.code})** 得分 {s.score:+d} → **{s.action}** [置信度{s.confidence}]")
        for r in s.reasons:
            lines.append(f"    - {r}")
    return "\n".join(lines)
