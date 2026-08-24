#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""妙想模拟组合落地桥接（P2-4）

把「持仓加减仓量化信号」/手动指令落地到妙想模拟盘（mockTrading），形成
「信号 → 模拟下单 → 复盘」的验证闭环。全部操作复用 app.miaoxiang.MXClient
的 mock_* 方法（多 key 轮询 + 限频重试 + 失效切换）。

注意：需先在妙想页面绑定模拟组合账户，否则所有接口返回 code=404。
"""


def _ok(resp) -> bool:
    """判断妙想响应是否成功（success 或 code=200）"""
    if not isinstance(resp, dict):
        return False
    return bool(resp.get("success")) or str(resp.get("code")) == "200"


def _err_msg(resp) -> str:
    if not isinstance(resp, dict):
        return "响应解析失败"
    code = resp.get("code")
    msg = resp.get("message") or resp.get("msg") or ""
    if str(code) == "404":
        return "未绑定模拟组合账户（请在妙想页面绑定后重试）"
    return f"code={code} {msg}".strip()


def _format_data(data, indent: str = "  ") -> str:
    """通用渲染模拟盘返回的 data（dict/list），未知结构时兜底"""
    if data is None:
        return f"{indent}(无数据)"
    if isinstance(data, list):
        if not data:
            return f"{indent}(空)"
        lines = []
        for item in data[:20]:
            if isinstance(item, dict):
                cells = [f"{k}={v}" for k, v in item.items() if v not in (None, "")]
                lines.append(f"{indent}- " + (" | ".join(cells) if cells else "{}"))
            else:
                lines.append(f"{indent}- {item}")
        return "\n".join(lines)
    if isinstance(data, dict):
        if not data:
            return f"{indent}(空)"
        lines = []
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{indent}- {k}:")
                lines.append(_format_data(v, indent + "  "))
            elif v not in (None, ""):
                lines.append(f"{indent}- {k}: {v}")
        return "\n".join(lines)
    return f"{indent}{data}"


def get_mock_portfolio(config) -> str:
    """查询模拟组合：持仓 + 资金 + 委托，格式化为文本"""
    from app.miaoxiang import get_mx_client
    client = get_mx_client(config)
    parts = []

    positions = client.mock_positions()
    if _ok(positions):
        parts.append(f"**模拟持仓**:\n{_format_data(positions.get('data'))}")
    else:
        parts.append(f"**模拟持仓**: ⚠️ {_err_msg(positions)}")

    balance = client.mock_balance()
    if _ok(balance):
        data = balance.get("data")
        if isinstance(data, dict) and data.get("totalAssets") is not None:
            total = data.get("totalAssets")
            avail = data.get("availBalance")
            parts.append(f"**模拟资金**: 总资产 {total} | 可用 {avail if avail is not None else '--'}")
        else:
            parts.append(f"**模拟资金**:\n{_format_data(data)}")
    else:
        parts.append(f"**模拟资金**: ⚠️ {_err_msg(balance)}")

    orders = client.mock_orders()
    if _ok(orders):
        parts.append(f"**模拟委托**:\n{_format_data(orders.get('data'))}")
    else:
        parts.append(f"**模拟委托**: ⚠️ {_err_msg(orders)}")

    return "\n\n".join(parts)


def place_mock_order(config, side: str, stock_code: str, quantity: int, price=None) -> str:
    """模拟下单（side: 'buy'/'sell'），quantity 需为 100 的整数倍"""
    from app.miaoxiang import get_mx_client
    if side not in ("buy", "sell"):
        return "⚠️ side 必须为 buy 或 sell"
    if quantity % 100 != 0:
        return "⚠️ 数量必须为 100 的整数倍"
    client = get_mx_client(config)
    resp = client.mock_trade(side, stock_code, quantity, price)
    if _ok(resp):
        data = resp.get("data")
        oid = data.get("orderId") if isinstance(data, dict) else None
        side_cn = "买入" if side == "buy" else "卖出"
        price_str = f"@{price}" if price is not None else "(市价)"
        return f"✅ {side_cn} {stock_code} {quantity}股 {price_str} 已提交" + (f" | 委托号 {oid}" if oid else "")
    return f"❌ 下单失败: {_err_msg(resp)}"


def cancel_mock_order(config, order_id=None, stock_code=None) -> str:
    """撤单：order_id 为空时撤销全部未成交委托"""
    from app.miaoxiang import get_mx_client
    client = get_mx_client(config)
    resp = client.mock_cancel(order_id=order_id, stock_code=stock_code)
    if _ok(resp):
        return "✅ 撤单成功" + (f" (委托号 {order_id})" if order_id else " (全部撤单)")
    return f"❌ 撤单失败: {_err_msg(resp)}"


def signals_to_orders(signals, holdings) -> str:
    """把量化信号映射为模拟盘下单建议（不自动执行，仅给出可复制的指令）

    加仓→买入 10% 持仓 / 持有偏多→买入 5% / 减仓→卖出 20% / 清仓预警→卖出全部。
    数量向下取整到 100 股。
    """
    if not signals:
        return ""
    hmap = {h.code: h for h in holdings}
    side_map = {
        "加仓": ("buy", 0.10),
        "持有偏多": ("buy", 0.05),
        "减仓": ("sell", 0.20),
        "清仓预警": ("sell", 1.00),
    }
    lines = ["**信号→模拟盘下单建议**\n"]
    for s in signals:
        h = hmap.get(s.code)
        if not h or s.action not in side_map:
            continue
        side, frac = side_map[s.action]
        qty = int(getattr(h, "amount", 0) * frac)
        qty = max(100, qty // 100 * 100)
        if qty <= 0:
            continue
        side_cn = "买入" if side == "buy" else "卖出"
        lines.append(f"- {s.name}({s.code}) [{s.action}] → 市价{side_cn} {qty}股")
    return "\n".join(lines) if len(lines) > 1 else ""
