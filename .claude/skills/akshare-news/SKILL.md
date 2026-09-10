---
name: akshare-news
description: AKShare 实时财经快讯 + 个股新闻（免费、实时，东财/新浪源，无需 MX_APIKEY）。实时快讯流可查「今晚美股为什么跌」「最近有什么实时财经消息」等全球市场突发/快讯；个股新闻可查某只股票的最新东财新闻。与 news-search（妙想，含研报/公告/评级）互补——妙想覆盖中文结构化资讯，本 skill 覆盖免费实时快讯与个股新闻。
---

# AKShare 实时快讯 + 个股新闻

用 AKShare 免费接口拉全球实时财经快讯和个股新闻，弥补妙想 `fin_search` 对**境外/盘中实时消息**覆盖不足、且需消耗 `MX_APIKEY` 额度的短板。全部数据源免费、不依赖 `MX_APIKEY`，仅需 `pip install akshare`。

## 用法

```bash
# 实时快讯流：拉全球财经快讯，可按关键词过滤
py .claude/skills/akshare-news/flash_news.py [关键词] [条数]

# 个股新闻：查某只股票的东财新闻
py .claude/skills/akshare-news/stock_news.py <代码> [名称]
```

示例：

```bash
py .claude/skills/akshare-news/flash_news.py 美联储 30      # 搜含「美联储」的最新快讯
py .claude/skills/akshare-news/flash_news.py               # 无关键词 = 最新全局快讯
py .claude/skills/akshare-news/stock_news.py 600519 贵州茅台
py .claude/skills/akshare-news/stock_news.py 000001 平安银行
```

参数说明：

- **关键词**（可选）：自然语言词，在标题/摘要/内容里做子串匹配过滤；缺省返回最新全局快讯。
- **条数**（可选，默认 50）：最多显示的快讯条数。
- **代码**（必填，个股新闻）：6 位 A 股代码。
- **名称**（可选，个股新闻）：仅用于展示标签，不影响查询。

## 数据源与字段

| 脚本 | 数据源 | 字段 | 特点 |
|---|---|---|---|
| `flash_news.py` | 东财 `stock_info_global_em` | 标题 / 摘要 / 发布时间 / 链接 | 全球快讯约 200 条，带链接可深挖 |
| `flash_news.py` | 新浪 `stock_info_global_sina` | 时间 / 内容 | 最新 20 条电报式快讯，更实时 |
| `stock_news.py` | 东财 `stock_news_em` | 新闻标题 / 新闻内容 / 发布时间 / 文章来源 / 链接 | 单只股票近 10 条新闻 |

> 注：财联社电报 `stock_info_global_cls` 在部分网络环境下访问 cls.cn 会超时，故默认不启用；如需开启可在 `flash_news.py` 中取消对应注释。

## 脚本输出与解读

`flash_news.py` 输出两段：

1. **【东财全球快讯】**：标题 + 时间 + 摘要（截断）+ 链接，适合看事件背景与深挖。
2. **【新浪快讯】**：时间 + 内容（电报式一句话），适合抓最新变化。

`stock_news.py` 输出单只股票的新闻列表：标题 + `[来源, 时间]` + 摘要 + 链接。

## 分析框架（AI 在数据包之上生成结论）

1. **事件定性**：区分利好/利空/中性，标注事件类型（央行利率、地缘、油价、国债收益率、个股公告/业绩）。
2. **影响方向**：快讯中的宏观/地缘/利率信息 → 对应 A 股/美股的板块与方向。
3. **时效**：按发布时间排序，判断是盘前/盘中/盘后突发，评估持续性。
4. **结论**：一句话总结最新消息面及其对相关标的的潜在影响。

## 注意事项

- **依赖**：`pip install akshare`；无需 `MX_APIKEY`。
- **与 news-search 的分工**：需要**研报评级 / 公告原文 / 机构观点 / 关联证券映射**时用 `news-search`（妙想）；需要**免费实时快讯 / 全球市场盘中消息 / 单股东财新闻**时用本 skill。
- **关键词过滤回退**：若关键词过滤后为空，脚本回退显示全部最新快讯（与 news-search 的窗口回退逻辑一致）。
