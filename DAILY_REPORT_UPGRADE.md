# 日报系统升级方案 —— 对标开源生态的差距分析与改造路线

> 生成日期：2026-05-15
> 范围：reporter.py + ai_analyzer.py + fund_analyzer.py

---

## 一、当前日报系统能力画像

### 现有成果

market_watcher 已具备一套**可运行的早/午/晚报 LLM 生成系统**：

| 报告类型 | 触发时机 | 数据输入 | 输出方式 |
|----------|---------|---------|---------|
| Morning Brief | 08:25 | 隔夜全球市场 + 昨日情绪 + 持仓概况 | Markdown 文件 + ServerChan 推送 |
| Midday Review | 11:35 | 上午行情 + 情绪评分 + 持仓盈亏 | Markdown 文件 + ServerChan 推送 |
| Evening Review | 16:00 | 全日数据 + 资金流向 + 板块轮动 + 技术指标 | Markdown 文件 + ServerChan 推送 |
| 盯盘 AI 研判 | 每轮扫描 | 实时行情 + 异动 + 技术指标 | 控制台输出 + ServerChan 推送 |

**代码量**：`reporter.py` ~550 行，`ai_analyzer.py` ~160 行

### 差距分析

对标 StockAgent、OpenClaw、daily_stock_analysis 等成熟方案，当前系统存在 9 个核心差距：

---

## 二、9 大差距与改进方案

### 差距一：❌ 无新闻聚合层

**现状**：
- `reporter.py` 的早报只带隔夜美股 + A50 + 恒生指数
- 没有任何国内财经新闻（财联社、华尔街见闻、金十数据、雪球、东方财富）
- 没有政策新闻（国务院、工信部公告）
- AI 相当于"闭着眼睛写早报"，只知道指数涨跌，不知道背后原因

**对标**：StockAgent 内置新闻采集模块，5 分钟间隔抓取 7 个信息源

**改进方案**——新增 `app/news_collector.py`：

```python
"""
新闻采集模块 —— 多源聚合 + 去重 + 摘要
对标 StockAgent 的新闻聚合设计
"""

import re
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from app.http_client import sina_client
from app.utils import log


@dataclass
class NewsItem:
    """单条新闻"""
    title: str
    source: str          # 财联社 / 华尔街见闻 / 金十数据 / 雪球
    url: str = ""
    summary: str = ""
    time: str = ""
    impact: str = ""     # 利好/利空/中性（AI 标注）
    topics: list[str] = field(default_factory=list)  # 涉及板块/主题


def fetch_cailianshe() -> list[NewsItem]:
    """财联社快讯（5 分钟间隔）"""
    url = "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.5.6"
    ...

def fetch_wallstreetcn() -> list[NewsItem]:
    """华尔街见闻快讯"""
    url = "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=20"
    ...

def fetch_jin10() -> list[NewsItem]:
    """金十数据快讯"""
    url = "https://cdn.jin10.com/data_center/reports/telegraph/list?limit=20"
    ...

def fetch_xueqiu_hot() -> list[NewsItem]:
    """雪球热门讨论"""
    url = "https://xueqiu.com/statuses/hot/listV2.json?count=20"
    ...

def collect_morning_news() -> list[NewsItem]:
    """早盘新闻聚合：合并多源，去重，返回 30 分钟内最新"""
    all_news = []
    for fetcher in [fetch_cailianshe, fetch_wallstreetcn, fetch_jin10, fetch_xueqiu_hot]:
        try:
            items = fetcher()
            all_news.extend(items)
        except Exception as e:
            log.warning(f"新闻源 {fetcher.__name__} 获取失败: {e}")

    # 标题去重
    seen = set()
    unique = []
    for item in all_news:
        h = hashlib.md5(item.title.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(item)

    return unique[:30]  # 最多 30 条
```

**集成到 reporter.py**：

```python
# Morning Brief 改造
def generate_morning_brief(config: Config) -> Path | None:
    ...
    # 新增：新闻聚合
    news_items = collect_morning_news()
    lines.append("\n## 盘前舆情")
    for item in news_items[:10]:
        lines.append(f"- [{item.source}] {item.title}")
    ...
```

---

### 差距二：❌ 单次 LLM 调用 = 无多视角分析

**现状**：
- `ai_analyzer.py` 和 `reporter.py` 都是**一次 prompt → 一次 LLM 调用**生成全部内容
- 没有技术/情绪/消息分角色独立分析
- 没有多方观点碰撞

**对标**：TradingAgents 的多 Agent 辩论模式，StockAgent 的多模型 Prompt 编排

**改进方案**——改造 `reporter.py` 为多阶段流水线：

```python
def generate_morning_brief(config: Config) -> Path | None:
    llm = get_llm_client(config)

    # Phase 1: 数据采集
    global_data = fetch_global_markets()
    news_items = collect_morning_news()
    quotes = fetch_quotes(...)
    tech_summaries = {...}
    sentiment = calc_market_sentiment(quotes)

    # Phase 2: 多角色并行分析
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tech_future = pool.submit(_tech_analyst, quotes, tech_summaries, llm)
        news_future = pool.submit(_news_analyst, news_items, global_data, llm)
        macro_future = pool.submit(_macro_analyst, global_data, sentiment, llm)

    tech_view = tech_future.result()
    news_view = news_future.result()
    macro_view = macro_future.result()

    # Phase 3: 首席策略师合成
    final_report = _chief_strategist(
        tech_view, news_view, macro_view, llm
    )

    # Phase 4: 结构化输出
    _save_report(final_report, "Morning Brief", report_dir)
    _push_report(...)
```

---

### 差距三：❌ 无结构化输出（Pydantic）

**现状**：
- LLM 返回自由文本，无法程序化解析
- 无法做数据校验、无法提取关键字段

**对标**：LangChain + Pydantic 的结构化输出

**改进方案**——用 Pydantic 定义报告结构：

```python
from pydantic import BaseModel, Field

class ReportSection(BaseModel):
    title: str
    content: str
    key_points: list[str] = Field(default_factory=list)

class DailyReport(BaseModel):
    date: str
    report_type: str  # morning / midday / evening
    sections: list[ReportSection]
    sentiment_score: int = Field(ge=0, le=100)
    risk_level: str = Field(pattern="低|中|高")
    action_items: list[str] = Field(default_factory=list)

# LLM 调用时要求返回 JSON 格式
prompt += """
请以 JSON 格式输出，结构如下：
{
  "sections": [
    {"title": "...", "content": "...", "key_points": ["..."]}
  ],
  "sentiment_score": 0-100,
  "risk_level": "低/中/高",
  "action_items": ["..."]
}
"""
```

**好处**：
- 可以在 HTML/PDF/微信推送间自由渲染
- 可以比较"AI 昨天的看多判断"和"今天的实际行情"
- 可以做历史报告结构化查询

---

### 差距四：❌ 无定时调度

**现状**：
- 报告是手动触发（`__main__.py` 菜单选择 1/2/3）
- 无法自动在 08:25、11:35、16:00 准时生成

**对标**：StockAgent 的 APScheduler、OpenClaw 的 Cron 式管理

**改进方案**——依赖最小的轻量调度：

```python
# 方式一：用 schedule 库（零依赖安装，轻量）
import schedule

def setup_scheduled_reports(config, north_fetcher):
    schedule.every().day.at("08:25").do(generate_morning_brief, config)
    schedule.every().day.at("11:35").do(generate_midday_review, config, north_fetcher)
    schedule.every().day.at("16:00").do(generate_evening_review, config, north_fetcher)

    while True:
        schedule.run_pending()
        time.sleep(30)

# 方式二：用 APScheduler（更健壮，支持持久化）
from apscheduler.schedulers.background import BackgroundScheduler

def setup_apscheduler(config, north_fetcher):
    scheduler = BackgroundScheduler()
    scheduler.add_job(generate_morning_brief, 'cron', hour=8, minute=25, args=[config])
    scheduler.add_job(generate_midday_review, 'cron', hour=11, minute=35, args=[config, north_fetcher])
    scheduler.add_job(generate_evening_review, 'cron', hour=16, minute=0, args=[config, north_fetcher])
    scheduler.start()
```

**集成到 `__main__.py`**：

```python
# 新增菜单选项 7: 启动自动报告模式
if choice == "7":
    log.info("启动定时报告模式（早 08:25 / 午 11:35 / 晚 16:00）")
    setup_scheduled_reports(config, north_fetcher)
```

---

### 差距五：❌ 午评缺少盘中关键数据

**现状**：
- `generate_midday_review` 只传了情绪评分和涨跌分布
- 缺少盘中关键数据：
  - ❌ 上午成交额对比（放量/缩量）
  - ❌ 涨停/跌停梯队
  - ❌ 北向资金半日流向
  - ❌ 板块资金流入/流出排名
  - ❌ 上午关键指数分时特征

**改进方案**——增加午评数据维度：

```python
def _midday_market_data(quotes, config) -> dict:
    """提取午评需要的关键市场数据"""
    result = {}

    # 成交额估算（基于监控标的）
    total_amt = sum(q.amount for q in quotes if q.amount)
    result["estimated_volume"] = format_amount(total_amt)

    # 涨跌停（简易版：基于阈值）
   涨停 = [q for q in quotes if q.change_pct and q.change_pct >= 9.8]
   跌停 = [q for q in quotes if q.change_pct and q.change_pct <= -9.8]
    result["limit_up"] = [f"{q.name}({q.code})" for q in 涨停]
    result["limit_down"] = [f"{q.name}({q.code})" for q in 跌停]

    # 板块资金排名（根据板块内个股均值）
    sectors = _calc_sector_performance(quotes)
    result["top_sectors"] = sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:5]
    result["bottom_sectors"] = sorted(sectors.items(), key=lambda x: x[1])[:5]

    return result
```

---

### 差距六：❌ 晚报缺少龙虎榜与资金面深度

**现状**：
- 晚报只有简单的"主力/散户"判断（`_analyze_capital_flow`）
- 缺少真正的龙虎榜数据
- 缺少行业资金流入/流出排名

**对标**：东方财富每日盘后的资金流向数据

**改进方案**——通过东方财富 API 获取盘后资金数据：

```python
def fetch_capital_flow(date: str) -> dict:
    """获取东方财富行业资金流向"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=30&po=1&np=1"
        "&fields=f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124"
        "&fid=f62"
        "&fs=m:90+t:2"
    )
    ...

def fetch_dragon_tiger(date: str) -> list[dict]:
    """获取龙虎榜数据"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?fid=f3&po=1&pz=10&pn=1&np=1"
        "&fs=m:0+t:13+f:!50"
        "&fields=f12,f14,f3,f62,f184,f66"
    )
    ...
```

---

### 差距七：❌ 无报告归档与索引

**现状**：
- `monitoring_briefs/` 和 `investment_reports/` 下文件按时间命名堆积
- 没有索引文件，无法快速查看"上周三的午评"
- 没有对比功能（今天和昨天说的矛盾吗？）

**对标**：StockAgent 的 MongoDB 存储 + 检索

**改进方案**——轻量级归档（不引入数据库）：

```python
# 方式一：JSON 索引文件
def _update_report_index(filepath: Path, report_type: str):
    index_path = filepath.parent / "_index.json"
    index = {}
    if index_path.exists():
        with open(index_path) as f:
            index = json.load(f)

    index[datetime.now().strftime("%Y-%m-%d_%H%M")] = {
        "type": report_type,
        "file": filepath.name,
        "title": filepath.stem,
    }
    with open(index_path, "w") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

# 方式二：Git 天然归档（已有 .git，每次生成就是一次 commit）
import subprocess
def _git_commit_report(filepath: Path, report_type: str):
    subprocess.run(["git", "add", str(filepath)], cwd=filepath.parent.parent)
    subprocess.run(["git", "commit", "-m", f"report: {report_type} {datetime.now():%Y-%m-%d %H:%M}"],
                   cwd=filepath.parent.parent)
```

---

### 差距八：❌ 数据源单一（仅新浪财经）

**现状**：
- 所有行情数据来自新浪财经
- K 线来自新浪
- 全球数据来自新浪
- 单点故障风险

**对标**：StockAgent 多源备份、adata 自动故障切换

**改进方案**——新增 AKShare 备选通道：

```python
def fetch_quotes_with_fallback(items: list[WatchItem]) -> list[Quote]:
    """主源新浪 → 备选 AKShare"""
    quotes = fetch_quotes(items)  # 主源
    if not quotes:
        log.warning("新浪财经无数据，切换 AKShare...")
        quotes = fetch_quotes_akshare(items)  # 备源
    return quotes

def fetch_quotes_akshare(items: list[WatchItem]) -> list[Quote]:
    """AKShare 行情获取"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    results = []
    for item in items:
        row = df[df["代码"] == item.code]
        if row.empty:
            continue
        r = row.iloc[0]
        results.append(Quote(
            code=item.code,
            name=r["名称"],
            ...
        ))
    return results
```

---

### 差距九：❌ AI Prompt 粒度不够精细

**现状**：
- 早/午/晚使用相同的 `SYSTEM_PROMPTS["analyst"]` 或 `"strategist"`
- 三个时段共用一个角色设定，输出风格趋同

**对标**：不同时段应有不同的 prompt 模板结构和角色设定

**改进方案**——为每个时段设计独立角色：

```python
SYSTEM_PROMPTS = {
    # 早盘策略师：偏宏观 + 前瞻
    "morning_strategist": (
        "你是一名拥有20年经验的A股首席策略分析师。你的专长是："
        "通过隔夜市场表现，判断今日开盘情绪；"
        "结合财经日历和舆情，提前识别今日可能发酵的主线。"
        "你的报告风格：简洁、犀利、有明确的操盘指向。"
    ),
    # 午间观察员：偏盘中 + 敏锐
    "midday_observer": (
        "你是一名拥有15年经验的盘中交易员。你的专长是："
        "从上午的量价行为中判断资金意图；"
        "识别上午的强势板块能否延续到下午。"
        "你的报告风格：实时、敏锐、不废话。"
    ),
    # 晚间复盘师：偏总结 + 反思
    "evening_reviewer": (
        "你是一名拥有20年经验的基金经理。你的专长是："
        "复盘当日持仓表现，识别操作中的错误；"
        "通过资金流向和技术面判断明日的胜率分布。"
        "你的报告风格：坦诚、辛辣、有数据支撑。"
    ),
}
```

---

## 三、优先级路线图

| 优先级 | 改进项 | 价值 | 工作量 | 对标来源 |
|--------|--------|------|--------|---------|
| 🥇 P0 | 新闻聚合（新增 `news_collector.py`） | 🔥🔥🔥 早报质量直接翻倍 | 1 天 | StockAgent |
| 🥇 P0 | 定时调度（集成 schedule 或 APScheduler） | 🔥🔥🔥 从手动到自动 | 0.5 天 | OpenClaw / StockAgent |
| 🥇 P0 | 数据源备选（AKShare 回退） | 🔥🔥🔥 解决单点故障 | 0.5 天 | adata |
| 🥈 P1 | 多角色并行分析（拆分 reporter.py） | 🔥🔥 输出质量大幅提升 | 2 天 | TradingAgents |
| 🥈 P1 | 结构化输出（Pydantic） | 🔥🔥 便于渲染和比对 | 1 天 | LangChain |
| 🥈 P1 | 午评数据增强（量能/涨停/资金排名） | 🔥🔥 午评不再单薄 | 0.5 天 | StockAgent |
| 🥈 P1 | 独立 Prompt 模板（三时段角色分化） | 🔥🔥 输出不再千篇一律 | 0.5 天 | StockAgent |
| 🥉 P2 | 龙虎榜 + 行业资金流 | 🔥 晚报深度提升 | 1 天 | 东方财富 |
| 🥉 P2 | 报告归档索引 | 🔥 便于追溯 | 0.5 天 | StockAgent |
| 🥉 P2 | PDF 输出（ReportLab） | 🔥 正式研报格式 | 1 天 | automated-market-report |

### 推荐起步路径（本周可完成）

```
Day 1: 新闻聚合器 news_collector.py（4 个信息源）
        + AKShare 数据备选

Day 2: 定时调度集成（APScheduler）
        + 三个时段独立 Prompt 模板

Day 3: 午评数据增强（量能、涨停梯队、板块排名）
        + 报告归档索引

Day 4: 多角色并行分析改造（技术/情绪/消息分路）
        + Pydantic 结构化输出
```

---

## 四、关键设计原则

1. **增量改造**：不重写 `reporter.py`，而是通过新增模块和函数增强
2. **开关控制**：所有新功能默认关闭，通过 `watchlist_config.json` 启用
3. **异常隔离**：新闻采集失败不影响行情扫描主循环
4. **轻量优先**：优先标准库 + 最少依赖，避免引入重型框架

---

*本文档综合 2026-05-15 的 StockAgent、OpenClaw、daily_stock_analysis 等开源项目分析生成*
