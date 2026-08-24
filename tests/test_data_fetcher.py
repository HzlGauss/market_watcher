"""
数据获取模块测试 - fetch_global_markets
"""
from unittest.mock import patch, MagicMock
import pytest
from app.data_fetcher import fetch_global_markets


def _mock_sina_resp(text, encoding="gbk"):
    """构造模拟的 HTTP Response"""
    resp = MagicMock()
    resp.encoding = encoding
    resp.text = text
    return resp


def _mock_eastmoney_resp(data: dict):
    """构造模拟的东方财富 HTTP Response（JSON）"""
    resp = MagicMock()
    resp.json.return_value = {"rc": 0, "data": data}
    return resp


# 美股数据（fields: 名称,价格,涨跌幅,...）
US_STOCKS_RESP = (
    'var hq_str_gb_ixic="纳斯达克,18600.50,1.25,2026-05-16";\n'
    'var hq_str_gb_dji="道琼斯,41000.00,-0.50,2026-05-16";\n'
    'var hq_str_gb_inx="标普500指数,5600.00,0.80,2026-05-16";\n'
)

A50_EM_RESP = _mock_eastmoney_resp({"f2": 13200.00, "f3": 0.35, "f14": "A50期指当月连续"})

HSI_RESP = 'var hq_str_hkHSI="恒生指数,19450.00,19350.00,19500.00,19580.00,19380.00,150.00,0.78,1500000000,30000000000";\n'

FOREX_RESP = 'var hq_str_fx_susdcny="美元人民币,7.2500,2026-05-15";\n'


class _SinaOnly:
    """Patch helper: only sina_client.get, eastmoney returns None"""

    @staticmethod
    def sina(url, **kwargs):
        if "gb_ixic" in url:
            return _mock_sina_resp(US_STOCKS_RESP)
        elif "hkHSI" in url:
            return _mock_sina_resp(HSI_RESP)
        elif "fx_susdcny" in url:
            return _mock_sina_resp(FOREX_RESP)
        return None

    @staticmethod
    def eastmoney(url, **kwargs):
        return None


def _patch_both(sina_side=None, em_return=None):
    """Convenience: patch both sina and eastmoney clients"""
    return patch.multiple(
        "app.data_fetcher",
        sina_client=MagicMock(get=MagicMock(side_effect=sina_side)),
        eastmoney_client=MagicMock(get=MagicMock(return_value=em_return)),
    )


class TestFetchGlobalMarkets:
    """fetch_global_markets 测试"""

    def test_all_markets_success(self):
        """所有市场正常返回数据"""
        def mock_sina(url, **kwargs):
            if "gb_ixic" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            elif "hkHSI" in url:
                return _mock_sina_resp(HSI_RESP)
            elif "fx_susdcny" in url:
                return _mock_sina_resp(FOREX_RESP)
            return None

        mock_em = MagicMock(return_value=A50_EM_RESP)

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", mock_em):
            result = fetch_global_markets()

        assert "纳斯达克" in result
        assert "18600.50" in result["纳斯达克"]
        assert "道琼斯" in result
        assert "41000.00" in result["道琼斯"]
        assert "标普500" in result
        assert "5600.00" in result["标普500"]
        assert "A50期指当月连续" in result
        assert "13200.00" in result["A50期指当月连续"]
        assert "恒生指数" in result
        assert "19500.00" in result["恒生指数"]
        assert "汇率" in result
        assert "7.2500" in result["汇率"]

    def test_partial_data_us_only(self):
        """只有美股和A50有数据，其他无响应"""
        def mock_sina(url, **kwargs):
            if "gb_ixic" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert "纳斯达克" in result
        assert "道琼斯" in result
        assert "标普500" in result
        # A50: 东方财富API未返回数据
        assert "A50期指当月连续" not in result
        assert "恒生指数" not in result
        assert "汇率" not in result

    def test_api_returns_empty_data(self):
        """新浪API返回空响应体"""
        def mock_sina(url, **kwargs):
            return _mock_sina_resp("")

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert result == {}

    def test_api_returns_null_data(self):
        """API 返回无引号字段的响应（数据不完整）"""
        no_quote_resp = "var hq_str_gb_ixic=;\n"

        with patch("app.data_fetcher.sina_client.get", return_value=_mock_sina_resp(no_quote_resp)), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert result == {}

    def test_api_returns_partial_fields(self):
        """API 返回字段不足"""
        partial_fields_resp = 'var hq_str_gb_ixic="纳斯达克,18600.50";\n'

        def mock_sina(url, **kwargs):
            if "gb_ixic" in url:
                return _mock_sina_resp(partial_fields_resp)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        # 字段数 < 3，不满足 len(fields) >= 3，不会加入结果
        assert "纳斯达克" not in result

    def test_exception_handling(self):
        """网络异常时容错，返回空字典"""
        with patch("app.data_fetcher.sina_client.get", side_effect=Exception("Network error")), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert result == {}

    def test_mixed_valid_and_error(self):
        """部分接口正常、部分异常"""
        def mock_sina(url, **kwargs):
            if "gb_ixic" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            raise Exception("Timeout")

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", side_effect=Exception("Timeout")):
            result = fetch_global_markets()

        # 美股正常获取，A50异常但不影响
        assert "纳斯达克" in result
        assert "道琼斯" in result
        assert "标普500" in result
        assert "A50期指当月连续" not in result
        assert "恒生指数" not in result
        assert "汇率" not in result

    def test_returns_dict(self):
        """验证返回类型为 dict"""
        def mock_sina(url, **kwargs):
            return _mock_sina_resp(US_STOCKS_RESP)

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert isinstance(result, dict)

    def test_us_stocks_parse_format(self):
        """美股数据解析后格式为 '价格 (涨跌幅%)' """
        def mock_sina(url, **kwargs):
            if "gb_ixic" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_sina), \
             patch("app.data_fetcher.eastmoney_client.get", return_value=None):
            result = fetch_global_markets()

        assert result["纳斯达克"] == "18600.50 (1.25%)"
        assert result["道琼斯"] == "41000.00 (-0.50%)"
        assert result["标普500"] == "5600.00 (0.80%)"


# ============================================================
# fetch_quotes_rich 测试
# ============================================================

from app.data_fetcher import fetch_quotes_rich, fetch_market_news
from app.models import WatchItem, Quote, MarketNews


def _mock_sina_quote_resp():
    """构造模拟的新浪财经行情响应"""
    return _mock_sina_resp(
        'var hq_str_sh510300="沪深300ETF,3.500,3.450,3.500,3.520,3.480,3.490,3.510,'
        '1200000000,4200000000.000,0,0,0,0,0,0,0,0,0,0,'
        '0,0.85,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,3.550,3.420";\n'
    )


def _mock_akshare_df():
    """构造模拟的 AKShare DataFrame"""
    import pandas as pd

    return pd.DataFrame([{
        "代码": "510300",
        "名称": "沪深300ETF",
        "最新价": 3.500,
        "涨跌幅": 1.45,
        "涨跌额": 0.050,
        "昨收": 3.450,
        "今开": 3.460,
        "最高": 3.520,
        "最低": 3.480,
        "成交量": 1200000,
        "成交额": 4200000000,
        "振幅": 1.16,
        "换手率": 0.85,
        "市盈率(动态)": 12.5,
        "市净率": 1.35,
        "总市值": 350000000000,
        "涨停价": 3.800,
        "跌停价": 3.100,
    }])


class TestFetchQuotesRich:
    """fetch_quotes_rich 测试"""

    def test_sina_success_akshare_enriches(self):
        """新浪返回基础行情，AKShare 补查丰富字段"""
        basic_quote = Quote(code="510300", name="沪深300ETF", price=4.878)

        def enrich(quotes):
            quotes[0].pe_ratio = 12.5
            quotes[0].pb_ratio = 1.35
            quotes[0].market_cap = 350000000000
            quotes[0].turnover_rate = 0.85
            quotes[0].upper_limit = 3.800
            quotes[0].lower_limit = 3.100

        with patch("app.data_fetcher.fetch_quotes", return_value=[basic_quote]), \
             patch("app.data_fetcher._enrich_from_akshare", side_effect=enrich):
            result = fetch_quotes_rich([WatchItem(name="沪深300ETF", code="510300", market="SH")])

        assert len(result) == 1
        assert result[0].code == "510300"
        assert result[0].pe_ratio == 12.5
        assert result[0].pb_ratio == 1.35
        assert result[0].market_cap == 350000000000
        assert result[0].turnover_rate == 0.85
        assert result[0].upper_limit == 3.800
        assert result[0].lower_limit == 3.100

    def test_sina_success_akshare_skipped_rich_none(self):
        """新浪返回基础行情，AKShare 补查失败时丰富字段留 None"""
        basic_quote = Quote(code="510300", name="沪深300ETF", price=4.878)

        with patch("app.data_fetcher.fetch_quotes", return_value=[basic_quote]), \
             patch("app.data_fetcher._enrich_from_akshare", side_effect=Exception("timeout")):
            result = fetch_quotes_rich([WatchItem(name="沪深300ETF", code="510300", market="SH")])

        assert len(result) == 1
        assert result[0].code == "510300"
        # 新浪返回的 Quote 中丰富字段为 None
        assert result[0].pe_ratio is None
        assert result[0].market_cap is None

    def test_sina_fails_fallback_akshare(self):
        """新浪无数据时 fallback 到 AKShare 全量获取"""
        mock_quote = Quote(
            code="510300", name="沪深300ETF", pe_ratio=12.5, pb_ratio=1.35,
            market_cap=350000000000, turnover_rate=0.85,
            upper_limit=3.800, lower_limit=3.100
        )

        with patch("app.data_fetcher.fetch_quotes", return_value=[]), \
             patch("app.data_fetcher._fetch_quotes_akshare", return_value=[mock_quote]):
            result = fetch_quotes_rich([WatchItem(name="沪深300ETF", code="510300", market="SH")])

        assert len(result) == 1
        assert result[0].code == "510300"
        assert result[0].pe_ratio == 12.5
        assert result[0].market_cap == 350000000000

    def test_empty_items(self):
        """空列表返回空列表"""
        result = fetch_quotes_rich([])
        assert result == []

    def test_rich_fields_present_after_enrich(self):
        """丰富字段来自 AKShare 补查"""
        basic_quote = Quote(code="510300", name="沪深300ETF", price=4.878)

        def enrich(quotes):
            quotes[0].pe_ratio = 12.5
            quotes[0].pb_ratio = 1.35
            quotes[0].market_cap = 350000000000
            quotes[0].turnover_rate = 0.85
            quotes[0].upper_limit = 3.800
            quotes[0].lower_limit = 3.100

        with patch("app.data_fetcher.fetch_quotes", return_value=[basic_quote]), \
             patch("app.data_fetcher._enrich_from_akshare", side_effect=enrich):
            result = fetch_quotes_rich([WatchItem(name="沪深300ETF", code="510300", market="SH")])

        assert len(result) == 1
        q = result[0]
        assert q.pe_ratio is not None
        assert q.pb_ratio is not None
        assert q.market_cap is not None
        assert q.turnover_rate is not None
        assert q.upper_limit is not None
        assert q.lower_limit is not None

    def test_sina_fails_akshare_also_fails_returns_empty(self):
        """新浪无数据，AKShare fallback 也失败时返回空列表"""
        with patch("app.data_fetcher.fetch_quotes", return_value=[]), \
             patch("app.data_fetcher._fetch_quotes_akshare", side_effect=Exception("timeout")):
            result = fetch_quotes_rich([WatchItem(name="沪深300ETF", code="510300", market="SH")])

        assert result == []


# ============================================================
# fetch_market_news 测试
# ============================================================

def _mock_news_resp(items: list[dict]):
    """构造模拟的新浪财经快讯响应（fetch_market_news 用 requests.get 直连新浪）"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"result": {"data": items}}
    return resp


def _make_news_item(title: str, ctime: str, content: str = "", category: str = "") -> dict:
    return {
        "title": title,
        "ctime": ctime,
        "intro": content,        # fetch_market_news 读 intro 作为正文
        "media_name": category,  # fetch_market_news 读 media_name 作为来源/分类
        "url": f"http://example.com/{title}",
    }


class TestFetchMarketNews:
    """fetch_market_news 测试"""

    def test_success_within_time_window(self):
        """时间窗口内有新闻，正确返回"""
        items = [
            _make_news_item("新闻A", "2026-05-16 08:30:00", "内容A", "宏观"),
            _make_news_item("新闻B", "2026-05-16 07:15:00", "内容B", "政策"),
            _make_news_item("新闻C", "2026-05-16 09:05:00", "内容C"),
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9, max_count=15)

        assert len(result) == 2
        assert result[0].title == "新闻A"
        assert result[0].time == "08:30"
        assert result[0].category == "宏观"
        assert result[0].content == "内容A"
        assert result[1].title == "新闻B"

    def test_filters_out_of_window(self):
        """时间窗口外的新闻被过滤"""
        items = [
            _make_news_item("夜间新闻", "2026-05-16 03:00:00", ""),
            _make_news_item("盘中新闻", "2026-05-16 10:00:00", ""),
            _make_news_item("晚间新闻", "2026-05-16 15:00:00", ""),
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=9, end_hour=12, max_count=15)

        assert len(result) == 1
        assert result[0].title == "盘中新闻"

    def test_api_returns_none(self):
        """API 返回 None 时返回空列表"""
        with patch("requests.get", return_value=None):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert result == []

    def test_api_returns_invalid_json(self):
        """API 返回非 JSON 时返回空列表"""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = Exception("Invalid JSON")

        with patch("requests.get", return_value=resp):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert result == []

    def test_no_news_in_window(self):
        """时间窗口内无新闻时返回空列表"""
        items = [
            _make_news_item("早间新闻", "2026-05-16 02:00:00", ""),
            _make_news_item("晚间新闻", "2026-05-16 20:00:00", ""),
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=9, end_hour=12, max_count=15)

        assert result == []

    def test_max_count_limit(self):
        """max_count 限制生效"""
        items = [
            _make_news_item(f"新闻{i}", "2026-05-16 08:00:00", "")
            for i in range(20)
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9, max_count=5)

        assert len(result) == 5

    def test_content_truncation(self):
        """超长内容被截断到200字"""
        long_content = "A" * 500
        items = [_make_news_item("长新闻", "2026-05-16 08:00:00", long_content)]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert len(result) == 1
        assert len(result[0].content) == 200

    def test_returns_market_news_objects(self):
        """返回的是 MarketNews 对象"""
        items = [_make_news_item("测试", "2026-05-16 08:00:00", "内容", "测试")]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert len(result) == 1
        assert isinstance(result[0], MarketNews)

    def test_skips_items_without_title(self):
        """没有标题的条目被跳过"""
        items = [
            {"ctime": "2026-05-16 08:00:00", "content": "无标题"},
            _make_news_item("有标题", "2026-05-16 08:05:00", "内容"),
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert len(result) == 1
        assert result[0].title == "有标题"

    def test_empty_data_response(self):
        """data 字段为空时返回空列表"""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"result": {"data": None}}

        with patch("requests.get", return_value=resp):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert result == []

    def test_skips_items_without_ctime(self):
        """没有时间的条目被跳过"""
        items = [
            {"title": "无时间", "content": "内容"},
            _make_news_item("有时间", "2026-05-16 08:00:00", "内容"),
        ]

        with patch("requests.get", return_value=_mock_news_resp(items)):
            result = fetch_market_news(start_hour=0, end_hour=9)

        assert len(result) == 1
        assert result[0].title == "有时间"
