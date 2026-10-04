代码审查 Agent 项目说明

一、这个项目是做什么的

这是一个用 Python 写的代码审查助手。你把一段 Python 代码交给它，它会先自己判断需要做哪些分析，然后去调用工具（解析语法结构、算圈复杂度、跑静态检查），把工具返回的结果拿回来接着推理，最后给出一份带行号和严重等级的修改建议。整个过程是"推理、调用工具、再推理"的闭环，而不是把代码丢给模型一次就让它直接下结论。

Agent 的主循环是自己手写的，没有用 LangChain 这类框架。原因很直接：这门课里 Agent 架构占 30 分，如果交给框架，决策、工具调度、上下文管理这些核心逻辑都被藏在框架内部，答辩时很难逐行讲清楚。用大模型的原生接口自己搭，主循环大概两百行，每一步都能说明白。

二、支持的功能

手写主循环，模型自主决定调用哪个工具，形成推理闭环。

一共四个工具。ast_parse 解析语法结构，包括导入、类、函数，以及语法错误；radon_complexity 计算每个函数的圈复杂度；pylint_check 跑静态检查；read_file 用于让 Agent 按需读取文件补充上下文。

审查结果结构化，每条问题都带行号、严重等级（严重、中等、轻微）、类别、问题描述和修复建议，尽量给出可以直接替换的代码。

支持上下文记忆，多轮消息会累积，工具返回的结果会回填给模型继续分析。

有错误处理和重试。调用模型超时或遇到限流会自动重试；工具执行出错不会让程序崩溃，而是把错误信息交回模型继续推理。

对边界情况做了处理：空输入、不是代码的文本、语法错误的代码、超长的代码、接口调用失败，都能正常应对。

命令行交互，可以审查文件，也可以直接传一段代码字符串，或者从管道读入，或者进入交互模式粘贴代码。

额外提供了离线模式，没有配置 API Key 时也能跑本地静态分析，方便演示。

三、目录结构

code-review-agent
    main.py            程序入口，命令行参数解析和交互界面
    agent/loop.py      Agent 主循环
    agent/prompts.py   所有提示词模板
    agent/tools.py     四个工具和统一调度
    llm/client.py      模型调用封装和重试
    examples/          示例代码和预期的审查输出
    tests/             自测用例
    requirements.txt   依赖清单
    README.md          本文件
    Design.md          设计文档

四、安装

需要 Python 3.9 或更高版本。先安装依赖：

pip install -r requirements.txt

如果想用虚拟环境，可以先执行：

python -m venv .venv

Windows 下激活：

.venv\Scripts\activate

macOS 或 Linux 下激活：

source .venv/bin/activate

五、配置 API Key

代码里不写死密钥，全部从环境变量读取，默认使用 DeepSeek 的模型。涉及的变量有三个。

LLM_API_KEY，必填，你的密钥。也兼容 DEEPSEEK_API_KEY、DASHSCOPE_API_KEY、OPENAI_API_KEY 这几个名字。

LLM_BASE_URL，选填，默认是 https://api.deepseek.com。

LLM_MODEL，选填，默认是 deepseek-chat。

Windows PowerShell 里临时设置，只对当前窗口有效：

$env:LLM_API_KEY = "sk-你的密钥"

想永久生效，可以用 setx 命令设置，设置完要重新开一个窗口：

setx LLM_API_KEY "sk-你的密钥"

macOS 或 Linux：

export LLM_API_KEY="sk-你的密钥"

如果要换成通义千问或者 OpenAI，只要另外设置 LLM_BASE_URL 和 LLM_MODEL 就行，代码不用改。比如 Qwen 可以设成下面这样。

LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

六、使用方法

审查一个文件：

python main.py examples/buggy_sample.py

直接审查一段代码：

python main.py -c "def f(x): return x / 0"

从标准输入读取，便于和别的命令配合，需要加上 --stdin 参数。这个方式主要用于脚本调用或其它支持管道的命令行环境；在 Windows PowerShell 里直接审查文件更省事，用前面提到的文件路径方式即可。

进入交互模式手动粘贴代码：

python main.py

进入交互模式后，直接粘贴 Python 代码，然后单独一行输入 :end 表示结束；输入 :file 加路径可以审查文件；输入 :quit 退出。

常用参数如下。

--offline 离线模式，不调用大模型，只做本地静态分析
--verbose 打印 Agent 的决策轨迹，能看到推理和工具调用的过程
--json 以原始 JSON 输出报告，方便后续处理
--max-iterations N 最大工具调用轮数，默认 6
--model NAME 覆盖模型名
--base-url URL 覆盖接口地址

例如同时打开轨迹并审查示例文件：

python main.py examples/buggy_sample.py --verbose

七、一个例子

输入是示例文件里的一段代码：

DB_PASSWORD = "super_secret_123"

def get_user(user_id, cache={}):
    query = "SELECT id, name FROM users WHERE id = " + str(user_id)
    return eval(query)

def average(numbers):
    total = sum(numbers)
    return total / len(numbers)

输出的报告节选：

代码审查报告    综合评分：25 / 100

问题清单，共 12 项

第 1 项，严重，安全，第 13 行，硬编码数据库密码
说明：密钥直接写在源码里，代码一旦泄露密钥就跟着泄露。
建议：改为从环境变量读取，并轮换已经泄露的密码。

第 2 项，严重，Bug，第 16 行，可变默认参数被跨调用共享
建议：把 cache={} 改成 cache=None，函数体内部再判断并赋值为空字典。

第 3 项，严重，安全，第 20 行，SQL 字符串拼接存在注入风险
建议：改用参数化查询，让数据库驱动负责转义。

第 4 项，严重，Bug，第 27 行，空列表会导致除零错误
建议：函数开头先判断 if not numbers 再计算。

完整示例和结构化 JSON 见 examples 目录下的 expected_output.md。

八、Agent 是怎么工作的

流程大致是这样。先对输入做预处理，包括判断空输入、识别是不是代码、超长就截断、用 ast 做一次语法预检。然后进入主循环，每一轮让模型输出一个 JSON，要么表示要调用某个工具，要么表示可以给最终报告了。如果模型要调用工具，主循环就执行工具，把结果回填给模型，再进入下一轮推理。如果模型给出最终报告，就整理并输出。如果一直不给报告，到最大轮数就强制它综合一次，或者用本地工具的结果兜底。

几个关键点。每轮模型只能输出一个 JSON，协议写在 agent/prompts.py 里。工具需要的代码由主循环自动注入，不需要模型把整段代码再抄一遍，这样既省 token，也避免模型抄错代码导致分析对象不一致。工具调用失败时会返回结构化的错误信息，让模型看到错误后继续推理，而不是让程序直接崩掉。

更完整的架构说明和模块职责见 Design.md。

九、边界情况和错误处理

空输入：直接返回提示，不浪费一次模型调用。

不是代码的文本：预处理会给出提示，模型在总结里如实说明，不会硬编造问题。

语法错误：ast.parse 会抛出 SyntaxError，程序捕获后把行号和错误信息结构化返回给模型处理。

代码太长：超过设定长度时保留头尾、截断中间，并告诉模型。

模型超时、限流或服务端错误：用 tenacity 做指数退避重试，默认重试三次。

模型整体不可用：自动降级为本地静态分析，报告里会标明这次是降级结果。

工具执行异常或调用未知工具：全部用 try 和 except 包住，错误信息回填给模型，主循环不崩溃。

模型输出非法 JSON：先提示纠正再重试，轮数用尽则强制综合或本地兜底。

十、运行自测

自测不需要 API Key，用标准库就能跑：

python -m unittest discover -s tests -v

覆盖的内容包括工具在异常输入下的保护、JSON 解析的健壮性，以及用假模型驱动的完整主循环闭环测试。

十一、常见问题

问：没有 API Key 能运行吗。
答：可以。加上 --offline 参数，或者直接运行，程序检测不到 Key 时会自动降级为本地静态分析。

问：怎么换成 Qwen 或 OpenAI。
答：设置 LLM_BASE_URL 和 LLM_MODEL 两个环境变量即可，代码不用动。

问：为什么不用 LangChain。
答：这是教学和答辩用的项目，手写循环能把 Agent 的设计模式完整展示出来，包括决策、工具调度、上下文管理和容错，避免框架把核心逻辑挡住。

问：pylint 或 radon 没安装会怎样。
答：对应的工具会返回依赖缺失的信息，Agent 继续用其它工具工作，不会崩溃。
