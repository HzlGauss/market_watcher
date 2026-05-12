"""
工具函数测试
"""

import pytest
from pathlib import Path
from datetime import datetime
from app.utils import (
    format_volume, format_amount, format_percentage,
    format_number, safe_float, safe_int,
    time_to_minutes, is_in_time_range
)


class TestFormatFunctions:
    """格式化函数测试"""
    
    def test_format_volume(self):
        """测试成交量格式化"""
        assert format_volume(None) == "--"
        assert format_volume(100) == "100"
        assert format_volume(15000) == "1.5 万"
        assert format_volume(150000000) == "1.50 亿"
    
    def test_format_amount(self):
        """测试成交额格式化"""
        assert format_amount(None) == "--"
        assert format_amount(100) == "100"
        assert format_amount(15000) == "1 万"
        assert format_amount(150000000) == "1.50 亿"
    
    def test_format_percentage(self):
        """测试百分比格式化"""
        assert format_percentage(None) == "--"
        assert format_percentage(0.0) == "+0.00%"
        assert format_percentage(1.5) == "+1.50%"
        assert format_percentage(-2.3) == "-2.30%"
        assert format_percentage(1.567, decimals=1) == "+1.6%"
    
    def test_format_number(self):
        """测试数字格式化"""
        assert format_number(None) == "--"
        assert format_number(3.14159) == "3.14"
        assert format_number(3.14159, decimals=3) == "3.142"


class TestSafeConversion:
    """安全转换函数测试"""
    
    def test_safe_float(self):
        """测试安全浮点数转换"""
        assert safe_float(10) == 10.0
        assert safe_float("3.14") == 3.14
        assert safe_float("invalid") == 0.0
        assert safe_float(None, default=-1.0) == -1.0
    
    def test_safe_int(self):
        """测试安全整数转换"""
        assert safe_int(10.5) == 10
        assert safe_int("20") == 20
        assert safe_int("invalid") == 0
        assert safe_int(None, default=-1) == -1


class TestTimeFunctions:
    """时间函数测试"""
    
    def test_time_to_minutes(self):
        """测试时间转换为分钟数"""
        assert time_to_minutes("09:30") == 570  # 9*60 + 30
        assert time_to_minutes("15:00") == 900  # 15*60 + 0
        assert time_to_minutes("00:00") == 0
        assert time_to_minutes("23:59") == 1439
    
    def test_time_to_minutes_invalid(self):
        """测试无效时间格式"""
        with pytest.raises(ValueError):
            time_to_minutes("invalid")
        with pytest.raises(ValueError):
            time_to_minutes("25:00")
    
    def test_is_in_time_range(self):
        """测试时间范围判断"""
        # 上午交易时段
        morning_start = datetime(2024, 1, 1, 10, 0)  # 10:00
        assert is_in_time_range(morning_start, "09:30", "11:30") is True
        
        # 午休时段
        lunch_time = datetime(2024, 1, 1, 12, 0)  # 12:00
        assert is_in_time_range(lunch_time, "09:30", "11:30") is False
        
        # 下午交易时段
        afternoon = datetime(2024, 1, 1, 14, 0)  # 14:00
        assert is_in_time_range(afternoon, "13:00", "15:00") is True
