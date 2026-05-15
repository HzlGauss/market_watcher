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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    resp = eastmoney_client.get(url, headers=_FUND_HEADERS)
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
        if not isinstance(data, dict):
            return None

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
    """批量获取所有基金净值（并发请求，max_workers=5 避免触发频率限制）"""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_nav, f["code"]): f for f in funds}
        for future in as_completed(futures):
            f = futures[future]
            nav = future.result()
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

def _call_llm(prompt: str, config: Config) -> str | None:
    """调用 DeepSeek 分析"""
    llm = get_llm_client(config)
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
        for name, val in global_data.items():
            parts.append(f"{name}: {val}")
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

    prompt = f"""今天是 {today}。请作为资深基金专家，为投资者生成一份**极具决策价值**的主动管理基金深度分析报告。

### 1. 核心输入数据

**【当前市场环境】**
- A股情绪: {mood_str}
- 行业板块走势:
{sectors_str}
- 全球市场参考: {global_str}

**【待分析基金列表】**
{chr(10).join(fund_table_lines)}

{"\n【权威深度数据（持仓/评级）】\n" + mx_data if mx_data else ""}

---

### 2. 报告输出要求（必须严格遵守以下 Markdown 结构）

#### **🏆 今日决策摘要**
- 给出 1-2 句关于当前市场风格的定性研判。
- 列出今日**最值得关注**的 1-2 只基金及其推荐动作（如：重点加仓、止盈观察）。

#### **📋 基金行情速览表**
请使用 Markdown 表格列出所有 {len(valid_funds)} 只基金的摘要：
| 基金代码 | 基金名称 | 日涨跌 | 风格匹配度 | 建议操作 |
| :--- | :--- | :--- | :--- | :--- |
| (代码) | (名称) | (带符号百分比) | (使用 ⭐ 数量表示) | (统一标签) |

*风格匹配度说明：⭐(极差) 到 ⭐⭐⭐⭐⭐(完美契合)*
*操作建议标签：【强力买入】、【分批加仓】、【继续持有】、【风险观望】、【逢高止盈】*

#### **🔍 深度研判与经理点评**
请将基金进行分类或挑选重点进行点评（不需要逐一罗列，突出重点）：
- **持仓体感分析**：直接点出基金目前的“真实体感”（例如：“这只基金本质上是在赌AI”、“这是一篮子红利资产”）。
- **经理评价**：简述经理在当前环境下的应对能力。
- **风格契合度**：为什么给出的星级评价？（例如：“在半导体大跌时仍重仓，匹配度极低”）。

#### **💡 总结建议**
- 给出总体的仓位控制建议。
- 提醒未来 1-3 个交易日需警惕的风险点。

**要求：**
- 语言专业、辛辣、有洞察力，拒绝模棱两可。
- 总字数控制在 800 字以内。
- 充分利用表格、列表、Emoji 和加粗语法提升可读性。"""

    # 5. 调用 AI
    log.info("  🤖 DeepSeek 分析中（约30秒）...")
    content = _call_llm(prompt, config)

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
