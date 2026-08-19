#!/usr/bin/env python3
"""测试东方财富妙想 Skills 三个端点，验证返回结构"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient


def test_query(mx: MXClient):
    print("=" * 60)
    print("测试 1: 金融数据查询 (query)")
    print("=" * 60)
    result = mx.query("招商银行最新价 涨跌幅")
    if result is None:
        print("❌ 请求失败（可能是网络或 key 问题）")
        return
    # 打印顶层结构（不打印完整数据，只看 status）
    print(f"status: {result.get('status')}")
    print(f"message: {result.get('message', '')}")
    data = result.get("data", {})
    inner = data.get("data", {})
    search = inner.get("searchDataResultDTO", {})
    dto_list = search.get("dataTableDTOList", [])
    print(f"dataTableDTOList 数量: {len(dto_list)}")
    if dto_list:
        dto = dto_list[0]
        print(f"  title: {dto.get('title')}")
        print(f"  entityName: {dto.get('entityName')}")
        print(f"  table 键: {list((dto.get('table') or {}).keys())}")
        print(f"  nameMap: {json.dumps(dto.get('nameMap'), ensure_ascii=False)[:200]}")


def test_stock_screen(mx: MXClient):
    print("\n" + "=" * 60)
    print("测试 2: 智能选股 (stock-screen)")
    print("=" * 60)
    result = mx.stock_screen("今日涨幅超过3%的股票", page_size=10)
    if result is None:
        print("❌ 请求失败")
        return
    print(f"status: {result.get('status')}")
    print(f"message: {result.get('message', '')}")
    # 打印完整结构前 500 字符，看字段
    print(f"完整响应(前600字符): {json.dumps(result, ensure_ascii=False)[:600]}")


def test_fin_search(mx: MXClient):
    print("\n" + "=" * 60)
    print("测试 3: 财经资讯搜索 (news-search)")
    print("=" * 60)
    result = mx.fin_search("招商银行")
    if result is None:
        print("❌ 请求失败")
        return
    print(f"status: {result.get('status')}")
    print(f"message: {result.get('message', '')}")
    # 打印完整结构，看字段
    print(f"完整响应(前1500字符): {json.dumps(result, ensure_ascii=False)[:1500]}")


def main():
    base = Path(__file__).resolve().parent.parent
    load_env(base)
    config = Config(base / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY")
        return
    mx = MXClient(api_keys)
    print(f"妙想客户端初始化成功（{len(api_keys)} 个 key）\n")

    test_query(mx)
    test_stock_screen(mx)
    test_fin_search(mx)


if __name__ == "__main__":
    main()
