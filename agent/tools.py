# -*- coding: utf-8 -*-
"""Agent 可调用的工具集合。

每个工具都是一个纯函数：输入代码/参数，返回 JSON 可序列化的结果，**永不抛异常**。
所有异常都会被转成 {"error": ...} 结构返回给 LLM，让 Agent 能「看到错误并继续推理」，
而不是让主循环崩溃。

工具清单：
    ast_parse          —— 解析语法结构、函数、类、导入
    radon_complexity   —— 计算每个函数的圈复杂度
    pylint_check       —— 运行 pylint 静态检查
    read_file          —— 读取项目内文件（供 Agent 按需查看更多上下文）

通过 `execute_tool` 统一调度：它负责参数注入与 try/except 保护。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional

# 读取文件工具允许的最大字节数，避免一次读入超大文件
MAX_FILE_BYTES = 200_000
# pylint 消息最多返回条数，避免提示词被淹没
MAX_PYLINT_MESSAGES = 50


# --------------------------------------------------------------------------- #
# 工具 1：AST 解析
# --------------------------------------------------------------------------- #
def _format_arguments(args: ast.arguments) -> List[str]:
    """把 ast.arguments 转成人类可读的参数名列表。"""
    names = [a.arg for a in args.posonlyargs]  # type: ignore[attr-defined]
    names += [a.arg for a in args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def _summarize_function(node: ast.AST) -> Dict[str, Any]:
    """提取单个函数/方法的核心信息。"""
    return {
        "name": getattr(node, "name", "<lambda>"),
        "lineno": getattr(node, "lineno", None),
        "args": _format_arguments(node.args),  # type: ignore[attr-defined]
        "has_docstring": ast.get_docstring(node) is not None,  # type: ignore[arg-type]
        "decorators": [
            ast.unparse(d) if hasattr(ast, "unparse") else "?"
            for d in getattr(node, "decorator_list", [])
        ],
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def ast_parse(code: str) -> Dict[str, Any]:
    """解析代码的语法结构：导入、类、函数、顶层语句。

    语法错误时返回结构化错误信息（而非抛异常），供 LLM 处理。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        # 语法错误是重要的审查输入，单独结构化返回
        return {
            "error": "SyntaxError",
            "message": exc.msg,
            "lineno": exc.lineno,
            "offset": exc.offset,
            "text": (exc.text or "").rstrip(),
            "hint": "代码存在语法错误，无法解析 AST，请先修复语法。",
        }
    except Exception as exc:  # noqa: BLE001 - 兜底，工具绝不崩溃
        return {"error": type(exc).__name__, "message": str(exc)}

    imports: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []
    top_level_stmts: List[str] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.append(
                {
                    "type": "import",
                    "module": ",".join(a.name for a in node.names),
                    "lineno": node.lineno,
                }
            )
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                {
                    "type": "from",
                    "module": node.module,
                    "names": [a.name for a in node.names],
                    "lineno": node.lineno,
                }
            )
        elif isinstance(node, ast.ClassDef):
            methods = [
                _summarize_function(child)
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append(
                {
                    "name": node.name,
                    "lineno": node.lineno,
                    "bases": [
                        ast.unparse(b) if hasattr(ast, "unparse") else "?"
                        for b in node.bases
                    ],
                    "has_docstring": ast.get_docstring(node) is not None,
                    "methods": methods,
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_summarize_function(node))
        else:
            top_level_stmts.append(type(node).__name__)

    return {
        "ok": True,
        "module_docstring": ast.get_docstring(tree) is not None,
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "top_level_statement_types": top_level_stmts,
        "summary": {
            "num_imports": len(imports),
            "num_classes": len(classes),
            "num_functions": len(functions) + sum(len(c["methods"]) for c in classes),
        },
    }


# --------------------------------------------------------------------------- #
# 工具 2：radon 圈复杂度
# --------------------------------------------------------------------------- #
def radon_complexity(code: str) -> Dict[str, Any]:
    """用 radon 计算每个函数/方法的圈复杂度（McCabe）。"""
    try:
        from radon.complexity import cc_rank, cc_visit
    except ImportError:
        return {
            "error": "DependencyMissing",
            "message": "未安装 radon，请执行 `pip install radon` 后重试。",
        }

    try:
        blocks = cc_visit(code)
    except SyntaxError as exc:
        return {
            "error": "SyntaxError",
            "message": str(exc),
            "hint": "语法错误导致无法计算复杂度。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "message": str(exc)}

    results: List[Dict[str, Any]] = []
    total = 0

    def _collect(items: List[Any]) -> None:
        for block in items:
            results.append(
                {
                    "name": block.name,
                    "lineno": block.lineno,
                    "complexity": block.complexity,
                    "rank": cc_rank(block.complexity),
                }
            )
            # 类对象会带 methods，需要递归展开
            for method in getattr(block, "methods", []) or []:
                results.append(
                    {
                        "name": "%s.%s" % (block.name, method.name),
                        "lineno": method.lineno,
                        "complexity": method.complexity,
                        "rank": cc_rank(method.complexity),
                    }
                )

    _collect(blocks)

    for item in results:
        total += item["complexity"]

    average = round(total / len(results), 2) if results else 0.0
    high = [r for r in results if r["rank"] in ("C", "D", "E", "F")]

    return {
        "ok": True,
        "functions": results,
        "average_complexity": average,
        "high_complexity_functions": high,
        "note": "rank A/B 较优；C 起提示复杂度偏高，E/F 需要重点重构。",
    }


# --------------------------------------------------------------------------- #
# 工具 3：pylint 静态检查
# --------------------------------------------------------------------------- #
def pylint_check(code: str) -> Dict[str, Any]:
    """把代码写入临时文件并运行 pylint，返回结构化检查结果。"""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="cra_pylint_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pylint",
                "--output-format=json",
                "--score=n",
                "--persistent=n",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Timeout", "message": "pylint 运行超过 60 秒已终止。"}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "message": str(exc)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    stderr = proc.stderr or ""
    # 未安装 pylint 时 python -m 会报 No module named pylint
    if "No module named pylint" in stderr:
        return {
            "error": "DependencyMissing",
            "message": "未安装 pylint，请执行 `pip install pylint` 后重试。",
        }

    try:
        messages = json.loads(proc.stdout or "[]")
    except ValueError:
        return {
            "error": "ParseError",
            "message": "pylint 输出无法解析。",
            "stderr": stderr[:300],
        }

    # 精简字段，只保留对审查有用的信息
    simplified = [
        {
            "line": m.get("line"),
            "type": m.get("type"),          # convention/refactor/warning/error/fatal
            "symbol": m.get("symbol"),
            "message_id": m.get("message-id"),
            "message": m.get("message"),
        }
        for m in messages
    ]
    truncated = len(simplified) > MAX_PYLINT_MESSAGES
    simplified = simplified[:MAX_PYLINT_MESSAGES]

    # 统计各类问题数量
    counts: Dict[str, int] = {}
    for m in simplified:
        counts[m["type"]] = counts.get(m["type"], 0) + 1

    return {
        "ok": True,
        "num_messages": len(messages),
        "counts_by_type": counts,
        "messages": simplified,
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# 工具 4：读取文件
# --------------------------------------------------------------------------- #
def read_file(path: str, max_bytes: int = MAX_FILE_BYTES) -> Dict[str, Any]:
    """读取指定文件内容，限制大小，供 Agent 按需补充上下文。"""
    if not path:
        return {"error": "BadArgument", "message": "read_file 需要参数 path。"}
    if not os.path.exists(path):
        return {"error": "NotFound", "message": "文件不存在：%s" % path}
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(max_bytes)
        return {
            "ok": True,
            "path": path,
            "size_bytes": size,
            "truncated": size > max_bytes,
            "content": content,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "message": str(exc)}


# --------------------------------------------------------------------------- #
# 工具注册表 + 统一调度器
# --------------------------------------------------------------------------- #
TOOLS: Dict[str, Dict[str, Any]] = {
    "ast_parse": {
        "func": ast_parse,
        "needs_code": True,
        "description": "解析 Python 代码的语法结构，返回导入、类、函数列表与语法错误信息。",
    },
    "radon_complexity": {
        "func": radon_complexity,
        "needs_code": True,
        "description": "计算每个函数的圈复杂度（McCabe）并标记高风险函数，用于发现难维护代码。",
    },
    "pylint_check": {
        "func": pylint_check,
        "needs_code": True,
        "description": "运行 pylint 静态检查，返回告警、错误、风格等结构化问题列表。",
    },
    "read_file": {
        "func": read_file,
        "needs_code": False,
        "description": "读取项目内指定路径的文件内容，参数为 {\"path\": \"文件路径\"}。",
    },
}


def describe_tools() -> str:
    """生成工具清单文本，注入系统提示词，保证 Prompt 与代码一致。"""
    lines = []
    for name, meta in TOOLS.items():
        lines.append("- `%s`：%s" % (name, meta["description"]))
    return "\n".join(lines)


def execute_tool(name: str, args: Optional[Dict[str, Any]], code: str) -> Dict[str, Any]:
    """统一执行工具，带完整错误保护。

    Args:
        name: 工具名。
        args: LLM 提供的参数字典。
        code: 当前待审查的代码，会自动注入给 needs_code=True 的工具。

    Returns:
        统一信封：{"tool", "ok", "result"} 或 {"tool", "ok": False, "error"}。
        无论内部发生什么，都不会向外抛异常。
    """
    args = args or {}
    meta = TOOLS.get(name)
    if meta is None:
        return {
            "tool": name,
            "ok": False,
            "error": "UnknownTool",
            "message": "未知工具：%s，可用工具：%s" % (name, list(TOOLS)),
        }

    try:
        if meta["needs_code"]:
            result = meta["func"](code)
        else:
            result = meta["func"](**args)
        # 工具函数自身返回 {"error": ...} 时视为失败
        ok = not (isinstance(result, dict) and "error" in result)
        return {"tool": name, "ok": ok, "result": result}
    except TypeError as exc:
        # 多为参数不匹配，把正确用法反馈给 LLM
        return {
            "tool": name,
            "ok": False,
            "error": "BadArguments",
            "message": "参数错误：%s。请参考工具说明重新调用。" % exc,
        }
    except Exception as exc:  # noqa: BLE001 - 最后一道防线
        return {
            "tool": name,
            "ok": False,
            "error": type(exc).__name__,
            "message": "工具执行失败：%s" % exc,
        }
