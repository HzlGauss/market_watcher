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

    # ---- 3. 财经资讯搜索 ----

    def fin_search(self, keyword: str) -> Optional[dict]:
        """财经资讯搜索（自然语言）"""
        payload = {"query": keyword}
        return self._post("news-search", payload)

    def fin_search_as_text(self, keyword: str) -> str:
        """财经资讯搜索，返回格式化的文本结果"""
        result = self.fin_search(keyword)
        if result is None:
            return ""
        return self._format_fin_search(result)

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
    def _format_fin_search(result: dict) -> str:
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

        # 日期过滤：只保留最近 7 天的资讯
        from datetime import datetime as _dt, timedelta
        import re as _re

        def _parse_date(date_str) -> Optional["_dt"]:
            m = _re.search(r"(\d{4}-\d{2}-\d{2})", str(date_str or ""))
            if m:
                try:
                    return _dt.strptime(m.group(1), "%Y-%m-%d")
                except ValueError:
                    pass
            return None

        cutoff = _dt.now() - timedelta(days=7)
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
            # 归一化标题（去空格标点）用于相似判断
            norm = _re.sub(r"[\s\W]", "", title)[:40]
            if norm and norm in seen_titles:
                continue  # 重复，跳过
            if norm:
                seen_titles.add(norm)
            deduped.append(item)

        lines.append(f"**资讯结果: {len(deduped)} 条（最近7天，已去重）**\n")
        for item in deduped[:15]:
            if not isinstance(item, dict):
                lines.append(f"  - {item}")
                continue
            title = item.get("title") or "?"
            source = item.get("source") or ""
            date = item.get("date") or ""
            url = item.get("jumpUrl") or ""
            content = item.get("content") or ""
            info_type = item.get("informationType") or ""

            # 标题（去掉尾部 "-来源" 后缀）
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]

            line = f"  - {title}"
            meta = []
            if source:
                meta.append(source)
            if date:
                meta.append(date[:10])
            if info_type:
                meta.append(info_type)
            if meta:
                line += f"  [{', '.join(meta)}]"
            lines.append(line)
            if url:
                lines.append(f"    {url}")
            if content:
                # 内容截取前 100 字
                lines.append(f"    {content[:100]}")
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


def fetch_holdings_news(config, holdings, quotes, max_holdings: int = 20) -> str:
    """报告用：并发搜索每个持仓的消息面

    Args:
        config: 配置对象
        holdings: 持仓列表
        quotes: 行情列表（用于识别异动股，优先搜索）
        max_holdings: 最多搜索的持仓数（保护调用限额）

    Returns:
        格式化的持仓消息面汇总，失败返回空串
    """
    if not config.mx_apikeys:
        return ""

    from concurrent.futures import ThreadPoolExecutor

    client = get_mx_client(config)

    # 构建持仓名称列表，异动股（涨跌幅>2%）优先
    quote_map = {q.code: q for q in quotes}
    items = []
    for h in holdings:
        q = quote_map.get(h.code)
        chg = q.change_pct if q and q.change_pct is not None else 0
        # 只搜有持仓数量（amount>0）或有行情的
        if h.amount <= 0 and not q:
            continue
        items.append((h.name, h.code, chg))

    # 按异动幅度排序，异动大的优先
    items.sort(key=lambda x: abs(x[2]), reverse=True)
    items = items[:max_holdings]

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
        futures = {pool.submit(_search_one, item): item for item in items}
        for fut in futures:
            try:
                r = fut.result(timeout=30)
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return ""

    lines = ["**持仓消息面（妙想）**\n"]
    for name, code, chg, news_lines in results:
        chg_str = f" ({chg:+.1f}%)" if chg else ""
        lines.append(f"### {name}({code}){chg_str}")
        for nl in news_lines:
            lines.append(nl)
        lines.append("")

    return "\n".join(lines)


_mx_client: Optional[MXClient] = None
