---
name: t0-query
description: 查询当前做T信号快照。读取做T监控线程（T0MonitorThread）每轮扫描后写入的 state/t0_signals.json，输出某标的当前的买/卖挂单价、支撑位、压力位、盈亏比与触发时间，或列出当前所有做T信号。当用户问「XX 现在做T该挂多少钱买/卖」「XX 的支撑压力在哪」「当前有哪些做T信号」「XX 刚触发的做T信号是什么」时使用。
---

# 做T信号查询（t0-query）

盘中随时查询做T监控线程（`T0MonitorThread`）生成的**最新信号快照**，解决滚动打印里找某只标的最新挂单价/支撑压力不方便的问题。

## 用法

```bash
py .claude/skills/t0-query/query_t0.py            # 列出当前所有做T信号
py .claude/skills/t0-query/query_t0.py 600519     # 查单个标的详情
py .claude/skills/t0-query/query_t0.py 茅台       # 支持名称/代码模糊匹配
```

脚本输出该标的的：方向（买/卖）、现价、**建议买入挂单价 / 卖出挂单价**、**支撑位 / 压力位**、**布林带（上轨 / 中轨 / 下轨 + 带宽 + 位置 + 挤压）**、盈亏比 RR、触发条件、信号年龄（秒）。列表视图额外加「上/下轨」（布林带上轨/下轨数值，如 `0.884/0.865`）与「布林」位置标签（上轨/下轨/偏强/偏弱/中）两列。

## 数据来源与时效

- 快照文件 `state/t0_signals.json` 由做T监控线程 `app/t0_monitor.py` 的 `_write_snapshot()` 每轮扫描后**覆盖写**。
- 快照反映的是 `_last_signals`（每个标的**最新**一条信号，去重缓存），信号超过 10 分钟未更新会被 `_prune_stale_signals` 清理。
- 因此 `age_sec` 越大代表信号越旧；超过 600 秒的信号理论上已被清理，查不到属正常（说明近期无新信号）。
- 快照字段：`signal_type`（buy/sell）、`price`、`buy_price`、`sell_price`、`support`、`resistance`、`risk_reward`、`reason`、`bb_upper`/`bb_middle`/`bb_lower`（布林三轨，基于 5 分钟 K 线 period=20）、`bb_width`（带宽%）、`bb_signal`（触及上轨/下轨/偏强/偏弱/中性）、`bb_squeeze`+`bb_squeeze_note`（挤压状态与带宽/分位）、`time`、`age_sec`。

## 注意事项

- **纯读 JSON，零依赖、零网络**：不依赖 `MX_APIKEY` / `HITHINK_FINANCE_API_KEY`，也不打任何行情接口。
- **必须先启动做T监控**：快照由监控线程写入，若 `state/t0_signals.json` 不存在，说明监控未运行或尚未触发信号。
- **非交易时段**：监控线程收盘后停止扫描，快照保留最后一次交易时段状态。
- **只查最新、不查历史**：这是「最新状态快照」，不含历史事件流；要复盘某标的今日触发过几次信号，需另做信号事件落库（当前未做）。
