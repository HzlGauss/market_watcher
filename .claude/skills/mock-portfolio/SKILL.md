---
name: mock-portfolio
description: 妙想模拟盘管理。查询模拟盘持仓/资金/委托，或模拟买卖下单、撤单（妙想 mockTrading，复用 app.mock_trader）。当用户问「查我的模拟盘/模拟组合持仓」「模拟盘买入/卖出某股票」「模拟盘撤单」时使用。需先在妙想页面绑定模拟组合账户。
---

# 模拟盘管理（妙想）

用妙想 `mockTrading` 接口管理模拟组合：查持仓/资金/委托、模拟买卖下单、撤单。落地「信号 → 模拟下单 → 复盘」验证闭环的最后一环。

## 用法

```bash
py .claude/skills/mock-portfolio/mock_portfolio.py                 # 查持仓+资金+委托
py .claude/skills/mock-portfolio/mock_portfolio.py 持仓             # 同上
py .claude/skills/mock-portfolio/mock_portfolio.py 买入 <代码> <数量> [价格]
py .claude/skills/mock-portfolio/mock_portfolio.py 卖出 <代码> <数量> [价格]
py .claude/skills/mock-portfolio/mock_portfolio.py 撤单 [委托号]    # 无委托号=全部撤单
```

示例：

```bash
py .claude/skills/mock-portfolio/mock_portfolio.py
py .claude/skills/mock-portfolio/mock_portfolio.py 买入 300432 1000
py .claude/skills/mock-portfolio/mock_portfolio.py 买入 300432 1000 18.50
py .claude/skills/mock-portfolio/mock_portfolio.py 卖出 300432 500
py .claude/skills/mock-portfolio/mock_portfolio.py 撤单 123456
```

参数说明：

- **买入/卖出**：`<代码>` 6 位 A 股代码；`<数量>` 股数，须为 100 的整数倍；`[价格]` 可选，限价（不传 = 市价委托）。
- **撤单**：`[委托号]` 可选，不传则一键撤销当日所有未成交委托。

## 脚本输出与解读

- **查持仓**：输出模拟持仓列表 + 资金（总资产/可用资金）+ 当日委托（含已成交/未成交/已撤单）。
- **下单**：成功返回 `✅ 买入/卖出 代码 数量股 [@价格] 已提交 | 委托号 xxx`；失败返回具体原因（404 = 未绑定模拟账户）。
- **撤单**：成功返回 `✅ 撤单成功`。

## 注意事项

- **前置条件**：需先在妙想页面绑定模拟组合账户，否则所有 `mockTrading` 接口返回 `code=404`。
- **依赖**：需在 `.env` 配置 `MX_APIKEY`（可选 `MX_APIKEY_2` 备用）。
- **数量约束**：A 股按 100 股整数倍下单，非整百会被拒绝。
- **市价 vs 限价**：不传价格时 `useMarketPrice=true`，妙想自动以最新价成交；传价格则按限价委托。
- **与信号闭环**：可配合 `app/position_signal.py` 的加减仓量化信号 + `app/mock_trader.py` 的 `signals_to_orders`，把信号映射为模拟下单建议。
