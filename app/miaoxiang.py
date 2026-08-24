"""
东方财富妙想 (Miaoxiang) Skills 统一客户端

封装妙想 Skills 的多个能力：
- 金融数据查询（自然语言 → 行情/财务/资金流数据）
- 智能选股（自然语言条件 → 股票列表）
- 财经资讯搜索（自然语言 → 新闻/公告/研报）

认证：MX_APIKEY（.env 中配置）
API 域名：https://mkapi2.dfcfs.com
"""

from __future__ import annotations
from typing import Optional, Any
import time
import requests

from app.utils import log


class MXClient:
    """妙想 Skills 客户端（支持多 key 轮询 + 失效自动切换）"""

    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw"

    def __init__(self, api_key=None):
        # 兼容：接受单个字符串或列表
        if api_key is None:
            keys = []
        elif isinstance(api_key, str):
            keys = [api_key] if api_key else []
        else:
            keys = [k for k in api_key if k]
        self._api_keys = keys
        self._key_index = 0
        self._disabled_keys = set()  # 失效/额度用尽的 key
        self.api_key = keys[0] if keys else None  # 兼容旧代码
        self._available = bool(keys)

    @property
    def available(self) -> bool:
        return self._available

    def _active_keys(self) -> list:
        """当前可用的 key 列表（排除已失效的）"""
        return [k for k in self._api_keys if k not in self._disabled_keys]

    def _next_key(self) -> Optional[str]:
        """轮询获取下一个可用的 key"""
        active = self._active_keys()
        if not active:
            return None
        key = active[self._key_index % len(active)]
        self._key_index += 1
        return key

    def _post(self, endpoint: str, payload: dict, timeout: int = 30, retries: int = 3) -> Optional[dict]:
        """统一 POST 请求（多 key 轮询 + 频率限制重试 + 失效切换）"""
        if not self._available:
            return None
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(retries):
            key = self._next_key()
            if key is None:
                return None
            headers = {
                "Content-Type": "application/json",
                "apikey": key,
            }
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                resp.raise_for_status()
                result = resp.json()
                # 业务错误码处理
                code = result.get("status") or result.get("code")
                if code == 112:  # 请求频率过高，等待后重试
                    wait = 2 * (attempt + 1)
                    log.debug(f"妙想频率限制(112)，{wait}秒后重试 ({attempt+1}/{retries})")
                    time.sleep(wait)
                    continue
                if code == 113:  # 该 key 今日额度用尽，禁用并切换
                    log.warning("妙想 key 额度用尽(113)，切换到下一个 key")
                    self._disabled_keys.add(key)
                    continue
                if code in (114, 116):  # key 失效
                    log.warning("妙想 key 失效，切换到下一个")
                    self._disabled_keys.add(key)
                    continue
                return result
            except requests.HTTPError as e:
                if resp is not None and resp.status_code == 401:
                    log.warning("妙想 key 401 失效，切换到下一个")
                    self._disabled_keys.add(key)
                    continue
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                log.warning(f"妙想 API 调用失败 ({endpoint}): {e}")
                return None
        return None

    # ---- 1. 金融数据查询 ----

    def query(self, tool_query: str) -> Optional[dict]:
        """金融数据查询（自然语言）"""
        return self._post("query", {"toolQuery": tool_query})

    def query_as_text(self, tool_query: str) -> str:
        """金融数据查询，返回格式化的文本结果"""
        result = self.query(tool_query)
        if result is None:
            return ""
        return self._format_query_result(result)

    def query_structured(self, tool_query: str) -> list[dict]:
        """金融数据查询，返回结构化表格列表（替代 query_as_text，供机器处理）

        相比 query_as_text 只返回文本，本方法解析 searchDataResultDTO.dataTableDTOList，
        每个数据表归一化为 {title, entity_name, code, columns, rows}，直接供持仓体检、
        财务数据批量取数、历史序列等场景使用（数值保留原始字符串，单位可再用 _parse_amount 解析）。

        返回的每个表格 dict 字段：
            title(表格标题), entity_name(证券全称), code(证券代码),
            columns([日期, ...中文列名]), rows([{列名: 值, ...}，日期为行])

        失败/无数据返回空列表（不抛异常）。
        """
        result = self.query(tool_query)
        if not result or result.get("status") != 0:
            return []
        try:
            data = result.get("data") or {}
            inner = data.get("data") or {}
            search = inner.get("searchDataResultDTO") or {}
            dto_list = search.get("dataTableDTOList") or []
            if not isinstance(dto_list, list):
                return []
            tables = []
            for dto in dto_list:
                if not isinstance(dto, dict):
                    continue
                parsed = self._parse_query_dto(dto)
                if parsed:
                    tables.append(parsed)
            return tables
        except Exception as e:
            log.debug(f"妙想查询结构化解析失败: {e}")
            return []

    # ---- 1.5 个股资金流 ----

    def stock_fund_flow(self, code: str, name: str = "") -> Optional["FundFlowDetail"]:
        """查询个股当日实时资金流向（4 档分类），返回 FundFlowDetail

        通过自然语言 query 接口查询，解析 rawTable 中的主力/超大单/大单/中单/小单净流入。
        字段码前缀映射：ZLJE=主力、CDDJE=超大单、DDJE=大单、ZDJE=中单、XDJE=小单。

        Args:
            code: 6 位 A 股代码
            name: 股票名称（可选，提升查询精度）

        Returns:
            FundFlowDetail 或 None（无 key / 查询失败 / 无数据）
        """
        from app.models import FundFlowDetail

        query_text = f"{code} {name} 今日资金流向 主力 超大单 大单 中单 小单".strip()
        result = self.query(query_text)
        if not result or result.get("status") != 0:
            return None
        try:
            dto_list = (
                (result.get("data") or {}).get("data", {})
                .get("searchDataResultDTO", {})
                .get("dataTableDTOList") or []
            )
            if not dto_list:
                return None
            raw = dto_list[0].get("rawTable") or {}
            if not isinstance(raw, dict) or not raw:
                return None

            head = raw.get("headName") or []
            # 定位目标证券所在列：headName 含代码（如 "沃尔核材(002130.SZ)"）时精确匹配，
            # 否则取最后一列（单证券时 headName 为日期，仅一列）。
            col = 0
            for i, h in enumerate(head):
                if code in str(h):
                    col = i
                    break
            else:
                col = len(head) - 1 if head else 0

            def _val(prefix: str) -> Optional[float]:
                """按字段码前缀取目标列数值（元）"""
                for k, vals in raw.items():
                    if k == "headName" or not str(k).startswith(prefix):
                        continue
                    if isinstance(vals, list) and vals:
                        idx = col if col < len(vals) else len(vals) - 1
                        return self._parse_amount(vals[idx])
                    return self._parse_amount(vals)
                return None

            main_net = _val("ZLJE")          # 主力净流入
            super_large_net = _val("CDDJE")  # 超大单净流入
            large_net = _val("DDJE")         # 大单净流入
            medium_net = _val("ZDJE")        # 中单净流入
            small_net = _val("XDJE")         # 小单净流入

            if main_net is None and super_large_net is None:
                return None
            return FundFlowDetail(
                main_net=main_net,
                super_large_net=super_large_net,
                large_net=large_net,
                medium_net=medium_net,
                small_net=small_net,
            )
        except Exception as e:
            log.debug(f"妙想个股资金流解析失败 {code}: {e}")
            return None

    # ---- 2. 智能选股 ----

    def stock_screen(self, keyword: str, page_no: int = 1, page_size: int = 20) -> Optional[dict]:
        """智能选股（自然语言条件）"""
        payload = {"keyword": keyword, "pageNo": page_no, "pageSize": page_size}
        return self._post("stock-screen", payload)

    def stock_screen_as_text(self, keyword: str) -> str:
        """智能选股，返回格式化的文本结果"""
        result = self.stock_screen(keyword)
        if result is None:
            return ""
        return self._format_stock_screen(result)

    def stock_screen_structured(self, keyword: str, page_size: int = 20) -> list[dict]:
        """智能选股，返回结构化候选列表（解析 allResults.result.dataList）

        相比 stock_screen_as_text 只返回文本表格，本方法解析结构化字段，
        供智能选股管线直接使用（含代码/名称/行业/主力净额等，便于黑名单过滤与资金流入+估值评分）。

        返回的每个 dict 字段（标准化键名）：
            code, name, market, price, change_pct, industry, concept,
            main_net(元), low_position(bool), turnover_rate, vol_ratio,
            flow_days([{date, main_net}...]，按日期升序的多日主力净额，查「连续N日净流入」时返回),
            circulation_value(元), valuation_status(估值较低/适中/较高), valuation_percentile(0-100)

        失败/无结果返回空列表（不抛异常）。
        """
        result = self.stock_screen(keyword, page_size=page_size)
        if not result:
            return []
        try:
            data = result.get("data") or {}
            inner = data.get("data") or {}
            all_results = inner.get("allResults") or {}
            res = all_results.get("result") or {}
            data_list = res.get("dataList") or []
            if not isinstance(data_list, list):
                return []

            parsed = []
            for row in data_list:
                if not isinstance(row, dict):
                    continue
                item = self._parse_screen_row(row)
                if item and item.get("code"):
                    parsed.append(item)
            return parsed
        except Exception as e:
            log.debug(f"妙想选股结构化解析失败: {e}")
            return []

    # ---- 3. 财经资讯搜索 ----

    def fin_search(self, keyword: str) -> Optional[dict]:
        """财经资讯搜索（自然语言）"""
        payload = {"query": keyword}
        return self._post("news-search", payload)

    def fin_search_as_text(self, keyword: str, hours: Optional[int] = None) -> str:
        """财经资讯搜索，返回格式化的文本结果

        Args:
            keyword: 搜索关键词
            hours: 只保留最近 N 小时内的资讯（None=默认最近 7 天）
        """
        result = self.fin_search(keyword)
        if result is None:
            return ""
        return self._format_fin_search(result, hours=hours)

    def fin_search_structured(self, keyword: str, hours: Optional[int] = None) -> list[dict]:
        """财经资讯搜索，返回结构化列表（含评级/机构/关联证券/公告类型等完整字段）

        相比 fin_search_as_text 只返回文本，本方法保留每条资讯的全部字段，
        供「研报评级上调/下调→加减仓」「公告事件驱动（减持/增持/回购/解禁）→建仓/清仓」
        「题材新闻→个股映射（secu_list）」等场景直接使用。

        返回的每个 dict 字段：
            title, source, date, url, content,
            information_type(REPORT=研报/NEWS=新闻/ANNOUNCEMENT=公告),
            rating(研报评级), ins_name(机构), entity_full_name(关联证券全称),
            secu_list([{code,name,type}]), trunk(结构化正文块)

        Args:
            keyword: 搜索关键词
            hours: 只保留最近 N 小时内的资讯（None=默认最近 7 天）

        失败/无结果返回空列表（不抛异常）。
        """
        result = self.fin_search(keyword)
        if not result or result.get("status") != 0:
            return []
        try:
            data = result.get("data") or {}
            inner = data.get("data") or {}
            search_resp = inner.get("llmSearchResponse") or {}
            items = search_resp.get("data") or []
            if not isinstance(items, list) or not items:
                return []
            items = self._filter_recent_news(items, hours=hours)
            parsed = []
            for item in items:
                if isinstance(item, dict):
                    parsed.append(self._parse_fin_item(item))
            return parsed
        except Exception as e:
            log.debug(f"妙想资讯结构化解析失败: {e}")
            return []

    # ---- 4. 自选股管理 ----

    def self_select_get(self) -> Optional[dict]:
        """查询自选股列表"""
        return self._post("self-select/get", {})

    def self_select_get_as_text(self) -> str:
        """查询自选股，返回格式化的文本结果"""
        result = self.self_select_get()
        if result is None:
            return ""
        return self._format_self_select(result)

    def self_select_manage(self, instruction: str) -> Optional[dict]:
        """管理自选股（添加/删除，自然语言指令）"""
        return self._post("self-select/manage", {"query": instruction})

    def self_select_manage_as_text(self, instruction: str) -> str:
        """管理自选股，返回格式化的文本结果"""
        result = self.self_select_manage(instruction)
        if result is None:
            return ""
        return self._format_self_select(result)

    # ---- 5. 模拟组合（mockTrading） ----

    def mock_positions(self) -> Optional[dict]:
        """查询模拟组合当前持仓

        返回 data 结构（成功时 code=200）：持仓列表，含股票代码/名称/持仓数量/成本/现价/盈亏等。
        未绑定模拟组合账户时返回 code=404。
        """
        return self._post("mockTrading/positions", {"moneyUnit": 1})

    def mock_balance(self) -> Optional[dict]:
        """查询模拟组合资金

        返回 data 结构：totalAssets(总资产)/availBalance(可用资金) 等。
        """
        return self._post("mockTrading/balance", {"moneyUnit": 1})

    def mock_orders(self) -> Optional[dict]:
        """查询模拟组合委托订单（含已成交/未成交/已撤单）"""
        return self._post("mockTrading/orders", {"fltOrderDrt": 0, "fltOrderStatus": 0})

    def mock_trade(self, side: str, stock_code: str, quantity: int, price: Optional[float] = None) -> Optional[dict]:
        """模拟买卖下单

        Args:
            side: "buy" 或 "sell"
            stock_code: 6 位 A 股代码
            quantity: 数量（股），须为 100 的整数倍
            price: 限价（None=市价委托，自动以最新价成交）

        Returns:
            data 结构：成功含 orderId(委托编号)。
        """
        payload = {
            "type": side,
            "stockCode": stock_code,
            "quantity": quantity,
            "useMarketPrice": price is None,
        }
        if price is not None:
            payload["price"] = price
        return self._post("mockTrading/trade", payload)

    def mock_cancel(self, order_id: Optional[str] = None, stock_code: Optional[str] = None) -> Optional[dict]:
        """撤销模拟组合委托

        Args:
            order_id: 委托编号（None=一键撤单，撤销当日所有未成交委托）
            stock_code: 可选，按股票代码过滤
        """
        if order_id is None:
            payload = {"type": "all"}
        else:
            payload = {"type": "order", "orderId": order_id}
            if stock_code:
                payload["stockCode"] = stock_code
        return self._post("mockTrading/cancel", payload)

    # ---- 结果格式化 ----

    @staticmethod
    def _format_query_result(result: dict) -> str:
        """格式化金融数据查询结果"""
        lines = []
        status = result.get("status")
        message = result.get("message", "")
        if status != 0:
            lines.append(f"❌ 错误: 状态码 {status} - {message}")
            # 113 = 调用次数超限
            if status == 113:
                lines.append("  (今日妙想调用次数已达上限)")
            return "\n".join(lines)

        data = result.get("data") or {}
        inner = data.get("data") or {}
        search = inner.get("searchDataResultDTO") or {}
        dto_list = search.get("dataTableDTOList") or []

        if not dto_list:
            lines.append("⚠️ 未查询到数据")
            return "\n".join(lines)

        # 证券主体信息
        entity_tags = search.get("entityTagDTOList", [])
        if entity_tags:
            lines.append("**查询证券:**")
            for tag in entity_tags:
                name = tag.get("fullName", "")
                code = tag.get("secuCode", "")
                type_name = tag.get("entityTypeName", "")
                lines.append(f"  - {name} ({code}) - {type_name}")
            lines.append("")

        # 数据表
        for dto in dto_list:
            title = dto.get("title") or dto.get("entityName") or "数据"
            lines.append(f"**{title}**")
            table = dto.get("table") or {}
            name_map = dto.get("nameMap") or {}
            head_name = table.get("headName") or []

            # 解析列名和值
            if isinstance(name_map, list):
                name_map = {str(i): v for i, v in enumerate(name_map)}
            elif not isinstance(name_map, dict):
                name_map = {}

            if not head_name:
                # 单值（当前报价等）
                for key, values in table.items():
                    if key == "headName":
                        continue
                    label = name_map.get(key, name_map.get(str(key), key))
                    val = values[0] if isinstance(values, list) and values else values
                    lines.append(f"  - {label}: {val}")
            else:
                # 表格（日期为行）
                lines.append("  | " + " | ".join(["日期"] + [str(name_map.get(k, k)) for k in table.keys() if k != "headName"]) + " |")
                for i, date in enumerate(head_name[:10]):
                    cells = [str(date)]
                    for key in table.keys():
                        if key == "headName":
                            continue
                        values = table.get(key, [])
                        cells.append(str(values[i]) if i < len(values) else "")
                    lines.append("  | " + " | ".join(cells) + " |")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _flatten_query_value(value) -> str:
        """查询结果单元格值 → 字符串（dict/list 序列化为 JSON）"""
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            import json as _json
            return _json.dumps(value, ensure_ascii=False)
        return str(value)

    @classmethod
    def _parse_query_dto(cls, dto: dict) -> Optional[dict]:
        """解析 query 返回的单个 dataTableDTO → 标准化表格 dict（无有效 table 返回 None）"""
        table = dto.get("table") or {}
        if not isinstance(table, dict):
            return None

        name_map = dto.get("nameMap") or {}
        if isinstance(name_map, list):
            name_map = {str(i): v for i, v in enumerate(name_map)}
        elif not isinstance(name_map, dict):
            name_map = {}

        def _label(key) -> str:
            v = name_map.get(key)
            if v is None:
                v = name_map.get(str(key))
            if v is None and isinstance(key, str) and key.isdigit():
                v = name_map.get(int(key))
            return str(v) if v not in (None, "") else str(key)

        # 证券代码：优先 dto.code（含市场后缀），回退 entityTagDTO.secuCode
        code = str(dto.get("code") or "").strip()
        if not code:
            tag = dto.get("entityTagDTO") or {}
            code = str(tag.get("secuCode") or "").strip()

        head = table.get("headName") or []
        if not isinstance(head, list):
            head = []

        # 指标列（排除 headName），按 indicatorOrder 排序
        data_keys = [k for k in table.keys() if k != "headName"]
        order = dto.get("indicatorOrder") or []
        if isinstance(order, list) and order:
            key_map = {str(k): k for k in data_keys}
            ordered, seen = [], set()
            for k in order:
                ks = str(k)
                if ks in key_map and ks not in seen:
                    ordered.append(key_map[ks])
                    seen.add(ks)
            for k in data_keys:
                if k not in seen:
                    ordered.append(k)
                    seen.add(k)
            data_keys = ordered

        columns = ["日期"] + [_label(k) for k in data_keys]
        rows = []
        if head:
            # 日期为行：headName 为日期列，每个指标是等长数组
            for i, date in enumerate(head):
                row = {"日期": str(date)}
                for k in data_keys:
                    vals = table.get(k, [])
                    v = vals[i] if isinstance(vals, list) and i < len(vals) else ""
                    row[_label(k)] = cls._flatten_query_value(v)
                rows.append(row)
        else:
            # 单值（当前报价等）：每个指标只有一个值
            row = {"日期": ""}
            for k in data_keys:
                vals = table.get(k, [])
                v = vals[0] if isinstance(vals, list) and vals else vals
                row[_label(k)] = cls._flatten_query_value(v)
            rows.append(row)

        return {
            "title": str(dto.get("title") or dto.get("entityName") or "").strip(),
            "entity_name": str(dto.get("entityName") or "").strip(),
            "code": code,
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def _format_stock_screen(result: dict) -> str:
        """格式化智能选股结果"""
        lines = []
        status = result.get("status")
        message = result.get("message", "")
        if status != 0:
            lines.append(f"❌ 错误: 状态码 {status} - {message}")
            if status == 113:
                lines.append("  (今日妙想调用次数已达上限)")
            return "\n".join(lines)

        data = result.get("data") or {}
        inner = data.get("data") or {}

        # 条件汇总
        total_condition = inner.get("totalCondition", "")
        security_count = inner.get("securityCount", 0)
        if total_condition:
            lines.append(f"**筛选条件**: {total_condition}")
        if security_count:
            lines.append(f"**符合条件的股票数**: {security_count} 只")

        # partialResults 是 | 分隔的表格文本（markdown 风格）
        partial = inner.get("partialResults", "")
        if partial:
            lines.append("")
            lines.append(partial)  # 直接展示表格
        else:
            lines.append("⚠️ 无详细结果数据")

        return "\n".join(lines)

    @staticmethod
    def _find_key(row: dict, *prefixes: str) -> str:
        """按前缀匹配找 row 的 key（处理带日期后缀的动态字段）"""
        for key in row:
            for p in prefixes:
                if key.startswith(p):
                    return key
        return ""

    @staticmethod
    def _parse_amount(value) -> Optional[float]:
        """解析妙想返回的数值（可能带单位/百分号/千分位）

        "198.88万" → 1988800.0，"-3.20亿" → -320000000.0，"+5.09%" → 5.09，
        "1,234.5" → 1234.5，None/"--"/"-" → None
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace(",", "").replace("%", "").replace("+", "")
        if not s or s in ("-", "--", "None", "null", "nan"):
            return None
        try:
            if s.endswith("亿"):
                return float(s[:-1]) * 1e8
            if s.endswith("万"):
                return float(s[:-1]) * 1e4
            return float(s)
        except ValueError:
            return None

    @classmethod
    def _parse_screen_row(cls, row: dict) -> Optional[dict]:
        """解析 stock_screen dataList 单行 → 标准化 dict（无 code 返回 None）"""
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not code:
            return None

        # 东财行业总分类（三级行业，黑名单硬过滤用）
        industry_key = cls._find_key(row, "010000_RPT_F10_ORG_BASICINFO_BOARD_NAME")
        industry = str(row.get(industry_key) or "").strip() if industry_key else ""

        # 主力净额多日序列（带日期后缀，如 010000_FLOWZLAMOUNT<70>{2026-08-20}）。
        # 妙想按查询条件动态返回：查「连续N日净流入」会返回最近 N 个交易日的主力净额
        # （实测最多 5 个交易日），正好替代东财 daykline 的多日资金流取数。
        import re as _re
        flow_days: list[dict] = []
        for key, val in row.items():
            if not key.startswith("010000_FLOWZLAMOUNT"):
                continue
            m = _re.search(r"\{(\d{4}-\d{2}-\d{2})\}", key)
            amt = cls._parse_amount(val)
            if amt is None:
                continue
            flow_days.append({"date": m.group(1) if m else "", "main_net": amt})
        flow_days.sort(key=lambda d: d["date"])  # 按日期升序
        main_net = flow_days[-1]["main_net"] if flow_days else None  # 最新交易日主力净额

        # 流通市值（主力净流入强度归一化用）
        circ_key = cls._find_key(row, "010000_CIRCULATION_MARKET_VALUE")
        circulation_value = cls._parse_amount(row.get(circ_key)) if circ_key else None

        # 估值状态（分类：估值较低/适中/较高，组合查询时返回）
        val_status_key = cls._find_key(row, "010000_RPT_VALUATIONSTATUS_VALATION_STATUS")
        valuation_status = str(row.get(val_status_key) or "").strip() if val_status_key else ""

        # PE-TTM 历史百分位（数值 0-100，越小越便宜，纯估值查询时返回）
        pct_key = cls._find_key(row, "010000_RPT_IA_VALUEINDICATOR_HIST_PE_TTM_PERCENTILE")
        valuation_percentile = cls._parse_amount(row.get(pct_key)) if pct_key else None

        # 低位标记（带日期后缀，如 010000_DW<70>{2026-08-20} = "符合"）
        low_key = cls._find_key(row, "010000_DW")
        low_position = str(row.get(low_key) or "").strip() == "符合" if low_key else False

        return {
            "code": code,
            "name": str(row.get("SECURITY_SHORT_NAME") or "").strip(),
            "market": str(row.get("MARKET_SHORT_NAME") or "").strip().upper(),
            "price": cls._parse_amount(row.get("NEWEST_PRICE")),
            "change_pct": cls._parse_amount(row.get("CHG")),
            "industry": industry,
            "concept": str(row.get("STYLE_CONCEPT") or "").strip(),
            "main_net": main_net,
            "flow_days": flow_days,
            "circulation_value": circulation_value,
            "valuation_status": valuation_status,
            "valuation_percentile": valuation_percentile,
            "low_position": low_position,
            "turnover_rate": cls._parse_amount(row.get("010000_TURNOVER_RATE")),
            "vol_ratio": cls._parse_amount(row.get("010000_LIANGBI")),
        }

    @staticmethod
    def _filter_recent_news(items: list, hours: Optional[int] = None) -> list:
        """资讯列表：时间窗口过滤 + 标题去重 + 日期降序（格式化与结构化共用）"""
        from datetime import datetime as _dt, timedelta
        import re as _re

        def _parse_date(date_str):
            m = _re.search(r"(\d{4}-\d{2}-\d{2})", str(date_str or ""))
            if m:
                try:
                    return _dt.strptime(m.group(1), "%Y-%m-%d")
                except ValueError:
                    pass
            return None

        cutoff = _dt.now() - (timedelta(hours=hours) if hours else timedelta(days=7))
        recent_items = []
        for item in items:
            d = _parse_date(item.get("date", "") if isinstance(item, dict) else "")
            if d is None or d >= cutoff:
                recent_items.append(item)
        # 无最近资讯时回退到全部（避免空结果）
        if recent_items:
            items = recent_items
        # 按日期降序排列
        items.sort(key=lambda x: _parse_date(x.get("date", "") if isinstance(x, dict) else "") or _dt.now(), reverse=True)

        # 标题去重：同一事件多来源转载只保留一条
        seen_titles = set()
        deduped = []
        for item in items:
            if not isinstance(item, dict):
                deduped.append(item)
                continue
            title = item.get("title") or "?"
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            norm = _re.sub(r"[\s\W]", "", title)[:40]
            if norm and norm in seen_titles:
                continue
            if norm:
                seen_titles.add(norm)
            deduped.append(item)
        return deduped

    @staticmethod
    def _parse_fin_item(item: dict) -> dict:
        """归一化单条资讯 → dict（含评级/机构/关联证券/公告类型等完整字段）"""
        title = item.get("title") or "?"
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]

        secu_list = item.get("secuList") or []
        if isinstance(secu_list, list):
            secu_list = [
                {
                    "code": str(s.get("secuCode") or "").strip(),
                    "name": str(s.get("secuName") or "").strip(),
                    "type": str(s.get("secuType") or "").strip(),
                }
                for s in secu_list
                if isinstance(s, dict)
            ]
        else:
            secu_list = []

        return {
            "title": title,
            "source": str(item.get("source") or "").strip(),
            "date": str(item.get("date") or "").strip(),
            "url": str(item.get("jumpUrl") or "").strip(),
            "content": str(item.get("content") or "").strip(),
            "information_type": str(item.get("informationType") or "").strip(),
            "rating": str(item.get("rating") or "").strip(),
            "ins_name": str(item.get("insName") or "").strip(),
            "entity_full_name": str(item.get("entityFullName") or "").strip(),
            "secu_list": secu_list,
            "trunk": item.get("trunk"),
        }

    @staticmethod
    def _format_fin_search(result: dict, hours: Optional[int] = None) -> str:
        """格式化财经资讯搜索结果"""
        lines = []
        status = result.get("status")
        message = result.get("message", "")
        if status != 0:
            lines.append(f"❌ 错误: 状态码 {status} - {message}")
            return "\n".join(lines)

        data = result.get("data") or {}
        inner = data.get("data") or {}
        search_resp = inner.get("llmSearchResponse") or {}
        items = search_resp.get("data") or []
        if not isinstance(items, list):
            items = []

        if not items:
            lines.append("⚠️ 未搜索到相关资讯")
            return "\n".join(lines)

        items = MXClient._filter_recent_news(items, hours=hours)

        window = f"最近{hours}小时" if hours else "最近7天"
        lines.append(f"**资讯结果: {len(items)} 条（{window}，已去重）**\n")
        for item in items[:15]:
            if not isinstance(item, dict):
                lines.append(f"  - {item}")
                continue
            info = MXClient._parse_fin_item(item)
            line = f"  - {info['title']}"
            meta = []
            if info["source"] or info["ins_name"]:
                meta.append(info["source"] or info["ins_name"])
            if info["date"]:
                meta.append(info["date"][:10])
            if info["information_type"]:
                meta.append(info["information_type"])
            if meta:
                line += f"  [{', '.join(meta)}]"
            lines.append(line)
            if info["url"]:
                lines.append(f"    {info['url']}")
            if info["content"]:
                # 内容截取前 100 字
                lines.append(f"    {info['content'][:100]}")
        return "\n".join(lines)

    @staticmethod
    def _format_self_select(result: dict) -> str:
        """格式化自选股管理结果"""
        lines = []
        status = result.get("status") or result.get("code")
        message = result.get("message", "")
        if status not in (0, None) and status != "0":
            lines.append(f"❌ 错误: 状态码 {status} - {message}")
            if status == 113:
                lines.append("  (今日妙想调用次数已达上限)")
            if status == 404:
                lines.append("  (未绑定模拟组合，请先在妙想页面创建)")
            return "\n".join(lines)

        # 操作结果描述
        data = result.get("data") or {}
        inner = data.get("data") or {}
        # 尝试提取描述文本
        desc = inner.get("message") or inner.get("result") or inner.get("description") or ""
        if desc:
            lines.append(f"✅ {desc}")
            return "\n".join(lines)

        # 自选股列表
        stocks = inner.get("list") or inner.get("stocks") or inner.get("selfSelectList") or []
        if not isinstance(stocks, list):
            stocks = []

        if stocks:
            lines.append(f"**自选股列表: {len(stocks)} 只**\n")
            lines.append("| 代码 | 名称 | 最新价 | 涨跌幅 |")
            lines.append("|------|------|--------|--------|")
            for s in stocks[:30]:
                if isinstance(s, dict):
                    code = s.get("code") or s.get("secuCode") or s.get("股票代码") or "--"
                    name = s.get("name") or s.get("secuName") or s.get("股票名称") or "--"
                    price = s.get("price") or s.get("最新价") or s.get("newPrice")
                    chg = s.get("changePct") or s.get("涨跌幅") or s.get("change_pct")
                    price_str = f"{price}" if price is not None else "--"
                    chg_str = f"{chg:+.2f}%" if chg is not None else "--"
                    lines.append(f"| {code} | {name} | {price_str} | {chg_str} |")
                else:
                    lines.append(f"| {s} | | | |")
            return "\n".join(lines)

        # 兜底：打印原始响应
        raw = result.get("data") or result
        lines.append("✅ 操作完成")
        lines.append(str(raw)[:500])
        return "\n".join(lines)


def get_mx_client(config=None) -> MXClient:
    """获取妙想客户端单例（支持多 key 轮询）"""
    global _mx_client
    api_keys = []
    if config is not None:
        api_keys = config.mx_apikeys  # [主key, 备用key]
    if _mx_client is None or (api_keys and _mx_client.api_key != api_keys[0]):
        _mx_client = MXClient(api_keys)
    return _mx_client


def fetch_news_for_report(config, query: str) -> str:
    """报告用：获取妙想资讯（静默降级，失败返回空串）

    Args:
        config: 配置对象
        query: 资讯搜索关键词（自然语言）

    Returns:
        格式化的资讯文本，失败或无 key 返回空串
    """
    if not config.mx_apikeys:
        return ""
    try:
        client = get_mx_client(config)
        text = client.fin_search_as_text(query)
        return text or ""
    except Exception as e:
        log.debug(f"妙想资讯获取失败: {e}")
        return ""


def fetch_data_for_report(config, query: str) -> str:
    """报告用：获取妙想金融数据（静默降级，失败返回空串）"""
    if not config.mx_apikeys:
        return ""
    try:
        client = get_mx_client(config)
        text = client.query_as_text(query)
        return text or ""
    except Exception as e:
        log.debug(f"妙想数据获取失败: {e}")
        return ""


def fetch_stock_screen_for_report(config, keyword: str) -> str:
    """报告用：获取妙想选股结果（静默降级，失败返回空串）"""
    if not config.mx_apikeys:
        return ""
    try:
        client = get_mx_client(config)
        text = client.stock_screen_as_text(keyword)
        return text or ""
    except Exception as e:
        log.debug(f"妙想选股失败: {e}")
        return ""


def fetch_alert_news_batch(config, movers: list, max_alerts: int = 3) -> list:
    """盯盘用：并发搜索异动标的的新闻（P0 异动消息面）

    Args:
        config: 配置对象
        movers: [(name, code, change_pct), ...] 异动标的列表
        max_alerts: 最多搜索的标的数

    Returns:
        [(name, code, news_text), ...] 有新闻的异动标的列表
    """
    if not config.mx_apikeys or not movers:
        return []

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)

    def _search(mover):
        name, code, chg = mover
        try:
            text = client.fin_search_as_text(f"{name} 异动原因")
            if text and "错误" not in text[:20]:
                # 提取前几行
                lines = [l for l in text.split("\n") if l.strip()][:5]
                return (name, code, chg, "\n".join(lines))
        except Exception as e:
            log.debug(f"异动消息搜索失败 {name}: {e}")
        return None

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_search, m) for m in movers[:max_alerts]]
        for fut in futures:
            try:
                r = fut.result(timeout=15)
                if r:
                    results.append(r)
            except Exception:
                pass
    return results


# 非行业板块的名称（宽基指数/ETF类别，查询返回的是日线而非实时）
_NON_SECTOR_NAMES = {
    "创业板", "沪深300", "中证500", "中证1000", "科创", "红利",
    "科技", "港股", "港股科技", "港股互联网", "中概互联", "消费",
}


def fetch_sector_flow_scan(config, sectors: list = None) -> str:
    """盯盘用：查询持仓所属板块涨跌幅（妙想兜底，单板块逐个查询）

    Args:
        config: 配置对象
        sectors: 板块名列表（如 ['半导体', '银行', '证券']），空则查默认热门板块

    Returns:
        格式化的板块涨跌幅摘要，失败或无数据返回空串
    """
    if not config.mx_apikeys:
        return ""

    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime as _dt

    client = get_mx_client(config)

    if sectors:
        # 过滤掉宽基指数类"板块"（查询返回的是日线，非实时）
        sectors = list(dict.fromkeys(
            s for s in sectors if s and s not in _NON_SECTOR_NAMES
        ))[:8]
    if not sectors:
        # 默认热门行业板块
        sectors = ["半导体", "银行", "证券", "新能源", "医药", "军工", "人工智能"]

    today_str = _dt.now().strftime("%Y-%m-%d")

    def _query_one(sector):
        try:
            time.sleep(0.4)  # 降低请求频率
            result = client.query(f"{sector}板块涨跌幅")
            if not result:
                return None
            data = result.get("data") or {}
            inner = data.get("data") or {}
            search = inner.get("searchDataResultDTO") or {}
            dto_list = search.get("dataTableDTOList") or []
            if not dto_list:
                return None
            # 第一个 dto 通常是当前涨跌幅
            dto = dto_list[0]
            table = dto.get("table") or {}
            head = table.get("headName") or []
            # 校验数据时效：headName 必须是今天的（实时数据），否则是昨天的日线
            if head and not str(head[0]).startswith(today_str):
                log.debug(f"板块数据过期 {sector}: {head[0]}")
                return None
            for key, values in table.items():
                if key == "headName" or not isinstance(values, list) or not values:
                    continue
                val = str(values[0]).replace("%", "").replace("+", "").strip()
                try:
                    return (sector, float(val))
                except ValueError:
                    continue
            return None
        except Exception as e:
            log.debug(f"板块查询失败 {sector}: {e}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_query_one, s) for s in sectors]
        for fut in futures:
            try:
                r = fut.result(timeout=20)
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return ""

    results.sort(key=lambda x: x[1], reverse=True)
    lines = ["**持仓所属板块涨跌幅（妙想兜底）**"]
    for sector, chg in results:
        arrow = "涨" if chg > 0 else ("跌" if chg < 0 else "平")
        lines.append(f"  [{arrow}] {sector}: {chg:+.2f}%")
    return "\n".join(lines)


def fetch_opportunity_screen(config) -> str:
    """盯盘用：定期智能选股（P3 发现新机会）"""
    if not config.mx_apikeys:
        return ""
    try:
        client = get_mx_client(config)
        text = client.stock_screen_as_text("今日放量上涨且主力资金净流入的股票")
        return text or ""
    except Exception as e:
        log.debug(f"妙想选股扫描失败: {e}")
        return ""


def fetch_holdings_news(config, items, quotes, max_holdings: Optional[int] = None) -> str:
    """报告用：并发搜索列表标的（持仓+自选）的消息面

    Args:
        config: 配置对象
        items: 标的列表（Holding 或 WatchItem，需含 name/code）
        quotes: 行情列表（用于识别异动股，优先搜索）
        max_holdings: 最多搜索的标的数（None=全量，保护调用限额用）

    Returns:
        格式化的标的消息面汇总，失败返回空串
    """
    if not config.mx_apikeys:
        return ""

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)

    # 构建标的名称列表，异动股（涨跌幅>2%）优先
    quote_map = {q.code: q for q in quotes}
    items_out = []
    for it in items:
        q = quote_map.get(it.code)
        chg = q.change_pct if q and q.change_pct is not None else 0
        items_out.append((it.name, it.code, chg))

    # 按异动幅度排序，异动大的优先
    items_out.sort(key=lambda x: abs(x[2]), reverse=True)
    if max_holdings:
        items_out = items_out[:max_holdings]

    def _search_one(name_code_chg):
        name, code, chg = name_code_chg
        query = f"{name} 最新消息 公告"
        try:
            time.sleep(0.5)  # 降低请求频率，避免触发 112 限频
            text = client.fin_search_as_text(query)
            if text:
                # 提取第一行标题作为摘要
                lines = [l for l in text.split("\n") if l.strip()]
                return (name, code, chg, lines[:4])  # 只取前4行
        except Exception as e:
            log.debug(f"持仓消息搜索失败 {name}: {e}")
        return None

    results = []
    # 并发度降为 2，配合重试退避，避免触发妙想限频(112)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_search_one, item): item for item in items_out}
        for fut in futures:
            try:
                r = fut.result(timeout=30)
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return ""

    lines = ["**标的消息面（妙想）**\n"]
    for name, code, chg, news_lines in results:
        chg_str = f" ({chg:+.1f}%)" if chg else ""
        lines.append(f"### {name}({code}){chg_str}")
        for nl in news_lines:
            lines.append(nl)
        lines.append("")

    return "\n".join(lines)


# ============================================================
# 持仓体检 / 评级事件（P2：给加减仓/建仓/清仓补充基本面+资金面+事件面）
# ============================================================

def _pick_columns(columns: list, patterns: list[str]) -> list[str]:
    """按子串匹配从列名列表中挑选关键列（保序去重，每个 pattern 只取首个命中）"""
    picked: list[str] = []
    for pat in patterns:
        for c in columns:
            if pat in c and c not in picked:
                picked.append(c)
                break
    return picked


def _compact_table(table: dict, patterns: list[str], max_rows: int = 4) -> str:
    """把 query_structured 的表格压缩成紧凑文本（只保留关键列）"""
    columns = table.get("columns") or []
    cols = _pick_columns(columns, patterns)
    if not cols:
        return ""
    lines = []
    for row in (table.get("rows") or [])[:max_rows]:
        date = str(row.get("日期", "")).strip()
        cells = []
        for c in cols:
            v = row.get(c)
            if v is not None and str(v).strip() not in ("", "-"):
                label = c.replace("(区间)", "").strip()
                cells.append(f"{label}={v}")
        if cells:
            prefix = f"{date}: " if date else ""
            lines.append(f"  {prefix}{' | '.join(cells)}")
    return "\n".join(lines)


def _norm_code(c: str) -> str:
    """归一化证券代码（去 SH/SZ/BJ 前缀与 . 后缀），用于 secu_list 精确匹配"""
    c = (c or "").strip().upper()
    for p in ("SH", "SZ", "BJ"):
        c = c.replace(p, "")
    return c.replace(".", "")


def _belongs_to(item: dict, name: str, code: str) -> bool:
    """资讯是否真正关联该证券：优先 secu_list 代码/简称匹配，缺失时退化标题/全称包含简称"""
    secus = item.get("secu_list") or []
    if secus:
        for s in secus:
            if _norm_code(s.get("code")) == _norm_code(code):
                return True
            if s.get("name") and s.get("name") == name:
                return True
        return False
    hay = (item.get("title") or "") + (item.get("entity_full_name") or "")
    return name in hay


def fetch_holdings_fundamental(config, items, max_holdings: Optional[int] = None) -> str:
    """报告用：并发查询列表标的（持仓+自选）的资金面+筹码+基本面（妙想 query_structured）

    每个标的 3 个维度（自然语言 → 结构化表格 → 压缩摘要）：
      1. 资金面：近5日主力资金净流入趋势
      2. 筹码：机构持股比例合计（按报告期，看机构进出）
      3. 基本面：最新财报净利润同比/营收/ROE/负债率

    返回格式化文本，失败或无 key 返回空串。
    """
    if not config.mx_apikeys or not items:
        return ""

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)
    items = list(items)
    if max_holdings:
        items = items[:max_holdings]
    if not items:
        return ""

    def _query_one(h):
        name, code = h.name, h.code
        parts: list[str] = []
        try:
            # 1. 资金面
            tables = client.query_structured(f"{name} 近5日主力资金净流入")
            if tables:
                txt = _compact_table(tables[0], ["主力净流入资金", "净流入天数", "净流出天数"], max_rows=5)
                if txt:
                    parts.append(f"**资金面(近5日主力净流入)**:\n{txt}")
            time.sleep(0.4)
            # 2. 筹码
            tables = client.query_structured(f"{name} 机构持股比例")
            if tables:
                txt = _compact_table(tables[0], ["机构持股比例"], max_rows=4)
                if txt:
                    parts.append(f"**筹码(机构持股比例)**:\n{txt}")
            time.sleep(0.4)
            # 3. 基本面
            tables = client.query_structured(f"{name} 最新财报 净利润 营业收入 同比增长")
            if tables:
                txt = _compact_table(
                    tables[0],
                    ["净利润同比增长率", "营业收入", "净资产收益率ROE", "资产负债率"],
                    max_rows=2,
                )
                if txt:
                    parts.append(f"**基本面(最新财报)**:\n{txt}")
        except Exception as e:
            log.debug(f"持仓体检查询失败 {name}: {e}")
        if not parts:
            return None
        return f"### {name}({code})\n" + "\n".join(parts)

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_query_one, h) for h in items]
        for fut in futures:
            try:
                r = fut.result(timeout=60)
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return ""
    return "**标的体检（资金面+筹码+基本面）**\n\n" + "\n\n".join(results)


def fetch_holdings_events(config, items, max_holdings: Optional[int] = None) -> str:
    """报告用：并发检索列表标的（持仓+自选）的研报评级 + 减持/增持/回购/解禁事件（妙想 fin_search_structured）

    每个标的 2 个维度：
      1. 研报评级：最新研报的评级（买入/增持/中性/减持/卖出）+ 机构
      2. 事件监控：减持/增持/回购/解禁/业绩预告/质押

    返回格式化文本，失败或无 key 返回空串。
    """
    if not config.mx_apikeys or not items:
        return ""

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)
    items = list(items)
    if max_holdings:
        items = items[:max_holdings]
    if not items:
        return ""

    _EVENT_KEYWORDS = ("减持", "增持", "回购", "解禁", "业绩预告", "质押")

    def _search_one(h):
        name, code = h.name, h.code
        parts: list[str] = []
        try:
            # 1. 研报评级
            reports = client.fin_search_structured(f"{name} 研报 评级 目标价")
            rating_lines = []
            for it in reports[:6]:
                if (
                    it.get("information_type") == "REPORT"
                    and it.get("rating")
                    and _belongs_to(it, name, code)
                ):
                    ins = it.get("ins_name") or "研报"
                    rating_lines.append(f"    [{it['rating']}] {ins}: {it['title'][:36]}")
            if rating_lines:
                parts.append("**研报评级**:\n" + "\n".join(rating_lines[:3]))
            time.sleep(0.4)
            # 2. 事件监控
            events = client.fin_search_structured(f"{name} 减持 增持 回购 解禁")
            event_lines = []
            for it in events[:8]:
                t = it.get("title", "")
                if any(k in t for k in _EVENT_KEYWORDS) and _belongs_to(it, name, code):
                    itype = it.get("information_type", "")
                    tag = "公告" if itype == "ANNOUNCEMENT" else (itype or "资讯")
                    event_lines.append(f"    [{tag}] {t[:36]}")
            if event_lines:
                parts.append("**事件监控(减持/增持/回购/解禁)**:\n" + "\n".join(event_lines[:4]))
        except Exception as e:
            log.debug(f"持仓事件检索失败 {name}: {e}")
        if not parts:
            return None
        return f"### {name}({code})\n" + "\n".join(parts)

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_search_one, h) for h in items]
        for fut in futures:
            try:
                r = fut.result(timeout=60)
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return ""
    return "**标的评级与事件监控**\n\n" + "\n\n".join(results)


def fetch_etf_fund_flow(config, items) -> dict:
    """盯盘/报告用：查询 ETF 资金流（主力净流入 + 净申购额，申赎口径）

    妙想对 ETF 额外返回「净申购额估算值」（申赎口径），这是东财 fflow 接口给不了、
    且更贴合 ETF 真实资金进出的指标。本函数从传入的标的列表里过滤出 ETF，
    并发查询每只 ETF 的最新主力净流入与净申购额。

    Args:
        config: 配置对象
        items: 标的列表（Holding 或 WatchItem，需含 name/code/type）

    Returns:
        {code: {"name": str, "main_net": float|None, "net_subscribe": float|None}}
        失败/无 ETF/无数据返回空 dict
    """
    if not config.mx_apikeys:
        return {}

    # 过滤出 ETF：优先 A股 ETF 代码号段，type/name 兜底（holdings 里的 ETF 常无 type 且名不带"ETF"）
    _ETF_PREFIXES = ("51", "56", "58", "159")
    etfs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for it in items:
        code = str(getattr(it, "code", "") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        itype = str(getattr(it, "type", "") or "")
        name = str(getattr(it, "name", "") or "")
        if code.startswith(_ETF_PREFIXES) or "ETF" in itype or "ETF" in name:
            etfs.append((name.strip(), code))
    if not etfs:
        return {}

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)

    def _query_one(name: str, code: str):
        try:
            tables = client.query_structured(f"{name} {code} 主力资金净流入 净申购额")
            main_net = None
            net_subscribe = None
            for t in tables:
                cols = t.get("columns") or []
                # 字段名不统一：沪深300ETF 用「区间主力净流入资金」，半导体ETF 用「主力净流入资金」，
                # 净申购额列名「区间净申购额估算值(区间净流入额)」含「净申购」。按子串匹配，取最新一行。
                main_col = next((c for c in cols if "主力净流入" in c), None)
                sub_col = next((c for c in cols if "净申购" in c or "申赎" in c), None)
                rows = t.get("rows") or []
                if not rows:
                    continue
                row = rows[0]  # 最新交易日/区间在前
                if main_col and main_net is None:
                    main_net = MXClient._parse_amount(row.get(main_col))
                if sub_col and net_subscribe is None:
                    net_subscribe = MXClient._parse_amount(row.get(sub_col))
            if main_net is None and net_subscribe is None:
                return None
            return (code, {"name": name, "main_net": main_net, "net_subscribe": net_subscribe})
        except Exception as e:
            log.debug(f"ETF资金流查询失败 {name}: {e}")
            return None

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_query_one, n, c) for n, c in etfs]
        for fut in futures:
            try:
                r = fut.result(timeout=30)
                if r:
                    results[r[0]] = r[1]
            except Exception:
                pass
    return results


_mx_client: Optional[MXClient] = None
