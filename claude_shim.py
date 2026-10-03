# -*- coding: utf-8 -*-
"""
把 DeepSeek 网页版伪装成 Anthropic Messages API —— 让 Claude Code 直接用它当大脑。

原理：
    Claude Code  →  (Anthropic /v1/messages 格式)
                            ↓  转成一段带工具说明的提示词
                     DeepSeek 网页版（免费，浏览器自动化）
                            ↓  从回答里解析出 tool_use
                 ←  (Anthropic /v1/messages 响应格式)

启动：
    python claude_shim.py --port 8799

让 Claude Code 用它（另开一个终端，别污染你日常那个配置）：
    $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8799"
    $env:ANTHROPIC_AUTH_TOKEN = "dummy"
    claude

⚠️ 三条实话：
  1. **慢**。每轮要浏览器点一遍网页，10-60 秒。Claude Code 一个任务几十轮
     → 可能要几十分钟。真 API 是秒级。
  2. **提示词有大小上限**。Claude Code 的工具定义 + 系统提示 + 历史记录很大，
     超过 MAX_PROMPT_CHARS 会被截断，可能出错。
  3. **可靠性没测到生产级**。简单场景 100%，但 20 个工具 × 50 轮会怎样，未知。
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_ask as ds          # noqa: E402

# 提示词上限。实测输入框能吃 100 万字以上（没探到底），而且 box.input() 不是
# 逐字敲、是瞬间灌入的（实测 200 万字符/秒）—— 所以这里根本不是瓶颈。
# 之前设 60000 纯属自己吓自己，导致长对话一切过来就被砍、把输出规则都砍没了。
# 现在放到 30 万：既不会被截断，也给模型留了合理的处理量。
MAX_PROMPT_CHARS = 300000

# 深度思考开关。两种切法：
#   ① 模型名带 think：ANTHROPIC_MODEL=deepseek-web-think
#      → Claude Code 里 /model 就能中途切，不用重启
#   ② HTTP 开关：GET /think?set=on|off
#      → 终端里 dscthink on / dscthink off
DEFAULT_THINK = False
_think_state = {'on': DEFAULT_THINK}

# 智能搜索开关。切法和 think 一样：
#   ① 模型名带 search：ANTHROPIC_MODEL=deepseek-web-search → /model 中途切
#   ② HTTP 开关：GET /search?set=on|off  →  终端里 dscsearch on / dscsearch off
#   ③ 模型自己按需申请：回复里输出 [[SEARCH]] 标记（见 NEED_MARKER_RE）
#
# ★ 为什么默认关，且主要靠 ③：开着搜索时每一轮都要先联网搜一轮，
#   首字 30 秒起步。常开等于把每一轮都拖慢，长任务直接不能用。
DEFAULT_SEARCH = False
_search_state = {'on': DEFAULT_SEARCH}

# 全量发送时最多带多少条历史。
# 为什么要有这个：把一晚上的长对话（实测 631 条消息 = 91 万字）第一次切到
# shim 时，全量发过去会被截断到 6 万字 —— 输出规则和近半上下文全丢，模型
# 直接输出乱码。只带最近的若干条，既保住了格式规则，也保住了「眼下在聊什么」。
MAX_HISTORY_MSGS = 24

# Claude Code 压缩上下文后，消息计数会和网页对话对不上。
# 这时不重开对话，改发最近的这几条来「重新对齐」——
# 网页那边有压缩前的完整历史，只要给它最近几条就能接上话。
REBASE_MSGS = 6

# 模型写出坏工具调用时，最多重问几次（不含第一次）。
#
# ★ 为什么要多次：这类毛病**不是偶发**，而是模型写命令时不转义引号这种
#   系统性习惯 —— 实测同一个错连犯两次。只重试一次的话，第二次撞上同一个
#   习惯就认输，把 JSON 原文当回答交给 Claude Code，那一轮没有工具可调、
#   会话就停在提示符上等用户手打「继续」。
#   每次重试开新对话、带全量上下文，最坏 ~60 秒，所以别调太大。
MAX_RETRIES = 2

# ── 会话映射表：Claude Code 的 session_id  →  网页对话 ──
# Claude Code 每个请求都会在 metadata.user_id 里带上自己的 session_id，
# `claude -r` 恢复会话时这个 id 不变 —— 所以可以把「一个 Claude 会话」和
# 「一个 DeepSeek 网页对话」绑死，之后每轮只发增量，不用重发全部历史。
SESSIONS_FILE = os.path.join(os.path.expanduser('~'), '.deepseek_shim_sessions.json')

# 注意：这里**没有**进程内的浏览器锁 —— 串行化交给 ds.browser_lock(key)，
# 它按会话分锁，不同会话能真并行。加一把全局的会把并行优势全吃掉。
_sessions_lock = threading.Lock()  # 保护会话表文件

def load_sessions():
    try:
        with open(SESSIONS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_sessions(d):
    try:
        with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:
        ds.log(f'[警告] 会话表存不下来：{e}')


def session_id_of(req):
    """从 metadata.user_id 里挖出 Claude Code 的 session_id。"""
    uid = (req.get('metadata') or {}).get('user_id')
    if isinstance(uid, str):
        try:
            return json.loads(uid).get('session_id')
        except Exception:
            return None
    if isinstance(uid, dict):
        return uid.get('session_id')
    return None


def fingerprint(system, tools):
    """
    只按【系统提示】算指纹，故意不含工具列表。

    为什么排除工具：Claude Code 的 MCP server 是**异步加载**的 ——
    第一个请求只有 46 个工具，等 MCP 连上，第二个请求就变成 109 个。
    如果把工具算进指纹，第二轮必然「变了」，会话复用永远触发不了。

    代价：后来才连上的工具，模型不知道它们存在。用 --strict-mcp-config
    关掉 MCP 就没这个问题（本来也建议关）。
    """
    import hashlib
    return hashlib.sha1(
        json.dumps(system, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


# ============================================================
# 提示词构造：把 Anthropic 的请求压成一段能塞进对话框的文字
# ============================================================

OUTPUT_RULES = """
═══ 输出格式 ═══

**情况一：需要调用工具**

**先用一句话说明你接下来要干什么**（不超过 100 字，就像跟人说话那样，
比如「先看看目录里有什么」「这三处我一起改掉」），**然后另起一段**输出 JSON。

★ **JSON 不要包在 markdown 代码块里** —— 不要 ```json、不要 ```，直接写 `{`。
  包了围栏，我们会把**围栏里那坨 JSON 本身**当成「你要写的内容」挂到工具参数上，
  结果是给工具塞一个它根本没有的参数，上游直接报「参数非法」，整轮白费。
  （实测踩过：模型给 Read 调用加了围栏，被塞进一个 `content` 参数。）

  一个工具： {"tool_use": {"name": "<工具名>", "input": {<参数>}}}

  多个工具： {"tool_use": [{"name": "<名1>", "input": {<参数1>}},
                          {"name": "<名2>", "input": {<参数2>}}]}

★ 那句话是**给用户看的** —— 他在终端上只看得见你调了哪些工具，不说明一句，
  他完全不知道你在干嘛、要往哪走。**一句话就够**，别写小作文：说多了每轮都变慢。

  ⚠️ **例外**：这一轮要是想申请联网 / 深度思考，**就不要说这句话** ——
     那是下面「情况三」，整条回复**只写那个方括号标记**。
     **光用嘴说「我先联网搜一轮」没有任何用**：我们看不见你的想法，
     那一轮什么都不会发生，用户只会看到一句空话然后卡在那儿。

★ **几件事互不依赖时，一次全列出来** —— 比如一次读好几个文件、跑几条独立命令。
  不要一个一个来：每多一轮就要多等十几秒，读 20 个文件就是 20 轮。
  只有「下一步依赖上一步结果」时才分开。

★ **要看多张图时，把所有 Read 一次全列出来**（一条消息最多 50 张，够用）。
  图片会作为附件**一起**传上去，你能**同时看到全部** ——
  没有「一次只能看一张」这回事，别一张一张来。
  一张一张来要多花好几倍时间，而且毫无好处：每多一轮就多等十几秒，
  看 20 张图就是 20 轮。一次列全，一轮就能看完。

★ **要放长文本（文件内容、整段代码、命令）时，不要塞进 JSON 字符串里。**

  改成：JSON 里只写其他参数，长文本**原样放在 JSON 后面的代码块里** ——
  这样**一个字符都不用转义**，引号、反斜杠、换行照写：

    {"tool_use": {"name": "Write", "input": {"file_path": "D:\\\\创业\\\\a.py"}}}
    ```python
    # 这里原样写文件内容 —— 引号、反斜杠、换行都不用管
    def f():
        return "hello"
    ```

    {"tool_use": {"name": "PowerShell", "input": {"description": "跑一下自测"}}}
    ```
    py -3.11 selftest.py --fast 2>&1; Write-Output "EXIT=$LASTEXITCODE"
    ```

  ★ **命令一律走代码块**，不要写在 JSON 字符串里 —— 命令里几乎一定有引号，
    塞进 JSON 就得转义，而转义一错**整个工具调用就废了**（实测反复栽在这上面）。
  （只有路径、文件名这类短参数才照常写在 JSON 里。）
  Windows 路径的反斜杠写两个：D:\\\\创业\\\\file.txt

**情况二：不需要工具，直接回答用户**

★ 用**正常的 Markdown 直接回答**，不要包 JSON、不要包代码块、不要加任何包装 ★

就像平时聊天那样写就行。想写多长写多长，可以用标题、列表、表格、代码块。
不需要考虑转义，不需要管格式限制。

（之所以这样区分：工具调用很短，包成 JSON 稳妥；但最终回答可能几千字，
塞进 JSON 字符串会因为换行和引号转义而报废 —— 实测翻过车。）

**情况三：这一轮需要联网搜索，或者需要更深的推理**

★ 你要是判断**这件事需要最新信息**（新闻、价格、版本号、今天发生的事、
  你拿不准的事实），或者**需要慢慢推理才做得对**（复杂算法、架构设计、
  容易想错的逻辑），**这一轮不要硬答** —— 整条回复**只有那个标记**，
  一个字都别多写：

    [[SEARCH]]        想先联网搜一轮
    [[THINK]]         想开深度思考
    [[SEARCH+THINK]]  两个都要

  我们看到标记会替你打开对应的开关，然后**把这个问题重新发给你一次**。
  那一轮你就能用上搜索 / 思考了，正常作答即可。

  ⚠️ **不要写成句子**。写成「我先联网搜一轮」这种话我们是**看不见**的 ——
     只认方括号标记。写成句子，那一轮就白过了，用户只看到一句空话。
  ⚠️ 这一轮**不要**再按「情况一」先说一句说明 —— 申请开关的时候，
     整条回复就只是那个标记。

★ **别滥用**：搜索每轮要多等 30 秒以上，深度思考每轮要好几分钟。
  日常问答、读文件、改代码、跑命令，都不需要。判断不了就别加，
  硬答一版出来比空等一轮强。
"""


# 没有工具时用的输出规则。
#
# ★ 为什么要单独一份：实测发现，只要输出规则里写着「需要工具时输出 JSON」，
#   哪怕「可用工具」那节明说没有工具，模型还是会硬编一个工具名出来
#   （见过 none / noop / no_tool / no_tool_available）。两句话互相打架。
#   没有工具的时候，就**根本不要提工具这回事**。
OUTPUT_RULES_NO_TOOLS = """
═══ 输出格式 ═══

这次没有配置任何工具，你只能直接用文字回答。

★ 用正常的 Markdown 直接回答，不要包 JSON、不要包代码块、不要尝试调用工具 ★
就像平时聊天那样写就行，想写多长写多长。
"""


# 工具描述的截断长度。名字 + 参数 schema 才是重点，说明留个头就够。
DESC_LIMIT = 400

# 但这几个是例外 —— 它们是**交互类**工具，「什么时候该用」那段长说明才是关键，
# 砍到 400 字就等于没教。
# ★ 实测：这几个的描述原先一律被截到 400 字，模型压根不知道有这回事，
#   于是 dsc 里从来不弹 AskUserQuestion、从来不进 plan mode、从来不列 todo。
# ★ 只给这几个放宽，**刻意不全局放宽** —— Claude Code 一次发 100+ 个工具，
#   全局放宽会让每轮提示词凭空涨几十 K，每轮都变慢（schema 本来就不截断）。
DETAILED_DESC_TOOLS = {
    'AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode', 'TodoWrite', 'Task',
}
DESC_LIMIT_DETAILED = 2000


def render_tools(tools):
    if not tools:
        # 必须说得够狠 —— 实测只写「没有可用的工具」，模型会凭空编一个
        # 工具名出来（见过 noop / no_tool_available 这种）。
        # 刻意不提那个 JSON 关键字 —— 一提模型就会在回答里复述它，
        # 而 looks_broken() 会被这种复述误触发。
        return '（本次没有配置任何工具。你只能直接用文字回答，不要尝试调用工具。）'
    out = []
    for t in tools:
        name = t.get('name')
        limit = DESC_LIMIT_DETAILED if name in DETAILED_DESC_TOOLS else DESC_LIMIT
        schema = t.get('input_schema') or t.get('parameters') or {}
        out.append(json.dumps({
            'name': name,
            'description': (t.get('description') or '')[:limit],
            'input_schema': schema,
        }, ensure_ascii=False))
    return '\n'.join(out)


# 教模型用「交互类工具」。
#
# ★ 为什么必须有这一段：这些工具**确实在** Claude Code 发来的工具清单里，但我们
#   从不告诉模型「什么时候该用」。而 DeepSeek 网页版不是 Claude —— 没人专门训练
#   它去用这些 agentic 工具。**你不教，它就不会用**，只会闷头调 Read/Write/Bash。
#   实测症状：dsc 里从来不弹选择框、从来不进 plan mode、从来不列任务清单 ——
#   而真实 API 直连时这些都会自然发生。
#
# ★ 这段必须【每一轮】都出现（见 build_prompt 和 build_delta_prompt）。
#   delta 路径原先不带任何规则，只写进新会话提示词的话，模型只有第一轮看得见。
TOOL_GUIDANCE = """
═══ 什么时候该用这几个工具 ═══

★ **有岔路口、要人拍板时，用 `AskUserQuestion` 问，不要自己猜。**
  典型场景：两种做法都说得通、要选技术方案、要确认删掉/覆盖某个文件、
  需求含糊到会明显影响结果。**猜错方向的代价远大于多问一句。**
  这条**只管「问人」**：能自己查清楚的（读文件、搜代码、跑命令）就先自己查。

★ **任务复杂时，先 `EnterPlanMode` 拿个方案再动手。**
  什么算复杂：要改三个以上文件、要动架构、要做技术选型、你心里没底。
  简单任务（改个错字、跑条命令、读个文件）直接做，别为了走流程而走流程。

★ **要动三步以上（哪怕每一步都很简单），先 `TodoWrite` 列个清单**，
  之后每完成一步更新一次。

  ⚠️ **这条和上面那条 AskUserQuestion 是两回事**：
     · `AskUserQuestion` 是**问人** —— 能自己查清楚的就别去烦用户；
     · `TodoWrite` 是**给自己记账** —— 跟「能不能自己查」**没关系**。
       就算每一步都只是读个文件、跑条命令，只要有三步以上，也该先列清单。
       实测：把「能自己查的就先自己查」套到 TodoWrite 上，模型就再也不列清单了
       （给个三步任务，它直接闷头开干）。
     · 清单是给用户看的进度，也是给你自己记的账 —— 长任务里很容易忘了还剩哪几件。

★ **工具名必须和「可用工具」清单里的一字不差。** 尤其注意：这台机器上跑命令的
  工具叫 `PowerShell`，**不叫 `Bash`**。名字写错那个调用会被直接丢弃，整轮白费。
"""


ATTACH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_attachments')

_EXT_OF = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
           'image/webp': 'webp', 'application/pdf': 'pdf'}


_last_cleanup = 0.0


def cleanup_attachments(max_age_hours=24, min_interval=3600):
    """
    清理 _attachments/ 里的旧文件。自带节流，调用方随便调。

    为什么要清理：每张图片/PDF 都会 base64 解码落一个临时文件，长会话跑一晚上
    能堆出几十上百个。文件本身不大，但没有上限地涨是不对的。

    策略：清掉超过 max_age_hours 的文件，最多每小时扫一次目录。
    刻意**不删刚写入的** —— Chrome 可能还占着句柄，正在上传的更不能动。
    """
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < min_interval:
        return
    _last_cleanup = now
    try:
        if not os.path.isdir(ATTACH_DIR):
            return
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        for name in os.listdir(ATTACH_DIR):
            p = os.path.join(ATTACH_DIR, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass        # 被占用就跳过，下次再说
        if removed:
            ds.log(f'[附件] 清理了 {removed} 个过期临时文件')
    except Exception:
        pass


def extract_attachments(messages):
    """
    把消息里的图片/文档块解出来，存成临时文件，返回路径列表。

    Claude Code 用 Read 工具读图片时，返回的是 image content block
    （base64 内嵌）。这种东西直接当文本发过去就是一坨乱码 ——
    得存成真文件、走网页的上传接口。
    """
    import base64
    paths = []

    def take(b):
        src = b.get('source') or {}
        data = src.get('data')
        if not data:
            return
        mt = src.get('media_type') or 'image/png'
        ext = _EXT_OF.get(mt, 'bin')
        os.makedirs(ATTACH_DIR, exist_ok=True)
        p = os.path.join(ATTACH_DIR, f'att_{uuid.uuid4().hex[:12]}.{ext}')
        try:
            with open(p, 'wb') as f:
                f.write(base64.b64decode(data))
            paths.append(p)
        except Exception as e:
            ds.log(f'[附件] 解码失败：{e}')

    def walk(blocks):
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get('type')
            if t in ('image', 'document'):
                take(b)
            elif t == 'tool_result':
                c = b.get('content')
                if isinstance(c, list):
                    walk(c)

    for m in messages:
        c = m.get('content')
        if isinstance(c, list):
            walk(c)
    return paths


def render_block(b):
    """把 Anthropic 的一个 content block 渲染成文字。"""
    if isinstance(b, str):
        return b
    t = b.get('type')
    if t == 'text':
        return b.get('text', '')
    if t == 'tool_use':
        return (f'[我调用了工具 {b.get("name")}，参数：'
                f'{json.dumps(b.get("input"), ensure_ascii=False)}]')
    if t == 'tool_result':
        c = b.get('content')
        img_note = ''
        if isinstance(c, list):
            kinds = [x.get('type') for x in c if isinstance(x, dict)]
            if any(k in ('image', 'document') for k in kinds):
                # ★ 这句话是必须的。实测：只写「已作为附件上传」，模型会**再调一次
                #   Read 去读它**（它没意识到附件就是刚才要读的内容），于是陷入死循环，
                #   读 21 张图要 21 轮还读不完。明确告诉它「附件就是内容」才行。
                img_note = ('\n★ 上面这个附件**就是**这个文件的内容本身 —— '
                            '直接看附件里的图回答，**不要再调工具去读它**。')
            c = '\n'.join(x for x in (render_block(x) for x in c) if x)
        flag = '（执行出错）' if b.get('is_error') else ''
        return f'[工具返回结果{flag}]\n{c}{img_note}'
    if t == 'thinking':
        return ''            # 思考块不回喂，省空间
    if t in ('image', 'document'):
        # 图片/文档走附件上传（见 extract_attachments），这里只留个占位说明
        name = (b.get('source') or {}).get('media_type', '文件')
        return f'［已作为附件上传：{name}］'
    return json.dumps(b, ensure_ascii=False)


def build_prompt(system, messages, tools):
    parts = []

    if system:
        if isinstance(system, list):
            system = '\n'.join(render_block(b) for b in system)
        parts.append(f'═══ 系统提示 ═══\n{system}')

    parts.append(f'═══ 可用工具 ═══\n{render_tools(tools)}')
    parts.append(OUTPUT_RULES if tools else OUTPUT_RULES_NO_TOOLS)
    if tools:
        # 交互类工具的用法。必须每轮都在 —— 见 TOOL_GUIDANCE 上面的说明。
        parts.append(TOOL_GUIDANCE)

    # ★ 到这儿为止都是「头」，截断时原样保住。用 len() 记下来，**不要写死
    #   parts[:3]** —— 早先就是写死的，加一段就正好被切掉，而且不报错、日志干净。
    head_n = len(parts)

    # 历史太长只取最近的 —— 见 MAX_HISTORY_MSGS 的说明
    total = len(messages)
    trimmed = total > MAX_HISTORY_MSGS
    if trimmed:
        messages = messages[-MAX_HISTORY_MSGS:]

    convo = []
    for m in messages:
        role = m.get('role')
        content = m.get('content')
        if isinstance(content, list):
            text = '\n'.join(x for x in (render_block(b) for b in content) if x)
        else:
            text = str(content or '')
        if not text.strip():
            continue
        who = {'user': '用户', 'assistant': '助手'}.get(role, role)
        convo.append(f'【{who}】\n{text}')

    if convo:
        head = f'（前 {total - MAX_HISTORY_MSGS} 条较早的对话已省略）\n\n' if trimmed else ''
        parts.append('═══ 对话历史 ═══\n' + head + '\n\n'.join(convo))

    # ★★ 收尾这句也**必须跟着 tools 分岔**（第十八轮补）。
    #
    #   上面 OUTPUT_RULES_NO_TOOLS 已经说了「不要包 JSON、不要尝试调用工具」，
    #   而这一句原先无条件写「然后输出 JSON」—— 同一段提示词里两句话打架。
    #   实测（冒烟测试）模型整条回复都在**跟我们吵架**：
    #       「我不能按这个要求做 —— 你前面说『只回答两个字：收到』，后面又要求
    #         先说明再输出 JSON，这两条互相冲突，而且本次明确不允许调用工具」
    #   正经答案被挤到最后一句。
    #
    #   ★ 为什么容易漏：`build_prompt` 里那个 `OUTPUT_RULES if tools else ...`
    #     的分岔看着像「已经处理过了」，于是**开头处理了、结尾忘了**。
    #     同一个错在这一版里出现了**三处**（build_prompt 结尾、build_delta_prompt
    #     的 JSON 那句、以及 delta 的工具引导）—— 典型的「分岔只做了一半」。
    parts.append('现在轮到你（助手）回复。记住：先一句话说明你要做什么，然后输出 JSON。'
                 if tools else
                 '现在轮到你（助手）回复。**这次没有配置任何工具**，'
                 '直接用正常的 Markdown 回答就行，不要包 JSON、不要尝试调用工具。')

    text = '\n\n'.join(parts)
    if len(text) > MAX_PROMPT_CHARS:
        # 保头（系统提示 + 工具定义 + 输出规则 + 工具引导）保尾（最近的对话）
        head = '\n\n'.join(parts[:head_n])
        keep_tail = MAX_PROMPT_CHARS - len(head) - 200
        ds.log(f'[警告] 提示词 {len(text)} 字，超限，截断到 {MAX_PROMPT_CHARS}')
        text = head + '\n\n（中间历史已省略）\n\n' + text[-keep_tail:]
    return text


# ============================================================
# 解析网页版的回答
# ============================================================

# 匹配「盘符开头的 Windows 路径」（含已经转义过的片段）
_WIN_PATH_RE = re.compile(r'[A-Za-z]:\\(?:[^"\\]|\\.)*')


def _escape_win_paths(s):
    """
    把 Windows 路径里的单反斜杠补成双的。

    ★ 为什么单独做这一步：JSON 里 `\\b` `\\f` `\\n` `\\r` `\\t` `\\u` 都是
      **合法转义**，所以「补非法转义」那一步不会碰它们 —— 而模型写路径时
      恰恰不会转义，于是路径被静默吃掉：
          C:\\Users\\...\\buck_hw\\figs.py
      解析成 C:\\Users\\...<退格>uck_hw<换页>igs.py
      结果文件写到奇怪的地方去，而且**不报错**。
    """
    def fix(m):
        seg = m.group(0)
        # 先按已转义的 \\ 切开，剩下的段里把单个 \ 都变成 \\
        return '\\\\'.join(re.sub(r'\\', r'\\\\', p) for p in seg.split('\\\\'))
    return _WIN_PATH_RE.sub(fix, s)


_VALID_ESCAPES = set('"\\/bfnrtu')


def _fix_invalid_escapes(s):
    """
    补上「非法转义」的反斜杠（\\U \\M \\D 这种 JSON 不认的）。

    ⚠️ 必须**扫描**而不是用正则 `\\\\(?!["\\\\/bfnrtu])` —— 那种写法在处理
       `\\\\U`（已转义的一对）时，会去检查**第二个**反斜杠，后面跟着 U 就被
       判成非法，又补一个变成三个反斜杠，把前面修好的路径重新搞坏。
       （踩过：路径修复明明对了，解析却还是失败。）
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt in ('\\',) or nxt in _VALID_ESCAPES:
                out.append('\\' + nxt)      # 已转义的 / 合法的，原样保留
                i += 2
                continue
            out.append('\\\\')              # 非法转义 → 补成一对
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _loads_lenient(s):
    """
    宽松解析。

    ★ 顺序很重要：**Windows 路径的修复必须放在最前面、无条件执行**。
      因为 `"C:\\a\\buck_hw\\figs.py"`（单反斜杠）**本身是合法 JSON** ——
      它会顺利解析成 `C:\\a<退格>uck_hw<换页>igs.py`，不报错、也走不到兜底分支。
      只有先修再解析才拦得住。踩过：文件路径被静默搞坏。
    """
    # ① 先把路径里的单反斜杠补好（对已转义的 \\ 无影响）
    # ② 再补「非法转义」的反斜杠
    fixed = _fix_invalid_escapes(_escape_win_paths(s))
    try:
        return json.loads(fixed)
    except Exception:
        pass
    # ③ 兜底：万一上面改坏了什么，原样再试一次
    try:
        return json.loads(s)
    except Exception:
        return None


def _norm_tool(obj):
    """把各种写法的单个工具调用归一成 {'name':..., 'input':{...}}。"""
    if not isinstance(obj, dict):
        return None
    # ★ 名字键走共用词汇表（name / tool / tool_name）—— 和 `_looks_like_call`、
    #   `_MAYBE_CALL_RE` 是同一份。三处各认各的，缝就是这么来的（见词汇表说明）。
    name = next((obj.get(k) for k in _TOOL_NAME_KEYS
                 if isinstance(obj.get(k), str) and obj.get(k).strip()), None)
    if not name:
        return None
    # 参数可能叫 input / arguments / parameters / args —— 各家格式不同
    for k in _TOOL_PARAM_KEYS:
        v = obj.get(k)
        if isinstance(v, dict):
            return {'name': name.strip(), 'input': v}
    # ★ 平铺写法：参数直接摊在顶层
    #       {"tool": "PowerShell", "command": "Get-Location"}
    #   除名字键/外层键以外的字段全收进 input。**不收就等于把参数丢掉** ——
    #   调用会以「缺 command」被拦下重试，白烧一个来回（实测见过这个形状）。
    flat = {k: v for k, v in obj.items()
            if k not in _TOOL_NAME_KEYS and k not in _TOOL_WRAP_KEYS}
    return {'name': name.strip(), 'input': flat}


def _tools_from(obj):
    """
    从一个 JSON 对象里抠出工具调用列表（可能是 0 个、1 个或多个）。
    """
    if not isinstance(obj, dict):
        return []

    # ① {"tool_use": {...}} / {"tool_use": [{...}, {...}]}   ← 我们教它的格式
    # ② {"tool_calls": [...]}                                ← OpenAI 风格
    # ③ {"tool_call": {...}}
    for key in ('tool_use', 'tool_calls', 'tool_call', 'tools_use'):
        v = obj.get(key)
        if isinstance(v, dict):
            one = _norm_tool(v)
            return [one] if one else []
        if isinstance(v, list):
            return [t for t in (_norm_tool(x) for x in v) if t]

    # ④ 裸的 {"name":..., "input"/"arguments":...}
    one = _norm_tool(obj)
    return [one] if one else []


# ── 工具调用的「键名词汇表」 ────────────────────────────────
#
# ★ **三处识别必须共用这一份**：`_looks_like_call()`（对象级判断）、
#   `_MARKER_RE`（文本里扫候选位置）、`looks_broken()`（兜底重试）。
#   各写各的就会长出缝来 —— 这个项目已经在「同一个职责两份实现」上栽过四次
#   （前端序列、工具识别表、流式字节写入、以及这次的**名字键**）：
#   **改了一份、忘了另一份，而且失败得很安静。**
_TOOL_WRAP_KEYS = ('tool_use', 'tool_calls', 'tool_call', 'tools_use')
# ★ 名字键**必须和 `_norm_tool` 认的那套完全一致**（它收 name / tool / tool_name）。
#   实测（听刻会话 2026-10-03 23:31）：`_norm_tool` 明明认 `obj.get('tool')`，
#   而标记表和 `_looks_like_call` 只认 `name` —— 于是
#       {"tool": "PowerShell", "input": {"command": "..."}}
#   参数**是嵌套的、完全合规**，却从来轮不到 `_norm_tool` 去看它一眼。
_TOOL_NAME_KEYS = ('name', 'tool', 'tool_name')
_TOOL_PARAM_KEYS = ('input', 'arguments', 'parameters', 'args')

# 「平铺」写法用的参数键：模型偶尔把参数直接摊在顶层
#     {"tool": "PowerShell", "command": "Get-Location"}
# 只收**工具专用**的那几个。`description` / `content` 这种太通用的**不收** ——
# 正常回答里举 JSON 例子经常带它们，收进来就是把回答误判成工具调用。
_TOOL_FLAT_KEYS = ('command', 'file_path', 'new_string', 'old_string')


# 「这段文本里**可能**藏着工具调用」的结构判据。
#
# ★ 它只回答「值不值得停下来试一次」，**不回答「是不是」** —— 在**解析**那条
#   路上，认不认由 `_looks_like_call()` 兜底，所以放宽空白和键名不增加误判面。
#
# ★ 但 `looks_broken()` 那条路**没有这层兜底**（它就是最后一道），所以那边
#   不能直接拿这个正则当结论 —— 它得先配平、解出对象来问 `_looks_like_call()`，
#   只有**所有候选都解不出来**时才退回文本级（见那里的 ④ / ⑤）。
#   同一个正则，在两个位置承担的责任不一样 —— 这一点当初「拆成两张表」
#   就是这个道理，别看到现在共用一份就以为可以随手再放宽。
#
# ★ 为什么是正则、而不是早先那几个字面量：`{"name"` / `{ "name"` 只认「紧跟」
#   和「一个空格」。而模型是会 pretty-print 的：
#       {
#         "name": "AskUserQuestion",
#   这样一个都命中不了，扫描根本不会去看那个位置。实测（听刻会话 23:31）：
#   同一段文本只差一个换行加缩进，一个废一个通，**用户那一轮直接结束**。
_MAYBE_CALL_RE = re.compile(
    r'\{\s*"(?:%s)"' % '|'.join(_TOOL_WRAP_KEYS + _TOOL_NAME_KEYS))

# `_find_tool_call_span` 靠它按位置扫候选（兼容旧名）
_MARKER_RE = _MAYBE_CALL_RE


def _find_tool_call_span(text):
    """
    找出第一段**能解析成工具调用**的 JSON，返回 (起, 止) 下标；找不到返回 (-1, -1)。

    ★ 抽出来是因为两处要用同一套逻辑：抠工具调用（`_extract_embedded_tools`）和
      「切掉工具调用再找内容块」（`_remove_tool_call_span`）。各写一遍迟早不一致。
    ★ 按**位置**逐个候选试，而不是「每种标记只看第一次出现」—— 早先的写法碰上
      「正文里先举个 JSON 例子、后面才是真调用」会整个错过。
    """
    for m in _MARKER_RE.finditer(text):
        idx = m.start()
        depth = 0
        for i in range(idx, len(text)):
            c = text[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    obj = _loads_lenient(text[idx:i + 1])
                    # 两道都要过：像调用 + 真能抠出工具来
                    if _looks_like_call(obj) and _tools_from(obj):
                        return idx, i + 1
                    break
    return -1, -1


# 「长内容放代码块」协议用的正则：抓 JSON 后面那个围栏代码块。
#
# ★ 必须**贪婪**匹配（吃到最后一个 ```），不能非贪婪。非贪婪会在内容里第一次
#   出现 ``` 时就收尾 —— 而内容里带围栏太正常了：写 .md、写带示例的脚本、
#   写这个项目自己的文档都是。结果就是**文件被静默截断**，只剩开头几行，
#   不报错、日志干净，打开文件才发现少了一大半。
#   宁可多吃（多出来的顶多是结尾几句说明）也不能少吃（那是数据损坏）。
_CODE_BLOCK_RE = re.compile(r'```[a-zA-Z0-9_+\-]*\r?\n(.*)\r?\n?\s*```', re.S)

# 哪个工具缺哪个字段时，用代码块补上
#
# ★ 这张表必须**写全**。漏了哪个工具，_attach_code_block 就会掉进下面那个
#   「填第一个空字段」的兜底分支：兜底按 ('content','command',...) 的顺序填，
#   于是 PowerShell 的代码块内容会被填进 `content`、而 `command` 空着 ——
#   发上去 Claude Code 直接拒，比不填还糟。
#   Windows 上 Claude Code 的命令工具叫 PowerShell（不是 Bash），实测踩过。
_BLOCK_FIELD = {
    'Write': 'content',
    'Bash': 'command',
    'PowerShell': 'command',
    'Edit': 'new_string',
    'NotebookEdit': 'new_source',
    # 子代理的长字段叫 prompt（不叫 content）—— 漏了它的后果见上面
    # 「不认识的工具什么都不填」那段注释。
    'Task': 'prompt',
}


# 非贪婪版代码块正则。**只在「多个调用各配一块」时用。**
#
# ★ 单个内容块时绝不能用它 —— 内容自带围栏很正常（写 .md、写带示例的脚本），
#   非贪婪会在内容里第一次出现 ``` 时就收尾，文件被静默截断。那个坑见
#   _CODE_BLOCK_RE 上面的说明（所以那一版是贪婪的，且保持不动）。
#   多块配对的场合不一样：那里我们**知道**应该有几块，可以拿数量对账。
_CODE_BLOCK_RE_NG = re.compile(r'```[a-zA-Z0-9_+\-]*\r?\n(.*?)\r?\n?\s*```', re.S)


def needy_calls(tools):
    """
    哪些调用**缺**那个必须由代码块补的长字段，返回 [(下标, 字段名), ...]。

    ★ 抽出来是因为两处要用同一套判据：补内容（_attach_code_block）和
      「形状分不清」时给重试提示（retry_reason）。各写一遍迟早不一致 ——
      这个项目栽过六次了。
    ★ 不认识的工具（_BLOCK_FIELD 里没有）**不算** needy：它本来就不该被填。
    """
    out = []
    for i, t in enumerate(tools or []):
        field = _BLOCK_FIELD.get(t.get('name'))
        if field and not (t.get('input') or {}).get(field):
            out.append((i, field))
    return out


# 代码块的**语言标记**能告诉我们它是「文件内容」还是「要跑的命令」——
# 多块配对时这是最可靠的信号（见 _attach_code_block 里那段说明）。
#
# ★ 只列**认得出来的**：没列的一律算「认不出」，两边都能配（保守）。
#   宁可让认不出的块保持弹性，也不要凭猜把它归到某一类。
_CMD_BLOCK_TAGS = {'powershell', 'ps1', 'ps', 'bash', 'sh', 'shell', 'zsh',
                   'cmd', 'bat', 'console', 'terminal', 'pwsh'}
_CONTENT_BLOCK_TAGS = {'python', 'py', 'python3', 'text', 'txt', 'plaintext',
                       'markdown', 'md', 'json', 'yaml', 'yml', 'toml', 'ini',
                       'javascript', 'js', 'typescript', 'ts', 'html', 'css',
                       'sql', 'csv', 'xml', 'java', 'c', 'cpp', 'h', 'go',
                       'rust', 'rb', 'php', 'vue', 'dockerfile'}


# 文档类的块：它们里面出现围栏是**正常内容**，不能拿 trim 去切。
# （见 trim_trailing_command_blocks）
_MARKUP_BLOCK_TAGS = {'markdown', 'md', 'text', 'txt', 'plaintext', 'rst',
                      'adoc', 'asciidoc'}


def _block_kind(match):
    """这一块是 'command' / 'content' / None（认不出）。"""
    tag = match.group(0).split('\n', 1)[0].lstrip('`').strip().lower()
    if tag in _CMD_BLOCK_TAGS:
        return 'command'
    if tag in _CONTENT_BLOCK_TAGS:
        return 'content'
    return None


def pair_blocks_by_kind(region, need):
    """
    按「这块是命令还是内容」把代码块配给缺字段的调用，返回 {调用下标: 内容}。

    配不上返回 None（调用方自己去拒绝/报错）。

    ★ 为什么需要这个：实测形状（第十八轮补二，日志「各块的样子」）——
        调用：Write(a.py), Write(b.txt)        ← 两个都要文件内容
        代码块：#1[python] 脚本 / #2[text] 测试文本 / #3[powershell] 运行命令
      第 3 块是**模型打算下一步跑的命令**，它这一轮压根没为它发工具调用。
      只数数量（2 个要补 vs 3 块）会判成「分不清谁配谁」→ 白重试一轮；
      而按语言标记一看就清楚：那两个 Write 要的是**内容**块，
      `powershell` 那块不是给它们的。
    实测这个形状在 4 轮对拍里出现了 5 次，每次都白烧一次重试。
    """
    cand = [(m, _block_kind(m)) for m in _CODE_BLOCK_RE_NG.finditer(region)]
    # ★ 一个可信标记都没有 → **不在这儿配**，交给「按个数配」那条路。
    #   全是「认不出」的时候，按顺序取前 N 块就是纯粹的猜：模型完全可能
    #   把命令块写在最前面（[命令, 脚本, 文本]），取前 2 块就把命令写进了
    #   .py 文件。**只有语言标记真的能区分时才用它**。
    if not any(k for _m, k in cand):
        return None
    used, assign = set(), {}
    for (i, field) in need:
        want = 'command' if field == 'command' else 'content'
        for j, (m, kind) in enumerate(cand):
            if j in used:
                continue
            if kind is None or kind == want:
                used.add(j)
                assign[i] = m.group(1)
                break
        else:
            return None                      # 有一个配不上 → 整体不动手
    return assign


def trim_trailing_command_blocks(region):
    """
    单个内容块后面又跟了**清一色命令块**时，切掉后面那截，返回内容；否则 None。

    ★ 为什么需要它（第十八轮补四，实测**每次长任务都中**）：
      模型写完文件正文后，习惯性地再贴一个「下一步要跑的命令」的代码块——

          ```python
          <文件正文>
          ```
          文件写好了，接下来跑一下确认：
          ```powershell
          cd ...; py -3.11 big.py 2>&1; Write-Output "EXIT=$LASTEXITCODE"
          ```

      上面那些代码块由**贪婪**正则（有意为之，见 _CODE_BLOCK_RE 的说明）一口吃下，
      于是**命令被当成了文件内容的一部分**。日志里的实证（尾 80 字）：

          [警告] 附上去的内容里还带围栏。长度 9127，
                 尾 '...run1\shim; py -3.11 big.py 2>&1; Write-Output "EXIT=$LASTEXITCODE"\n'

      ——4 轮长任务里触发了 **6 次**，每次都让模型多花好几轮去发现和清理
      （它自己的叙述：「第 273 行往后混进了围栏和 JSON，是写入时多带的尾巴」）。

    ★ 判据为什么敢下刀（而不是像之前那样只记不裁）：
      用**非贪婪**切一遍，看得见块边界。只有当
          第一块是**内容**块，且**后面每一块都是命令**块
      时才切。这个条件排掉了「内容自带围栏」那种情况 ——
      那时非贪婪会把一段内容切成好几块，后面那些块是**内容**（或认不出），
      不满足「清一色命令」，于是原样返回 None、走老路。
      （实测：写一个「里面同时有 python 和 powershell 示例」的 .md 就是这种，
        它会落到 None 分支，一个字符都不动。）

    ★ 仍然只是「多一层保护」：切不了就返回 None，绝不猜。
    """
    blocks = list(_CODE_BLOCK_RE_NG.finditer(region))
    if len(blocks) < 2:
        return None

    def tag(m):
        return m.group(0).split('\n', 1)[0].lstrip('`').strip().lower()

    kinds = [_block_kind(m) for m in blocks]
    if kinds[0] != 'content':
        return None                       # 第一块得**明确**是内容，不然不动手
    # ★ 第一块还得是**代码类**（python / js / sql…），不能是 markdown / text。
    #   理由：这项修复针对的是「写代码文件时尾巴多挂了一条命令」，
    #   而**文档类**文件里出现围栏是正常内容（写说明、写带示例的 README）。
    #   把 markdown/text 排除掉，就排掉了最可能被误裁的那一类。
    if tag(blocks[0]) in _MARKUP_BLOCK_TAGS:
        return None
    # ★ 后面每一块要么明确是命令，要么**没有语言标记**。
    #
    #   为什么允许「没标记」：实测模型经常把那条尾巴写成**裸围栏**——
    #       ```
    #       py -3.11 big.py; Write-Output "EXIT=$LASTEXITCODE"
    #       ```
    #   第一版要求「清一色 command」，于是**一次都没生效**（日志里全是
    #   「切不了」）。但「后面还有**内容**块」仍然必须排除 —— 那才是
    #   「内容自带围栏被切坏了」的形状。
    if not all(k in ('command', None) for k in kinds[1:]):
        return None
    return blocks[0].group(1)


def block_pairing_failed(region, tools):
    """
    补内容那一步会不会**放弃**（一块都不挂）？会的话返回 True。

    ★ 抽出来给两处共用：补内容（_attach_code_block）和「形状分不清」时给
      重试提示（retry_reason）。这个项目栽过六次「同一个职责两份实现」，
      而这两处**判反了更糟** —— 一边按「能配上」去配、另一边按「配不上」
      去报错，用户看到的提示就和实际行为矛盾。
    返回 (是否放弃, 块数, 缺字段的调用数)。
    """
    need = needy_calls(tools)
    n_blk = len(_CODE_BLOCK_RE_NG.findall(region))
    if len(need) <= 1:
        return False, n_blk, len(need)       # 单个走贪婪老路径，不会放弃
    if pair_blocks_by_kind(region, need):
        return False, n_blk, len(need)       # ① 按形态配得上
    return n_blk != len(need), n_blk, len(need)   # ② 只剩「按个数配」


def _attach_code_block(text, tools):
    """
    把 JSON 后面的代码块内容，补给工具调用里缺的那个长字段。

    ★ 为什么必须这么干：模型写文件内容时**几乎不可能每次都转义对** ——
      代码里的 `\"\"\"docstring\"\"\"`、`len("abc")` 会把 JSON 字符串提前截断，
      整个工具调用作废、任务中断。实测反复栽在这上面（写 Python 画图脚本）。
      与其要求模型「转义永远不出错」，不如**让它根本不用转义**：
      长文本放代码块里，原样写。

    兼容两种写法：内容在 JSON 里（短内容）→ 不动；内容在代码块里 → 补进去。

    ★★ 第十八轮补：**「一次多个调用 + 多个代码块」原来会静默写串**。
      老实现只找**一块**，然后把它补给**每一个**缺字段的调用 —— 于是
      「写脚本 + 写测试文本」这种一轮两个 Write 的活，两个文件拿到**同一份
      内容**；而贪婪的正则又把两块焊在一起，**围栏也混进了文件里**。

      这是 A/B 对拍（ab_check.py）跑一次就抓到的，而且是**模型自己先发现、
      替我们收拾的** —— 它的叙述原话：
          「sample.txt 被写成了脚本内容，我重写它」
          「gen_report.py 里被混进了 markdown 围栏，我重写干净的脚本文件再跑」
      也就是说这个 bug 一直在**偷轮数**：每次多花一两轮返工，日志干净、
      自测全绿，只有拿两个后端对着跑才露出来。

      修法：先数「有几个调用缺字段」，再按数量配对 ——
        · 缺 1 个 → 老路径（贪婪抓一块），行为一个字节都不变
        · 缺 N≥2 个 → 用非贪婪切出全部块；**数量正好对上才按顺序配**
          （对不上说明分不清谁是谁，宁可一块都不挂，让「缺 content」那条
            重试去报错 —— 大声失败 >> 悄悄写错文件）
    """
    if not tools:
        return tools

    # ★ 先把工具调用那段 JSON（连同包着它的围栏）切掉，再找内容块 —— 见
    #   _remove_tool_call_span 的说明。不切的话，贪婪的代码块正则会从
    #   「包 JSON 的那个围栏」一路吃到「内容那个围栏」，把 JSON 也当成内容。
    region = _remove_tool_call_span(text)

    # ★ 不认识的工具**什么都不填**（_BLOCK_FIELD 里没有它的名字就不在 need 里）。
    #   早先这里会「填第一个空的文本类字段」—— 那是瞎猜，而且猜错是**必然**
    #   被上游拒（参数非法，红的），比不填还糟：Read 没有 content、
    #   WebFetch 没有 command、Task 的长字段叫 prompt 不叫 content。
    #   要支持新工具就往 _BLOCK_FIELD 里加一条，别让它猜。
    need = needy_calls(tools)
    if not need:
        return tools

    if len(need) == 1:
        # 一个要补的 → 老路径（贪婪抓一块），行为一个字节都不变
        m = _CODE_BLOCK_RE.search(region)
        if not m:
            return tools
        body = m.group(1)
        # ★ 「贪婪」是有意的（内容自带围栏时才不会截断），但它的代价是
        #   **可能把后面的东西也吃进来**：模型写完正文爱再贴一个
        #   「下一步要跑的命令」的代码块，于是**命令被当成了文件内容**。
        #   实测 4 轮长任务里中了 6 次（见 trim_trailing_command_blocks）。
        #
        #   ★ 只在判据**明确**时才切（第一块是内容块 + 后面清一色命令块）；
        #     判不了就一个字符都不动 —— 裁错是静默截断，比不裁严重得多。
        if need[0][1] != 'command':
            trimmed = trim_trailing_command_blocks(region)
            if trimmed is not None and len(trimmed) < len(body):
                ds.log('[提示] 内容后面还跟着命令块 —— 已切掉，'
                       '免得命令被写进文件（%d 字 → %d 字）'
                       % (len(body), len(trimmed)))
                body = trimmed
            elif '```' in body:
                # 切不了（可能是内容自带围栏）→ 只记不改。
                # 记下来才有得统计：这个分支该不该有、有多少。
                ds.log('[警告] 内容里带围栏且切不了（可能是自带围栏，'
                       '也可能是尾巴垃圾）。长度 %d，尾 80 字：%r'
                       % (len(body), body[-80:]))
        fill = {need[0][0]: body}
    else:
        blocks = list(_CODE_BLOCK_RE_NG.finditer(region))
        bodies = [m.group(1) for m in blocks]

        # 两条配对路子，谁先成谁算 —— 都成不了就**一块都不挂**。
        #
        # ① 按形态配（首选）：看代码块的语言标记是「命令」还是「文件内容」。
        #    实测形状：调用是 [Write(a.py), Write(b.txt)]，块是
        #    [#1 python 脚本, #2 text 测试文本, #3 powershell 运行命令] ——
        #    第 3 块是**模型打算下一步跑的命令**，这一轮压根没为它发工具调用。
        #    只数数量会判成「分不清谁配谁」→ 白重试一轮（实测 4 轮里出现 5 次），
        #    而按语言标记一看就清楚：两个 Write 要的是**内容**块。
        _by_kind = pair_blocks_by_kind(region, need)
        if _by_kind:
            fill = _by_kind
            ds.log('[提示] %d 个调用按「命令/内容」配对成功（共 %d 块代码）'
                   % (len(fill), len(bodies)))
        # ② 按个数配：块数和「缺字段的调用数」正好相等 → 按出现顺序一一对应。
        elif len(bodies) == len(need):
            fill = {i: b for (i, _f), b in zip(need, bodies)}
            ds.log('[提示] %d 个调用各配一块代码块（按出现顺序）' % len(fill))
        else:
            # ★ 都成不了 —— **不猜**，一块都不挂，交给重试报错。
            #   分不清谁配谁还硬挂，就是把内容写进错的文件，而且不报错。
            #   把**每一块的样子**记下来，别只说「5 块代码」：实测这个分支会
            #   连着触发好几次（模型反复写同一种形状），日志只有一个数字的话，
            #   下一次还是只能靠猜它长什么样。语言标记 + 长度 + 头 30 字，
            #   一眼就能认出「它到底在写什么」。
            ds.log('[提示] %d 个调用缺长字段，切出 %d 块代码 —— 分不清谁配谁，'
                   '一块都不挂（交给重试报错，总比写错文件强）。各块的样子：%s'
                   % (len(need), len(bodies),
                      ' | '.join('#%d[%s]%d字:%r'
                                 % (i + 1,
                                    (blocks[i].group(0).split('\n')[0]
                                     .lstrip('`') or '(无)')[:12],
                                    len(b), b.strip()[:30])
                                 for i, b in enumerate(bodies))))
            return tools
    out = []
    for i, t in enumerate(tools):
        t = dict(t)
        if i in fill:
            inp = dict(t.get('input') or {})
            inp[_BLOCK_FIELD[t.get('name')]] = fill[i]
            t['input'] = inp
        out.append(t)
    return out


# 紧挨在工具调用 JSON 前后的围栏。模型有时会写成：
#
#     我这就写。
#     ```json
#     {"tool_use": {"name": "Write", "input": {"file_path": "..."}}}
#     ```
#     ```python
#     ...真正的文件内容（几千字）...
#     ```
#
# 中间的 ```json 是**包 JSON 的**，```python 才是内容开头。要能分辨这两者。
_FENCE_OPEN_BEFORE_RE = re.compile(r'```[a-zA-Z0-9_+\-]*[ \t]*\r?\n[ \t]*$')
_FENCE_CLOSE_AFTER_RE = re.compile(r'^[ \t]*\r?\n?[ \t]*```[ \t]*(?:\r?\n|$)')


def _remove_tool_call_span(text):
    """
    把工具调用那段 JSON（连同包着它的围栏）从文本里切掉，返回剩下的文本。

    ★ 为什么必须切：抓代码块的正则是**贪婪**的 —— 而且必须贪婪，否则
      「文件内容里自带围栏」时会被静默截断（那个坑踩过，代价是文件少一大半）。
      可贪婪一旦碰上「模型给 JSON 也加了围栏」，就会从**第一个** ``` 一路吃到
      **最后一个** ```，把 JSON 和围栏一起圈成「文件内容」。

      后果分两种，都不好：
        · 不拦 → **静默写出一份带 JSON 和围栏的垃圾文件**（数据损坏，最危险的一类）
        · 拦 → 白重试一轮（实测 e2e 就是这么红的）
      切开之后，贪婪正则只在自己那一块里贪婪，两个问题一起没了。
    """
    # ★ 第十八轮补四：**一段回复里可能有好几个工具调用**，而这里原来只切
    #   第一个。剩下那些留在文本里，会被贪婪的代码块正则当成「内容」圈进去
    #   —— 实测长任务的现场（模型自己的叙述）：
    #       「第 273 行往后混进了我上一条消息里的 markdown 围栏和 JSON，
    #         是写入时多带的尾巴」
    #       「正文到第 367 行结束，368 行往后都是围栏和 JSON 垃圾」
    #   于是模型写完文件要再花好几轮把垃圾抠掉（那一轮 16 轮 vs 对方 3 轮）。
    #   切干净是**没有争议**的：JSON 本来就不该出现在内容里。
    out = text
    for _ in range(8):                     # 上限防死循环，实际极少超过 2 个
        start, end = _find_tool_call_span(out)
        if start < 0:
            break
        # ★ 只有「工具调用确实被围栏包着」时才连围栏一起切。判据是**围栏数量的奇偶**：
        #
        #     我这就写。          ← 1 个围栏（奇数）→ 最后那个是「还没闭合的开头」
        #     ```json                → JSON 被包着，必须切
        #     {"tool_use": ...}
        #
        #     我这就写。          ← 2 个围栏（偶数）→ 最后那个只是**收尾**
        #     ```python              → JSON 没被包，后面/前面那个围栏是内容自己的
        #     <内容>                   一个字符都不能动，动了内容就丢
        #     ```
        #     {"tool_use": ...}
        #
        #   ★ 光看「紧邻的是不是 ```」分不出来 —— 包 JSON 的开头围栏和前一个
        #     内容块的收尾围栏**长得一模一样**。我第一版就是这么写错的，
        #     后果是**把已经拿到手的内容整段切掉**（静默丢数据，最危险的一类）。
        head_text = out[:start]
        if head_text.count('```') % 2 == 0:
            # JSON 没被围栏包着 —— 只把那一段 JSON 本身删掉，前后一个字不动
            out = out[:start] + out[end:]
            continue
        fm = _FENCE_OPEN_BEFORE_RE.search(head_text)
        if not fm:
            out = out[:start] + out[end:]
            continue
        # 括号配对交给 _find_tool_call_span 了 —— end 已经是那段 JSON 的结尾
        head = out[:fm.start()]
        tail = _FENCE_CLOSE_AFTER_RE.sub('', out[end:], count=1)
        out = head + tail
    return out


def _looks_like_call(obj):
    """
    这个 JSON 对象像不像一个工具调用。

    用于「夹带」场景，要求比外层解析更严 —— 因为这里是在一段普通文字里找，
    误判的代价是**把正常回答变成工具调用**，比漏判严重得多。

    ★ 键名一律走共用词汇表（见 `_TOOL_WRAP_KEYS` 上面那段）。这里原先只认
      `name`，而 `_norm_tool` 认 name / tool / tool_name 三个 ——
      **缝就是这么来的**：一种写法明明能归一化，却永远轮不到它被看见。
    """
    if not isinstance(obj, dict):
        return False
    if any(k in obj for k in _TOOL_WRAP_KEYS):
        return True
    # 裸写法必须**同时**有工具名和参数；光有 name 不算（正常回答里举例太多）
    has_name = any(isinstance(obj.get(k), str) and obj.get(k).strip()
                   for k in _TOOL_NAME_KEYS)
    if not has_name:
        return False
    # ① 参数字典（我们教的 input、OpenAI 的 arguments 都算）
    if any(isinstance(obj.get(k), dict) for k in _TOOL_PARAM_KEYS):
        return True
    # ② 平铺写法：名字键 + **工具专用**的参数键同框。`_TOOL_FLAT_KEYS` 只收
    #    command / file_path / new_string / old_string —— 正常回答里举例
    #    （`{"name": "张三", "age": 18}`）过不了这一关，所以放开它是安全的。
    return any(isinstance(obj.get(k), str) for k in _TOOL_FLAT_KEYS)


def _extract_embedded_tools(text):
    """
    从「先解释一段、再给工具调用」的混合输出里把工具调用抠出来。

    ★ 实测模型经常这么干 —— 它先说明情况（尤其在上一步失败之后），再把
      工具调用附在后面。而我们的解析要求「整段就是一个 JSON 对象」，
      不满足就整段当回答返回 —— 用户看到的是一坨 JSON 文本，任务直接断掉。
      （踩过：卸载软件时第一步失败，模型解释了原因再重试，就因为这段解释废了。）

    做法：找到疑似工具调用的开头，做括号配对切出那一段，能解出来就用。

    ★ 必须扫**全文**。早先截在前 20000 字符，理由是「免得挨个括号试太慢」——
      但括号配对是 O(n)，一次遍历而已，根本不慢。而**模型把长文件内容内联进
      JSON 时，工具调用本身就能超过 20000 字**（写 figs.py 这种画图脚本就是），
      于是配对永远到不了 depth 0 → 抠不出来 → 整坨 JSON 泄漏成「回答」，
      用户看到一屏 JSON，任务断掉。踩过。

    返回 `(tools, idx)` —— `idx` 是**成功解析出来的**那段 JSON 的起点，
    调用方靠它切出前面的说明文字（见 parse_reply 的第三个返回值）。
    没抠出来就是 `([], -1)`。
    """
    idx, end = _find_tool_call_span(text)
    if idx < 0:
        return [], -1
    return _tools_from(_loads_lenient(text[idx:end])), idx


# 夹带叙述的长度上限。提示词里跟模型说的是「不超过 100 字」，但它会飘 ——
# 这里兜一道，免得它写一屏（每多一个字，用户就多等一点）。
MAX_PROSE_CHARS = 400


def parse_reply(raw):
    """
    返回三元组 `(kind, payload, prose)`：

      ('tools', [{'name':..., 'input':{...}}, ...], '一句叙述')  ← 要调工具，可能带说明
      ('reply', '正文', '')                                      ← 直接回答

    ★ 为什么多返回一个 prose：真实 API 那边，模型是「先说一句我在干嘛、再调工具」，
      用户在界面上看得见它在往哪走。我们早先把这段说明**扔了** —— 用户在 dsc 里
      只看到一排光秃秃的工具调用，完全不知道模型在想什么（实测反馈：
      「dsc 基本上全是命令，只有最后会输出一段结果文字」）。
      叙述现在会作为 text 块，放在 tool_use 块**前面**（见 to_anthropic_tools）。

    ★ 三元组是**索引兼容**的：全仓调用方都只用 `r[0]`/`r[1]`，多一个元素不破坏
      任何现有代码（handler 两处 + selftest 约 30 处，逐个确认过）。

    设计要点：
      · **只有工具调用需要是 JSON，最终回答就是普通 Markdown。**
        解析不出工具调用就整段当作回答 —— 几千字的长回答不会因为 JSON
        转义问题报废（之前强制包 JSON，长回答必翻车）。
      · **容忍多种写法。** 实测模型会混用：
            {"tool_use":   {"name": ..., "input": ...}}       ← 我们教的
            {"tool_calls": [{"name": ..., "arguments": ...}]} ← OpenAI 风格
        只认一种的话，模型换个写法我们就把整坨 JSON 当「普通回答」返回，
        用户看到的是乱码、任务直接断掉（踩过：21 张图的整理任务废在这）。
      · **支持一次多个** —— 读 21 个文件时一次要 21 个 Read，不然就是 21 轮。
    """
    if not raw:
        return ('reply', '', '')

    t = raw.strip()

    # 只有「整个回答就是一个 JSON 对象」时才尝试解析（首字符 { 末字符 }）——
    # 这样既认得出工具调用，又不会把中间带代码块的 Markdown 回答误判成 JSON。
    # 反过来说：能进这个分支，就说明前面**没有**说明文字，prose 恒为空。
    stripped = re.sub(r'```(?:json)?\s*|\s*```', '', t).strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        i, j = stripped.find('{'), stripped.rfind('}')
        if i >= 0 and j > i:
            obj = _loads_lenient(stripped[i:j + 1])
            if isinstance(obj, dict):
                tools = _tools_from(obj)
                if tools:
                    # 长内容可能放在 JSON 后面的代码块里（不用转义）——
                    # 短内容直接写在 JSON 里也认
                    return ('tools', _attach_code_block(t, tools), '')
                # 兼容各种「回答」的包法。实测见过 reply / answer —— 模型
                # 对格式的想象力比我们以为的丰富，多认几种不会错。
                for k in ('reply', 'answer', 'response', 'text', 'content'):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return ('reply', v, '')

    # 整段不是 JSON —— 但可能是「先解释一段、再给工具调用」的混合输出。
    # 模型在上一步失败之后特别爱这么写。整段当回答返回的话，
    # 用户看到的就是一坨 JSON 文本、任务直接断掉。
    embedded, idx = _extract_embedded_tools(t)
    if embedded:
        ds.log(f'[提示] 模型在 {len(t)} 字的说明里夹带了 {len(embedded)} 个工具调用，已抠出来')
        # idx 之前那段就是叙述 —— 这就是用户要看的「中间文字」。
        # 末尾可能挂着个围栏开头（模型爱先写说明、再开一个 json 围栏包住 JSON），
        # 剥掉 —— 否则用户会看到一句以围栏结尾的怪话。
        prose = re.sub(r'```[a-zA-Z0-9_+\-]*\s*$', '', t[:idx]).strip()[:MAX_PROSE_CHARS]
        # ★ 这里也要补代码块 —— 「JSON + 后面跟代码块」走的正是这条路
        #   （整段不以 } 结尾，所以进不了上面那个分支）。漏了的话长内容就丢了。
        return ('tools', _attach_code_block(t, embedded), prose)

    return ('reply', t, '')


# 模型「申请开开关」的标记。整条回复就是它，别的什么都不写。
#
# ★ 为什么要有这个协议：真实 API 那边，模型想联网就直接调 WebSearch 工具 ——
#   「该不该搜」由它自己判断。网页版这边搜索是个手动开关，我们没法让模型去点，
#   于是约定：**它写标记，我们替它开好、再把问题重发一次**。
#   把判断权交给模型，比在 shim 里堆「最新/新闻/今天」这类关键词靠谱得多
#   （关键词法会误判：该搜的没搜、不该搜的乱搜还白等 30 秒）。
#
# ★ 必须匹配**整条回复**，不能只是「包含」：模型在回答里解释这个协议本身
#   （比如被问到「你怎么联网的」）时也会写出这几个字，那绝不能触发重搜。
NEED_MARKER_RE = re.compile(r'^\s*\[\[\s*([A-Za-z+\s]+?)\s*\]\]\s*$')


def parse_need_marker(raw):
    """
    模型是不是在申请开开关。返回 (want_search, want_think)。

    认两种写法：
      · 整条回复就是那个标记
      · **第一行**就是那个标记（模型有时会忍不住在后面再补一句 ——
        实测它甚至会只写「我先联网搜一轮」这种人话，那种认不出来，
        所以提示词里专门加了「不要写成句子」的警告）

    ★ 两种都要求那一行**只有**标记本身。`[[SEARCH]] 这个词的意思是……` 不算 ——
      模型解释这个协议本身时就是这种句子，误判的代价是白等 30 秒重搜一轮。
    """
    m = NEED_MARKER_RE.match(raw or '')
    if not m:
        m = NEED_MARKER_RE.match((raw or '').strip().split('\n', 1)[0])
    if not m:
        return False, False
    which = re.sub(r'\s+', '', m.group(1)).upper()
    return 'SEARCH' in which, 'THINK' in which


def looks_broken(raw):
    """
    模型想调工具但 JSON 写坏了。

    实测遇到的情况：模型把 \\n 写成了真换行，或者字符串里的 $ 之类字符被
    网页的 Markdown 渲染吃掉 —— 结果是一坨既不是合法 JSON、又明显在试图
    调工具的乱码。这种不能当成「最终回答」丢给 Claude Code，要重试。
    """
    if not raw:
        return False
    t = raw.strip()

    # ① 「开了个头就没了」：以 { 开头、却不以 } 收尾 —— 必然是**被截断的 JSON**，
    #    不可能是完整回答。
    #
    #    实测模型只吐出 `{"` 两个字就收工了。按下面那套判据它一个关键字都不含，
    #    于是被当成「正经回答」交给 Claude Code —— 界面上就是一个光秃秃的 `{"`，
    #    会话直接卡死在那儿（今天出现过 4 次）。这种回复只可能是坏的。
    if t.startswith('{') and not t.endswith('}'):
        return True

    # ② 判定「像不像在尝试调工具」必须认**带花括号的键**（{"tool_use" 这种），
    #    不能只认「文本里出现了 tool_use 这个词」—— 那样模型在正常回答里提一句
    #    「我不会输出 tool_use」就会被误判成坏调用，白重试一轮（踩过）。
    #
    #    但也不能**只**认「以 { 开头」：模型很爱先解释一句、再把调用附在后面，
    #    那种混合输出同样得触发重试，否则用户看到的就是一坨 JSON 文本。
    #
    #    ★ 判据走 `_MAYBE_CALL_RE`（共用词汇表 + 容忍空白）。早先这里是几个
    #      字面量（`{"name"` / `{ "name"`），pretty-print 成 `{\n  "name":` 就
    #      一个都命中不了 —— 解析漏、兜底也漏，两头都放行（缺陷 48）。
    if not (t.startswith('{') or _MAYBE_CALL_RE.search(t)):
        return False

    # ③ 有明确的外层键（tool_use / tool_calls / …）—— 只可能是想调工具
    if any('"%s"' % k in t for k in _TOOL_WRAP_KEYS):
        return True

    # ④ 把候选位置的 JSON 配平、解出来，问 `_looks_like_call()`
    #    —— **和「抠夹带调用」用的是同一个判据**，不另立一套。
    #
    #    ★ 为什么不能停在文本级的关键字同现（这是我自己踩的）：正常回答里
    #      举例  {"name": "张三", "input": "值"}  ——  `input` 是**字符串不是
    #      字典**，根本不像调用。可只要按「文本里同时出现 name 和 input」判，
    #      它就被当成坏调用 → 白重试一轮。自测的钉子用例当场抓到了这条回归。
    #    ★ 只要有一个候选**解出来了**，就以对象级判据为准、不再往下退 ——
    #      否则上面那个例子会从 ⑤ 漏回来（同一个误判换个地方发生）。
    for m in _MAYBE_CALL_RE.finditer(t):
        idx = m.start()
        depth = 0
        for i in range(idx, len(t)):
            c = t[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    obj = _loads_lenient(t[idx:i + 1])
                    if isinstance(obj, dict):
                        return _looks_like_call(obj)
                    break                 # 这一段解不出来 → 试下一个候选

    # ⑤ 所有候选都配不出平 / 解不出来 —— 那是**真写坏了**（被截断、转义炸了）。
    #    这时候做不了对象级判断，只能退回文本级：键词对得上就算。
    #
    #    ★ 这一支是**兜底重试**，保证「宁可白重试一轮，也绝不把一坨 JSON
    #      当回答交出去」。把失败伪装成「回答」是最坏的一种失败：看起来像成功，
    #      于是上游不会重试、用户只能手打「继续」，日志还干干净净
    #      （缺陷 31 的教训，这次换了个入口）。
    has_name = any('"%s"' % k in t for k in _TOOL_NAME_KEYS)
    has_params = any('"%s"' % k in t
                     for k in _TOOL_PARAM_KEYS + _TOOL_FLAT_KEYS)
    return has_name and has_params


# 模型（尤其 Claude 系）习惯把「跑命令」那个工具叫 `Bash` —— 它的训练里就是这名字。
# 而 Windows 上 Claude Code 给的工具叫 `PowerShell`。
#
# ★ 实测：它写出来的命令**已经是 PowerShell 语法**了
#   （Get-Location / Write-Output / [System.IO.Ports.SerialPort]::GetPortNames()），
#   唯独名字写成 Bash。直接改名就对了，比让它重发一轮省一整个来回。
# ★ 万一它真写的是 bash 命令，PowerShell 会报错，它看到错误自然会改 —— 不会更糟。
# ★ 只在「别名可用、原名确实不可用」时才改：两个都在就尊重模型的选择。
TOOL_ALIASES = {'Bash': 'PowerShell'}


def apply_tool_aliases(parsed, tools):
    """把模型叫错名字的工具改对。返回新的 parsed（没改就是原样）。"""
    if parsed[0] != 'tools':
        return parsed
    valid = {t.get('name') for t in tools}
    out, changed = [], []
    for t in parsed[1]:
        alias = TOOL_ALIASES.get(t.get('name'))
        if alias and alias in valid and t.get('name') not in valid:
            changed.append('%s→%s' % (t['name'], alias))
            t = dict(t, name=alias)
        out.append(t)
    if changed:
        ds.log(f'[提示] 工具名认错了，自动改名：{"、".join(changed)}')
        return ('tools', out, parsed[2])
    return parsed


def parse_and_align(raw, tools):
    """
    **解析模型回复的唯一入口**：先 parse_reply，再把认错的工具名改对。

    ★ 为什么要收成一个入口：`apply_tool_aliases(parse_reply(...))` 原先只写在
      **主路径**上，**重试路径漏了** —— 而重试恰恰是最容易写出 `Bash` 的地方
      （模型被要求把刚才那个调用重发一遍时，更依赖训练里的老名字）。
      实测日志对得上：

          02:30:12 [重试] 第 1/2 次：输出像是坏掉的工具调用，重新问一次
          02:30:23 [警告] 模型编造了工具 ['Bash']，可用的有 [...]… 丢弃
          02:30:23 [回答] 42 字          ← 「请换个方式提问」当回答，这一轮结束

      漏掉的后果不只是白烧一次重试。更坏的是**混合调用**：`[Bash, Read]` 里
      `Bash` 会被当成编造的名字**静默丢掉**，那条命令永远不执行 ——
      而模型以为自己跑了，接着往下走。

    ★ 收成入口而不是「记得两处都改」：这是这个项目反复犯的病（同一个职责
      有两份实现，改一份忘一份）。现在全仓只有这里调 parse_reply，
      加第三个解析点时也没法再漏 —— selftest 用 AST 钉住了这一点。
    """
    return apply_tool_aliases(parse_reply(raw), tools)


def should_retry(parsed, tools):
    """
    这次回复该不该「重问一遍」。

    条件：解析成了「回答」、看着像写坏了的工具调用、且这一轮确实带了工具。

    ★ 抽成独立函数是为了**能测** —— 这条规则原先内联在 HTTP handler 里，
      还挂着一个 `len(...) < 4000` 的长度闸门，把最需要重试的场景（模型把
      长文件内容内联进 JSON，工具调用上万字）全挡在外面，而测试根本够不着它。
      抽出来之后 selftest 能直接对着这条规则写正反用例。

    ★ 没有长度上限，是有意的：误判的代价是一轮重试（开新对话做，不污染主
      对话），漏判的代价是整个任务废掉 —— 用户看到一屏 JSON，只能重来。
    """
    if not tools:
        return False
    if parsed[0] != 'reply':
        return False
    return looks_broken(parsed[1])


def incomplete_tool(tools):
    """
    找出「必填的长字段是空的」那个工具调用。没有就返回 None。

    ★ 为什么必须有这道闸：Write 少了 content，发上去 Claude Code 只会回一句
      "Error writing file" —— 真正的原因（模型那条回复被网页的长度上限截断了，
      内容只写了一半）在这句话里**完全看不出来**，用户和模型都只能瞎猜。
      宁可我们这边拦住、重问一次，也不要放过去换一个看不懂的报错。

    ★ 只查「长字段」那一列（见 _BLOCK_FIELD）。短参数（路径、命令、行号）
      模型本来就直接写在 JSON 里，缺了说明它根本没想调这个工具。
    """
    for t in tools or []:
        field = _BLOCK_FIELD.get(t.get('name'))
        if field and not (t.get('input') or {}).get(field):
            return t
    return None


# 「模型压根没写正文」和「写到一半被截断」的分界（第十八轮补）。
#
# 两种情况的**现象一样**（Write 缺 content），但原因和建议完全不同：
#   短 + 没有代码块  → 模型只给了 file_path，正文一个字没写
#   长 / 有代码块    → 真的是被网页长度上限截断了
#
# 实测（16 份现场）：没写正文的那批是 147~341 字，被截断的那批是 2 万字上下，
# 中间空得很，2000 是个安全的分界。
OMITTED_BODY_CHARS = 2000


def retry_reason(parsed, tools, raw=None):
    """
    这条回复为什么**不能**就这么交给上游。返回 None = 可以放行。

    两类问题，都得重问（最多 MAX_RETRIES 次）：
      ① 看着像写坏了的工具调用（JSON 转义炸了）—— 见 should_retry()
      ② 解析出了工具调用，但必填的长字段是空的 —— 典型是 Write 没有 content

    抽成一个函数是为了**能测**：这两条规则原先散在 HTTP handler 里，
    selftest 够不着，正是当初能悄悄塞进一个错误闸门的原因。

    raw 传原始回复时，②还能再分出「被截断」和「压根没写」两种 —— 见
    OMITTED_BODY_CHARS。不传就按「被截断」给建议（保守：那套建议对两种情况
    都不算错，只是对「没写」那种不够对症）。
    """
    if should_retry(parsed, tools):
        return ('你上一条回复不是合法 JSON —— 长文本塞进字符串时转义出错了。'
                '**改用这个写法**：JSON 里只留路径等短参数，要写的文件内容、'
                '要跑的命令都**原样放在 JSON 后面的代码块里**，一个字符都不用转义：\n'
                '{"tool_use": {"name": "Write", "input": {"file_path": "..."}}}\n'
                '```\n（这里原样写内容）\n```\n'
                '★ **命令（PowerShell / Bash）一律这么写** —— 命令里几乎一定有引号，'
                '塞进 JSON 字符串就得转义，而转义一错**整个工具调用就废了**。')

    if not tools or parsed[0] != 'tools':
        return None

    # ★ 工具名全都不存在 —— 模型编了个名字。**绝不能回一句「请换个方式提问」
    #   当回答**：那是**助手消息**，Claude Code 收到就认为这一轮说完了、直接结束，
    #   用户只能手打「继续」。实测踩过：模型写对了 PowerShell 命令，只是名字叫成了
    #   Bash，整个会话就停在那儿，整轮白费。
    #   交给重试，把真实可用的名字告诉它。
    #   （部分名字错的情况不在这儿管 —— 那由调用方过滤掉坏的、留下好的。）
    valid = {t.get('name') for t in tools}
    bogus = [t['name'] for t in parsed[1] if t['name'] not in valid]
    if len(bogus) == len(parsed[1]):
        return ('你调用的工具名不存在：%s。\n'
                '**这次真正可用的工具名**（拼写和大小写都要一致）：%s\n'
                '请用上面某一个名字，把刚才那个调用原样重发一次。'
                % (bogus, '、'.join(sorted(valid)[:25])))

    bad = incomplete_tool(parsed[1])
    if not bad:
        return None
    field = _BLOCK_FIELD.get(bad.get('name'))

    # 命令类和文件内容类要给**完全不同的**建议：命令没有「分几段写」这回事，
    # 照搬下面那套会让模型去分段拼一条命令，越修越乱。
    if field == 'command':
        return ('你上一条回复**被长度上限截断了** —— 工具调用只写了一半：'
                '要调 %s，但 %s 是空的。这种调用发出去必然失败。\n\n'
                '**把命令原样写在 JSON 后面的代码块里**，一个字符都不用转义：\n'
                '{"tool_use": {"name": "%s", "input": {"description": "..."}}}\n'
                '```\n（这里原样写命令，引号照写）\n```\n'
                '不要为了塞进 JSON 字符串去转义引号 —— 实测转义一错整个调用就废。'
                % (bad.get('name'), field, bad.get('name')))

    # ★ 「缺 content」有**两种完全不同的原因**，给的建议也必须不同（第十八轮补）。
    #
    #   原来只有下面那套「你被长度上限截断了，要分段写」—— 而实测 16 份现场里
    #   有 6 份只有 147~341 字，离任何长度上限都远得很：模型只是**说了句
    #   「现在写 X」、给了个路径，正文一个字都没写**。
    #   给这种回复发「你被截断了、要分段」是**错误的诊断**，模型会以为自己已经
    #   写了一半、去纠结怎么分段。实测同一形状**连着重试 3 轮**，
    #   三次回复是 147/151/147 字，几乎一模一样 —— 提示词没起作用。
    # ★★ 「形状分不清谁配谁」要单独说 —— 这是第十八轮补二实测出来的
    #   第三种原因，而且**代价最大**：模型写了 N 个调用、却给了不是 N 块代码，
    #   我们分不清哪块配哪个 → 一块都不挂 → 重试。而重试提示原先只有
    #   「你被截断了/你漏写了」两种，**都不对症**，于是模型一遍遍重复
    #   同一个形状：实测连着重试 3~4 次（4204/3903/3924/3936 字，形状几乎
    #   一样），最后**一个文件都没写出来**（产物 0 字节）。
    #
    #   提示必须直接说「我数到几块、应该几块、你该怎么写」——
    #   让它知道错在哪，而不是让它猜。
    if raw is not None and parsed[0] == 'tools':
        # ★ 判据和 _attach_code_block 共用一份（block_count_mismatch）——
        #   两边判反了的话，提示说的和实际做的不一样。
        _bad, _nblk, _nneed = block_pairing_failed(
            _remove_tool_call_span(raw), parsed[1])
        if _nneed >= 2 and _bad:
            return ('你这一轮要写 %d 个东西（%s），但代码块有 %d 段 —— '
                    '**我分不清哪一段是给哪个文件的**，所以一个都没敢用，'
                    '这一轮什么都没写成。\n\n'
                    '**请这样写**：每个需要长内容的调用，**紧跟一个代码块**，'
                    '顺序和上面 JSON 里的调用顺序**一一对应**，'
                    '中间不要插别的代码块、也不要把 JSON 本身包进 ``` 里：\n'
                    '{"tool_use": [{"name": "Write", "input": {"file_path": "a.py"}},'
                    ' {"name": "Write", "input": {"file_path": "b.txt"}}]}\n'
                    '```\n（a.py 的内容）\n```\n'
                    '```\n（b.txt 的内容）\n```\n\n'
                    '★ 拿不准就**一次只写一个文件** —— 分两轮写完全没问题，'
                    '总比一轮里写串了强。'
                    % (_nneed,
                       '、'.join(_BLOCK_FIELD[t.get('name')]
                                 for t in parsed[1]
                                 if _BLOCK_FIELD.get(t.get('name'))
                                 and not (t.get('input') or {})
                                 .get(_BLOCK_FIELD[t.get('name')])),
                       _nblk))

    if raw is not None and len(raw) < OMITTED_BODY_CHARS and '```' not in raw:
        return ('你上一条回复**只给了 %s，正文一个字都没写** —— 不是被截断，'
                '就是漏了。这种调用发出去必然失败。\n\n'
                '**正文必须原样放在 JSON 后面的代码块里**（这是硬要求，不能省）：\n'
                '{"tool_use": {"name": "%s", "input": {"file_path": "..."}}}\n'
                '```\n（这里原样写文件内容，多少字都行，一个字符都不用转义）\n```\n\n'
                '★ 别把内容塞进 JSON 字符串（转义一错整个调用就废），'
                '也别只给路径就完事。'
                % (field, bad.get('name')))

    return ('你上一条回复**被网页的长度上限截断了** —— 工具调用只写了一半：'
            '要调 %s，但 %s 是空的。这种调用发出去必然失败。\n\n'
            '一次回复最多只能输出约 12000 字符，所以**长文件必须分几段写**：\n'
            '1) 先用 Write 只写开头一部分（8000 字符以内），末尾单独留一行标记，'
            '比如  # ===PART1===\n'
            '2) 再用 Edit 追加下一段：old_string 填那一行标记（原样复制），'
            'new_string 用代码块写「标记行 + 新内容」，末尾换一个新标记 '
            '# ===PART2===\n'
            '3) 重复第 2 步直到写完。\n\n'
            '每次只发一个工具调用，等结果回来再发下一个。'
            % (bad.get('name'), field))


# ============================================================
# 问网页版
# ============================================================

def decide_prompt(messages, info, system, tools):
    """
    决定这次发什么、发给哪个网页对话。

    返回 (prompt, goto_url_or_None, payload_msgs, 说明文字)

    抽成独立函数是为了**能测** —— 这段逻辑里藏着那个「压缩后重开对话」的 bug，
    而它当时完全没有测试覆盖。三个分支：
      新会话       → 全量提示词，开新对话
      正常增量     → 只发 messages[sent:] 那几条，复用原对话
      压缩/回退过  → 计数对不上，改发最近几条，**仍然复用原对话**
    """
    if info and info.get('web_url'):
        sent = info.get('sent', 0)
        if len(messages) > sent:
            new_msgs = messages[sent:]
            return (build_delta_prompt(new_msgs, tools), info['web_url'], new_msgs,
                    f'复用网页对话 · 增量 {len(new_msgs)} 条')
        if len(messages) == sent:
            # 没有新消息（少见，通常是上游重发同一份）—— 把最后一条再发一次。
            # 早先这里会落进下面的「压缩」分支，日志谎报「压缩过（49 → 49）」
            # 而且把最近 6 条重发一遍，白白往对话里灌重复内容。
            new_msgs = messages[-1:]
            return (build_delta_prompt(new_msgs, tools), info['web_url'], new_msgs,
                    f'没有新消息（{sent} 条），重发最后一条')
        # 真的变少了 = 上下文被压缩或回退过：计数对不上，但对话必须接着用。
        new_msgs = messages[-REBASE_MSGS:]
        return (build_delta_prompt(new_msgs, tools), info['web_url'], new_msgs,
                f'复用网页对话 · 上下文压缩过（{sent} → {len(messages)}），'
                f'改发最近 {len(new_msgs)} 条')
    return (build_prompt(system, messages, tools), None, messages, '新会话')


def build_delta_prompt(new_msgs, tools=None):
    """
    只把「新增的那几条」发给已经在进行中的网页对话。

    tools 的约定：**None = 不知道，按老行为（当有工具）**；传 [] = 明确没有工具。
    ★ 为什么这么定：这个参数是后加的，老调用点（和自测里那几处）都不传 ——
      让 None 走老路，它们的行为一个字都不变。
    """
    parts = []
    for m in new_msgs:
        content = m.get('content')
        if isinstance(content, list):
            text = '\n'.join(x for x in (render_block(b) for b in content) if x)
        else:
            text = str(content or '')
        if not text.strip():
            continue
        who = {'user': '用户', 'assistant': '你自己刚才说的'}.get(m.get('role'), m.get('role'))
        parts.append(f'【{who}】\n{text}')
    if not parts:
        return '继续。'

    # ★★ 没有工具时**绝不能**再说「然后输出 JSON」（第十八轮补）。
    #
    #   `build_prompt` 早就分了岔（有工具用 OUTPUT_RULES、没工具用
    #   OUTPUT_RULES_NO_TOOLS），但**这条 delta 路径没跟着分** ——
    #   而 delta 才是**每轮都走**的那条。
    #   后果：同一段提示词里一边写着「不要包 JSON、不要尝试调用工具」，
    #   另一边写着「先一句话说明你要做什么，然后输出 JSON（工具调用或最终回答）」。
    #   实测（冒烟测试）模型的回答整个跑偏：
    #       「我不能按这个要求做 —— 你前面说『只回答两个字』，后面又要求
    #         先说明再输出 JSON，这两条互相冲突，而且本次明确不允许调用工具」
    #   它把自己那点输出预算全花在**跟我们吵架**上了，正经答案挤在最后。
    #
    #   这是这个项目第六次「同一个职责两份实现」：分岔只做了一半。
    #   自测当时也是绿的 —— 它只断言「无工具时不返回 tool_use」，
    #   不断言**回答还正不正常**。结构对了、质量没了。
    if tools is not None and not tools:
        return ('═══ 继续 ═══\n\n' + '\n\n'.join(parts) +
                '\n\n继续。**这次没有配置任何工具**，直接用正常的 Markdown 回答就行，'
                '不要包 JSON、不要尝试调用工具。')

    return ('═══ 继续 ═══\n\n' + '\n\n'.join(parts) +
            '\n\n继续。记住：先一句话说明你要做什么，然后输出 JSON（工具调用或最终回答）。\n'
            # ★ 工具引导在这儿只留压缩版：delta 是**每轮**都走的路径，而完整版
            #   （TOOL_GUIDANCE）只在会话第一轮出现。这里不重复一句的话，
            #   模型从第二轮起就把这些工具忘光了。
            '（有岔路口要人拍板 → 用 AskUserQuestion 问，别自己猜；'
            '复杂的活 → 先 EnterPlanMode 拿方案；三步以上 → 先 TodoWrite 列清单；'
            '工具名照抄清单 —— 跑命令那个叫 `PowerShell`，不叫 `Bash`。）')


def ask_web(prompt, goto_url=None, think=None, attachments=None,
            start_limit=None, total_limit=None, key=None, search=None,
            skip_toggles=False, on_delta=None):
    """
    goto_url=None → 开新对话；否则跳回指定的网页对话。
    think/search=None → 用当前全局开关；True/False → 本次强制。
    返回 (回答原文, 当前对话的 URL)

    ★ `search` 必须留在**参数表末尾**（`skip_toggles` 只能加在它后面）。
      底下那个重试调用点是**位置传参**
      （`(prompt + reason, None, think, attach, ...)`），往中间插一个参数会让
      attachments 静默错位；而且 selftest 用 AST 检查「第 4 个位置参数是
      attachments」来防止重试路径漏传附件 —— 错位会让那层保护**静默失效**。
    """
    if think is None:
        think = _think_state['on']
    if search is None:
        search = _search_state['on']
    # ★ 这里**刻意不设进程内全局锁**。
    #   串行化现在由 ds.ask_in_session → browser_lock(key) 负责，而且是**按会话分锁**：
    #   不同会话各用各的标签页，可以真并行。
    #   早先这里有一把 threading.Lock() 把所有请求串起来 —— 加了跨进程锁之后
    #   它既多余又有害：实测两个会话本该并行，却被它串成 11 秒 + 11 秒。
    page, text, err = ds.ask_in_session(
        prompt, think, new_chat=True, attachments=attachments,
        navigate_to=goto_url, start_limit=start_limit, total_limit=total_limit,
        # key = 会话 id → 每个 dsc 会话用自己的标签页
        key=key, search=search, skip_toggles=skip_toggles, on_delta=on_delta)
    if err and err != ds.TRUNCATED_WARN:
        raise RuntimeError(err)
    # ★ TRUNCATED_WARN **不是失败**，是「这轮可能是半截」的提醒 ——
    #   绝不能在这里 raise（那会把本来能用的回答变成 500）。
    #   它只记一行日志，让下次排查能一眼看出这轮为什么短。
    #   真正该不该重试，由下面的 retry_reason(parsed, tools) 按解析结果判。
    if err == ds.TRUNCATED_WARN:
        ds.log('[续写] 这一轮可能被截断，但先按正常流程解析；'
               '缺长字段的话重试会接管')
    # ★ URL 必须在【发出消息之后】取 —— 发之前页面还是 chat.deepseek.com/ 根地址，
    #   只有发出第一条消息后才会变成 /a/chat/s/<uuid> 这种真正的对话地址。
    return text, page.url


# ============================================================
# Anthropic 响应格式
# ============================================================

def to_anthropic_tools(tools, model='deepseek-web', text=None):
    """
    一个或多个工具调用 → 一条 Anthropic 消息。

    多个就是 content 里放多个 tool_use 块 —— Claude Code 会**并行**执行它们。
    读 21 个文件时这一条就是 1 轮 vs 21 轮的区别。

    `text` 非空时，会在所有 tool_use 块**前面**插一个 text 块 —— 这就是模型的
    「中间文字」（「先看看目录里有什么」）。真实 API 的模型一直这么干，用户在界面上
    看得见它在往哪走；我们早先把这段扔了，dsc 里就只剩光秃秃的工具调用。

    ★ 顺序必须是 text 在前、tool_use 在后 —— 用户先看见说明，再看见工具。
    ★ `stop_reason` 必须仍是 `'tool_use'`：带工具就要让 Claude Code 继续跑，
      不能改成 end_turn，否则这一轮直接结束。
    """
    content = []
    if text:
        content.append({'type': 'text', 'text': text})
    content.extend({'type': 'tool_use',
                    'id': 'toolu_' + uuid.uuid4().hex[:20],
                    'name': t['name'], 'input': t['input']}
                   for t in tools)
    return {
        'id': 'msg_' + uuid.uuid4().hex[:20],
        'type': 'message', 'role': 'assistant', 'model': model,
        'content': content,
        'stop_reason': 'tool_use', 'stop_sequence': None,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    }


def to_anthropic_text(text, model='deepseek-web'):
    return {
        'id': 'msg_' + uuid.uuid4().hex[:20],
        'type': 'message', 'role': 'assistant', 'model': model,
        'content': [{'type': 'text', 'text': text or ''}],
        'stop_reason': 'end_turn', 'stop_sequence': None,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    }


def _sse_event(name, data):
    """单个 SSE 事件的字节。"""
    return ('event: %s\ndata: %s\n\n'
            % (name, json.dumps(data, ensure_ascii=False))).encode('utf-8')


class DeltaWriter:
    """
    把「累积文本的前缀」转成 SSE 的**差量**事件，边收边写。

    ★ 为什么需要它：ds.wait_answer 的 on_delta 给的是**当前全文的前缀**
      （每次都重新给一遍全文的开头），而 SSE 的 text_delta 语义是
      「这次新增的片段」。所以要自己记住上次吐到哪了、只发差量。

    ★ 为什么不会吐重：只认「比上次长」的情况。网页重渲染让文本变短时
      ds 那边已经过滤了，这里再兜一道（短了就原样返回，不发）。
    """
    def __init__(self, handler, index=0):
        # ★ 持有 **handler** 而不是裸的 wfile —— 所有写入都必须走
        #   handler._raw_write（它负责 chunked 编码）。
        #   踩过：这里原先自己拿 wfile.write 裸写，而响应是 chunked 的，
        #   结果混了两种格式，客户端报
        #     ValueError: invalid literal for int() with base 16: b'event: ...'
        #   —— 它把 SSE 那行当成块长度了。只留一个写入口就不会再犯。
        self.handler = handler
        self.index = index
        self.shown = ''          # 已经吐出去的（累积）
        self.open = False

    def _raw(self, b):
        self.handler._raw_write(b)

    def text(self, full_prefix):
        if not full_prefix or len(full_prefix) <= len(self.shown):
            return
        delta = full_prefix[len(self.shown):]
        if not self.open:
            self._raw(_sse_event('content_block_start', {
                'type': 'content_block_start', 'index': self.index,
                'content_block': {'type': 'text', 'text': ''}}))
            self.open = True
        self.shown = full_prefix
        self._raw(_sse_event('content_block_delta', {
            'type': 'content_block_delta', 'index': self.index,
            'delta': {'type': 'text_delta', 'text': delta}}))

    def close_text(self):
        if self.open:
            self._raw(_sse_event('content_block_stop', {
                'type': 'content_block_stop', 'index': self.index}))
            self.open = False
            self.index += 1
        return self.index


def stream_headers(handler):
    """
    开一个流式响应。

    ★ 必须用 chunked：原来那套「先算 Content-Length 再发」在这里行不通 ——
      要发的时候长度还不知道（内容还在生成）。HTTP/1.1 有 chunked 正好办这事。
    """
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Transfer-Encoding', 'chunked')
    handler.end_headers()


def sse_events(msg):
    """把完整消息拆成 Anthropic 的 SSE 事件流（一次性吐完，够客户端用了）。"""
    ev = []

    def add(name, data):
        ev.append(f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n')

    add('message_start', {'type': 'message_start', 'message': {
        'id': msg['id'], 'type': 'message', 'role': 'assistant', 'model': msg['model'],
        'content': [], 'stop_reason': None, 'stop_sequence': None,
        'usage': {'input_tokens': 0, 'output_tokens': 0}}})

    for idx, blk in enumerate(msg['content']):
        if blk['type'] == 'text':
            add('content_block_start', {'type': 'content_block_start', 'index': idx,
                                        'content_block': {'type': 'text', 'text': ''}})
            add('content_block_delta', {'type': 'content_block_delta', 'index': idx,
                                        'delta': {'type': 'text_delta', 'text': blk['text']}})
        else:
            add('content_block_start', {'type': 'content_block_start', 'index': idx,
                                        'content_block': {'type': 'tool_use', 'id': blk['id'],
                                                          'name': blk['name'], 'input': {}}})
            add('content_block_delta', {'type': 'content_block_delta', 'index': idx,
                                        'delta': {'type': 'input_json_delta',
                                                  'partial_json': json.dumps(blk['input'],
                                                                             ensure_ascii=False)}})
        add('content_block_stop', {'type': 'content_block_stop', 'index': idx})

    add('message_delta', {'type': 'message_delta',
                          'delta': {'stop_reason': msg['stop_reason'], 'stop_sequence': None},
                          'usage': {'output_tokens': 0}})
    add('message_stop', {'type': 'message_stop'})
    return ''.join(ev)


# ============================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        ds.log('[HTTP] ' + (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw_write(self, b):
        """
        往流式响应里写一段，按 HTTP/1.1 的 **chunked** 编码包一层。

        ★ 为什么手写分块：BaseHTTPRequestHandler 不自动做 chunked。
          格式是「十六进制长度\r\n 数据 \r\n」，最后用一个 0 长度的块收尾。
        """
        self.wfile.write(('%x\r\n' % len(b)).encode('ascii') + b + b'\r\n')
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _raw_write_end(self):
        """chunked 的结束块（0 长度）。★ 和 _raw_write 放一起 ——
        它们都是「往流式响应里写原始字节」这件事，散开迟早漏一个。"""
        try:
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except Exception:
            pass

    def _finish_stream(self, dw, msg):
        """
        流式响应的收尾：关 text 块、补 tool_use 块、发 message_stop。

        ★ 为什么叙述不会重复：`dw.shown` 记着已经吐出去多少。如果解析结果里
          的 text 块（就是 prose）比已吐的**长**，说明还有没吐完的，补上；
          比已吐的短或相等，说明已经吐全了，一个字都不再发。
          （判断的是 text 块 —— tool_use 块不该走文本通道。）
        """
        try:
            text = ''.join(b.get('text', '') for b in msg['content']
                           if b.get('type') == 'text')
            if text and len(text) > len(dw.shown):
                dw.text(text)
            idx = dw.close_text()
            for blk in msg['content']:
                if blk.get('type') != 'tool_use':
                    continue
                self._raw_write(_sse_event('content_block_start', {
                    'type': 'content_block_start', 'index': idx,
                    'content_block': {'type': 'tool_use', 'id': blk['id'],
                                      'name': blk['name'], 'input': {}}}))
                self._raw_write(_sse_event('content_block_delta', {
                    'type': 'content_block_delta', 'index': idx,
                    'delta': {'type': 'input_json_delta',
                              'partial_json': json.dumps(blk['input'],
                                                         ensure_ascii=False)}}))
                self._raw_write(_sse_event('content_block_stop', {
                    'type': 'content_block_stop', 'index': idx}))
                idx += 1
            self._raw_write(_sse_event('message_delta', {
                'type': 'message_delta',
                'delta': {'stop_reason': msg['stop_reason'], 'stop_sequence': None},
                'usage': {'output_tokens': 0}}))
            self._raw_write(_sse_event('message_stop', {'type': 'message_stop'}))
            self._raw_write_end()
        except Exception as e:
            ds.log(f'[流式] 收尾出错：{str(e)[:80]}')

    def _fail(self, code, message, streaming=False, dw=None):
        """
        报错的**唯一出口**。

        ★ 为什么必须收成一个口子：流式的头一旦发出去（200 + chunked），响应就
          已经提交了 —— 此时再 `send_response(500)` 会把 HTTP 状态行写进
          **响应体**里，客户端的 chunked 解码器拿它当块长度解析，直接炸。
          实测（`_gap_probe.py`）收到的字节：

              HTTP/1.1 200 X
              Transfer-Encoding: chunked
              36
              event: message_start
              ...
              HTTP/1.1 500 X          ← 混进响应体的第二个状态行
              Content-Length: 82
              {"type": "error", ...}

          客户端读下一个块长度时是 `int('HTTP/1.1 500 X', 16)` → ValueError。
          **这和 DeltaWriter 裸写那次是同一个病**（往已提交的响应里混格式），
          只是入口不同 —— 收成一个出口才不会再有第三个。

          流式下只能用 SSE 的 `error` 事件报错：那是 Anthropic 流式协议本来
          就有的东西，客户端会把它转成 APIError，行为和非流式的 500 一致。

        ★ 为什么加 `streaming` 参数，而不是拿 `dw is None` 当判据：
          `stream_headers()` 和 `dw = DeltaWriter(self)` 之间还夹着一次
          `_raw_write(message_start)`。那一步若抛异常，头已经出去了而 dw 还是
          None —— 拿 dw 当判据就会走错分支，正好在最需要它的时候写坏响应。
        """
        if streaming:
            try:
                if dw is not None:
                    dw.close_text()
                self._raw_write(_sse_event('error', {
                    'type': 'error',
                    'error': {'type': 'api_error', 'message': message}}))
                self._raw_write_end()
            except Exception as e:
                ds.log(f'[流式] 报错收尾也失败了：{str(e)[:80]}')
            return
        self._json(code, {'type': 'error',
                          'error': {'type': 'api_error', 'message': message}})

    def _error(self, code, msg, kind='invalid_request_error'):
        self._json(code, {'type': 'error',
                          'error': {'type': kind, 'message': msg}})

    def _path(self):
        """
        去掉查询串再匹配路由。
        坑：Claude Code 打的是 /v1/messages?beta=true，
        直接拿 self.path 比会因为 ?beta=true 匹配不上 → 404。
        """
        return self.path.split('?')[0].rstrip('/')

    def do_HEAD(self):
        """Claude Code 启动时会发 HEAD 探活。"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        p = self._path()

        # /think?set=on|off   —— 中途切深度思考，不用重启
        # /search?set=on|off  —— 同上，切智能搜索
        # 两个开关除了状态字典和名字以外完全一样，所以合成一个分支 ——
        # 抄两份的话，下次再加开关又要抄第三份（这个项目栽过「抄三份改两份」的跟头）。
        if p in ('/think', '/search'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            want = (q.get('set') or [''])[0].lower()
            state = _think_state if p == '/think' else _search_state
            name = '深度思考' if p == '/think' else '智能搜索'
            out_key = 'thinking' if p == '/think' else 'search'
            if want in ('on', '1', 'true', 'yes'):
                state['on'] = True
            elif want in ('off', '0', 'false', 'no'):
                state['on'] = False
            elif want == 'toggle':
                state['on'] = not state['on']
            ds.log(f'[{name}] {"开" if state["on"] else "关"}')
            self._json(200, {out_key: state['on']})
            return

        if p in ('', '/health', '/api/hello'):
            self._json(200, {'ok': True, 'service': 'deepseek-web → anthropic shim',
                             'thinking': _think_state['on'],
                             'search': _search_state['on']})
        else:
            self._json(404, {'type': 'error', 'error': {'type': 'not_found',
                                                        'message': self.path}})

    def do_POST(self):
        p = self._path()
        cleanup_attachments()      # 自带节流，最多每小时扫一次

        # Claude Code 会调这个端点估算上下文用量。不实现的话它会拿到 404，
        # 然后弹出「API Error: Error response」—— 实测就是这么翻车的。
        # 我们没有真实的分词器，按字符数粗估即可：这个数字只用于上下文管理，
        # 不参与计费，估个大概就够用。
        if p in ('/v1/messages/count_tokens', '/messages/count_tokens'):
            try:
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n).decode('utf-8', 'replace')
            except Exception:
                body = ''
            self._json(200, {'input_tokens': max(1, len(body) // 3)})
            return

        if p not in ('/v1/messages', '/messages'):
            self._json(404, {'type': 'error', 'error': {'type': 'not_found',
                                                        'message': self.path}})
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception as e:
            self._json(400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                        'message': f'bad json: {e}'}})
            return

        # ── 调试：看 Claude Code 到底发了什么，找出能标识「会话」的东西 ──
        if os.environ.get('SHIM_DEBUG'):
            interesting = {k: v for k, v in self.headers.items()
                           if k.lower() in ('user-agent', 'x-session-id', 'x-api-key',
                                            'anthropic-beta', 'anthropic-version',
                                            'x-stainless-arch', 'x-app', 'x-title',
                                            'x-stainless-package-version')}
            ds.log(f'[调试] 请求头 = {json.dumps(interesting, ensure_ascii=False)}')
            ds.log(f'[调试] 顶层字段 = {sorted(req.keys())}')
            if req.get('metadata'):
                ds.log(f'[调试] metadata = {json.dumps(req["metadata"], ensure_ascii=False)[:300]}')
            msgs = req.get('messages') or []
            if msgs:
                first = msgs[0].get('content')
                if isinstance(first, list):
                    first = ' '.join(str(b.get('text', '')) for b in first if isinstance(b, dict))
                ds.log(f'[调试] 首条消息前 120 字 = {str(first)[:120]!r}')
                ds.log(f'[调试] messages[0] 的键 = {sorted(msgs[0].keys())}')

        system = req.get('system')
        tools = req.get('tools') or []
        messages = req.get('messages') or []

        # 空 messages 必须挡在这里。之前这个检查被误删过，结果空请求被当成
        # 正常对话发给了模型 —— 白等十秒，还让模型凭空编了个不存在的工具名。
        if not messages:
            self._error(400, 'messages 不能为空')
            return

        sid = session_id_of(req)
        fp = fingerprint(system, tools)

        with _sessions_lock:
            sessions = load_sessions()
        info = sessions.get(sid) if sid else None

        # 复用已有网页对话的条件：① 有会话记录 ② 系统提示没变 ③ 有 web_url。
        #
        # ★ 这里**刻意不要求**「消息数变多」。
        #   早先的版本把「len(messages) > sent」当成必需条件，理由是「历史被重写过
        #   就对不上」。但那是错的：Claude Code 压缩上下文时消息数会**变少**
        #   （46 < 83 这种），旧逻辑就判定「历史回退了」→ **丢掉整个网页对话重开**。
        #   后果有两个，都很糟：
        #     · 用户看到的是「接着问，浏览器却开了另一个对话」
        #     · 网页对话里积累的**压缩前完整历史**被白白扔掉 ——
        #       而压缩后的数组只是「摘要 + 最近几条」，信息只会更少。
        #   正确做法：计数对不上时，改用「发最近几条」的办法重定位，
        #   网页对话本身保留。
        # 指纹只算系统提示，不算工具（MCP 是异步加载的，工具数开头会变）。
        # 系统提示真变了才重开 —— 那种情况网页对话的前提已经不同了。
        if info and info.get('fp') != fp:
            ds.log(f'[会话 {(sid or "?")[:8]}] 系统提示变了，重开网页对话')
            info = None

        prompt, goto, payload_msgs, note = decide_prompt(messages, info, system, tools)
        ds.log(f'[会话 {(sid or "?")[:8]}] {note} · 提示词 {len(prompt)} 字')

        # 图片/PDF 这类二进制内容走附件上传，不能当文本发（当文本就是乱码）
        attach = extract_attachments(payload_msgs)
        if attach:
            ds.log(f'[附件] {len(attach)} 个（图片/文档）')

        # 深度思考：全局开关 或 模型名里带 think（后者让你能在 Claude Code 里 /model 中途切）
        think = _think_state['on'] or ('think' in str(req.get('model') or '').lower())
        if think:
            ds.log('[深度思考] 本轮开启，会慢很多')

        # 智能搜索：同理（模型名带 search 就能在 Claude Code 里 /model 中途切）。
        # ★ 默认关，而且**刻意不做关键词判定** —— 什么时候该联网交给模型自己判断
        #   （它会在回复里写 [[SEARCH]] 标记，见下面的「模型申请开开关」）。
        #   关键词法会误判：该搜的没搜、不该搜的乱搜还白等 30 秒。
        search = _search_state['on'] or ('search' in str(req.get('model') or '').lower())

        # ★ 流式（伪）：把 wait_answer 轮询到的增量实时吐出去。
        #   只有请求要 stream 时才挂 —— 不要流的时候这个回调纯属浪费。
        #   ★ 重试路径**不挂**（见下面那处），它吐的东西可能马上被丢弃重来。
        #   ★ `streaming` 单独记账，**不拿 `dw is None` 代替**：头和 dw 之间
        #     还夹着一次 `_raw_write`，那一步抛异常时头已经出去了。见 _fail()。
        streaming = bool(req.get('stream'))
        dw = None
        on_delta = None
        if streaming:
            stream_headers(self)
            self._raw_write(_sse_event('message_start', {
                'type': 'message_start', 'message': {
                    'id': 'msg_' + uuid.uuid4().hex[:20], 'type': 'message',
                    'role': 'assistant', 'model': req.get('model', 'deepseek-web'),
                    'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 0, 'output_tokens': 0}}}))
            dw = DeltaWriter(self)
            on_delta = dw.text
        try:
            raw, web_url = ask_web(prompt, goto, think, attach, key=sid, search=search,
                                   on_delta=on_delta)
        except Exception as e:
            ds.log(f'[失败] {e}')
            self._fail(500, str(e), streaming=streaming, dw=dw)
            return

        # ★ 模型申请开开关（[[SEARCH]] / [[THINK]]）—— 真实 API 那边是模型自己
        #   调 WebSearch，这边换成「它写个标记、我们替它开、再把问题重发一次」。
        #
        #   为什么在**同一个网页对话**里重发，而不是像重试那样开新对话：
        #   重试是「纠正错误」，开新对话是为了不把错误留在历史里；而这里模型只是
        #   说了句「我需要联网」，是**正当的对话内容**，不是错误。同对话还能保住
        #   上下文 —— 不然得把几万字的提示词整个重发一遍。
        #
        #   ★ 每条请求**最多认一次**（下面那个二次检查就是防循环的闸），
        #     否则模型犯起轴来会来回死循环。
        want_search, want_think = parse_need_marker(raw)
        if want_search or want_think:
            opened = [n for n, w in (('联网搜索', want_search),
                                     ('深度思考', want_think)) if w]
            ds.log(f'[申请] 模型要求打开「{"、".join(opened)}」—— 开好后重发同一问题')
            try:
                # ★ 这一轮**不挂 on_delta**：它是在同一对话里重发问题，
                #   增量会从「上一轮已经吐过的位置」接着来，语义乱。
                #   反正只多等一轮，不值得为它搞复杂。
                raw, web_url = ask_web(
                    '（已为你打开%s。请重新回答上面那个问题。）' % '、'.join(opened),
                    web_url,                      # ← 同一个网页对话，不新开
                    think or want_think,
                    # 附件原样带上。对话里其实已经有了，重发一遍是冗余的 ——
                    # 但宁可冗余也别漏：漏附件会让模型看不见图，然后反复调 Read，
                    # 陷入死循环（这个项目栽过）。冗余的代价只是多传一次。
                    attach,
                    key=sid, search=search or want_search)
            except Exception as e:
                ds.log(f'[申请] 重发失败：{e}（按原来那条回复继续）')
            again_s, again_t = parse_need_marker(raw)
            if again_s or again_t:
                # 开好了还申请 —— 不再循环，给一句人话收场。
                # （这比把标记原样交给 Claude Code 强：那样这一轮会以一句
                #   `[[SEARCH]]` 结束，用户完全看不懂发生了什么。）
                ds.log('[申请] 开关已经开了还要标记 —— 不再重发，就此打住')
                raw = ('（联网搜索 / 深度思考已经打开了，但这一轮没能生成出有效内容。'
                       '请再说一次，或换个问法。）')

        if sid:
            with _sessions_lock:
                sessions = load_sessions()
                sessions[sid] = {
                    'web_url': web_url,
                    'fp': fp,
                    'sent': len(messages),
                    'turns': ((info or {}).get('turns') or 0) + 1,
                    'updated': time.time(),
                }
                save_sessions(sessions)

        # 模型把工具名认错了（比如把 PowerShell 叫成 Bash）→ **先改名再往下走**，
        # 这样重试判据和长字段检查看到的都是正确的名字。
        parsed = parse_and_align(raw, tools)

        # 这条回复有没有「不能就这么发上去」的毛病 → 有就重问。
        # 两类毛病（JSON 写坏了 / 工具调用缺长字段）见 retry_reason()。
        #
        # ★ 为什么是**多次**而不是一次：这两类毛病不是偶发，而是模型写命令时
        #   不转义引号这种系统性习惯 —— 实测同一个错连犯两次。只重试一次的话，
        #   第二次撞上同一个习惯就认输，把 JSON 原文当回答交给 Claude Code，
        #   那一轮没有工具可调、会话就停在提示符上等用户手打「继续」。
        #   有界重试的形状照抄 deepseek_ask.ensure_browser。
        for attempt in range(1, MAX_RETRIES + 1):
            reason = retry_reason(parsed, tools, raw)
            if not reason:
                break

            _bad = incomplete_tool(parsed[1]) if parsed[0] == 'tools' else None

            # ★ 原始回复**两种重试都要记**，而且记在分支外面。
            #   光记一句「缺 content」是查不出原因的 —— 到底是「回复被网页截断了」
            #   还是「我们把内容挂错了地方」还是「模型根本没写正文」，
            #   现象一模一样。把原文的头尾和围栏数量记下来，一眼就能分辨：
            #     结尾没有闭合围栏 + 括号不配平 → 网页截断（真的没写完）
            #     围栏数量不对                  → 是我们切错了（见 _remove_tool_call_span）
            #     很短 + 围栏 0 个              → 模型只给了路径、正文一个字没写
            #
            #   ★ 第十八轮补：原先只有「缺字段」那条记原文，而**占重试 77%
            #     的是另一条**（「输出像是坏掉的工具调用」，实测两天 63 次）——
            #     它只留了个名字、没有现场，排查时只能靠猜。收到分支外面，
            #     两种都留下现场。
            ds.log('[重试] 原始回复 %d 字 · 围栏 %d 个 · 头 %r · 尾 %r'
                   % (len(raw), raw.count('```'), raw[:90], raw[-90:]))
            if _bad:
                # ★ 去掉「（多半是被长度上限截断了）」那句 —— 实测它**经常是错的**：
                #   16 份现场里 6 份只有 147~341 字，离任何长度上限都远得很。
                #   真正的原因是模型只给了路径、正文没写。诊断写在日志里会被当成事实。
                ds.log('[重试] 第 %d/%d 次：%s 缺 %s，重新问一次'
                       % (attempt, MAX_RETRIES, _bad.get('name'),
                          _BLOCK_FIELD.get(_bad.get('name'))))
            else:
                ds.log('[重试] 第 %d/%d 次：输出像是坏掉的工具调用，重新问一次'
                       % (attempt, MAX_RETRIES))

            # ★ 同一个错犯第二次，说明是系统性习惯，把原话再说一遍没用 ——
            #   第二遍换一句更狠的：只要一个工具调用、命令放代码块、别的都别写。
            if attempt > 1:
                reason += ('\n\n★ 上一次你还是这么写坏的。这次**只输出一个 JSON 对象，'
                           '后面紧跟一个代码块，不要有任何别的文字**。'
                           '要跑的命令**原样写在代码块里**，'
                           'JSON 字符串里**一个引号都不要出现**。')

            try:
                # ★ 只发一句短的追问，不要把整个提示词重发一遍。
                #   早先是 `prompt + 提醒` 一起发 —— 那等于往同一个对话里
                #   灌第二份完整提示词，对话状态会乱，回答反而起不来
                #   （实测卡满 90 秒超时，把上游客户端也拖超时了）。
                # ★ 在【新对话】里做重试，不污染原对话。
                #   早先是往同一个网页对话里再塞一条「你刚才的回复不是合法
                #   JSON……」—— 那条会**永久留在对话历史里**，之后每一轮模型
                #   都看得见「我犯过错、被纠正过」。不影响增量计算（发的是文本
                #   不是下标），但属于不可逆的状态泄漏，表现成「模型偶尔变笨」。
                #   开新对话做，代价是重试那轮要发一次完整上下文 ——
                #   而重试本身很罕见（实测一整晚只触发 2 次）。
                raw2, _ignored_url = ask_web(
                    prompt + '\n\n' + reason,
                    None, think,          # ← None = 开新对话，别用 web_url
                    # ★ 附件必须原样带上。
                    #   踩过：这里漏了 attachments，而重试又是**开新对话**做的 ——
                    #   于是图片既不在附件里、也不在对话历史里，可提示词里还写着
                    #   「★ 上面这个附件就是这个文件的内容本身」。模型找不到那个
                    #   附件，只能回一句「我无法看到附件中的图片内容」，然后
                    #   再调一次 Read → 再触发重试 → 死循环。
                    #   第九轮那个「读 21 张图要 21 轮」就是这么复现的：
                    #   当时修的是提示词，而十一、十二轮新加的重试路径把它绕过去了。
                    attach,
                    # 重试给独立的短超时 —— 用默认那套（90 秒起步）会把
                    # 上游客户端一起拖超时，用户看到的就是「API Error」。
                    #
                    # ★ 这里**必须显式关掉 search**：上面那个 25 秒的首字上限
                    #   比搜索所需的时间（30 秒起步）还短，透传进去必然报
                    #   「回答没有开始」，然后白白浪费一次重试。重试是修格式，
                    #   跟联不联网没关系。
                    start_limit=25.0, total_limit=60.0, key=sid, search=False,
                    # ★ 跳过开关对齐 —— 重试是去修 JSON 格式的，跟开关状态
                    #   毫无关系。不跳过的话，开关一次瞬时抖动就会把整个
                    #   重试废掉（set_toggle 的「强开找不到就抛错」语义），
                    #   用户直接吃 500。实测踩过：
                    #     03:03:33 [重试] 第 1/2 次：Write 缺 content……
                    #     03:03:35 [重试] 失败：「智能搜索」开关切换失败
                    #     03:03:35 [重试] 2 次都没修好，返回 500
                    skip_toggles=True)
                # ★ 重试结果必须走**同一个**入口。漏了这里就是「改了主路径、
                #   忘了重试路径」：重试产出的 `Bash` 不被改名，白烧掉这一次
                #   重试，两次都这样就是 500。实测日志正是这个形状（见
                #   parse_and_align 的说明）。
                p2 = parse_and_align(raw2, tools)
                if not retry_reason(p2, tools, raw2):
                    parsed, raw = p2, raw2
                    # ★ web_url 刻意不更新 —— 重试用的是临时对话，
                    #   主对话还是原来那个，下一轮继续在它上面接着聊。
                    ds.log('[重试] 成功（在临时对话里做的，主对话没被污染）')
                    break
                ds.log('[重试] 还是坏的，接着重问')
            except Exception as e:
                ds.log(f'[重试] 失败：{e}')
                # 重试本身出错（网页超时 / 服务器繁忙）—— 再试多半还是错。
                # 快速失败，别把上游客户端一起拖超时。
                break

        # ★ 几次都没修好 —— 这一轮拿不到可用的工具调用。
        #   **绝不要**把 JSON 原文当「回答」交出去：Claude Code 收到一段没有
        #   tool_use 的文本，这一轮就直接结束，用户看到一坨 JSON、还得自己手打
        #   「继续」—— 这正是这个分支以前的老毛病。
        #   改成报 5xx，让 Claude Code 自己的退避重试接管：它重发同一个请求时，
        #   decide_prompt 会走「没有新消息 → 重发最后一条」，在**同一个网页对话**
        #   里重问一次（对话历史不丢），等于自动替用户说了「继续」。
        if retry_reason(parsed, tools, raw):
            ds.log('[重试] %d 次都没修好，返回 500 交给上游重试。原始回复：%r'
                   % (MAX_RETRIES, raw[:400]))
            self._fail(500,
                       '模型这一轮的工具调用被引号转义弄坏了，重试 %d 次仍没修好。'
                       '这一轮没有可用的工具调用，请重试。' % MAX_RETRIES,
                       streaming=streaming, dw=dw)
            return

        # 过滤掉模型凭空编的工具名（实测见过 noop / no_tool_available）。
        # 直接透给 Claude Code 会变成「未知工具」报错，还不如当普通回答。
        # 注意不要写成 `if tools and ...` —— tools 为空时模型照样会编，
        # 那种情况 valid 是空集，任何名字都不合法，正好该被拦下。
        #
        # ★ 「名字**全**错」的情况其实到不了这里 —— retry_reason 已经把它拦去
        #   重试了（那里能把真实可用的名字告诉模型）。下面 else 那个「当普通回答」
        #   只是最后一道保险：它产出的是**助手消息**，Claude Code 收到就结束这一轮。
        if parsed[0] == 'tools':
            valid = {t.get('name') for t in tools}
            good = [t for t in parsed[1] if t['name'] in valid]
            bad = [t['name'] for t in parsed[1] if t['name'] not in valid]
            if bad:
                ds.log(f'[警告] 模型编造了工具 {bad}，可用的有 {sorted(valid)[:8]}… 丢弃')
            if good:
                # ★ 别把第三个元素（叙述）丢了 —— 重建元组时漏掉它，
                #   中间文字就没了，用户又只看到一排工具调用。
                parsed = ('tools', good, parsed[2])
            else:
                parsed = ('reply', f'（我试图调用不存在的工具 {bad}，'
                                   f'但它们不在可用列表里。请换个方式提问。）', '')

        if parsed[0] == 'tools':
            names = [t['name'] for t in parsed[1]]
            ds.log(f'[工具] {len(names)} 个: {names[:6]}{"…" if len(names) > 6 else ""}')
            if parsed[2]:
                # 记一句 —— 出问题时能看出模型当时以为自己要干嘛
                ds.log(f'[叙述] {parsed[2]!r}')
            msg = to_anthropic_tools(parsed[1], model=req.get('model', 'deepseek-web'),
                                     text=parsed[2])
        else:
            # ★ 短回答要连**内容**一起记。只记长度的话出了问题只能靠猜 ——
            #   实测 `[回答] 2 字` 今天出现了 4 次，谁都说不清那 2 个字是什么，
            #   排查时只能反过来推代码。
            preview = f'  内容={parsed[1]!r}' if len(parsed[1]) <= 40 else ''
            ds.log(f'[回答] {len(parsed[1])} 字{preview}')
            msg = to_anthropic_text(parsed[1], model=req.get('model', 'deepseek-web'))

        if req.get('stream'):
            # 流式：头已经发过了、叙述也已经边等边吐过了。这里只收尾 ——
            # 关掉 text 块，把 tool_use 块补上（如果有），最后 message_stop。
            self._finish_stream(dw, msg)
        else:
            self._json(200, msg)


def main():
    ap = argparse.ArgumentParser(description='DeepSeek 网页版 → Anthropic API')
    ap.add_argument('--port', type=int, default=8799)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()

    cleanup_attachments()          # 启动时清一次历史附件
    try:
        ds.close_extra_tabs()      # 清掉上次运行遗留的独立标签页
    except Exception as e:
        ds.log(f'[标签页] 清理失败（不影响使用）：{str(e)[:60]}')

    # 挂到非回环地址上等于把这台机器暴露出去 —— 这个工具本身就违反 ToS，
    # 再对外开门风险全在账号上。给个明确的警告和确认。
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        ds.log('')
        ds.log('⚠️⚠️⚠️  警告  ⚠️⚠️⚠️')
        ds.log(f'  你正在把服务挂到 {args.host} —— 不是本机回环地址。')
        ds.log('  这意味着同网络（甚至公网）的别人可以拿你的账号白嫖 DeepSeek，')
        ds.log('  风险全在你的账号上，出了问题是你担。')
        ds.log('  如果你只是自己用，请去掉 --host 参数（默认 127.0.0.1）。')
        ds.log('')
        try:
            if input('  确定要继续吗？输入 yes 继续：').strip().lower() != 'yes':
                ds.log('  已取消。')
                return 1
        except Exception:
            ds.log('  非交互环境，视为取消。')
            return 1

    ds.log('=' * 62)
    ds.log('  DeepSeek 网页版 → Anthropic Messages API')
    ds.log(f'  端点: http://{args.host}:{args.port}/v1/messages')
    ds.log('')
    ds.log('  让 Claude Code 用它（另开一个终端，别动你日常配置）：')
    ds.log(f'     $env:ANTHROPIC_BASE_URL = "http://{args.host}:{args.port}"')
    ds.log('     $env:ANTHROPIC_AUTH_TOKEN = "dummy"')
    ds.log('     claude')
    ds.log('')
    ds.log('  ⚠️ 每轮 10-60 秒，一个任务可能要几十分钟')
    ds.log('=' * 62)

    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        ds.log('\n已停止。')
    except OSError as e:
        ds.log(f'❌ 起不来: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
