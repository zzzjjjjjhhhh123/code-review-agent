# -*- coding: utf-8 -*-
"""轻量级自测用例（仅依赖标准库 unittest）。

运行方式：
    python -m unittest discover -s tests -v

重点验证：
1. 工具在异常输入下不崩溃；
2. JSON 解析的健壮性；
3. Agent 主循环的「推理 -> 工具 -> 再推理 -> 最终报告」闭环（用假 LLM 驱动）。
"""

import json
import os
import sys
import unittest

# 让测试可以直接导入项目根目录下的包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.loop import CodeReviewAgent  # noqa: E402
from agent.tools import ast_parse, execute_tool  # noqa: E402


class FakeLLM:
    """按顺序返回预设回复的假 LLM，用于离线测试主循环。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.api_key = "fake"  # 避免被误判为未配置

    def chat(self, messages, **kwargs):
        self.calls += 1
        if not self.responses:
            return json.dumps({"action": "final", "report": {"summary": "x"}})
        return self.responses.pop(0)


class TestTools(unittest.TestCase):
    def test_ast_parse_syntax_error_is_caught(self):
        result = ast_parse("def broken(:")
        self.assertEqual(result.get("error"), "SyntaxError")
        self.assertIn("lineno", result)

    def test_ast_parse_normal(self):
        result = ast_parse("import os\n\ndef f(a, b=1):\n    return a + b\n")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["summary"]["num_functions"], 1)
        self.assertEqual(result["functions"][0]["name"], "f")

    def test_execute_unknown_tool_returns_error(self):
        env = execute_tool("no_such_tool", {}, "code")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"], "UnknownTool")

    def test_read_file_missing(self):
        env = execute_tool("read_file", {"path": "not_exist_1234.py"}, "")
        self.assertFalse(env["ok"])


class TestJsonExtraction(unittest.TestCase):
    def test_plain_json(self):
        data = CodeReviewAgent._extract_json('{"action": "final"}')
        self.assertEqual(data["action"], "final")

    def test_fenced_json(self):
        text = '```json\n{"action": "tool_call", "tool": "ast_parse"}\n```'
        data = CodeReviewAgent._extract_json(text)
        self.assertEqual(data["tool"], "ast_parse")

    def test_json_with_prose(self):
        text = '好的，这是我的决定：{"action": "final", "report": {}} 完毕。'
        data = CodeReviewAgent._extract_json(text)
        self.assertEqual(data["action"], "final")

    def test_invalid(self):
        self.assertIsNone(CodeReviewAgent._extract_json("这不是 JSON"))


class TestAgentLoop(unittest.TestCase):
    def test_empty_input(self):
        agent = CodeReviewAgent(offline=True)
        report = agent.review("   ")
        self.assertEqual(report["issues"], [])
        self.assertIn("空", report["summary"])

    def test_loop_closes_with_tools_then_final(self):
        responses = [
            json.dumps(
                {
                    "thought": "先看语法结构",
                    "action": "tool_call",
                    "tool": "ast_parse",
                    "args": {},
                }
            ),
            json.dumps(
                {
                    "thought": "再算复杂度",
                    "action": "tool_call",
                    "tool": "radon_complexity",
                    "args": {},
                }
            ),
            json.dumps(
                {
                    "thought": "信息足够",
                    "action": "final",
                    "report": {
                        "summary": "整体尚可",
                        "overall_score": 80,
                        "issues": [
                            {
                                "line": 2,
                                "severity": "high",
                                "category": "bug",
                                "title": "除零风险",
                                "description": "可能除零",
                                "suggestion": "先判断分母",
                            }
                        ],
                        "strengths": ["命名清晰"],
                        "tool_summary": "复杂度低",
                    },
                }
            ),
        ]
        llm = FakeLLM(responses)
        agent = CodeReviewAgent(llm_client=llm, max_iterations=6)
        report = agent.review("def f(a, b):\n    return a / b\n", "demo.py")

        self.assertEqual(llm.calls, 3)
        self.assertEqual(report["overall_score"], 80)
        self.assertEqual(report["issues"][0]["severity"], "high")
        # 轨迹中应包含两次工具调用与一次最终输出
        tools_used = [t.get("tool") for t in agent.trace if t["step"] == "tool"]
        self.assertEqual(tools_used, ["ast_parse", "radon_complexity"])

    def test_invalid_json_then_final(self):
        responses = [
            "抱歉，我不能输出 JSON",  # 非法输出，应被纠正
            json.dumps({"action": "final", "report": {"summary": "ok"}}),
        ]
        llm = FakeLLM(responses)
        agent = CodeReviewAgent(llm_client=llm, max_iterations=4)
        report = agent.review("x = 1\n", "demo.py")
        self.assertEqual(report["summary"], "ok")
        self.assertEqual(llm.calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
