#!/usr/bin/env python3
"""妙想模拟盘管理（妙想 Miaoxiang mockTrading）

用法:
    py mock_portfolio.py                        # 查模拟盘持仓 + 资金 + 委托
    py mock_portfolio.py 持仓                   # 同上
    py mock_portfolio.py 买入 <代码> <数量> [价格]
    py mock_portfolio.py 卖出 <代码> <数量> [价格]
    py mock_portfolio.py 撤单 [委托号]          # 无委托号=一键撤销全部未成交委托

示例:
    py mock_portfolio.py
    py mock_portfolio.py 买入 300432 1000
    py mock_portfolio.py 买入 300432 1000 18.50
    py mock_portfolio.py 卖出 300432 500
    py mock_portfolio.py 撤单 123456

注意: 需先在妙想页面绑定模拟组合账户，否则接口返回 code=404。
"""
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/mock-portfolio 上三级：mock-portfolio -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env


def main():
    argv = sys.argv[1:]
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 0

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    from app.mock_trader import get_mock_portfolio, place_mock_order, cancel_mock_order

    if not argv or argv[0].strip() in ("持仓", "查", "持仓查询"):
        print(get_mock_portfolio(config))
        return 0

    cmd = argv[0].strip()
    if cmd in ("买入", "买", "buy"):
        if len(argv) < 3:
            print("用法: py mock_portfolio.py 买入 <代码> <数量> [价格]")
            return 2
        code = argv[1].strip()
        try:
            qty = int(argv[2])
        except ValueError:
            print("⚠️ 数量必须是整数（100 的整数倍）")
            return 2
        price = None
        if len(argv) >= 4:
            try:
                price = float(argv[3])
            except ValueError:
                print("⚠️ 价格必须是数字")
                return 2
        print(place_mock_order(config, "buy", code, qty, price))
        return 0

    if cmd in ("卖出", "卖", "sell"):
        if len(argv) < 3:
            print("用法: py mock_portfolio.py 卖出 <代码> <数量> [价格]")
            return 2
        code = argv[1].strip()
        try:
            qty = int(argv[2])
        except ValueError:
            print("⚠️ 数量必须是整数（100 的整数倍）")
            return 2
        price = None
        if len(argv) >= 4:
            try:
                price = float(argv[3])
            except ValueError:
                print("⚠️ 价格必须是数字")
                return 2
        print(place_mock_order(config, "sell", code, qty, price))
        return 0

    if cmd in ("撤单", "撤销", "cancel"):
        order_id = argv[1].strip() if len(argv) >= 2 else None
        print(cancel_mock_order(config, order_id=order_id))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
