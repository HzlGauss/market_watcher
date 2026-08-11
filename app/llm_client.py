"""
统一LLM客户端 —— 封装DeepSeek API调用，消除重复代码
"""

from __future__ import annotations
import json
import os
from typing import Optional, Dict, Any, List
from enum import Enum

from app.http_client import llm_client as http_client
from app.config import Config
from app.utils import log


class LLMModel(Enum):
    """支持的LLM模型"""
    DEEPSEEK_CHAT = "deepseek-chat"
    DEEPSEEK_CODER = "deepseek-coder"


class ToolCall:
    """LLM 返回的工具调用"""
    def __init__(self, call_id: str, name: str, arguments: dict):
        self.id = call_id
        self.name = name
        self.arguments = arguments


class ChatResponse:
    """LLM 对话返回"""
    def __init__(self, content: str = "", tool_calls: list[ToolCall] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient:
    """统一LLM客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        base_url: Optional[str] = None,
        verify_ssl: bool = True,
    ):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._model = model
        self._base_url = base_url or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
        self._verify_ssl = verify_ssl
        self._enabled = bool(self._api_key) and bool(self._base_url)

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

    def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 500,
        temperature: float = 0.3,
        stop: Optional[List[str]] = None,
        timeout: int = 60,
    ) -> Optional[str]:
        """
        调用LLM进行对话

        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词（角色设定）
            max_tokens: 最大生成token数
            temperature: 温度系数，越低越确定性
            stop: 停止词列表
            timeout: 请求超时时间（秒）

        Returns:
            LLM返回的内容，失败返回None
        """
        if not self._enabled:
            log.warning("LLM未启用（缺少API Key）")
            return None

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        return self._call_api(messages, max_tokens, temperature, stop, timeout)

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1500,
        temperature: float = 0.3,
        timeout: int = 120,
        tool_choice: str = "auto",
    ) -> ChatResponse:
        """调用 LLM 进行带工具的对话（支持 function calling）

        Args:
            messages: 完整对话历史 [{"role": "system/user/assistant/tool", "content": ...}]
            tools: OpenAI 兼容的 tools 定义列表
            max_tokens: 最大生成 token 数
            temperature: 温度系数
            timeout: 请求超时时间（秒）
            tool_choice: "auto" / "none" / "required"

        Returns:
            ChatResponse (content + tool_calls)
        """
        if not self._enabled:
            log.warning("LLM未启用（缺少API Key）")
            return ChatResponse(content="LLM 未启用")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        try:
            resp = http_client.post(
                "/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
                verify=self._verify_ssl,
            )
            if resp and resp.status_code == 200:
                result = resp.json()
                choice = result["choices"][0]
                msg = choice.get("message", {})

                content = (msg.get("content") or "").strip()
                tool_calls_raw = msg.get("tool_calls", [])

                tool_calls: list[ToolCall] = []
                for tc in tool_calls_raw:
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}
                    tool_calls.append(ToolCall(
                        call_id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=args,
                    ))

                if tool_calls:
                    names = [tc.name for tc in tool_calls]
                    log.info(f"LLM tool_calls: {names}")

                return ChatResponse(content=content, tool_calls=tool_calls)
            else:
                status = resp.status_code if resp else "None"
                log.warning(f"LLM tool_call 请求失败 (HTTP {status})")
                return ChatResponse(content=f"API 请求失败: HTTP {status}")
        except Exception as e:
            log.warning(f"LLM tool_call 异常: {e}")
            return ChatResponse(content=f"调用异常: {e}")

    def _call_api(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
        stop: Optional[List[str]] = None,
        timeout: int = 60,
    ) -> Optional[str]:
        """底层 API 调用"""
        # 打印请求提示词以便调试
        log.info(f"--- LLM请求 [model={self._model}, max_tokens={max_tokens}, temp={temperature}] ---")
        log.info("--- LLM请求结束 ---")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = stop

        try:
            resp = http_client.post(
                "/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
                verify=self._verify_ssl,
            )
            if resp and resp.status_code == 200:
                result = resp.json()
                content = result["choices"][0]["message"]["content"]
                return content.strip() if content else None
            else:
                status = resp.status_code if resp else "None"
                log.warning(f"LLM请求失败 (HTTP {status})")
                return None
        except Exception as e:
            log.warning(f"LLM调用异常: {e}")
            return None


# 全局默认LLM客户端实例
_default_llm_client: Optional[LLMClient] = None


def get_llm_client(config: Optional[Config] = None) -> LLMClient:
    """
    获取全局LLM客户端实例

    Args:
        config: 配置对象，可选，用于获取模型名称和自定义配置

    Returns:
        LLMClient实例
    """
    global _default_llm_client

    if _default_llm_client is None:
        if config:
            model = config.llm_model
            base_url = config.llm_base_url
            verify_ssl = config.llm_verify_ssl
            api_key = config.llm_api_key
        else:
            model = "deepseek-chat"
            base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
            verify_ssl = os.environ.get("LLM_VERIFY_SSL", "true").lower() != "false"
            api_key = None
        _default_llm_client = LLMClient(
            model=model,
            base_url=base_url,
            verify_ssl=verify_ssl,
            api_key=api_key,
        )

    return _default_llm_client


# 预设的系统提示词
SYSTEM_PROMPTS = {
    "analyst": (
        "你是一位在买方机构（公募私募）从业 8 年的 A 股盘面分析师。"
        "你的分析框架：首先确认量价关系（是否有量能支撑），然后判断资金属性（主力/散户/北向），最后看技术指标位置。"
        "你倾向于右侧交易思维，不做左侧预测。"
        "回答风格：先用一句话定性盘面，再分点展开推理过程。"
    ),

    "morning_brief": (
        "你是一位拥有 15 年以上经验的 A 股首席策略分析师，曾任职于头部券商研究所。"
        "你的分析框架：隔夜外盘 → 消息面过滤 → A 股联动逻辑 → 技术位参考（支撑/压力）→ 持仓应对。"
        "特别擅长从外盘变化推导 A 股的传导路径和程度。"
        "回答风格：先给今日核心判断（1-2 句），再按结构展开。关键判断须标注置信度。"
    ),

    "midday_review": (
        "你是一位专注盘中交易的资深策略分析师，擅长在上午盘面信号中捕捉下午的变盘线索。"
        "你的分析框架：上午量价特征（放量/缩量/震荡/突破）→ 热点持续性判断 → 北向资金态度 → 下午走势推演。"
        "特别注意识别\"诱多\"和\"假摔\"信号。"
        "回答风格：先定性上午走势类型，再给出下午两种可能情景及概率。"
    ),

    "evening_review": (
        "你是一位以绝对收益为目标的资深投资经理，管理过 10 亿以上资金，"
        "擅长从持仓管理和风险控制角度做日度复盘。"
        "你的分析框架：当日盈亏归因（β 还是 α）→ 每只持仓的资金行为分析 → 技术状态评估 → 次日多情景预案。"
        "对每只持仓必须给出明确的处理逻辑，不给出模糊的\"继续持有\"建议。"
        "回答风格：辛辣直接，每一个操作建议都必须同时给出触发条件和失效条件。"
    ),

    "fund_expert": (
        "你是一位拥有 15 年经验的基金研究专家，曾任晨星（Morningstar）高级分析师。"
        "你熟悉中国公募基金行业的所有明星基金经理。"
        "你的分析框架：定量指标（夏普/回撤/Alpha）→ 持仓风格穿透 → 规模变动分析 → 经理能力圈判断。"
        "特别关注基金的\"风格漂移\"风险——即基金实际持仓是否偏离了其招募说明书宣称的风格。"
        "回答风格：客观、数据驱动，对每只基金的判断须标注\"数据支持度\"（强/中/弱）。"
    ),

    "dragon_tiger_analyst": (
        "你是一位专注于A股龙虎榜数据的短线分析师，有10年以上游资席位追踪经验。"
        "你精通识别机构、游资、量化、散户四类资金的博弈格局，能从席位买卖对比中推断资金真实意图。"
        "你的分析框架：全市场多空全景 → 重点个股席位博弈拆解 → 板块轮动推演 → 次日操作预判。"
        "关键能力：能区分\"机构主导拉升\"与\"游资一日游\"；能识别\"涨停板诱多出货\"与\"放量烂板\"；能从板块内部分化推断资金切换方向。"
        "回答风格：犀利直接，每个判断给出次日可验证标准，不做模棱两可的结论。"
    ),
}
