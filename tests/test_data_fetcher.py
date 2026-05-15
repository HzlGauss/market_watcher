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


# 美股数据（fields: 名称,??,??,价格,涨跌幅,...）
US_STOCKS_RESP = (
    'var hq_str_gb_USTECH="纳斯达克,指数,昨日收盘,18600.50,1.25,2026-05-15";\n'
    'var hq_str_gb_US30="道琼斯,指数,昨日收盘,41000.00,-0.50,2026-05-15";\n'
    'var hq_str_gb_US500="标普500,指数,昨日收盘,5600.00,0.80,2026-05-15";\n'
)

A50_RESP = 'var hq_str_gb_NQH2="A50期货,期货,昨日收盘,13200.00,0.35,2026-05-15";\n'

HSI_RESP = 'var hq_str_hkHSI="恒生指数,指数,昨日收盘,19500.00,-0.25,2026-05-15";\n'

FOREX_RESP = 'var hq_str_fx_susdcny="美元人民币,7.2500,2026-05-15";\n'


class TestFetchGlobalMarkets:
    """fetch_global_markets 测试"""

    def test_all_markets_success(self):
        """所有市场正常返回数据"""
        def mock_get(url, **kwargs):
            if "gb_USTECH" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            elif "gb_NQH2" in url:
                return _mock_sina_resp(A50_RESP)
            elif "hkHSI" in url:
                return _mock_sina_resp(HSI_RESP)
            elif "fx_susdcny" in url:
                return _mock_sina_resp(FOREX_RESP)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        assert "纳斯达克" in result
        assert "18600.50" in result["纳斯达克"]
        assert "道琼斯" in result
        assert "41000.00" in result["道琼斯"]
        assert "标普500" in result
        assert "5600.00" in result["标普500"]
        assert "A50期货" in result
        assert "13200.00" in result["A50期货"]
        assert "恒生指数" in result
        assert "19500.00" in result["恒生指数"]
        assert "汇率" in result
        assert "7.2500" in result["汇率"]

    def test_partial_data_us_only(self):
        """只有美股有数据，其他市场无响应"""
        def mock_get(url, **kwargs):
            if "gb_USTECH" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        assert "纳斯达克" in result
        assert "道琼斯" in result
        assert "标普500" in result
        assert "A50期货" not in result
        assert "恒生指数" not in result
        assert "汇率" not in result

    def test_api_returns_empty_data(self):
        """API 返回空响应体"""
        def mock_get(url, **kwargs):
            return _mock_sina_resp("")

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        assert result == {}

    def test_api_returns_null_data(self):
        """API 返回无引号字段的响应（数据不完整）"""
        no_quote_resp = "var hq_str_gb_USTECH=;\n"

        with patch("app.data_fetcher.sina_client.get", return_value=_mock_sina_resp(no_quote_resp)):
            result = fetch_global_markets()

        assert result == {}

    def test_api_returns_partial_fields(self):
        """API 返回字段不足（如涨跌幅缺失）"""
        partial_fields_resp = 'var hq_str_gb_USTECH="纳斯达克,指数,18600.50";\n'

        def mock_get(url, **kwargs):
            if "gb_USTECH" in url:
                return _mock_sina_resp(partial_fields_resp)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        # 字段数 <= 3，不满足 len(fields) > 3，不会加入结果
        assert "纳斯达克" not in result

    def test_exception_handling(self):
        """网络异常时容错，返回空字典"""
        with patch("app.data_fetcher.sina_client.get", side_effect=Exception("Network error")):
            result = fetch_global_markets()

        assert result == {}

    def test_mixed_valid_and_error(self):
        """部分接口正常、部分异常"""
        def mock_get(url, **kwargs):
            if "gb_USTECH" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            raise Exception("Timeout")

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        # 美股在第一个请求中成功获取，后续请求抛出异常
        assert "纳斯达克" in result
        assert "道琼斯" in result
        assert "标普500" in result
        assert "A50期货" not in result
        assert "恒生指数" not in result
        assert "汇率" not in result

    def test_returns_dict(self):
        """验证返回类型为 dict"""
        def mock_get(url, **kwargs):
            return _mock_sina_resp(US_STOCKS_RESP)

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        assert isinstance(result, dict)

    def test_us_stocks_parse_format(self):
        """美股数据解析后格式为 '价格 (涨跌幅%)' """
        def mock_get(url, **kwargs):
            if "gb_USTECH" in url:
                return _mock_sina_resp(US_STOCKS_RESP)
            return None

        with patch("app.data_fetcher.sina_client.get", side_effect=mock_get):
            result = fetch_global_markets()

        assert result["纳斯达克"] == "18600.50 (1.25%)"
        assert result["道琼斯"] == "41000.00 (-0.50%)"
        assert result["标普500"] == "5600.00 (0.80%)"
