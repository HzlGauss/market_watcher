"""
主动基金分析器 —— 评级、持仓风格、经理评价、操作建议
基于实时净值 + DeepSeek AI + 市场风格匹配
支持从CSV文件读取基金列表，方便手动维护
"""

from __future__ import annotations
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.data_fetcher import fetch_quotes
from app.analyzer import calc_market_sentiment
from app.data_fetcher import fetch_global_markets
from app.reporter import _save_report, _push_report
from app.utils import log
from app.http_client import sina_client, eastmoney_client
from app.llm_client import get_llm_client, SYSTEM_PROMPTS

# ============================================================
# 基金净值数据获取（东方财富API）
# ============================================================

FUND_NAV_API = "https://api.fund.eastmoney.com/f10/lsjz"

_FUND_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/",
}


def _fetch_nav(code: str) -> dict | None:
    """获取单只基金近5日净值"""
    url = f"{FUND_NAV_API}?callback=jQuery&fundCode={code}&pageIndex=1&pageSize=5"

    resp = eastmoney_client.get(url)
    if resp is None:
        log.warning(f"基金{code}净值获取失败")
        return None

    try:
        # 去掉 JSONP 回调
        text = resp.text.strip()
        match = re.search(r'\{.*\}', text)
        if not match:
            return None
        data = json.loads(match.group())
        rows = data.get("Data", {}).get("LSJZList", [])
        if not rows:
            return None

        latest = rows[0]
        return {
            "date": latest.get("FSRQ", ""),
            "nav": float(latest.get("DWJZ", 0)),
            "daily_change": latest.get("JZZZL", "0"),
            "nav_1w_ago": float(rows[-1].get("DWJZ", 0)) if len(rows) > 1 else None,
        }
    except Exception as e:
        log.warning(f"基金{code}净值解析失败: {e}")
        return None


def _fetch_all_navs(funds: list[dict]) -> list[dict]:
    """批量获取所有基金净值"""
    results = []
    for f in funds:
        nav = _fetch_nav(f["code"])
        if nav:
            results.append({
                "code": f["code"],
                "name": f["name"],
                "manager": f.get("manager", ""),
                **nav,
            })
        else:
            results.append({
                "code": f["code"],
                "name": f["name"],
                "manager": f.get("manager", ""),
                "nav": None,
            })
    return results


# ============================================================
# mx-data 深度数据（选填，有API Key时启用）
# ============================================================

def _mx_query(query: str, config: Config) -> str | None:
    """调用 mx-data 查询东方财富深度数据"""
    api_key = config.mx_apikey
    if not api_key:
        return None

    import subprocess, tempfile

    mx_script = Path(os.path.expanduser("~/.workbuddy/skills/mx-data/mx_data.py"))
    if not mx_script.exists():
        log.warning("mx-data 脚本未安装 (~/.workbuddy/skills/mx-data/)")
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["MX_APIKEY"] = api_key
            result = subprocess.run(
                ["python", str(mx_script), query, tmp],
                capture_output=True, text=True, timeout=30, env=env,
            )
            output = result.stdout + result.stderr
            return output if "错误" not in output[:50] else None
    except Exception as e:
        log.warning(f"mx-data调用异常: {e}")
        return None


def _fetch_mx_fund_data(funds: list[dict], config: Config) -> str:
    """通过 mx-data 获取基金评级和持仓数据"""
    codes = ",".join(f["code"] for f in funds[:5])  # 每次查5只
    results = []

    # 查评级
    rating = _mx_query(f"{codes} 晨星评级 银河评级", config)
    if rating:
        results.append(f"【权威评级】\n{rating[:800]}")

    # 查持仓
    holdings = _mx_query(f"{codes} 前十大持仓 行业分布", config)
    if holdings:
        results.append(f"【持仓数据】\n{holdings[:800]}")

    return "\n\n".join(results) if results else ""


# ============================================================
# AI 分析（DeepSeek）
# ============================================================

def _call_llm(prompt: str) -> str | None:
    """调用 DeepSeek 分析"""
    llm = get_llm_client()
    if not llm.enabled:
        return None

    return llm.chat(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPTS["fund_expert"],
        max_tokens=1500,
        temperature=0.3,
    )


# ============================================================
# 主分析函数
# ============================================================

def analyze_funds(config: Config) -> Path | None:
    """
    主动基金综合分析 —— 评级 + 持仓风格 + 经理评价 + 操作建议
    """
    # 优先检查CSV文件，然后回退到JSON文件
    funds_csv_path = Path("funds.csv")
    funds_json_path = Path(config.get("盯盘设置", {}).get("基金配置路径", "funds_config.json"))

    fund_list = []

    # 优先从CSV文件读取
    if funds_csv_path.exists():
        try:
            with open(funds_csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                row_num = 1  # 从1开始，因为标题行是第1行
                for row in reader:
                    row_num += 1
                    
                    # 确保code字段不为空
                    code_value = row.get("code", "").strip()
                    if not code_value:
                        log.warning(f"Skipping row {row_num} in funds.csv: code field is empty")
                        continue
                    
                    # 处理空字段，设置为空字符串
                    for key, value in row.items():
                        if value is None:
                            row[key] = ""
                        elif isinstance(value, str) and value.strip() == "":
                            row[key] = ""
                    
                    fund_list.append(row)
            log.info(f"Loaded {len(fund_list)} funds from funds.csv")
        except Exception as e:
            log.warning(f"Failed to load funds.csv: {e}")

    # 如果CSV文件不存在或读取失败，从JSON文件读取（保持向后兼容）
    if not fund_list:
        if not funds_json_path.exists():
            funds_json_path = Path(__file__).resolve().parent.parent / funds_json_path
        if not funds_json_path.exists():
            log.error(f"基金配置文件不存在: {funds_json_path}")
            return None

        with open(str(funds_json_path), "r", encoding="utf-8") as f:
            fund_cfg = json.load(f)
        fund_list = fund_cfg.get("基金列表", [])
        log.info(f"Loaded {len(fund_list)} funds from funds_config.json")

    if not fund_list:
        log.warning("基金列表为空")
        return None

    log.info(f"📊 开始分析 {len(fund_list)} 只主动基金...")

    # 1. 获取实时净值数据
    log.info("  获取基金净值...")
    nav_data = _fetch_all_navs(fund_list)
    valid_funds = [f for f in nav_data if f["nav"] is not None]
    log.info(f"  ✅ 获取到 {len(valid_funds)}/{len(fund_list)} 只基金净值")

    # 2. 获取当前市场行情
    log.info("  获取当前市场风格...")
    watch_items = config.watch_items
    quotes = fetch_quotes(watch_items)
    sentiment = calc_market_sentiment(quotes) if quotes else None

    sectors_str = ""
    if quotes:
        sectors: dict[str, list[float]] = {}
        for q in quotes:
            sty = q.type
            if sty not in sectors:
                sectors[sty] = []
            if q.change_pct is not None:
                sectors[sty].append(q.change_pct)
        sector_lines = []
        for st, pcts in sorted(sectors.items()):
            if pcts:
                avg = sum(pcts) / len(pcts)
                sector_lines.append(f"  {st}: 均值 {avg:+.2f}%")
        sectors_str = "\n".join(sector_lines)

    # 3. 获取隔夜美股（用于判断全球风格）
    global_data = fetch_global_markets()
    global_str = ""
    if global_data:
        parts = []
        for name, d in global_data.items():
            parts.append(f"{name}: {d.get('price','--')} ({d.get('change_pct',0):+.2f}%)")
        global_str = " | ".join(parts)

    # 4. 调用 mx-data 获取深度数据（有API Key时）
    mx_data = ""
    if config.mx_apikey:
        log.info("  📡 查询东方财富深度数据（评级+持仓）...")
        mx_data = _fetch_mx_fund_data(valid_funds, config)
        if mx_data:
            log.info("  ✅ 获取到深度数据")
        else:
            log.info("  ⏭️  mx-data无返回（使用AI知识补充）")

    # 5. 构建 Prompt
    today = datetime.now().strftime("%Y-%m-%d")
    mood_str = f"{sentiment.score}/100 ({sentiment.label})" if sentiment else "--"

    fund_table_lines = []
    for f in valid_funds:
        change = f.get("daily_change", "0")
        fund_table_lines.append(
            f"- {f['name']}({f['code']}) 经理:{f['manager']} "
            f"| 最新净值:{f['nav']:.4f} | 日涨跌:{change}%"
        )

    prompt = f"""今天是 {today}。请对以下 {len(valid_funds)} 只主动管理基金进行专业分析。

## 当前市场环境
- A股情绪: {mood_str}
- 板块风格:
{sectors_str}
- 隔夜全球:
{global_str}

## 基金数据（最新净值+日涨跌）
{chr(10).join(fund_table_lines)}

{"\n## 权威数据（东方财富妙想）\n" + mx_data + "\n" if mx_data else ""}
请从以下5个维度进行分析，总字数不超过600字：

1️⃣ **权威评级**: 基于各权威机构(晨星/银河/招商等)评级，
   用 ★★★★☆ 格式给出每只基金的综合评级（按分数从高到低排列）

2️⃣ **持仓风格匹配**: 分析各基金持仓风格与当前市场风格的匹配程度，
   点出哪些基金"吃"当前行情、哪些"不吃"

3️⃣ **基金经理评价**: 对每位经理的历史业绩、风格稳定性、
   回撤控制能力做简要评价

4️⃣ **综合评分排序**: 按综合得分从高到低列出前5和后3

5️⃣ **操作建议**: 给出持有建议：加仓/持有/减仓/观望，
   并说明理由

要求：客观专业，引用具体数据（近1年收益、最大回撤等），不说模棱两可的话。"""

    # 5. 调用 AI
    log.info("  🤖 DeepSeek 分析中（约30秒）...")
    content = _call_llm(prompt)

    if not content:
        log.error("基金分析生成失败")
        return None

    # 6. 保存报告
    report_dir = Path(config.report_dir)
    filepath = _save_report(f"📊 主动基金深度分析报告\n\n{content}", "📊 基金分析", report_dir)

    # 7. 推送
    _push_report(f"📊 基金分析 {today}", f"## 基金分析报告\n\n{content[:1500]}", config)

    log.info(f"✅ 基金分析报告已生成: {filepath}")
    return filepath
