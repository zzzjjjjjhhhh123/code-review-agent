# -*- coding: utf-8 -*-
"""代码审查 Agent 命令行入口。

用法示例：
    python main.py examples/buggy_sample.py
    python main.py -c "def f(x): return x/0"
    type demo.py | python main.py --stdin
    python main.py --offline examples/buggy_sample.py
    python main.py            # 进入交互模式，可粘贴代码
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from agent import CodeReviewAgent, format_report, format_trace
from llm.client import LLMClient


def _force_utf8() -> None:
    """尽量让 Windows 控制台以 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-agent",
        description="基于 LLM 原生 API 手写主循环的代码审查 Agent。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py examples/buggy_sample.py\n"
            '  python main.py -c "import os\\ndef f(x): return x/0"\n'
            "  python main.py --offline examples/buggy_sample.py\n"
            "  python main.py            # 交互模式\n"
        ),
    )
    parser.add_argument("file", nargs="?", help="待审查的 Python 文件路径")
    parser.add_argument("-c", "--code", help="直接传入代码字符串进行审查")
    parser.add_argument(
        "--stdin", action="store_true", help="从标准输入读取代码（便于管道使用）"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线模式：不调用 LLM，仅运行本地静态分析工具",
    )
    parser.add_argument("--model", help="覆盖 LLM 模型名（默认读取环境变量 LLM_MODEL）")
    parser.add_argument("--base-url", help="覆盖 API Base URL（默认 DeepSeek）")
    parser.add_argument(
        "--max-iterations", type=int, default=6, help="Agent 最大工具调用轮数，默认 6"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="打印 Agent 决策轨迹（推理-工具闭环）"
    )
    parser.add_argument(
        "--json", action="store_true", help="以原始 JSON 输出报告，便于二次加工"
    )
    return parser


def read_file_safe(path: str) -> Optional[str]:
    """读取文件，失败时打印友好错误并返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        print("[错误] 文件不存在：%s" % path)
    except UnicodeDecodeError:
        print("[错误] 文件不是 UTF-8 编码，请转换后再试：%s" % path)
    except OSError as exc:
        print("[错误] 读取文件失败：%s" % exc)
    return None


def make_agent(args: argparse.Namespace) -> CodeReviewAgent:
    """根据命令行参数构建 Agent（含 LLM 客户端）。"""
    offline = args.offline
    client = None
    if not offline:
        client = LLMClient(model=args.model, base_url=args.base_url)
        if not client.api_key:
            print(
                "[提示] 未检测到 API Key（环境变量 LLM_API_KEY / DEEPSEEK_API_KEY）。\n"
                "       将自动降级为离线静态分析；设置 Key 后可获得完整 LLM 审查。\n"
            )
    return CodeReviewAgent(
        llm_client=client,
        max_iterations=args.max_iterations,
        offline=offline,
    )


def run_once(
    agent: CodeReviewAgent,
    code: str,
    filename: str,
    args: argparse.Namespace,
) -> None:
    """执行一次审查并打印结果。"""
    # --json 模式下保持 stdout 为纯 JSON，便于管道/程序消费
    if not args.json:
        print("\n>>> 正在审查：%s ...\n" % filename)

    report = agent.review(code, filename=filename)

    if args.verbose and not args.json:
        print(format_trace(agent.trace))
        print()

    if args.json:
        # evidence 可能很大，JSON 输出时去掉，保持简洁
        slim = {k: v for k, v in report.items() if k != "evidence"}
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
        print()


def interactive(agent: CodeReviewAgent, args: argparse.Namespace) -> None:
    """交互模式：支持粘贴代码块或输入 :file 路径。"""
    print(
        "=" * 60
        + "\n代码审查 Agent（交互模式）\n"
        + "  - 直接粘贴 Python 代码，单独一行输入 :end 结束\n"
        + "  - :file <路径>  审查指定文件\n"
        + "  - :help         查看帮助\n"
        + "  - :quit         退出\n"
        + "=" * 60
    )
    while True:
        try:
            first = input("\n>>> ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return

        command = first.strip()
        if command in (":quit", ":q", ":exit"):
            print("再见！")
            return
        if command == ":help":
            print(
                "粘贴代码后输入 :end 结束；:file <路径> 审查文件；:quit 退出。"
            )
            continue
        if command.startswith(":file"):
            parts = first.split(maxsplit=1)
            path = parts[1].strip() if len(parts) > 1 else input("请输入文件路径：").strip()
            content = read_file_safe(path)
            if content is not None:
                run_once(agent, content, path, args)
            continue

        # 收集多行代码
        lines = [first]
        print("（继续输入，单独一行 :end 结束）")
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() == ":end":
                break
            lines.append(line)
        code = "\n".join(lines)
        run_once(agent, code, "<交互输入>", args)


def main(argv: Optional[list] = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    agent = make_agent(args)

    # 输入来源优先级：-c > --stdin > 文件 > 交互模式
    if args.code is not None:
        run_once(agent, args.code, "<命令行 -c>", args)
        return 0

    if args.stdin:
        code = sys.stdin.read()
        run_once(agent, code, "<stdin>", args)
        return 0

    if args.file:
        content = read_file_safe(args.file)
        if content is None:
            return 1
        run_once(agent, content, args.file, args)
        return 0

    interactive(agent, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
