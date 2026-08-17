"""
Agent 循环引擎 —— LLM 按需调用工具函数进行多轮推理

核心思路：LLM 不是一次性回答问题，而是像人类分析师一样：
  1. 先看总览数据
  2. 发现问题 → 调用工具深入查询
  3. 根据查询结果 → 验证假设 or 继续深挖
  4. 最终输出结构化结论

用于龙虎榜深度分析等需要多维度交叉验证的场景。
"""

from __future__ import annotations
import json
from typing import Callable, Optional

from app.llm_client import get_llm_client, ChatResponse
from app.utils import log


# ---- Agent 系统提示词 ----

AGENT_SYSTEM_PROMPT = """你是一位专业的A股龙虎榜分析师，拥有调用以下工具函数的能力，可以主动查询你需要的数据。

**工作流程**：
1. 先仔细阅读用户提供的初始数据（龙虎榜汇总 + 席位报告）。
2. 发现信息缺口或值得深挖的线索时，调用工具函数查询。
3. 根据返回结果验证你的假设。
4. 最多进行3轮工具调用，然后给出最终结论。

**重要规则**：
- 不要重复查询同一只股票的同一信息。
- 每次只查询2-4只最关键的股票（通过工具参数传入多个code）。
- 如果工具返回空或错误，接受结果，不要反复重试。
- 最终结论必须基于实际查询到的数据，不要编造。

**最终输出格式**（Markdown）：

### 🔍 Agent 深度挖掘
> *以下是 AI 通过多轮工具查询的深度分析*

#### 查询概览
- 本轮Agent查询了 X 只股票、Y 个维度
- 发现 Z 个值得关注的关键线索

#### 关键发现
[每只深挖的股票输出:]
- **股票名称(代码)**: 核心发现 + 置信度[高/中/低]

#### 综合研判
- 基于汇总数据+工具查询结果的整体判断
"""


class AgentTool:
    """Agent 可调用的工具"""
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        parameters: dict | None = None,
    ):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters or {
            "type": "object",
            "properties": {},
            "required": [],
        }


def build_tool_definition(tool: AgentTool) -> dict:
    """构建 OpenAI 兼容的 tool definition"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


class LLMAgent:
    """轻量 Agent 循环引擎

    用法:
        agent = LLMAgent(config, tools=[tool1, tool2])
        result = agent.run(initial_prompt)
    """

    MAX_TOOL_CALLS = 3       # 最大工具调用轮次
    MAX_FINAL_TOKENS = 2000  # 最终结论的 max_tokens

    def __init__(self, config, tools: list[AgentTool]):
        self.llm = get_llm_client(config)
        self.tools: dict[str, AgentTool] = {t.name: t for t in tools}
        self.tool_defs = [build_tool_definition(t) for t in tools]
        self.history: list[dict] = []
        self.call_count = 0

    def run(self, initial_prompt: str) -> str:
        """执行 Agent 循环，返回最终结论

        Args:
            initial_prompt: 初始数据 + 分析要求

        Returns:
            LLM 最终分析结论（Markdown 格式）
        """
        if not self.llm.enabled:
            return "*Agent 未启用（缺少 API Key）*"

        # 初始化对话历史
        self.history = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": initial_prompt},
        ]
        self.call_count = 0

        while self.call_count < self.MAX_TOOL_CALLS:
            response = self.llm.chat_with_tools(
                messages=self.history,
                tools=self.tool_defs,
                max_tokens=1500,
                temperature=0.3,
                timeout=120,
            )

            if response.has_tool_calls:
                self.history.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                        }
                        for tc in response.tool_calls
                    ],
                })

                # 执行工具调用
                for tc in response.tool_calls:
                    tool_result = self._execute_tool(tc)
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    })

                self.call_count += 1
                log.info(f"Agent: 第{self.call_count}轮工具调用完成")
            else:
                # LLM 认为推理完毕
                log.info(f"Agent: LLM 完成推理（{self.call_count}轮工具调用）")
                return response.content or "*Agent 未返回内容*"

        # 达到最大轮次，要求总结
        log.info(f"Agent: 达到最大轮次({self.MAX_TOOL_CALLS})，强制总结")
        return self._force_conclude()

    def _execute_tool(self, tc) -> str:
        """执行单个工具调用，返回 JSON 字符串"""
        tool = self.tools.get(tc.name)
        if not tool:
            return json.dumps({"error": f"未知工具: {tc.name}"})

        try:
            result = tool.func(**tc.arguments)
            if result is None:
                return json.dumps({"data": None, "message": "工具返回空（可能无数据）"})
            # 确保返回值是 JSON 可序列化的
            return json.dumps({"data": result}, ensure_ascii=False, default=str)
        except Exception as e:
            log.warning(f"Agent工具执行失败 [{tc.name}]: {e}")
            return json.dumps({"error": str(e)})

    def _force_conclude(self) -> str:
        """达到最大轮次，强制 LLM 基于对话历史输出结论"""
        self.history.append({
            "role": "user",
            "content": (
                "已达到最大查询轮次。请基于以上所有数据（初始数据 + 各轮工具查询结果），"
                "按照 Agent 深度挖掘的格式输出最终分析结论。不要继续请求工具调用。"
            ),
        })

        response = self.llm.chat_with_tools(
            messages=self.history,
            tools=[],  # 不给工具，强制输出文本
            max_tokens=self.MAX_FINAL_TOKENS,
            temperature=0.3,
            timeout=120,
        )
        return response.content or "*Agent 总结失败*"
