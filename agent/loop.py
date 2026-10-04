# -*- coding: utf-8 -*-
"""Agent 主循环。

本模块是整个作业的核心，清晰体现了 Agent 设计模式：

    输入代码
      -> 预处理（空输入/非代码/超长/语法预检）
      -> LLM 推理：决定调用哪个工具
      -> 工具执行（ast / radon / pylint / read_file，失败也不崩溃）
      -> 工具结果回填给 LLM
      -> LLM 再推理 ...（闭环，直到 LLM 输出最终报告或达到最大轮数）
      -> 输出结构化审查报告

对外只暴露 `CodeReviewAgent.review()` 一个入口，方便测试与复用。
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from agent import prompts
from agent.tools import TOOLS, describe_tools, execute_tool
from llm.client import LLMClient, LLMError

logger = logging.getLogger(__name__)

# 严重等级排序，用于报告排序
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
# 低于该行数且无任何代码特征时，提示“疑似非代码文本”
_MIN_CODE_CHARS = 3


class CodeReviewAgent:
    """代码审查 Agent：手写 ReAct 风格主循环，不依赖任何重型框架。"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        max_iterations: int = 6,
        max_code_chars: int = 20000,
        offline: bool = False,
    ) -> None:
        self.llm = llm_client
        self.max_iterations = max_iterations
        self.max_code_chars = max_code_chars
        self.offline = offline
        # trace 记录每一步决策，答辩时可逐步回放
        self.trace: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def review(self, code: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """审查一段代码，返回结构化报告 dict。"""
        self.trace = []
        filename = filename or "<pasted-code>"

        # 1) 预处理：空输入 / 超长截断 / 语法预检
        clean_code, precheck, notes = self._preprocess(code)
        self.trace.append(
            {"step": "preprocess", "precheck": precheck, "notes": notes}
        )

        # 2) 空输入直接返回，不浪费一次 LLM 调用
        if not clean_code.strip():
            return self._empty_report()

        # 3) 离线模式：只用本地工具产出结论
        if self.offline or self.llm is None:
            return self._offline_review(clean_code, filename, precheck, notes)

        # 4) 在线模式：跑 LLM 主循环；若 LLM 整体不可用则优雅降级
        try:
            return self._run_llm_loop(
                clean_code, filename, precheck, notes
            )
        except LLMError as exc:
            logger.warning("LLM 不可用，降级为本地工具分析：%s", exc)
            report = self._offline_review(clean_code, filename, precheck, notes)
            report["degraded"] = True
            report["summary"] = (
                "【已降级为本地静态分析】LLM 调用失败：%s。以下结论由 ast/radon/pylint "
                "直接给出，未经过大模型综合。\n\n%s" % (exc, report.get("summary", ""))
            )
            return report

    # ------------------------------------------------------------------ #
    # 预处理
    # ------------------------------------------------------------------ #
    def _preprocess(self, code: str) -> Tuple[str, str, str]:
        """返回 (清洗后的代码, 预检说明, 额外提示)。"""
        if code is None:
            code = ""
        original_len = len(code)

        # 超长代码：保留头部与尾部（通常 Bug 集中在开头定义与结尾逻辑）
        if original_len > self.max_code_chars:
            head = code[: self.max_code_chars // 2]
            tail = code[-self.max_code_chars // 2 :]
            code = (
                head
                + "\n\n# ...【代码过长，中间部分已截断，原始长度 %d 字符】...\n\n"
                % original_len
                + tail
            )
            truncated = True
        else:
            truncated = False

        # 语法预检：提前发现 SyntaxError，作为客观事实喂给 LLM
        syntax_err = None
        num_lines = code.count("\n") + 1
        try:
            ast.parse(code)
            syntax_status = "语法检查通过（ast.parse 成功）"
        except SyntaxError as exc:
            syntax_err = exc
            syntax_status = "语法错误：第 %s 行 %s" % (exc.lineno, exc.msg)
        except Exception as exc:  # noqa: BLE001
            syntax_status = "语法预检失败：%s" % exc

        precheck_parts = [
            "原始字符数=%d，行数=%d" % (original_len, num_lines),
            syntax_status,
        ]
        if truncated:
            precheck_parts.append("代码过长，已截断中间部分")
        precheck = "；".join(precheck_parts)

        notes = ""
        if not self._looks_like_code(code):
            notes = "该输入可能不是 Python 代码，请先判断是否值得审查；若确实不是代码，请如实说明。"

        return code, precheck, notes

    @staticmethod
    def _looks_like_code(text: str) -> bool:
        """粗略判断文本是否像代码，用于提示“非代码文本”边界情况。"""
        if len(text.strip()) < _MIN_CODE_CHARS:
            return False
        indicators = ("def ", "class ", "import ", "return", "=", ":", "(", "{", "[")
        return any(token in text for token in indicators)

    # ------------------------------------------------------------------ #
    # LLM 主循环（推理 -> 工具 -> 再推理）
    # ------------------------------------------------------------------ #
    def _run_llm_loop(
        self, code: str, filename: str, precheck: str, notes: str
    ) -> Dict[str, Any]:
        system_prompt = prompts.AGENT_SYSTEM.substitute(
            tool_list=describe_tools()
        )
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": prompts.INITIAL_USER.substitute(
                    filename=filename,
                    precheck=precheck,
                    notes=notes or "无",
                    code=code,
                ),
            },
        ]

        tool_evidence: List[Dict[str, Any]] = []

        for iteration in range(1, self.max_iterations + 1):
            # --- 推理：请求 LLM 决策 ---
            raw = self.llm.chat(messages)  # type: ignore[union-attr]
            action = self._extract_json(raw)
            self.trace.append(
                {
                    "step": "llm",
                    "iteration": iteration,
                    "action": (action or {}).get("action", "unparsed"),
                    "thought": (action or {}).get("thought", ""),
                }
            )

            # --- 情况 1：输出非法，纠正后重试 ---
            if action is None:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": prompts.INVALID_OUTPUT.substitute(raw=raw[:800]),
                    }
                )
                continue

            act = action.get("action")

            # --- 情况 2：最终报告 ---
            if act == "final" or "report" in action:
                report = self._normalize_report(
                    action.get("report") or action, tool_evidence
                )
                self.trace.append({"step": "final", "iteration": iteration})
                return report

            # --- 情况 3：工具调用 ---
            if act == "tool_call":
                tool_name = action.get("tool", "")
                envelope = execute_tool(tool_name, action.get("args"), code)
                tool_evidence.append(envelope)
                self.trace.append(
                    {
                        "step": "tool",
                        "iteration": iteration,
                        "tool": tool_name,
                        "ok": envelope.get("ok", False),
                    }
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": prompts.TOOL_RESULT.substitute(
                            tool=tool_name,
                            result=json.dumps(envelope, ensure_ascii=False)[:6000],
                        ),
                    }
                )
                continue

            # --- 情况 4：JSON 合法但协议不对，当作非法处理 ---
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": prompts.INVALID_OUTPUT.substitute(raw=raw[:800]),
                }
            )

        # 达到最大轮数：强制 LLM 综合出报告
        self.trace.append({"step": "force_final"})
        messages.append(
            {"role": "user", "content": prompts.FORCE_FINAL.substitute()}
        )
        try:
            raw = self.llm.chat(messages)  # type: ignore[union-attr]
            action = self._extract_json(raw)
            if action is not None:
                return self._normalize_report(
                    action.get("report") or action, tool_evidence
                )
        except LLMError as exc:
            logger.warning("强制综合阶段 LLM 仍失败：%s", exc)

        # 最后兜底：用已有工具证据拼装报告，绝不给用户一个空结果
        return self._report_from_evidence(tool_evidence, code)

    # ------------------------------------------------------------------ #
    # JSON 解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """从模型输出中稳健地抽取第一个合法 JSON 对象。"""
        if not text:
            return None
        cleaned = text.strip()

        # 去掉 ```json ... ``` 代码块围栏
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        # 先直接尝试
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else None
        except ValueError:
            pass

        # 再扫描第一个配对的花括号区间（考虑字符串与转义）
        start = cleaned.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(cleaned)):
            ch = cleaned[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : idx + 1]
                    try:
                        data = json.loads(candidate)
                        return data if isinstance(data, dict) else None
                    except ValueError:
                        return None
        return None

    # ------------------------------------------------------------------ #
    # 报告规范化
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_report(
        raw: Dict[str, Any], evidence: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """补齐字段、排序、限制取值范围，保证下游渲染稳定。"""
        if not isinstance(raw, dict):
            raw = {}
        issues = raw.get("issues") or []
        normalized_issues: List[Dict[str, Any]] = []
        for item in issues:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "low")).lower()
            if severity not in _SEVERITY_ORDER:
                severity = "low"
            normalized_issues.append(
                {
                    "line": item.get("line", 0),
                    "severity": severity,
                    "category": item.get("category", "maintainability"),
                    "title": item.get("title", "未命名问题"),
                    "description": item.get("description", ""),
                    "suggestion": item.get("suggestion", ""),
                    "code": item.get("code", ""),
                }
            )
        # 高优先级问题排前面，其次按行号
        normalized_issues.sort(
            key=lambda x: (_SEVERITY_ORDER[x["severity"]], _as_int(x["line"]))
        )

        score = _as_int(raw.get("overall_score", 0))
        score = max(0, min(100, score))

        return {
            "summary": raw.get("summary", "（模型未提供总结）"),
            "overall_score": score,
            "issues": normalized_issues,
            "strengths": raw.get("strengths") or [],
            "tool_summary": raw.get("tool_summary", ""),
            "evidence": evidence,
        }

    # ------------------------------------------------------------------ #
    # 离线 / 降级：纯本地工具 + 启发式规则
    # ------------------------------------------------------------------ #
    def _offline_review(
        self, code: str, filename: str, precheck: str, notes: str
    ) -> Dict[str, Any]:
        """不调用 LLM，直接运行工具并用启发式规则生成报告。"""
        evidence: List[Dict[str, Any]] = []
        for tool_name in ("ast_parse", "radon_complexity", "pylint_check"):
            envelope = execute_tool(tool_name, {}, code)
            evidence.append(envelope)
            self.trace.append(
                {"step": "tool", "tool": tool_name, "ok": envelope.get("ok", False)}
            )

        report = self._report_from_evidence(evidence, code)
        report["summary"] = "%s\n\n（预处理：%s）" % (
            prompts.OFFLINE_NOTE,
            precheck,
        ) + ("\n" + notes if notes else "")
        return report

    def _report_from_evidence(
        self, evidence: List[Dict[str, Any]], code: str
    ) -> Dict[str, Any]:
        """把工具输出转成统一的报告结构（离线与兜底共用）。"""
        issues: List[Dict[str, Any]] = []
        by_tool: Dict[str, Any] = {}

        for env in evidence:
            name = env.get("tool")
            result = env.get("result")
            if isinstance(result, dict):
                by_tool[name] = result

        # 1) 语法错误 -> high
        ast_res = by_tool.get("ast_parse", {})
        if ast_res.get("error") == "SyntaxError":
            issues.append(
                {
                    "line": ast_res.get("lineno", 0),
                    "severity": "high",
                    "category": "bug",
                    "title": "语法错误",
                    "description": ast_res.get("message", ""),
                    "suggestion": "修复第 %s 行的语法问题后再进行结构分析。"
                    % ast_res.get("lineno", "?"),
                    "code": ast_res.get("text", ""),
                }
            )
        # 2) 启发式坏味道
        issues.extend(_static_smells(code))
        # 3) 高圈复杂度 -> medium
        radon_res = by_tool.get("radon_complexity", {})
        for fn in radon_res.get("high_complexity_functions", [])[:10]:
            issues.append(
                {
                    "line": fn.get("lineno", 0),
                    "severity": "medium" if fn.get("rank") in ("C", "D") else "high",
                    "category": "maintainability",
                    "title": "圈复杂度过高（%s, rank %s）"
                    % (fn.get("complexity"), fn.get("rank")),
                    "description": "函数 `%s` 分支过多，难以测试与维护。" % fn.get("name"),
                    "suggestion": "拆分条件分支为独立函数，或使用早返回降低嵌套。",
                    "code": "",
                }
            )
        # 4) pylint 结果映射严重等级
        pylint_res = by_tool.get("pylint_check", {})
        severity_map = {
            "fatal": "high",
            "error": "high",
            "warning": "medium",
            "refactor": "low",
            "convention": "low",
        }
        # 这些 pylint 规则与本地启发式检测重复，避免报告出现重复条目
        overlap_symbols = {
            "dangerous-default-value",
            "eval-used",
            "bare-except",
            "singleton-comparison",
            "consider-using-with",
            "syntax-error",
        }
        for msg in pylint_res.get("messages", [])[:20]:
            if msg.get("symbol") in overlap_symbols:
                continue
            issues.append(
                {
                    "line": msg.get("line", 0),
                    "severity": severity_map.get(msg.get("type", ""), "low"),
                    "category": "style",
                    "title": "[pylint] %s" % msg.get("symbol", ""),
                    "description": msg.get("message", ""),
                    "suggestion": "按 pylint 规则 `%s` 修复：%s"
                    % (msg.get("message_id", ""), msg.get("message", "")),
                    "code": "",
                }
            )

        # 去重（行号+标题）
        seen = set()
        unique: List[Dict[str, Any]] = []
        for issue in issues:
            key = (issue["line"], issue["title"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)

        unique.sort(
            key=lambda x: (_SEVERITY_ORDER.get(x["severity"], 3), _as_int(x["line"]))
        )

        # 启发式评分：扣分累计但封顶，避免问题多时一律显示 0 分
        penalty = {"high": 15, "medium": 7, "low": 2}
        total_penalty = sum(penalty.get(i["severity"], 0) for i in unique)
        score = max(0, 100 - min(total_penalty, 70))

        summary = "本地静态分析完成：共发现 %d 个问题（高 %d / 中 %d / 低 %d）。" % (
            len(unique),
            sum(1 for i in unique if i["severity"] == "high"),
            sum(1 for i in unique if i["severity"] == "medium"),
            sum(1 for i in unique if i["severity"] == "low"),
        )
        tool_summary = "平均圈复杂度：%s" % radon_res.get("average_complexity", "N/A")
        if pylint_res.get("num_messages") is not None:
            tool_summary += "；pylint 消息数：%s" % pylint_res.get("num_messages")

        return self._normalize_report(
            {
                "summary": summary,
                "overall_score": score,
                "issues": unique,
                "strengths": [],
                "tool_summary": tool_summary,
            },
            evidence,
        )

    # ------------------------------------------------------------------ #
    # 边界：空输入
    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_report() -> Dict[str, Any]:
        return {
            "summary": "输入为空，无法审查。请提供文件路径或粘贴 Python 代码。",
            "overall_score": 0,
            "issues": [],
            "strengths": [],
            "tool_summary": "",
            "evidence": [],
        }


# --------------------------------------------------------------------------- #
# 模块级辅助函数
# --------------------------------------------------------------------------- #
def _as_int(value: Any) -> int:
    """尽力把任意值转成 int，失败则返回 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _static_smells(code: str) -> List[Dict[str, Any]]:
    """一组基于 AST 的启发式坏味道检测，用于离线/降级报告。"""
    issues: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues  # 语法错误已在别处单独报告

    # 预先收集被 `with` 管理的调用，用于识别资源泄漏
    with_managed = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.With):
            for item in n.items:
                if isinstance(item.context_expr, ast.Call):
                    with_managed.add(id(item.context_expr))

    for node in ast.walk(tree):
        # (1) 可变默认参数：函数定义时只求值一次，会跨调用共享
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]
            for default in defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    issues.append(
                        {
                            "line": node.lineno,
                            "severity": "high",
                            "category": "bug",
                            "title": "可变默认参数",
                            "description": "函数 `%s` 使用可变对象作为默认参数，多次调用会共享同一对象。"
                            % node.name,
                            "suggestion": "改为默认 None，并在函数体内 `if x is None: x = []`。",
                            "code": "",
                        }
                    )
        # (2) 裸 except
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append(
                {
                    "line": node.lineno,
                    "severity": "medium",
                    "category": "bug",
                    "title": "裸 except",
                    "description": "`except:` 会吞掉所有异常（包括 KeyboardInterrupt/SystemExit），掩盖真实错误。",
                    "suggestion": "捕获具体异常类型，例如 `except ValueError as e:`。",
                    "code": "",
                }
            )
        # (3) eval / exec
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("eval", "exec")
        ):
            issues.append(
                {
                    "line": getattr(node, "lineno", 0),
                    "severity": "high",
                    "category": "security",
                    "title": "使用 %s 存在安全风险" % node.func.id,
                    "description": "对不可信输入执行 %s 可能导致任意代码执行。" % node.func.id,
                    "suggestion": "改用 `ast.literal_eval` 或明确的白名单解析逻辑。",
                    "code": "",
                }
            )
        # (4) == None
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and _is_none(comparator):
                    issues.append(
                        {
                            "line": getattr(node, "lineno", 0),
                            "severity": "low",
                            "category": "style",
                            "title": "应使用 is None",
                            "description": "与 None 比较应使用 `is` / `is not`。",
                            "suggestion": "将 `== None` 改为 `is None`。",
                            "code": "",
                        }
                    )
        # (5) 文件资源未使用 with 管理（可能泄漏）
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and id(node) not in with_managed
        ):
            issues.append(
                {
                    "line": getattr(node, "lineno", 0),
                    "severity": "medium",
                    "category": "maintainability",
                    "title": "文件句柄可能未关闭",
                    "description": "`open()` 未使用 `with` 管理，异常时文件句柄不会释放。",
                    "suggestion": "改用 `with open(path, encoding=\"utf-8\") as fh:` 自动关闭。",
                    "code": "",
                }
            )
        # (6) 硬编码密钥
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and any(
                        k in target.id.lower()
                        for k in ("password", "passwd", "secret", "token", "api_key", "apikey")
                    )
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value
                ):
                    issues.append(
                        {
                            "line": node.lineno,
                            "severity": "high",
                            "category": "security",
                            "title": "疑似硬编码密钥",
                            "description": "变量 `%s` 直接硬编码了敏感字符串。" % target.id,
                            "suggestion": "改为从环境变量或配置文件读取，并从版本库中移除已泄露的密钥。",
                            "code": "",
                        }
                    )
    return issues


def _is_none(node: ast.AST) -> bool:
    """兼容 Python 3.7 与 3.8+ 判断是否为 None 字面量。"""
    if isinstance(node, ast.Constant):
        return node.value is None
    return isinstance(node, ast.NameConstant) and node.value is None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
_SEVERITY_LABEL = {"high": "严重", "medium": "中等", "low": "轻微"}
_CATEGORY_LABEL = {
    "bug": "Bug",
    "security": "安全",
    "performance": "性能",
    "style": "风格",
    "maintainability": "可维护性",
}


def format_report(report: Dict[str, Any]) -> str:
    """把报告 dict 渲染成适合命令行阅读的文本。"""
    lines: List[str] = []
    score = report.get("overall_score", 0)
    lines.append("=" * 60)
    lines.append("代码审查报告    |    综合评分：%s / 100" % score)
    lines.append("=" * 60)
    lines.append("")
    lines.append("【总体评价】")
    lines.append(report.get("summary", ""))
    lines.append("")

    strengths = report.get("strengths") or []
    if strengths:
        lines.append("【值得肯定】")
        for item in strengths:
            lines.append("  + %s" % item)
        lines.append("")

    issues = report.get("issues") or []
    lines.append("【问题清单】共 %d 项" % len(issues))
    if not issues:
        lines.append("  未发现明显问题。")
    for idx, issue in enumerate(issues, 1):
        sev = _SEVERITY_LABEL.get(issue.get("severity", "low"), issue.get("severity"))
        cat = _CATEGORY_LABEL.get(issue.get("category", ""), issue.get("category", ""))
        lines.append("")
        lines.append(
            "  [%d] [%s][%s] 第 %s 行  %s"
            % (idx, sev, cat, issue.get("line", "?"), issue.get("title", ""))
        )
        if issue.get("description"):
            lines.append("      描述：%s" % issue["description"])
        if issue.get("suggestion"):
            lines.append("      建议：%s" % issue["suggestion"])
        if issue.get("code"):
            lines.append("      代码：%s" % issue["code"])
    lines.append("")

    if report.get("tool_summary"):
        lines.append("【工具分析摘要】")
        lines.append(report["tool_summary"])
        lines.append("")

    if report.get("degraded"):
        lines.append("（提示：本次未成功调用 LLM，报告由本地工具生成。）")

    return "\n".join(lines)


def format_trace(trace: List[Dict[str, Any]]) -> str:
    """把 Agent 决策轨迹渲染出来，便于演示“推理-工具-再推理”闭环。"""
    lines = ["--- Agent 决策轨迹 ---"]
    for item in trace:
        step = item.get("step")
        if step == "preprocess":
            lines.append("  [预处理] %s" % item.get("precheck", ""))
        elif step == "llm":
            lines.append(
                "  [第%s轮推理] action=%s | thought=%s"
                % (item.get("iteration"), item.get("action"), item.get("thought"))
            )
        elif step == "tool":
            lines.append(
                "  [工具调用] %s -> %s"
                % (item.get("tool"), "成功" if item.get("ok") else "失败")
            )
        elif step == "force_final":
            lines.append("  [达到上限] 强制综合")
        elif step == "final":
            lines.append("  [输出报告] 第%s轮" % item.get("iteration"))
    return "\n".join(lines)
