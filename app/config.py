"""
配置加载模块 —— 读取 watchlist_config.json 和环境变量

验证配置合法性，提供类型安全的访问接口。
支持从 CSV 文件读取持仓和标的列表，方便手动维护。
提供配置验证和默认值处理，确保应用健壮性。
"""

from __future__ import annotations
import csv
import json
import os
from pathlib import Path
from typing import Any, Optional

# 尝试导入 dotenv，如果没有安装则不加载 .env
try:
    from dotenv import load_dotenv
    _dotenv_available = True
except ImportError:
    load_dotenv = lambda **kwargs: None  # type: ignore
    _dotenv_available = False

from app.models import WatchItem, Holding, VALID_MARKETS
from app.utils import log, safe_float, safe_int
from app.helpers import validate_watch_item, validate_holding


class ConfigValidationError(Exception):
    """配置验证异常"""
    pass


class Config:
    """
    应用配置类，封装所有可配置项

    提供类型安全的属性访问，自动验证配置有效性，
    并在配置缺失时提供合理的默认值。
    """

    # 默认配置值
    DEFAULT_SCAN_INTERVAL = 15
    DEFAULT_TRADE_ONLY = True
    DEFAULT_LLM_MODEL = "deepseek-chat"
    DEFAULT_NORTH_FLOW_INTERVAL = 30
    DEFAULT_ADJUSTMENT_INTENSITY = 1.5
    DEFAULT_SECTOR_THRESHOLD = 2.0
    DEFAULT_SCREENING_CONDITIONS = 5
    DEFAULT_SCREENING_CANDIDATE_LIMIT = 30
    DEFAULT_SCREENING_FUND_FLOW_DAYS = 10
    DEFAULT_SCREENING_BLACKLIST = ["房地产", "地产", "保险"]
    DEFAULT_SCREENING_BLACKLIST_CONCEPTS = []
    DEFAULT_SCREENING_EXCLUDE_ST = True
    DEFAULT_SCREENING_SUB_NEW_DAYS = 60
    DEFAULT_STRONG_SECTOR_COUNT = 10   # 资金强势选股：热门板块数
    DEFAULT_STRONG_STOCK_PCT = 20      # 资金强势选股：每板块内个股前 N%
    DEFAULT_STRONG_STOCK_MIN = 3       # 资金强势选股：每板块最少个股数
    DEFAULT_STRONG_FLOW_PERIOD = "10日"  # 资金强势选股：资金流入周期
    DEFAULT_STRONG_CANDIDATE_LIMIT = 30  # 资金强势选股：候选总数上限
    DEFAULT_FLOW_REVERSAL_MIN = 1e7  # 资金流转向最小净额（元），默认 1000 万
    DEFAULT_FLOW_DIVERGE_PCT = 2.0   # 资金背离价格阈值（%）
    DEFAULT_FLOW_REVERSAL_PCT = 2.0  # 资金流转向最小占比（%），主力净流入占成交额低于此值不计转向
    DEFAULT_FLOW_LONG_WINDOW = 20    # 纵向异动历史窗口（交易日数）
    DEFAULT_FLOW_LONG_Z = 2.0        # 纵向异动 z-score 阈值（偏离自身均值多少个标准差）
    DEFAULT_FLOW_LONG_MIN_DAYS = 5   # 纵向异动最少历史天数（冷启动保护）

    def __init__(self, config_path: Path) -> None:
        """
        初始化配置

        Args:
            config_path: 配置文件路径
        """
        # 确保 config_path 是 Path 对象
        if isinstance(config_path, str):
            config_path = Path(config_path)

        self.config_path = config_path

        # 加载 .env 文件
        env_path = config_path.parent / ".env"
        if env_path.exists():
            if _dotenv_available:
                load_dotenv(dotenv_path=env_path, override=True)
                log.debug(f"Loaded environment variables from {env_path}")
            else:
                log.warning(f".env file exists but python-dotenv is not installed. "
                            f"Please install it with: pip install python-dotenv")

        # CSV 文件路径（需要在_validate 之前设置）
        self._holdings_csv = config_path.parent / "holdings.csv"
        self._watchlist_csv = config_path.parent / "watchlist.csv"

        self._raw: dict[str, Any] = self._load(config_path)
        self._validate()

    # ---- 加载 ----

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        """
        加载 JSON 配置文件

        Args:
            path: 配置文件路径

        Returns:
            配置字典

        Raises:
            FileNotFoundError: 配置文件不存在
            json.JSONDecodeError: JSON 格式错误
        """
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            with open(str(path), "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            log.error(f"Invalid JSON in config file: {e}")
            raise

    def _validate(self) -> None:
        """
        验证配置有效性

        Raises:
            ConfigValidationError: 配置验证失败
        """
        errors = []
        warnings = []

        # 检查标的列表
        has_items = bool(self._raw.get("标的列表"))
        has_csv = self._watchlist_csv.exists()
        if not has_items and not has_csv:
            errors.append("标的列表为空！请检查 watchlist_config.json 或 watchlist.csv")

        # 检查提醒阈值
        thresholds = self._raw.get("提醒阈值", {})
        if not thresholds:
            warnings.append("提醒阈值未配置，将使用默认值")

        # 检查 API Key
        if self._raw.get("大模型分析", {}).get("启用", False) and not self.deepseek_key:
            warnings.append("大模型分析已启用但未配置 DEEPSEEK_API_KEY")

        if self._raw.get("推送通知", {}).get("启用", False) and not self.sct_sendkey:
            warnings.append("推送通知已启用但未配置 SCT_SENDKEY")

        # 输出警告
        for warning in warnings:
            log.warning(warning)

        # 抛出错误
        if errors:
            raise ConfigValidationError("\n".join(errors))

    @staticmethod
    def _load_csv(filepath: Path) -> list[dict]:
        """
        从 CSV 文件加载数据

        Args:
            filepath: CSV 文件路径

        Returns:
            数据字典列表
        """
        result = []
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    row_num = 1  # 从 1 开始，因为标题行是第 1 行
                    for row in reader:
                        row_num += 1

                        # 确保 code 字段不为空
                        code_value = row.get("code", "").strip()
                        if not code_value:
                            log.warning(f"Skipping row {row_num} in {filepath.name}: code field is empty")
                            continue

                        # 处理空字段，设置为空字符串
                        for key, value in row.items():
                            if value is None:
                                row[key] = ""
                            elif isinstance(value, str) and value.strip() == "":
                                row[key] = ""

                        # 只转换特定的数字字段（amount, cost 等），字符串字段保持原样
                        numeric_fields = ["amount", "cost", "quantity", "price"]
                        for key in numeric_fields:
                            if key in row and row[key]:
                                try:
                                    if "." in str(row[key]):
                                        row[key] = safe_float(row[key])
                                    else:
                                        row[key] = safe_int(row[key])
                                except (ValueError, TypeError):
                                    pass

                        result.append(row)
                log.info(f"Loaded {len(result)} items from {filepath.name}")
            except Exception as e:
                log.warning(f"Failed to load {filepath.name}: {e}")
        return result

    # ---- 通用访问 ----

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项

        Args:
            key: 配置键
            default: 默认值

        Returns:
            配置值
        """
        return self._raw.get(key, default)

    # ---- 盯盘设置 ----

    @property
    def scan_interval(self) -> int:
        """扫描间隔（分钟）"""
        value = self._raw.get("盯盘设置", {}).get("扫描间隔分钟", self.DEFAULT_SCAN_INTERVAL)
        return max(1, safe_int(value, self.DEFAULT_SCAN_INTERVAL))

    @property
    def trade_only(self) -> bool:
        """是否仅在交易时段运行"""
        return self._raw.get("盯盘设置", {}).get("仅交易时段运行", self.DEFAULT_TRADE_ONLY)

    @property
    def sessions(self) -> dict[str, list[str]]:
        """A股交易时段配置"""
        return self._raw.get("盯盘设置", {}).get("A股交易时段", {
            "上午": ["09:30", "11:30"],
            "下午": ["13:00", "15:30"]
        })

    # ---- 动态阈值 ----

    @property
    def dynamic_threshold_enabled(self) -> bool:
        """是否启用动态阈值"""
        return self._raw.get("盯盘设置", {}).get("动态阈值", {}).get("启用", False)

    @property
    def adjustment_intensity(self) -> float:
        """情绪调整强度"""
        value = self._raw.get("盯盘设置", {}).get("动态阈值", {}).get(
            "情绪调整强度", self.DEFAULT_ADJUSTMENT_INTENSITY
        )
        return max(0.0, safe_float(value, self.DEFAULT_ADJUSTMENT_INTENSITY))

    @property
    def sector_threshold(self) -> float:
        """板块异动阈值"""
        value = self._raw.get("盯盘设置", {}).get("动态阈值", {}).get(
            "板块异动阈值", self.DEFAULT_SECTOR_THRESHOLD
        )
        return max(0.0, safe_float(value, self.DEFAULT_SECTOR_THRESHOLD))

    # ---- 基础阈值 ----

    @property
    def thresholds(self) -> dict[str, float]:
        """基础提醒阈值"""
        return dict(self._raw.get("提醒阈值", {
            "涨幅预警": 3.0,
            "涨幅关注": 2.0,
            "跌幅预警": -2.5,
            "跌幅关注": -1.5,
            "振幅预警": 5.0,
        }))

    @property
    def flow_reversal_min(self) -> float:
        """资金流转向最小净额（元），低于此值不计入主力/总资金转向提醒"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金流转向最小净额", self.DEFAULT_FLOW_REVERSAL_MIN
        )
        return max(0.0, safe_float(value, self.DEFAULT_FLOW_REVERSAL_MIN))

    @property
    def flow_diverge_pct(self) -> float:
        """资金背离价格阈值（%），涨跌幅超过此幅度才检查主力资金背离"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金背离价格阈值", self.DEFAULT_FLOW_DIVERGE_PCT
        )
        return max(0.0, safe_float(value, self.DEFAULT_FLOW_DIVERGE_PCT))

    @property
    def flow_reversal_pct(self) -> float:
        """资金流转向最小占比（%），主力净流入占成交额低于此值不计转向"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金流转向最小占比", self.DEFAULT_FLOW_REVERSAL_PCT
        )
        return max(0.0, safe_float(value, self.DEFAULT_FLOW_REVERSAL_PCT))

    @property
    def flow_longitudinal_window(self) -> int:
        """纵向异动历史窗口（交易日数），用于计算自身常态基线"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金纵向窗口", self.DEFAULT_FLOW_LONG_WINDOW
        )
        return max(1, int(safe_float(value, self.DEFAULT_FLOW_LONG_WINDOW)))

    @property
    def flow_longitudinal_z(self) -> float:
        """纵向异动 z-score 阈值，今日占比偏离自身均值多少个标准差算异动"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金纵向Z阈值", self.DEFAULT_FLOW_LONG_Z
        )
        return max(0.0, safe_float(value, self.DEFAULT_FLOW_LONG_Z))

    @property
    def flow_longitudinal_min_days(self) -> int:
        """纵向异动最少历史天数，历史不足则不触发（冷启动回退方案A占比阈值）"""
        value = self._raw.get("提醒阈值", {}).get(
            "资金纵向最少天数", self.DEFAULT_FLOW_LONG_MIN_DAYS
        )
        return max(1, int(safe_float(value, self.DEFAULT_FLOW_LONG_MIN_DAYS)))

    # ---- 标的列表 ----

    @property
    def watch_items(self) -> list[WatchItem]:
        """
        获取盯盘标的列表

        Returns:
            WatchItem 列表
        """
        # 优先从 CSV 文件读取
        csv_items = self._load_csv(self._watchlist_csv)
        if csv_items:
            items = []
            for item in csv_items:
                validated = validate_watch_item(item, self._watchlist_csv.name)
                if validated:
                    items.append(validated)
            return items

        # 如果 CSV 文件不存在，从 JSON 读取（保持向后兼容）
        items = []
        for item in self._raw.get("标的列表", []):
            validated = validate_watch_item(item, "watchlist_config.json")
            if validated:
                items.append(validated)
        return items

    # ---- 大模型分析 ----

    @property
    def llm_enabled(self) -> bool:
        """是否启用大模型分析"""
        return self._raw.get("大模型分析", {}).get("启用", False)

    @property
    def llm_trigger(self) -> str:
        """大模型分析触发时机"""
        return self._raw.get("大模型分析", {}).get("分析时机", "仅异动时")

    @property
    def llm_model(self) -> str:
        """大模型模型名称（JSON 模型 > 环境变量 LLM_MODEL > 默认）"""
        return self._raw.get("大模型分析", {}).get(
            "模型", os.environ.get("LLM_MODEL", self.DEFAULT_LLM_MODEL)
        )

    @property
    def llm_base_url(self) -> str:
        """大模型请求地址（支持本地部署）"""
        return self._raw.get("大模型分析", {}).get(
            "请求地址",
            os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
        )

    @property
    def llm_api_key(self) -> str:
        """大模型 API Key（优先从配置读取，回退到环境变量）"""
        return self._raw.get("大模型分析", {}).get(
            "API密钥",
            os.environ.get("DEEPSEEK_API_KEY", "")
        )

    @property
    def llm_verify_ssl(self) -> bool:
        """是否验证 SSL 证书（本地部署通常关闭）"""
        env_val = os.environ.get("LLM_VERIFY_SSL", "true").lower()
        return self._raw.get("大模型分析", {}).get("验证SSL", env_val != "false")

    @property
    def dragon_tiger_llm_enabled(self) -> bool:
        """龙虎榜 LLM 分析独立开关（不依赖全局 llm_enabled）"""
        return self._raw.get("龙虎榜AI分析", {}).get("启用", False)

    # ---- 推送 ----

    @property
    def push_enabled(self) -> bool:
        """是否启用推送通知"""
        return self._raw.get("推送通知", {}).get("启用", False)

    @property
    def push_trigger(self) -> str:
        """推送触发时机"""
        return self._raw.get("推送通知", {}).get("推送时机", "仅异动时")

    @property
    def push_include_llm(self) -> bool:
        """推送是否包含 AI 研判"""
        return self._raw.get("推送通知", {}).get("包含 AI 研判", True)

    # ---- 北向资金 ----

    @property
    def north_flow_enabled(self) -> bool:
        """是否启用北向资金监控"""
        return self._raw.get("北向资金", {}).get("启用", False)

    @property
    def north_flow_interval(self) -> int:
        """北向资金更新间隔（分钟）"""
        value = self._raw.get("北向资金", {}).get("更新间隔分钟", self.DEFAULT_NORTH_FLOW_INTERVAL)
        return max(1, safe_int(value, self.DEFAULT_NORTH_FLOW_INTERVAL))

    # ---- 做T监控 ----

    @property
    def t0_enabled(self) -> bool:
        """是否启用做T监控"""
        return self._raw.get("做T监控", {}).get("启用", False)

    @property
    def t0_push_enabled(self) -> bool:
        """是否启用做T信号微信推送"""
        return self._raw.get("做T监控", {}).get("推送微信", False)

    @property
    def t0_interval(self) -> int:
        """做T监控扫描间隔（秒）"""
        value = self._raw.get("做T监控", {}).get("扫描间隔秒", 30)
        return max(10, safe_int(value, 30))

    # ---- 智能选股 ----

    @property
    def screening_condition_count(self) -> int:
        """智能选股：LLM 生成的选股条件数（≈热点板块数）"""
        value = self._raw.get("智能选股", {}).get(
            "条件数", self.DEFAULT_SCREENING_CONDITIONS
        )
        return max(1, safe_int(value, self.DEFAULT_SCREENING_CONDITIONS))

    @property
    def screening_candidate_limit(self) -> int:
        """智能选股：候选池上限（资金流评分/LLM 排序前截断）"""
        value = self._raw.get("智能选股", {}).get(
            "候选数上限", self.DEFAULT_SCREENING_CANDIDATE_LIMIT
        )
        return max(5, safe_int(value, self.DEFAULT_SCREENING_CANDIDATE_LIMIT))

    @property
    def screening_fund_flow_days(self) -> int:
        """智能选股：资金流回看天数（历史主力资金流序列，保留以备扩展）"""
        value = self._raw.get("智能选股", {}).get(
            "资金流天数", self.DEFAULT_SCREENING_FUND_FLOW_DAYS
        )
        return max(5, safe_int(value, self.DEFAULT_SCREENING_FUND_FLOW_DAYS))

    @property
    def screening_blacklist_sectors(self) -> list[str]:
        """智能选股板块黑名单（排除不看的板块，如房地产/保险）"""
        raw = self._raw.get("智能选股", {}).get(
            "黑名单板块", self.DEFAULT_SCREENING_BLACKLIST
        )
        if not isinstance(raw, list):
            raw = [raw]
        return [str(s).strip() for s in raw if str(s).strip()]

    @property
    def screening_blacklist_concepts(self) -> list[str]:
        """智能选股概念黑名单（按概念题材排除，如「壳资源」「ST板块」，可留空）"""
        raw = self._raw.get("智能选股", {}).get(
            "黑名单概念", self.DEFAULT_SCREENING_BLACKLIST_CONCEPTS
        )
        if not isinstance(raw, list):
            raw = [raw]
        return [str(s).strip() for s in raw if str(s).strip()]

    @property
    def screening_exclude_st(self) -> bool:
        """智能选股：是否剔除 ST/*ST/退市整理等风险股"""
        return bool(self._raw.get("智能选股", {}).get(
            "排除ST", self.DEFAULT_SCREENING_EXCLUDE_ST
        ))

    @property
    def screening_sub_new_days(self) -> int:
        """智能选股：上市不满 N 个自然日视为新股/次新股剔除（0=不过滤）"""
        value = self._raw.get("智能选股", {}).get(
            "次新股天数", self.DEFAULT_SCREENING_SUB_NEW_DAYS
        )
        return max(0, safe_int(value, self.DEFAULT_SCREENING_SUB_NEW_DAYS))

    # ---- 资金强势选股 ----

    @property
    def strong_sector_count(self) -> int:
        """资金强势选股：热门板块数（主力净流入最多的前 K 个板块）"""
        value = self._raw.get("强势选股", {}).get(
            "热门板块数", self.DEFAULT_STRONG_SECTOR_COUNT
        )
        return max(1, safe_int(value, self.DEFAULT_STRONG_SECTOR_COUNT))

    @property
    def strong_stock_pct(self) -> int:
        """资金强势选股：每板块内取资金流入前 N% 个股（1-100）"""
        value = self._raw.get("强势选股", {}).get(
            "个股比例", self.DEFAULT_STRONG_STOCK_PCT
        )
        return max(1, min(100, safe_int(value, self.DEFAULT_STRONG_STOCK_PCT)))

    @property
    def strong_stock_min(self) -> int:
        """资金强势选股：每板块最少个股数（不足则全选）"""
        value = self._raw.get("强势选股", {}).get(
            "每板块最少个股", self.DEFAULT_STRONG_STOCK_MIN
        )
        return max(1, safe_int(value, self.DEFAULT_STRONG_STOCK_MIN))

    @property
    def strong_flow_period(self) -> str:
        """资金强势选股：资金流入周期（今日/5日/10日）"""
        value = str(self._raw.get("强势选股", {}).get(
            "资金周期", self.DEFAULT_STRONG_FLOW_PERIOD
        )).strip()
        return value if value in ("今日", "5日", "10日") else self.DEFAULT_STRONG_FLOW_PERIOD

    @property
    def strong_candidate_limit(self) -> int:
        """资金强势选股：候选总数上限（逐股深查前截断）"""
        value = self._raw.get("强势选股", {}).get(
            "候选数上限", self.DEFAULT_STRONG_CANDIDATE_LIMIT
        )
        return max(3, safe_int(value, self.DEFAULT_STRONG_CANDIDATE_LIMIT))

    # ---- Investment Reports ----

    @property
    def report_cfg(self) -> dict:
        """投资报告配置"""
        return self._raw.get("投资报告", {})

    @property
    def report_dir(self) -> str:
        """报告保存目录"""
        return self.report_cfg.get("保存目录", "investment_reports")

    # ---- 持仓 ----

    @property
    def holdings(self) -> list[Holding]:
        """
        用户持仓列表

        Returns:
            Holding 列表
        """
        # 优先从 CSV 文件读取
        csv_items = self._load_csv(self._holdings_csv)
        if csv_items:
            holdings = []
            for item in csv_items:
                validated = validate_holding(item, self._holdings_csv.name)
                if validated:
                    holdings.append(validated)
            return holdings

        # 如果 CSV 文件不存在，从 JSON 读取（保持向后兼容）
        holdings = []
        for h in self._raw.get("持仓", []):
            validated = validate_holding(h, "holdings_config.json")
            if validated:
                holdings.append(validated)
        return holdings

    # ---- API Key ----

    @property
    def deepseek_key(self) -> Optional[str]:
        """DeepSeek API Key（兼容旧接口，优先用 llm_api_key）"""
        return self.llm_api_key or self._env("DEEPSEEK_API_KEY")

    @property
    def sct_sendkey(self) -> Optional[str]:
        """Server 酱 SendKey"""
        return self._env("SCT_SENDKEY")

    @property
    def mx_apikey(self) -> Optional[str]:
        """东方财富妙想 API Key（基金深度数据用）"""
        return self._env("MX_APIKEY")

    @property
    def mx_apikey_2(self) -> Optional[str]:
        """东方财富妙想 API Key 2（备用，与主 key 轮询使用）"""
        return self._env("MX_APIKEY_2")

    @property
    def mx_apikeys(self) -> list[str]:
        """所有妙想 API Key 列表（主 key + 备用 key，去重去空）"""
        keys = [self.mx_apikey, self.mx_apikey_2]
        return [k for k in keys if k]

    @staticmethod
    def _env(key: str) -> Optional[str]:
        """获取环境变量"""
        return os.environ.get(key)

    def __repr__(self) -> str:
        """配置的字符串表示"""
        return (
            f"Config(scan_interval={self.scan_interval}, "
            f"trade_only={self.trade_only}, "
            f"llm_enabled={self.llm_enabled}, "
            f"push_enabled={self.push_enabled}, "
            f"watch_items={len(self.watch_items)})"
        )
