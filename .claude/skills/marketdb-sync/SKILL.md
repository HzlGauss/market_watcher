---
name: marketdb-sync
description: 手动增量更新本地 marketdb 行情库（DuckDB 落盘的日K/复权数据）。跑同花顺 marketdb 的 auto-sync，按本地最大日期与远端最新交易日的差距自动判断 skip/incremental/full，把本地日K补齐到最新交易日。当用户说「更新本地行情库」「增量更新 marketdb」「把日K数据同步到最新」时使用。
---

# 本地行情库增量同步（marketdb auto-sync）

调用同花顺 marketdb 的 `auto-sync`，根据本地 `raw_kline_daily` 最大日期与远端最新交易日的差距自动决定：

- `skip`：已最新，K线不动（仍刷新复权事件）
- `incremental`：落后 ≤7 交易日，下 10 日增量包合并
- `full`：落后 >7 交易日，全量覆盖重下

## 用法

```bash
python .claude/skills/marketdb-sync/sync_marketdb.py
```

等价于 `python tools/marketdb_local.py sync`。

## 依赖

- 首次需先 `python tools/marketdb_local.py bootstrap` 建库 + 全量同步（已完成的库跳过即可）
- `.env` 配置 `HITHINK_FINANCE_API_KEY`（下载 Parquet 用）
- 路径通过环境变量动态定位（`MARKETDB_SRC` / `MARKETDB_DB_PATH`），不写死，可跨环境运行

## 输出解读

- 打印 `decision: mode=... lag=... local_max=... target=...`，据此判断本次是跳过/增量/全量
- 结束后打印各表行数与最大日期（raw_kline_daily / dim_symbol 等）
