#!/usr/bin/env python3
"""持仓个股消息面扫描（利好/利空）。

对 holdings.csv 里的普通 A 股个股（排除 ETF/基金/B 股）逐个扫描新闻/公告/研报，
用 DeepSeek 判定净方向（利好/利空/中性）+ 强度 + 关键事件 + 一句话判断，
按「利空→利好→中性、强→弱」排序输出。

用法:
    py .claude/skills/holdings-news/scan_holdings_news.py [小时]

参数:
    小时   只保留最近 N 小时内的资讯（可选，默认最近 7 天）

数据源:
    - 主：妙想 fin_search（新闻/公告/研报，含评级/机构，需 .env 配 MX_APIKEY）
    - 兜底：akshare 东财个股新闻 stock_news_em（免费，需 pip install akshare）
    判定：DeepSeek（LLMClient，需 DEEPSEEK_API_KEY）

输出: 按信号强度排序的汇总表 + 每只票一句话判断，供 AI 转述给用户。
"""
import csv
import json
import os
import re
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

# 定位项目根目录（skills/holdings-news 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

import logging

logging.disable(logging.WARNING)

try:
    from app.utils import load_env

    load_env(_ROOT)
except Exception:
    pass

from app.config import Config
from app.helpers import is_a_share_stock
from app.llm_client import get_llm_client
from app.miaoxiang import MXClient

_TYPE_LABEL = {"REPORT": "研报", "ANNOUNCEMENT": "公告", "NEWS": "新闻"}

_SYSTEM_PROMPT = (
    "你是 A 股买方消息面分析师，专注从个股的新闻/公告/研报里判断「净方向」。"
    "给定一只股票及其近期资讯列表（每条含类型/日期/标题/摘要），你只做一件事：判定这些资讯对该股股价的综合影响方向。"
    "规则：\n"
    "1. direction 只能是「利好」「利空」「中性」三者之一；\n"
    "2. strength 只能是「强」「中」「弱」，表示消息的确定性与力度；\n"
    "3. 近期（尤其近 1-2 日）资讯权重高于更早资讯；业绩超预期、增持、回购、中标、政策利好、上调评级偏利好；减持、解禁、立案/处罚、业绩暴雷、下调评级偏利空；\n"
    "4. 资讯稀少或方向互相抵消时判「中性」；\n"
    "5. key_event 用不超过 15 字点出最重要的一件事；reason 用一句话（≤30字）说明判断依据；\n"
    "6. 只输出一个 JSON 对象，必须以 } 结尾，不要输出 JSON 之外的任何文字，格式："
    '{"direction":"利好|利空|中性","strength":"强|中|弱","key_event":"...","reason":"..."}'
)

# 排序权重：利空（风险优先）→ 利好 → 中性，同方向内强→弱
_RANK = {
    ("利空", "强"): 0, ("利空", "中"): 1, ("利空", "弱"): 2,
    ("利好", "强"): 3, ("利好", "中"): 4, ("利好", "弱"): 5,
    ("中性", "强"): 6, ("中性", "中"): 7, ("中性", "弱"): 8,
}


def _parse_args(argv):
    """解析命令行参数，返回 hours（None=默认最近 7 天）。"""
    hours = None
    for a in argv:
        a = a.strip()
        if a.isdigit():
            hours = max(1, int(a))
    return hours


def _read_holdings():
    """读 holdings.csv，过滤出普通 A 股个股（排除 ETF/基金/B股），返回 [{code, name}]。"""
    path = _ROOT / "holdings.csv"
    if not path.exists():
        return []
    out = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code", "")).strip().zfill(6)
                name = str(row.get("name", "")).strip()
                if len(code) != 6 or not name:
                    continue
                if not is_a_share_stock(code, str(row.get("market", "")).strip()):
                    continue
                out.append({"code": code, "name": name})
    except Exception:
        pass
    return out


def _filter_relevant(items, code, name):
    """保留与个股相关（标题/正文/关联证券含公司名或代码）的资讯；过滤后为空则回退全部。"""
    kept = []
    for it in items:
        title = str(it.get("title") or "")
        content = str(it.get("content") or "")
        entity = str(it.get("entity_full_name") or "")
        secu_codes = [str(s.get("code") or "") for s in (it.get("secu_list") or [])]
        hit = (name in title + content + entity) or any(code in c for c in secu_codes)
        if hit:
            kept.append(it)
    return kept or items


def _fetch_akshare_news(code):
    """akshare 东财个股新闻兜底，失败返回空列表。"""
    try:
        os.environ.setdefault("TQDM_DISABLE", "1")
        import akshare as ak

        df = ak.stock_news_em(symbol=code)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    items = []
    for _, r in df.iterrows():
        items.append({
            "title": str(r.get("新闻标题", "") or "").strip(),
            "content": str(r.get("新闻内容", "") or "").strip(),
            "date": str(r.get("发布时间", "") or "").strip(),
            "source": str(r.get("文章来源", "") or "").strip(),
            "information_type": "NEWS",
        })
    return items


def _fetch_news(mx, code, name, hours):
    """妙想为主，akshare 兜底，返回资讯 dict 列表（可能为空）。"""
    if mx is not None and mx.available:
        items = mx.fin_search_structured(name, hours=hours)
        items = _filter_relevant(items, code, name)
        if items:
            return items
    return _fetch_akshare_news(code)


def _format_for_llm(name, items):
    """把单只股票的资讯列表压成 LLM 输入文本。"""
    lines = [f"股票：{name}", "近期资讯："]
    for i, it in enumerate(items[:20], 1):
        typ = _TYPE_LABEL.get(it.get("information_type"), "资讯")
        meta = []
        if it.get("rating"):
            meta.append(it["rating"])
        if it.get("source"):
            meta.append(it["source"])
        if it.get("date"):
            meta.append(str(it["date"])[:10])
        meta_s = f"[{', '.join(meta)}]" if meta else ""
        lines.append(f"{i}. ({typ}) {it.get('title', '')}{meta_s}")
        if it.get("content"):
            lines.append(f"   {str(it['content'])[:100]}")
    return "\n".join(lines)


def _salvage_fields(s):
    """JSON 被截断或格式异常时，用正则抽取各字段（direction/strength/key_event 通常在 reason 之前，通常完整）。"""
    def _grab(key):
        m = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, s)
        return m.group(1) if m else ""

    direction = _grab("direction")
    strength = _grab("strength")
    if direction not in ("利好", "利空", "中性"):
        direction = "中性"
    if strength not in ("强", "中", "弱"):
        strength = "中"
    return {"direction": direction, "strength": strength,
            "key_event": _grab("key_event"), "reason": _grab("reason")}


def _parse_json(raw):
    """从 LLM 返回里解析结构化判定，容忍截断；失败返回 None。"""
    if not raw:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S).strip()
    start = s.find("{")
    if start == -1:
        return _heuristic(s)  # 无 JSON，纯文本关键词兜底
    # 先尝试完整 JSON，再退到最后一个 }（容忍尾部多余文字）
    d = None
    for end in (len(s), s.rfind("}") + 1):
        try:
            d = json.loads(s[start:end])
            break
        except Exception:
            d = None
    if d is None:
        return _salvage_fields(s[start:])  # 截断/异常 → 正则抽字段
    direction = str(d.get("direction", "")).strip()
    strength = str(d.get("strength", "")).strip()
    if direction not in ("利好", "利空", "中性"):
        direction = "中性"
    if strength not in ("强", "中", "弱"):
        strength = "中"
    return {
        "direction": direction,
        "strength": strength,
        "key_event": str(d.get("key_event", "")).strip(),
        "reason": str(d.get("reason", "")).strip(),
    }


def _heuristic(raw):
    """JSON 解析失败时，从原文里抓方向关键词兜底。"""
    direction = "中性"
    if "利空" in raw and "利好" not in raw:
        direction = "利空"
    elif "利好" in raw and "利空" not in raw:
        direction = "利好"
    return {"direction": direction, "strength": "中",
            "key_event": "", "reason": raw.strip()[:60]}


def _classify(llm, name, items, retries=4):
    """调用 DeepSeek 判定单只股票的净方向，带重试退避；彻底失败返回 None。"""
    if llm is None or not llm.enabled:
        return None
    prompt = _format_for_llm(name, items)
    for attempt in range(retries):
        raw = llm.chat(prompt, system_prompt=_SYSTEM_PROMPT, max_tokens=600, temperature=0.2)
        if raw:
            parsed = _parse_json(raw)
            if parsed is not None:
                return parsed
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))  # 退避，规避限频
    return None


def main():
    hours = _parse_args(sys.argv[1:])
    holdings = _read_holdings()

    if not holdings:
        print("⚠️ holdings.csv 为空，或没有普通 A 股个股（均被识别为 ETF/基金）")
        return 1

    config = Config(_ROOT / "watchlist_config.json")
    mx = MXClient(config.mx_apikeys) if config.mx_apikeys else None
    llm = get_llm_client(config)

    window = f"最近{hours}小时" if hours else "最近7天"
    print("=" * 74)
    print(f"持仓个股消息面扫描 · 共 {len(holdings)} 只个股 · {window}")
    print(f"数据源: 妙想(东财) 新闻/公告/研报 + akshare 兜底 · 判定: DeepSeek")
    print("=" * 74)

    results = []
    for h in holdings:
        items = _fetch_news(mx, h["code"], h["name"], hours)
        if not items:
            results.append({
                "code": h["code"], "name": h["name"],
                "direction": "中性", "strength": "弱",
                "key_event": "", "reason": "近窗口内无相关资讯",
                "n_news": 0,
            })
            continue
        verdict = _classify(llm, h["name"], items)
        if verdict is None:
            verdict = {"direction": "中性", "strength": "中",
                       "key_event": "", "reason": "（判定失败，见下方原始资讯）",
                       "_failed": True, "_items": items}
        else:
            verdict["_items"] = items
        verdict.update({"code": h["code"], "name": h["name"], "n_news": len(items)})
        results.append(verdict)
        time.sleep(0.8)  # 妙想 + DeepSeek 限频保护

    results.sort(key=lambda r: _RANK.get((r["direction"], r["strength"]), 9))

    # 汇总表
    print()
    print(f"  {'方向':<5}{'强度':<5}{'名称':<10}{'关键事件'}")
    print("  " + "-" * 66)
    for r in results:
        ev = r["key_event"] or r["reason"]
        print(f"  {r['direction']:<5}{r['strength']:<5}{r['name']:<10}{ev}")

    # 明细：每只一句话判断 + 资讯条数
    print()
    print("  ── 明细 ──")
    for r in results:
        flag = "⚠️" if r["direction"] == "利空" else ("✅" if r["direction"] == "利好" else "·")
        print(f"  {flag} {r['name']}({r['code']})  {r['direction']}·{r['strength']}（{r['n_news']}条）")
        if r["reason"]:
            print(f"      {r['reason']}")

    # 判定失败的票，额外贴出原始资讯供人工判断
    failed = [r for r in results if r.get("_failed")]
    if failed:
        print()
        print(f"  ── 判定失败的原始资讯（{len(failed)} 只，需人工判断）──")
        for r in failed:
            print(f"  【{r['name']}({r['code']})】")
            for it in r.get("_items", [])[:10]:
                typ = _TYPE_LABEL.get(it.get("information_type"), "资讯")
                meta = str(it.get("date") or "")[:10]
                print(f"    ({typ}) {it.get('title','')}  [{meta}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
