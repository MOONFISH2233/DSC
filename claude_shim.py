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

# 全量发送时最多带多少条历史。
# 为什么要有这个：把一晚上的长对话（实测 631 条消息 = 91 万字）第一次切到
# shim 时，全量发过去会被截断到 6 万字 —— 输出规则和近半上下文全丢，模型
# 直接输出乱码。只带最近的若干条，既保住了格式规则，也保住了「眼下在聊什么」。
MAX_HISTORY_MSGS = 24

# Claude Code 压缩上下文后，消息计数会和网页对话对不上。
# 这时不重开对话，改发最近的这几条来「重新对齐」——
# 网页那边有压缩前的完整历史，只要给它最近几条就能接上话。
REBASE_MSGS = 6

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

只输出一个 JSON 对象，前后不要有任何文字、不要 markdown 代码块：

  一个工具： {"tool_use": {"name": "<工具名>", "input": {<参数>}}}

  多个工具： {"tool_use": [{"name": "<名1>", "input": {<参数1>}},
                          {"name": "<名2>", "input": {<参数2>}}]}

★ **几件事互不依赖时，一次全列出来** —— 比如一次读好几个文件、跑几条独立命令。
  不要一个一个来：每多一轮就要多等十几秒，读 20 个文件就是 20 轮。
  只有「下一步依赖上一步结果」时才分开。

★ **要看多张图时，把所有 Read 一次全列出来**（一条消息最多 50 张，够用）。
  图片会作为附件**一起**传上去，你能**同时看到全部** ——
  没有「一次只能看一张」这回事，别一张一张来。
  一张一张来要多花好几倍时间，而且毫无好处：每多一轮就多等十几秒，
  看 20 张图就是 20 轮。一次列全，一轮就能看完。

★ **要放长文本（文件内容、整段代码、长命令）时，不要塞进 JSON 字符串里。**

  改成：JSON 里只写其他参数，长文本**原样放在 JSON 后面的代码块里** ——
  这样**一个字符都不用转义**，引号、反斜杠、换行照写：

    {"tool_use": {"name": "Write", "input": {"file_path": "D:\\\\创业\\\\a.py"}}}
    ```python
    # 这里原样写文件内容 —— 引号、反斜杠、换行都不用管
    def f():
        return "hello"
    ```

  （短参数比如路径、命令还是照常写在 JSON 里。）
  Windows 路径的反斜杠写两个：D:\\\\创业\\\\file.txt

**情况二：不需要工具，直接回答用户**

★ 用**正常的 Markdown 直接回答**，不要包 JSON、不要包代码块、不要加任何包装 ★

就像平时聊天那样写就行。想写多长写多长，可以用标题、列表、表格、代码块。
不需要考虑转义，不需要管格式限制。

（之所以这样区分：工具调用很短，包成 JSON 稳妥；但最终回答可能几千字，
塞进 JSON 字符串会因为换行和引号转义而报废 —— 实测翻过车。）
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


def render_tools(tools):
    if not tools:
        # 必须说得够狠 —— 实测只写「没有可用的工具」，模型会凭空编一个
        # 工具名出来（见过 noop / no_tool_available 这种）。
        # 刻意不提那个 JSON 关键字 —— 一提模型就会在回答里复述它，
        # 而 looks_broken() 会被这种复述误触发。
        return '（本次没有配置任何工具。你只能直接用文字回答，不要尝试调用工具。）'
    out = []
    for t in tools:
        schema = t.get('input_schema') or t.get('parameters') or {}
        out.append(json.dumps({
            'name': t.get('name'),
            'description': (t.get('description') or '')[:400],
            'input_schema': schema,
        }, ensure_ascii=False))
    return '\n'.join(out)


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

    parts.append('现在轮到你（助手）回复。记住：只输出一个 JSON 对象。')

    text = '\n\n'.join(parts)
    if len(text) > MAX_PROMPT_CHARS:
        # 保头（系统提示 + 工具定义 + 输出规则）保尾（最近的对话）
        head_len = len('\n\n'.join(parts[:3]))
        keep_tail = MAX_PROMPT_CHARS - head_len - 200
        ds.log(f'[警告] 提示词 {len(text)} 字，超限，截断到 {MAX_PROMPT_CHARS}')
        text = '\n\n'.join(parts[:3]) + '\n\n（中间历史已省略）\n\n' + text[-keep_tail:]
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
    name = obj.get('name') or obj.get('tool') or obj.get('tool_name')
    if not isinstance(name, str) or not name.strip():
        return None
    # 参数可能叫 input / arguments / parameters / args —— 各家格式不同
    for k in ('input', 'arguments', 'parameters', 'args'):
        v = obj.get(k)
        if isinstance(v, dict):
            return {'name': name.strip(), 'input': v}
    return {'name': name.strip(), 'input': {}}


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


# 工具调用常见的开头（只列出明确表示「这是工具调用」的键）。
#
# ⚠️ 刻意**不包含** `{"name"` —— 太宽泛了：任何带 name 字段的 JSON 都会命中，
#    正常回答里举个 {"name": "张三", "age": 18} 的例子就会被误判成工具调用。
_TOOL_MARKERS = ('{"tool_use"', '{ "tool_use"', '{"tool_calls"', '{ "tool_calls"',
                 '{"tool_call"')


# 「长内容放代码块」协议用的正则：抓 JSON 后面那个围栏代码块。
#
# ★ 必须**贪婪**匹配（吃到最后一个 ```），不能非贪婪。非贪婪会在内容里第一次
#   出现 ``` 时就收尾 —— 而内容里带围栏太正常了：写 .md、写带示例的脚本、
#   写这个项目自己的文档都是。结果就是**文件被静默截断**，只剩开头几行，
#   不报错、日志干净，打开文件才发现少了一大半。
#   宁可多吃（多出来的顶多是结尾几句说明）也不能少吃（那是数据损坏）。
_CODE_BLOCK_RE = re.compile(r'```[a-zA-Z0-9_+\-]*\r?\n(.*)\r?\n?\s*```', re.S)

# 哪个工具缺哪个字段时，用代码块补上
_BLOCK_FIELD = {
    'Write': 'content',
    'Bash': 'command',
    'Edit': 'new_string',
    'NotebookEdit': 'new_source',
}


def _attach_code_block(text, tools):
    """
    把 JSON 后面那个代码块的内容，补给工具调用里缺的那个长字段。

    ★ 为什么必须这么干：模型写文件内容时**几乎不可能每次都转义对** ——
      代码里的 `\"\"\"docstring\"\"\"`、`len("abc")` 会把 JSON 字符串提前截断，
      整个工具调用作废、任务中断。实测反复栽在这上面（写 Python 画图脚本）。
      与其要求模型「转义永远不出错」，不如**让它根本不用转义**：
      长文本放代码块里，原样写。

    兼容两种写法：内容在 JSON 里（短内容）→ 不动；内容在代码块里 → 补进去。
    """
    if not tools:
        return tools
    m = _CODE_BLOCK_RE.search(text)
    if not m:
        return tools
    body = m.group(1)

    out = []
    for t in tools:
        t = dict(t)
        inp = dict(t.get('input') or {})
        field = _BLOCK_FIELD.get(t.get('name'))
        if field is not None:
            if not inp.get(field):
                inp[field] = body
        else:
            # 不认识的工具：填第一个空的「文本类」字段
            for k in ('content', 'command', 'new_string', 'text'):
                if not inp.get(k):
                    inp[k] = body
                    break
        t['input'] = inp
        out.append(t)
    return out


def _looks_like_call(obj):
    """
    这个 JSON 对象像不像一个工具调用。

    用于「夹带」场景，要求比外层解析更严 —— 因为这里是在一段普通文字里找，
    误判的代价是**把正常回答变成工具调用**，比漏判严重得多。
    """
    if not isinstance(obj, dict):
        return False
    if any(k in obj for k in ('tool_use', 'tool_calls', 'tool_call', 'tools_use')):
        return True
    # 裸写法必须**同时**有 name 和参数字典；光有 name 不算
    return bool(obj.get('name')) and any(
        isinstance(obj.get(k), dict)
        for k in ('input', 'arguments', 'parameters', 'args'))


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
    """
    limit = len(text)
    for marker in _TOOL_MARKERS:
        idx = text.find(marker)
        if idx < 0:
            continue
        depth = 0
        for i in range(idx, limit):
            c = text[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    obj = _loads_lenient(text[idx:i + 1])
                    if _looks_like_call(obj):
                        tools = _tools_from(obj)
                        if tools:
                            return tools
                    break
    return []


def parse_reply(raw):
    """
    返回 ('tools', [{'name':..., 'input':{...}}, ...]) 或 ('reply', text)。

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
        return ('reply', '')

    t = raw.strip()

    # 只有「整个回答就是一个 JSON 对象」时才尝试解析（首字符 { 末字符 }）——
    # 这样既认得出工具调用，又不会把中间带代码块的 Markdown 回答误判成 JSON。
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
                    return ('tools', _attach_code_block(t, tools))
                # 兼容各种「回答」的包法。实测见过 reply / answer —— 模型
                # 对格式的想象力比我们以为的丰富，多认几种不会错。
                for k in ('reply', 'answer', 'response', 'text', 'content'):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return ('reply', v)

    # 整段不是 JSON —— 但可能是「先解释一段、再给工具调用」的混合输出。
    # 模型在上一步失败之后特别爱这么写。整段当回答返回的话，
    # 用户看到的就是一坨 JSON 文本、任务直接断掉。
    embedded = _extract_embedded_tools(t)
    if embedded:
        ds.log(f'[提示] 模型在 {len(t)} 字的说明里夹带了 {len(embedded)} 个工具调用，已抠出来')
        # ★ 这里也要补代码块 —— 「JSON + 后面跟代码块」走的正是这条路
        #   （整段不以 } 结尾，所以进不了上面那个分支）。漏了的话长内容就丢了。
        return ('tools', _attach_code_block(t, embedded))

    return ('reply', t)


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

    # ② 判定「像不像在尝试调工具」必须认**带花括号的标记**（{"tool_use" 这种），
    #    不能只认「文本里出现了 tool_use 这个词」—— 那样模型在正常回答里提一句
    #    「我不会输出 tool_use」就会被误判成坏调用，白重试一轮（踩过）。
    #
    #    但也不能**只**认「以 { 开头」：模型很爱先解释一句、再把调用附在后面，
    #    那种混合输出同样得触发重试，否则用户看到的就是一坨 JSON 文本。
    if not (t.startswith('{') or any(m in t for m in _TOOL_MARKERS)):
        return False
    # 两种风格都算：我们的 tool_use/input，和 OpenAI 的 tool_calls/arguments
    return ('"tool_use"' in t or '"tool_calls"' in t
            or ('"name"' in t and ('"input"' in t or '"arguments"' in t)))


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


def retry_reason(parsed, tools):
    """
    这条回复为什么**不能**就这么交给上游。返回 None = 可以放行。

    两类问题，都得重问一次：
      ① 看着像写坏了的工具调用（JSON 转义炸了）—— 见 should_retry()
      ② 解析出了工具调用，但必填的长字段是空的 —— 典型是 Write 没有 content，
         根因通常是回复被网页的输出长度上限截断了

    抽成一个函数是为了**能测**：这两条规则原先散在 HTTP handler 里，
    selftest 够不着，正是当初能悄悄塞进一个错误闸门的原因。
    """
    if should_retry(parsed, tools):
        return ('你上一条回复不是合法 JSON —— 长文本塞进字符串时转义出错了。'
                '**改用这个写法**：JSON 里只留路径等短参数，要写的文件内容'
                '**原样放在 JSON 后面的代码块里**，一个字符都不用转义：\n'
                '{"tool_use": {"name": "Write", "input": {"file_path": "..."}}}\n'
                '```\n（这里原样写内容）\n```')

    if not tools or parsed[0] != 'tools':
        return None
    bad = incomplete_tool(parsed[1])
    if not bad:
        return None
    field = _BLOCK_FIELD.get(bad.get('name'))
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
            return (build_delta_prompt(new_msgs), info['web_url'], new_msgs,
                    f'复用网页对话 · 增量 {len(new_msgs)} 条')
        if len(messages) == sent:
            # 没有新消息（少见，通常是上游重发同一份）—— 把最后一条再发一次。
            # 早先这里会落进下面的「压缩」分支，日志谎报「压缩过（49 → 49）」
            # 而且把最近 6 条重发一遍，白白往对话里灌重复内容。
            new_msgs = messages[-1:]
            return (build_delta_prompt(new_msgs), info['web_url'], new_msgs,
                    f'没有新消息（{sent} 条），重发最后一条')
        # 真的变少了 = 上下文被压缩或回退过：计数对不上，但对话必须接着用。
        new_msgs = messages[-REBASE_MSGS:]
        return (build_delta_prompt(new_msgs), info['web_url'], new_msgs,
                f'复用网页对话 · 上下文压缩过（{sent} → {len(messages)}），'
                f'改发最近 {len(new_msgs)} 条')
    return (build_prompt(system, messages, tools), None, messages, '新会话')


def build_delta_prompt(new_msgs):
    """只把「新增的那几条」发给已经在进行中的网页对话。"""
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
    return ('═══ 继续 ═══\n\n' + '\n\n'.join(parts) +
            '\n\n继续。记住：只输出一个 JSON 对象（工具调用或最终回答）。')


def ask_web(prompt, goto_url=None, think=None, attachments=None,
            start_limit=None, total_limit=None, key=None):
    """
    goto_url=None → 开新对话；否则跳回指定的网页对话。
    think=None → 用当前全局开关；True/False → 本次强制。
    返回 (回答原文, 当前对话的 URL)
    """
    if think is None:
        think = _think_state['on']
    # ★ 这里**刻意不设进程内全局锁**。
    #   串行化现在由 ds.ask_in_session → browser_lock(key) 负责，而且是**按会话分锁**：
    #   不同会话各用各的标签页，可以真并行。
    #   早先这里有一把 threading.Lock() 把所有请求串起来 —— 加了跨进程锁之后
    #   它既多余又有害：实测两个会话本该并行，却被它串成 11 秒 + 11 秒。
    page, text, err = ds.ask_in_session(
        prompt, think, new_chat=True, attachments=attachments,
        navigate_to=goto_url, start_limit=start_limit, total_limit=total_limit,
        # key = 会话 id → 每个 dsc 会话用自己的标签页
        key=key)
    if err:
        raise RuntimeError(err)
    # ★ URL 必须在【发出消息之后】取 —— 发之前页面还是 chat.deepseek.com/ 根地址，
    #   只有发出第一条消息后才会变成 /a/chat/s/<uuid> 这种真正的对话地址。
    return text, page.url


# ============================================================
# Anthropic 响应格式
# ============================================================

def to_anthropic_tools(tools, model='deepseek-web'):
    """
    一个或多个工具调用 → 一条 Anthropic 消息。

    多个就是 content 里放多个 tool_use 块 —— Claude Code 会**并行**执行它们。
    读 21 个文件时这一条就是 1 轮 vs 21 轮的区别。
    """
    return {
        'id': 'msg_' + uuid.uuid4().hex[:20],
        'type': 'message', 'role': 'assistant', 'model': model,
        'content': [{'type': 'tool_use',
                     'id': 'toolu_' + uuid.uuid4().hex[:20],
                     'name': t['name'], 'input': t['input']}
                    for t in tools],
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

        # /think?set=on|off  —— 中途切深度思考，不用重启
        if p == '/think':
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            want = (q.get('set') or [''])[0].lower()
            if want in ('on', '1', 'true', 'yes'):
                _think_state['on'] = True
            elif want in ('off', '0', 'false', 'no'):
                _think_state['on'] = False
            elif want == 'toggle':
                _think_state['on'] = not _think_state['on']
            ds.log(f'[深度思考] {"开" if _think_state["on"] else "关"}')
            self._json(200, {'thinking': _think_state['on']})
            return

        if p in ('', '/health', '/api/hello'):
            self._json(200, {'ok': True, 'service': 'deepseek-web → anthropic shim',
                             'thinking': _think_state['on']})
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

        try:
            raw, web_url = ask_web(prompt, goto, think, attach, key=sid)
        except Exception as e:
            ds.log(f'[失败] {e}')
            self._json(500, {'type': 'error', 'error': {'type': 'api_error',
                                                        'message': str(e)}})
            return

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

        parsed = parse_reply(raw)

        # 这条回复有没有「不能就这么发上去」的毛病 → 有就重问一次。
        # 两类毛病（JSON 写坏了 / 工具调用缺长字段）见 retry_reason()。
        reason = retry_reason(parsed, tools)
        if reason:
            _bad = incomplete_tool(parsed[1]) if parsed[0] == 'tools' else None
            if _bad:
                ds.log('[重试] %s 缺 %s（多半是被长度上限截断了），重新问一次'
                       % (_bad.get('name'), _BLOCK_FIELD.get(_bad.get('name'))))
            else:
                ds.log('[重试] 输出像是坏掉的工具调用，重新问一次')
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
                    start_limit=25.0, total_limit=60.0, key=sid)
                p2 = parse_reply(raw2)
                if not retry_reason(p2, tools):
                    parsed, raw = p2, raw2
                    # ★ web_url 刻意不更新 —— 重试用的是临时对话，
                    #   主对话还是原来那个，下一轮继续在它上面接着聊。
                    ds.log('[重试] 成功（在临时对话里做的，主对话没被污染）')
                else:
                    ds.log('[重试] 还是坏的，按普通回答处理')
            except Exception as e:
                ds.log(f'[重试] 失败：{e}')

        # 过滤掉模型凭空编的工具名（实测见过 noop / no_tool_available）。
        # 直接透给 Claude Code 会变成「未知工具」报错，还不如当普通回答。
        # 注意不要写成 `if tools and ...` —— tools 为空时模型照样会编，
        # 那种情况 valid 是空集，任何名字都不合法，正好该被拦下。
        if parsed[0] == 'tools':
            valid = {t.get('name') for t in tools}
            good = [t for t in parsed[1] if t['name'] in valid]
            bad = [t['name'] for t in parsed[1] if t['name'] not in valid]
            if bad:
                ds.log(f'[警告] 模型编造了工具 {bad}，可用的有 {sorted(valid)[:8]}… 丢弃')
            if good:
                parsed = ('tools', good)
            else:
                parsed = ('reply', f'（我试图调用不存在的工具 {bad}，'
                                   f'但它们不在可用列表里。请换个方式提问。）')

        if parsed[0] == 'tools':
            names = [t['name'] for t in parsed[1]]
            ds.log(f'[工具] {len(names)} 个: {names[:6]}{"…" if len(names) > 6 else ""}')
            msg = to_anthropic_tools(parsed[1], model=req.get('model', 'deepseek-web'))
        else:
            # ★ 短回答要连**内容**一起记。只记长度的话出了问题只能靠猜 ——
            #   实测 `[回答] 2 字` 今天出现了 4 次，谁都说不清那 2 个字是什么，
            #   排查时只能反过来推代码。
            preview = f'  内容={parsed[1]!r}' if len(parsed[1]) <= 40 else ''
            ds.log(f'[回答] {len(parsed[1])} 字{preview}')
            msg = to_anthropic_text(parsed[1], model=req.get('model', 'deepseek-web'))

        if req.get('stream'):
            body = sse_events(msg).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
