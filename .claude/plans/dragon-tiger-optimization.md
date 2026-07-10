# 龙虎榜分析代码优化方案

## 问题诊断

根据 2026-07-07 龙虎榜报告发现以下问题：

1. **重复数据**：蔚蓝锂芯（净买入5.10亿+4.12亿）、天娱数科（净卖出-1.98亿×2）各出现2次，惠科股份在游资动向中出现3次。原因：同一股票因多个上榜原因（如"涨幅偏离值"+"换手率异常"）产生多条记录，各条记录的买入/卖出额可能不同（代表不同席位的汇总），需要区分处理。

2. **缺失分析维度**：
   - 涨停板出货（东方盛虹：+10.04%涨停，但净卖出-3.30亿，买卖比仅0.24）
   - 跌停接筹（惠科股份：-8.76%大跌，但净买入4.74亿）
   - 封板质量评估（涨停板背后的资金结构是否健康）
   - 板块内部分化（半导体方向内部：华天科技/中国长城被爆买 vs 康强电子/东方钽业被抛弃）

3. **报告冗余**：重复条目让报告可读性差，资金汇总失真。

---

## 改动计划（仅修改 `app/dragon_tiger.py`）

### 改动 1：新增 `merge_duplicate_records()` 函数

**文件**：`app/dragon_tiger.py`  
**位置**：`fetch_dragon_tiger_list` 之后，`_parse_float` 之后

**逻辑**：
- 按 `code` 分组合并重复记录
- 对同一 code 的多条记录，买入额/卖出额/成交额/净买额**累加**（因为不同上榜原因对应不同席位的独立交易）
- 换手率、涨跌幅取**第一条有效值**（这些是股票自身属性，不应重复）
- 上榜原因用 `；` **拼接**（保留全部信息）
- 返回去重后的记录列表
- 日志输出去重数量（如 "去重: 80条 → 62只个股"）

### 改动 2：新增 `_detect_abnormal_patterns()` 函数

**文件**：`app/dragon_tiger.py`  
**位置**：`_aggregate_sector_flow` 之后

**逻辑**：从去重后的 records 中识别异常形态：

| 形态 | 判断条件 | 标签 |
|---|---|---|
| 涨停板出货 | change_pct >= 9.5% AND buy_sell_ratio < 0.5 | `limit_up_distribution` |
| 跌停接筹 | change_pct <= -7% AND net_buy > BUY_THRESHOLD | `limit_down_accumulation` |
| 封板缩量 | change_pct >= 9.5% AND turnover_rate < 5% | `tight_lockup` |
| 放量烂板 | change_pct >= 9.5% AND turnover_rate > 15% AND buy_sell_ratio < 1.5 | `weak_lockup` |
| 机构对倒 | abs(net_buy) < total_trade * 0.05 AND total_trade > 1e8 | `wash_trade` |

返回 `list[dict]`，每项包含 `code, name, pattern_type, change_pct, net_buy, buy_sell_ratio, turnover_rate, detail`。

### 改动 3：新增 `_detect_sector_divergence()` 函数

**文件**：`app/dragon_tiger.py`  
**位置**：`_detect_abnormal_patterns` 之后

**逻辑**：
- 先按 `_extract_sector_from_reason` 对个股分类
- 在同一个板块分类内，检测是否存在：既有净买入 > BUY_THRESHOLD 的个股，又有净卖出 < SELL_THRESHOLD 的个股
- 返回内部分化的板块列表：`{sector, buy_leaders, sell_laggards, note}`

### 改动 4：扩展 `DragonTigerSummary` 数据类

**文件**：`app/models.py`

新增字段（带默认值，向后兼容）：
```python
abnormal_patterns: list[dict] = field(default_factory=list)
sector_divergence: list[dict] = field(default_factory=list)
```

### 改动 5：修改 `analyze_dragon_tiger()` 

**文件**：`app/dragon_tiger.py`

- 在函数开头调用 `merge_duplicate_records()` 去重
- total_count 改为去重后的数量（同时日志输出原始条数）
- 调用 `_detect_abnormal_patterns(merged_records)` 和 `_detect_sector_divergence(merged_records)`
- 将结果填充到 `DragonTigerSummary.abnormal_patterns` 和 `sector_divergence`
- **整体研判**新增异常形态的关键信息（如 "涨停板出货: 东方盛虹等3只"）

### 改动 6：更新 `format_dragon_tiger_report()`

**文件**：`app/dragon_tiger.py`

在现有报告结构末尾（板块汇总之后）新增两个栏目：

```
### ⚠️ 异常形态警示
| 个股 | 形态 | 涨跌幅 | 净买入 | 详情 |
|------|------|--------|--------|------|
| 东方盛虹(000301) | 🚨涨停板出货 | +10.04% | -3.30亿 | 涨停但龙虎榜净卖出3.30亿，买卖比仅0.24 |

### 🔀 板块内部分化
- **主板/半导体链**: 华天科技(+15.77亿) vs 康强电子(-1.97亿)/东方钽业(-1.71亿) — 资金从材料切向封测
```

### 改动 7：修复下游引用

**文件**：`app/reporter.py`  
确认 `format_dragon_tiger_report(summary)` 调用无需修改（summary 对象无变化）。

**文件**：`app/dragon_seat.py` 的 `analyze_dragon_tiger_seats()`  
**不修改此文件**。席位级别去重由调用方负责：在 `__main__.py` 中调用 `analyze_dragon_tiger_seats` 之前先用去重后的 records。

---

## 不改动的部分

- **`app/dragon_seat.py`**：席位分析本身不需要去重（它按 code 逐一拉取席位明细，重复 code 会被后续记录覆盖）
- **`__main__.py`**：改动 5 中 `analyze_dragon_tiger` 内部已去重，不影响外部调用方式
- **`app/models.py`**：新增字段有默认值，不影响现有序列化/反序列化

## 文件改动清单

| 文件 | 改动内容 |
|---|---|
| `app/models.py` | `DragonTigerSummary` 新增 2 个字段 |
| `app/dragon_tiger.py` | 新增 3 个函数 + 修改 2 个现有函数 |
