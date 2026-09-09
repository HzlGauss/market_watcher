---
name: macro-data
description: 宏观数据查询（CPI/PPI 居民消费价格指数、工业生产者出厂价格指数）。当用户问「8月CPI/PPI数据」「通胀/通缩」「物价涨跌」「宏观数据」时使用，数据源 akshare（统计局口径）。
---

# 宏观数据查询（CPI / PPI）

用 akshare 的 `macro_china_cpi` / `macro_china_ppi` 接口拉取统计局月度 CPI/PPI 数据，输出最新一期关键数值 + 最近 N 个月趋势表。

> 注意：妙想（Miaoxiang）的 `query` 接口只覆盖「证券」范畴（行情/财务/资金流/筹码/估值/分红/板块），**取不到 CPI/PPI 这类宏观经济指标**。宏观数据必须走本 skill（akshare）或联网搜索。

## 用法

```bash
py .claude/skills/macro-data/query_macro.py [cpi|ppi|all] [月份数]
```

示例：

```bash
py .claude/skills/macro-data/query_macro.py            # CPI + PPI 最近 12 个月
py .claude/skills/macro-data/query_macro.py cpi        # 仅 CPI 最近 12 个月
py .claude/skills/macro-data/query_macro.py ppi 24     # 仅 PPI 最近 24 个月
py .claude/skills/macro-data/query_macro.py all 6      # CPI + PPI 最近 6 个月
```

参数说明：

- **指标**（可选，默认 `all`）：`cpi`=居民消费价格指数 / `ppi`=工业生产者出厂价格指数 / `all`=两者都查。
- **月份数**（可选，默认 12，上限 60）：显示最近 N 个月。

## 脚本输出

- **CPI 表**：月份 + 全国/城市/农村的同比与环比（基期=100 的当月指数值已折算为涨跌幅）。
- **PPI 表**：月份 + 当月同比 + 累计。
- 每张表上方有「最新一期」摘要行（最新月同比 / 环比 / 累计）。
- 数据按月份降序（最新在前），口径为统计局月度发布值。

## 分析框架（AI 在数据之上生成解读）

1. **先报数字**：最新 CPI 同比/环比、核心 CPI（如有）、PPI 同比/环比，并对比上期方向（回升/回落/由降转涨）。
2. **拆驱动**：判断上涨主因是「需求端回暖」还是「供给/输入性因素」（如能源、食品季节性），避免把成本推动误读为内需复苏。
3. **看趋势**：结合最近 N 个月序列判断是「温和回升 / 低位震荡 / 通缩尾部 / 再通胀」。
4. **落到市场**：PPI 回升利好上游周期/化工/资源；核心 CPI 低位说明内需消费仍弱；温和通胀 + 低核心 CPI 给政策（降准降息）留空间，对权益估值中性偏友好。

## 注意事项

- **依赖**：需 `pip install akshare`（项目已作为可选依赖）。
- **口径**：`macro_china_cpi` 的「全国-同比增长」即官方 CPI 同比；「全国-环比增长」即环比。`macro_china_ppi` 的「当月同比增长」即 PPI 同比。与统计局官网数值一致。
- **不臆造分项**：脚本只返回总量（全国/城市/农村），不含食品/能源/核心 CPI 分项。需要分项（猪肉、鲜菜、核心 CPI、能源）时，联网搜统计局官方解读或另接 akshare 分项接口，不要编造。
- **时效**：数据每月约 9~13 日发布上月值，脚本反映的是 akshare 已收录的最新一期。
