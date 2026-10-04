# 预期审查输出示例

本文件展示代码审查 Agent 对 `examples/buggy_sample.py` 的预期输出。

> 说明：LLM 的具体措辞每次可能不同，但**结构化字段（行号 / 严重等级 / 类别 /
> 问题 / 建议）与关键结论应当稳定**。以下给出两种形态：在线（LLM）模式的
> 结构化 JSON，以及离线模式的命令行渲染结果。

---

## 一、在线模式：最终报告 JSON（`action=final` 的 `report` 字段）

```json
{
  "summary": "该模块存在多处高危问题：硬编码密钥、可变默认参数、eval 与 SQL 注入风险，以及空列表除零。classify 函数圈复杂度达 14（rank C），建议拆分。整体不具备上生产条件，建议优先修复 high 级问题。",
  "overall_score": 25,
  "issues": [
    {
      "line": 13,
      "severity": "high",
      "category": "security",
      "title": "硬编码数据库密码",
      "description": "DB_PASSWORD 直接写在源码中，一旦代码泄露密钥即泄露。",
      "suggestion": "改为 os.environ[\"DB_PASSWORD\"]，并通过密钥管理服务注入；同时轮换已泄露的密码。",
      "code": "DB_PASSWORD = \"super_secret_123\""
    },
    {
      "line": 16,
      "severity": "high",
      "category": "bug",
      "title": "可变默认参数被跨调用共享",
      "description": "cache={} 在函数定义时只创建一次，多次调用会共享同一个字典，导致脏数据。",
      "suggestion": "改为 def get_user(user_id, cache=None): 并在函数体内 if cache is None: cache = {}。",
      "code": "def get_user(user_id, cache={}):"
    },
    {
      "line": 20,
      "severity": "high",
      "category": "security",
      "title": "SQL 语句字符串拼接，存在注入风险",
      "description": "query 由用户可控的 user_id 拼接而成，可构造恶意 SQL。",
      "suggestion": "使用参数化查询：cursor.execute(\"SELECT * FROM users WHERE id = %s\", (user_id,))。",
      "code": "query = \"SELECT * FROM users WHERE id = \" + str(user_id)"
    },
    {
      "line": 31,
      "severity": "high",
      "category": "security",
      "title": "使用 eval 执行任意代码",
      "description": "eval(sql) 若 sql 来自外部输入，可导致任意代码执行。",
      "suggestion": "移除 eval，改用 ast.literal_eval 或明确的白名单解析。",
      "code": "return eval(sql)"
    },
    {
      "line": 40,
      "severity": "high",
      "category": "bug",
      "title": "空列表导致 ZeroDivisionError",
      "description": "当 numbers 为空时 len(numbers)==0，触发除零异常。",
      "suggestion": "开头加 if not numbers: return 0，再执行 total / len(numbers)。",
      "code": "return total / len(numbers)"
    },
    {
      "line": 32,
      "severity": "medium",
      "category": "bug",
      "title": "裸 except 吞掉所有异常",
      "description": "except: 会隐藏真实错误，并吞掉 KeyboardInterrupt。",
      "suggestion": "捕获具体异常，例如 except (IOError, ValueError) as e:。",
      "code": "except:"
    },
    {
      "line": 29,
      "severity": "medium",
      "category": "maintainability",
      "title": "文件句柄未关闭",
      "description": "open() 未使用 with，异常时文件不会关闭，可能泄漏句柄。",
      "suggestion": "改为 with open(\"database.log\", \"a\", encoding=\"utf-8\") as f:。",
      "code": "f = open(\"database.log\", \"a\")"
    },
    {
      "line": 43,
      "severity": "medium",
      "category": "maintainability",
      "title": "classify 圈复杂度过高（14, rank C）",
      "description": "深层 if/elif 嵌套，共 20 个分支、14 个 return，难以测试与维护。",
      "suggestion": "用区间字典/查表法替代嵌套判断，或将分数段拆成独立函数。",
      "code": "def classify(score): ..."
    },
    {
      "line": 84,
      "severity": "medium",
      "category": "performance",
      "title": "循环内字符串拼接",
      "description": "report += 在循环中反复创建新字符串，数据量大时性能差。",
      "suggestion": "收集到列表后用 \",\".join(parts) 一次性拼接。",
      "code": "report += str(r) + \",\""
    },
    {
      "line": 89,
      "severity": "medium",
      "category": "style",
      "title": "变量遮蔽内置名 list",
      "description": "list = [1,2,3] 覆盖了内置类型 list，后续代码将无法使用 list()。",
      "suggestion": "重命名为 items 或 nums。",
      "code": "list = [1, 2, 3]"
    },
    {
      "line": 45,
      "severity": "low",
      "category": "style",
      "title": "应使用 is None",
      "description": "与 None 比较应使用身份运算符 is。",
      "suggestion": "将 score == None 改为 score is None。",
      "code": "if score == None:"
    },
    {
      "line": 8,
      "severity": "low",
      "category": "style",
      "title": "存在未使用的导入",
      "description": "os、sys、json 三个导入均未被使用。",
      "suggestion": "删除未使用的 import，或补充实际使用。",
      "code": "import os / import sys / import json"
    }
  ],
  "strengths": [
    "函数职责划分基本清晰，命名可读。",
    "使用了缓存思路减少重复查询（尽管默认参数写法有误）。"
  ],
  "tool_summary": "ast_parse 显示 6 个函数、3 个未使用导入；radon 显示 classify 复杂度 14（rank C），平均复杂度 3.83；pylint 报告 25 条消息（含 eval-used、bare-except、dangerous-default-value）。"
}
```

---

## 二、离线模式：命令行渲染结果（节选）

运行 `python main.py --offline examples/buggy_sample.py --verbose` 的关键输出：

```text
--- Agent 决策轨迹 ---
  [预处理] 原始字符数=1970，行数=96；语法检查通过（ast.parse 成功）
  [工具调用] ast_parse -> 成功
  [工具调用] radon_complexity -> 成功
  [工具调用] pylint_check -> 成功

============================================================
代码审查报告    |    综合评分：30 / 100
============================================================

【总体评价】
离线模式：未调用 LLM，以下结论完全由本地静态分析工具生成。

【问题清单】共 22 项

  [1] [严重][安全] 第 13 行  疑似硬编码密钥
      描述：变量 `DB_PASSWORD` 直接硬编码了敏感字符串。
      建议：改为从环境变量或配置文件读取，并从版本库中移除已泄露的密钥。

  [2] [严重][Bug] 第 16 行  可变默认参数
      描述：函数 `get_user` 使用可变对象作为默认参数，多次调用会共享同一对象。
      建议：改为默认 None，并在函数体内 `if x is None: x = []`。

  [3] [严重][安全] 第 31 行  使用 eval 存在安全风险
      建议：改用 `ast.literal_eval` 或明确的白名单解析逻辑。

  [4] [中等][可维护性] 第 29 行  文件句柄可能未关闭
  [5] [中等][Bug] 第 32 行  裸 except
  [6] [中等][可维护性] 第 43 行  圈复杂度过高（14, rank C）
  ...
【工具分析摘要】
平均圈复杂度：3.83；pylint 消息数：25
```

> 在线模式下，`highest` 优先级内容与上表一致，但总结、修复建议由 DeepSeek 生成，
> 更自然、更贴合上下文（例如会针对 SQL 注入给出参数化查询的具体代码）。

---

## 三、语法错误示例的预期行为

对 `examples/syntax_error_sample.py`（第 9 行缺失括号与冒号），Agent 不会崩溃：

```text
【总体评价】
...
（预处理：...；语法错误：第 9 行 invalid syntax）

【问题清单】共 1 项
  [1] [严重][Bug] 第 9 行  语法错误
      描述：invalid syntax
      建议：修复第 9 行的语法问题后再进行结构分析。
      代码：    print("缺少右括号和冒号"
```

在线模式下，LLM 会聚焦于语法错误的定位与修复，而不会编造函数结构。

---

## 四、其它边界情况的预期行为

| 输入 | 预期行为 |
|---|---|
| 空输入 / 纯空白 | 不做 LLM 调用，直接返回「输入为空，无法审查」 |
| 非代码文本（如 `hello world`） | 预检标记为疑似非代码；LLM 在 summary 中如实说明，不编造问题 |
| 超长代码（> 20000 字符） | 保留头尾、截断中间，并在预检信息中告知模型 |
| LLM 超时 / 限流 | tenacity 指数退避自动重试；仍失败则降级为本地工具分析（报告标注 degraded） |
| 工具执行失败 / 未知工具 | 错误信息回填给 LLM 继续推理，主循环不崩溃 |
