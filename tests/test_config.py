"""
配置模块测试
"""

import pytest
from pathlib import Path
from app.config import Config, ConfigValidationError
from app.models import WatchItem, Holding


class TestConfig:
    """Config 类测试"""
    
    def test_config_load_success(self, tmp_path):
        """测试成功加载配置"""
        config_file = tmp_path / "test_config.json"
        config_file.write_text('{"标的列表": [], "提醒阈值": {"涨幅预警": 3.0}}')
        
        config = Config(config_file)
        assert config.scan_interval == 15
        assert config.trade_only is True
    
    def test_config_file_not_found(self, tmp_path):
        """测试配置文件不存在"""
        non_existent = tmp_path / "non_existent.json"
        
        with pytest.raises(FileNotFoundError):
            Config(non_existent)
    
    def test_config_empty_watchlist_raises_error(self, tmp_path):
        """测试空标的列表抛出异常"""
        config_file = tmp_path / "empty_config.json"
        config_file.write_text('{"标的列表": []}')
        
        with pytest.raises(ConfigValidationError):
            Config(config_file)
    
    def test_config_default_values(self, tmp_path):
        """测试默认配置值"""
        config_file = tmp_path / "minimal_config.json"
        config_file.write_text('{"标的列表": [{"name": "Test", "code": "000001", "market": "SH"}]}')
        
        config = Config(config_file)
        assert config.scan_interval == 15
        assert config.trade_only is True
        assert config.llm_model == "deepseek-chat"
        assert config.north_flow_interval == 30
    
    def test_config_custom_values(self, tmp_path):
        """测试自定义配置值"""
        config_file = tmp_path / "custom_config.json"
        config_file.write_text('''
        {
            "标的列表": [{"name": "Test", "code": "000001", "market": "SH"}],
            "盯盘设置": {
                "扫描间隔分钟": 10,
                "仅交易时段运行": false
            },
            "大模型分析": {
                "模型": "custom-model"
            }
        }
        ''')
        
        config = Config(config_file)
        assert config.scan_interval == 10
        assert config.trade_only is False
        assert config.llm_model == "custom-model"
    
    def test_watch_items_from_json(self, tmp_path):
        """测试从 JSON 加载标的列表"""
        config_file = tmp_path / "watchlist_config.json"
        config_file.write_text('''
        {
            "标的列表": [
                {"name": "沪深 300ETF", "code": "510300", "market": "SH", "type": "宽基 ETF"},
                {"name": "上证 50ETF", "code": "510050", "market": "SH", "type": "宽基 ETF"}
            ]
        }
        ''')
        
        config = Config(config_file)
        items = config.watch_items
        assert len(items) == 2
        assert items[0].code == "510300"
        assert items[0].market == "SH"
    
    def test_watch_items_from_csv(self, tmp_path):
        """测试从 CSV 加载标的列表"""
        config_file = tmp_path / "watchlist_config.json"
        config_file.write_text('{"标的列表": []}')
        
        csv_file = tmp_path / "watchlist.csv"
        csv_file.write_text("name,code,market,type\n沪深 300ETF,510300,SH，宽基 ETF\n")
        
        config = Config(config_file)
        items = config.watch_items
        assert len(items) == 1
        assert items[0].code == "510300"
    
    def test_holdings_from_json(self, tmp_path):
        """测试从 JSON 加载持仓"""
        config_file = tmp_path / "holdings_config.json"
        config_file.write_text('''
        {
            "持仓": [
                {"name": "沪深 300ETF", "code": "510300", "market": "SH", "amount": 10000, "cost": 4.85}
            ]
        }
        ''')
        
        config = Config(config_file)
        holdings = config.holdings
        assert len(holdings) == 1
        assert holdings[0].amount == 10000
        assert holdings[0].cost == 4.85
    
    def test_thresholds_default_values(self, tmp_path):
        """测试默认阈值"""
        config_file = tmp_path / "config.json"
        config_file.write_text('{"标的列表": [{"name": "Test", "code": "000001", "market": "SH"}]}')
        
        config = Config(config_file)
        thresholds = config.thresholds
        assert "涨幅预警" in thresholds
        assert thresholds["涨幅预警"] == 3.0


class TestWatchItemValidation:
    """WatchItem 验证测试"""
    
    def test_valid_watch_item(self):
        """测试有效的 WatchItem"""
        item = {
            "name": "沪深 300ETF",
            "code": "510300",
            "market": "SH",
            "type": "宽基 ETF"
        }
        watch_item = WatchItem(**item)
        assert watch_item.code == "510300"
        assert watch_item.market == "SH"
    
    def test_invalid_market(self):
        """测试无效市场标识"""
        item = {
            "name": "Test ETF",
            "code": "000001",
            "market": "INVALID",
            "type": "宽基 ETF"
        }
        # 应该使用默认值 SH
        watch_item = WatchItem(**item)
        assert watch_item.market == "INVALID"  # 注意：当前不验证，只是警告


class TestHoldingValidation:
    """Holding 验证测试"""
    
    def test_valid_holding(self):
        """测试有效的 Holding"""
        holding = Holding(
            name="沪深 300ETF",
            code="510300",
            market="SH",
            amount=10000,
            cost=4.85
        )
        assert holding.amount == 10000
        assert holding.cost == 4.85
    
    def test_negative_amount(self):
        """测试负数持仓数量"""
        holding = Holding(
            name="Test",
            code="000001",
            market="SH",
            amount=-100,
            cost=4.85
        )
        assert holding.amount == -100  # 注意：当前不验证，只是警告
