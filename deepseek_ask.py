# -*- coding: utf-8 -*-
"""
DeepSeek 网页端命令行驱动。

在本地终端敲问题，由浏览器自动化驱动 chat.deepseek.com 回答，答案打印回终端。

    ask.cmd --login                     首次：扫码登录（只需一次）
    ask.cmd --probe                     打印各选择器命中数量，用来现场调参
    ask.cmd "什么是快速排序"             问问题（默认开新对话 + 普通模式）
    ask.cmd -i                          交互模式：在同一个对话里持续追问，exit 退出
    ask.cmd --continue "再讲详细点"      接着上一轮追问（单次）
    ask.cmd --think "9.11 和 9.9 哪个大"  开深度思考（超时放宽到 15 分钟）
    ask.cmd --quit                      关闭浏览器
    ask.cmd --dump                      把页面 HTML 落盘成 dump.html，排查 DOM 用

约定：日志走 stderr，答案走 stdout。
所以 `ask.cmd "问题" > 答案.txt` 拿到的文件里只有答案，没有杂音。

⚠️ 浏览器自动化通常不符合 DeepSeek 服务条款，个人小规模自用一般无人过问，风险自担。
⚠️ 网页 DOM 一改版选择器就失效 —— 这不是一次性工程。失效时跑 --probe 重调下面的 SEL。
"""

import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.request

# 控制台编码：cmd.exe 默认 GBK，DeepSeek 的回答里可能出现 GBK 编不了的字符
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    from DrissionPage.common import Keys
except ImportError:
    sys.exit(
        '没找到 DrissionPage。请先安装：\n'
        r'  "C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe" '
        '-m pip install "DrissionPage==4.1.1.4"'
    )


# ============================================================
# 配置
# ============================================================

PORT = 9333                      # 避开 9222（其他自动化工具常用，易冲突）
CHROME_EXE = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
PROFILE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
                           'deepseek_ask', 'chrome_profile')
URL = 'https://chat.deepseek.com/'

POLL = 0.25                      # 轮询间隔（秒）—— 起步用这个

# 轮询间隔自适应。
#
# ★ 为什么要自适应：首字延迟是体感关键（用户盯着空屏），所以开头要密；
#   但一旦开始出字，后面几十秒里文本变化没那么频繁，0.25 秒一查就是浪费。
#   实测一轮 30 秒的回答本来要 120 次 CDP 往返，降到 0.5 秒后省一半。
#
# ★ 不能设得更大（比如 1 秒）：完成判定靠「文本连续 N 秒不变」
#   （STABLE_NORMAL=2.5 秒），轮询太稀会让「已经停了」被晚发现，
#   判完成反而变慢。0.5 秒是平衡点。
POLL_FAST = POLL                  # 开头的密查
POLL_SLOW = 0.5                   # 之后的稀查
POLL_FAST_WINDOW = 3.0            # 开头多久用密查（秒）

# 「继续生成」按钮的最小扫描间隔。
#
# ★ 为什么降频：按钮一旦出现就不会瞬间消失，1 秒粒度足够。
#   而它每次都要一次 DOM 查询 —— 稳定性达标后本来每轮都查。
CONTINUE_SCAN_INTERVAL = 1.0

# 提示词上限。
# 实测：输入框能吃 100 万字以上（没探到底），box.input() 也不是逐字敲、
# 是瞬间灌入的（约 200 万字符/秒）—— 所以这里根本不是瓶颈。
# 早先某个 server 里设成 6000 纯属自己吓自己，而且只保尾巴，
# 结果带 system prompt 的客户端系统指令会被整段砍掉。
MAX_PROMPT_CHARS = 300000

# 头部吃掉全部预算时，留给「最后一段」的配额。见 join_prompt 的兜底分支。
MAX_TAIL_WHEN_HEAD_HUGE = 2000


def join_prompt(parts, head_parts=3, mark='（中间内容已省略）'):
    """
    把提示词各段拼成一段，超长时**保头 + 保尾**地截断。

    头几段通常是最要紧的（系统提示、工具定义、输出规则），尾巴是最近的对话。
    中间被挤掉的是最不重要的历史。

    ★ 两个 server 共用这一份 —— 之前各写各的，其中一处只保尾巴，
      把系统指令砍没了。
    """
    text = '\n\n'.join(parts)
    if len(text) <= MAX_PROMPT_CHARS:
        return text

    head = '\n\n'.join(parts[:head_parts])
    keep = MAX_PROMPT_CHARS - len(head) - 200
    if keep < 1000:
        # 光头部就把预算吃光了（比如客户端给了一个超长的 system prompt）。
        #
        # ★ 这里**不能只丢个头部了事** —— 那样整个对话历史全没了，模型会完全
        #   不知道刚才聊了什么，用户看到的是「答非所问」，而日志里只有一行警告。
        #   至少要保住最后一段（通常是用户刚说的话）。
        tail = parts[-1] if len(parts) > head_parts else ''
        # 尾巴最多拿 1/4 预算，剩下的留给头部 —— 两头都不能全丢，
        # 否则要么模型不知道自己是干嘛的，要么不知道用户刚说了什么。
        tail_budget = min(len(tail), MAX_TAIL_WHEN_HEAD_HUGE, MAX_PROMPT_CHARS // 4)
        head_budget = max(0, MAX_PROMPT_CHARS - tail_budget - 50)
        log(f'[警告] 提示词 {len(text)} 字，头部 {len(head)} 字已吃掉预算，'
            f'改为「头部截到 {head_budget} 字 + 保留最后 {tail_budget} 字」')
        return head[:head_budget] + ('\n\n' + tail[-tail_budget:] if tail_budget else '')
    log(f'[警告] 提示词 {len(text)} 字，超限截断到 {MAX_PROMPT_CHARS}')
    return head + f'\n\n{mark}\n\n' + text[-keep:]

# 每次问答追加到 history/YYYY-MM-DD.md，一天一个文件
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'history')

# 完成判定的时间参数：普通档 / 「慢档」
#
# ★ 「慢档」＝ 深度思考 **或** 智能搜索，两个走同一套预算。理由：开着智能搜索时
#   DeepSeek 要**先联网搜一轮**，首字动辄 30 秒以上、整轮也明显更长 —— 给它普通档
#   的预算就是间歇性误报「回答没有开始」，而且那种失败很安静，看起来像发不出去，
#   实际只是等得不够久（实测踩过：START 设 20 秒时，长对话里间歇性报错）。
STABLE_NORMAL, STABLE_THINK = 2.5, 6.0      # 文本连续多久不变算「生成完了」
TOTAL_NORMAL, TOTAL_THINK = 300.0, 1200.0   # 整体硬超时

# 等「回答开始」的上限。这是**最长**等待，不是固定等待 —— 一旦检测到文本变化
# 就立刻往下走，所以调大它平时不花任何代价。
START_NORMAL, START_THINK = 90.0, 180.0

if not (START_NORMAL < START_THINK and TOTAL_NORMAL < TOTAL_THINK):
    raise AssertionError('「慢档」（深度思考 / 智能搜索）的超时必须比普通档宽松')


# ============================================================
# 选择器 —— 网页改版后只需要改这里。改完跑 --probe 验证命中数量。
# ============================================================

THINK_LABEL = '深度思考'    # 用来在多个 ds-toggle-button 里认出「深度思考」那个
SEARCH_LABEL = '智能搜索'   # 同上。页面上就这两个开关，共用 toggle_button 选择器

SEL = {
    'chat_input': [
        # 实测：真实元素是 <textarea placeholder="给 DeepSeek 发送消息 " name="search">。
        # placeholder 里的 "DeepSeek" 是品牌名不会被翻译，所以它是最稳的一条。
        'css:textarea[placeholder*="DeepSeek"]',
        'css:textarea[name="search"]',
        'css:textarea[placeholder*="发送消息"]',
        'css:div[contenteditable="true"][role="textbox"]',
    ],
    'send_button': [
        # 实测：发送按钮是个圆形图标按钮，没有文字、也没有 aria-label ——
        # 所有靠文案/testid 的猜法全落空。唯一可靠锚点是 ds-button--primary，
        # 实测它在页面上只此一个。另外它空框时带 ds-button--disabled。
        'css:div[role="button"].ds-button--primary',
        'css:div[role="button"].ds-button--circle.ds-button--filled',
        'css:button[aria-label="Send message"]',
        'css:button[aria-label="发送消息"]',
    ],
    # 「停止生成」按钮实测抓不到：整场生成过程中它没有文字、没有 aria-label
    # （跟发送按钮一样是纯图标按钮），靠文案猜的选择器永远匹配不到。
    # 与其留一个永不触发、只提供虚假安全感的判据，不如删掉，改用下面的
    # action_button + 文本稳定性。代价是判完成慢一点，但不会判错。
    # 实测：AI 回答正文是 ds-markdown + ds-assistant-message-main-content。
    # 它只匹配 AI 回答、不匹配用户提问 —— 正是我们要的。
    # （方案里猜的 ds-markdown--block 根本不存在，害我查了半天。）
    'answer_body': 'css:.ds-assistant-message-main-content',
    # 回答下方那排操作按钮（复制/重新生成/点赞/朗读…），是正文的兄弟节点，
    # 生成结束才会渲染 —— 所以它出现就等于「这条回答写完了」。
    # 锚定设计系统类名，不用 ds-flex（太通用，实测误匹配 3 个）也不用哈希类名。
    'action_button': 'css:.ds-button--iconLabelTertiary',
    # 实测：带 aria-pressed 的是外层 div（class 含 ds-toggle-button），
    # 而 'text:深度思考' 只会匹配到内层那个没状态的 span —— 所以必须先按类名
    # 拿到所有开关，再按文字认出是哪一个。页面上有 2 个（另一个是「智能搜索」）。
    'toggle_button': 'css:.ds-toggle-button',
    # 侧边栏的对话条目。实测是 <a href="/a/chat/s/<uuid>">标题</a>，
    # class 是哈希值会变，但 href 的 URL 结构是产品级契约，稳得多。
    'conversation': 'css:a[href^="/a/chat/s/"]',
    # 回答被长度限制截断时，下方会出现一个「继续生成」按钮。
    # ★ 和发送/停止按钮不同，它**有文字**，所以能靠文案定位。
    #   实测结构：<div class="ds-button ds-button--outlinedNeutral ...">继续生成</div>
    #   （外面还套了两层同样含这段文字的容器，所以要点带 ds-button 类的那个）
    'continue_button': 'text:继续生成',
    # 服务端过载 / 限流时页面会弹一句「服务器繁忙，请稍后重试」。
    # ★ 这时**这一轮根本没生成出回答** —— 实测回答气泡里只剩一个残渣
    #   （就是 `{"` 两个字）。当成正经回答交给上游的话，用户界面上就是一个
    #   光秃秃的 `{"`，会话卡死在那儿，而且从现象完全看不出原因（今天碰上 4 次）。
    # 和 continue_button 一个思路：这类提示的 class 是会变的哈希值，
    # **文案**才是产品级的锚点。
    'server_busy': ['text:服务器繁忙', 'text:稍后重试'],
}

# 「这一轮没生成出来」的回答长度阈值（配合 find_server_busy 用）。
# 实测服务器繁忙时回答气泡里只剩 `{"` 这种残渣（2 个字）；
# 正常回答极少短于这个数（测试里那句「只回答两个字：收到」是 2 个字，
# 但它不带繁忙提示，所以不会被误判）。
BUSY_MIN_CHARS = 10

# 续写的最大次数 —— 防止按钮不消失时无限循环。
MAX_CONTINUES = 6

# 续写没能成功时的**警告**文本（不是错误！）。
#
# ★ 为什么是警告而不是错误：wait_answer 分不清「真被截断」和「按钮误报」。
#   只有解析出工具调用才知道 —— 所以判断交给上层（见 claude_shim）。
#   这里用一句**固定文本**，上层靠 `err == ds.TRUNCATED_WARN` 精确识别，
#   不要去 match 中文子串（改一个标点就失效，而且可能撞上别的错误）。
TRUNCATED_WARN = '回答可能被截断（续写没能成功）'

# 点「继续生成」的容错参数。
#
# ★ 为什么要重试：实测点击会**偶发**抛「该元素没有位置及大小」（React 重渲染的
#   瞬时抖动），也可能「点在空气上但 click() 不报错」（项目自己的坑 1）。
#   早先一次失败就认输 → 半截回答被当成完整回答交出去 → 上游报「缺 content」。
#
# 预算：CONTINUE_WAIT × CONTINUE_CLICK_TRIES = 90 秒，和早先单次等待一样长 ——
#       只是把「一次机会」换成了「两次机会」。
CONTINUE_WAIT = 45.0          # 点完等多久算「没反应」
CONTINUE_CLICK_TRIES = 2      # 每次续写最多点几遍

# 结果清洗：只删行首的思考标题行。绝不做「整块删除」—— 思考块边界不可靠，
# 删多了会连正文一起吃掉。主路径靠抓 .ds-markdown（思考块是它的兄弟节点）天然排除。
THINK_HEADER_RE = re.compile(
    r'^\s*(已深度思考|深度思考|思考中|正在思考|已思考|Thought about|Thinking)'
    r'[^\n]{0,40}\n+',
    re.M,
)


# ============================================================
# 小工具
# ============================================================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_log_fp = None          # 当天日志文件句柄，惰性打开


def _log_to_file(line):
    """
    把日志追加到 logs/YYYY-MM-DD.log。

    为什么要写文件：这套东西是长时间后台跑的，出了问题只能事后查。
    写日志失败绝不能影响主流程 —— 所以整个函数包在 try 里。
    """
    global _log_fp
    try:
        if _log_fp is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            _log_fp = open(os.path.join(LOG_DIR, time.strftime('%Y-%m-%d') + '.log'),
                           'a', encoding='utf-8')
        _log_fp.write(line + '\n')
        _log_fp.flush()
    except Exception:
        _log_fp = None      # 别再反复尝试


def log(msg):
    """日志走 stderr（保证 stdout 只有答案），同时落一份到文件。"""
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    print(line, file=sys.stderr, flush=True)
    _log_to_file(line)


def first_of(locs):
    return [locs] if isinstance(locs, str) else locs


def find_first(page, locs, timeout=3):
    """按候选顺序找第一个存在的元素。找不到返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for loc in first_of(locs):
            try:
                ele = page.ele(loc, timeout=0.3)
            except Exception:
                continue
            if ele:
                return ele
        time.sleep(0.2)
    return None


# ============================================================
# 浏览器生命周期
# ============================================================

# 绕过系统代理：国内常见全局代理会拦截 127.0.0.1 的连接
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 窗口可见性 ──────────────────────────────────────────────
# 默认把浏览器窗口最小化：每次操作都弹到前台很烦，还会抢走你正在打字的焦点。
#
# ⚠️ 注意 DrissionPage 的 hide() 是个空操作（实测调完 windowState 仍是 normal），
#    真正有效的是 mini()。三种状态下实测速度完全一样：
#        正常 8.4 秒 / 最小化 8.4 秒 / 移出屏幕外 8.3 秒
#    Chrome 没有对它节流，所以最小化是安全的。
#
# 需要看页面的场景（登录扫码、--probe、--dump）会自动恢复正常显示。
_win_state = {'force_visible': False}
#   force_visible : --show 会设成 True，这次全程不最小化


def window_state(page):
    """读窗口的真实状态：'normal' / 'minimized' / 'maximized' / None（读不到）。"""
    try:
        return page.run_cdp('Browser.getWindowForTarget').get('bounds', {}).get('windowState')
    except Exception:
        return None


def set_window_visible(page, visible):
    """
    显示或最小化浏览器窗口，**每次都核对真实状态**。

    早先的版本用内存里的布尔值记账，状态没变就跳过 —— 结果是：只要窗口被
    任何外部原因恢复过一次（用户点了任务栏、Chrome 自己弹出来、别的进程动过），
    记账就和现实脱节，之后再也不会自动最小化。

    改成每次读一次真实状态再决定，多花一次 CDP 往返（约 10 毫秒）。
    """
    if not visible and _win_state['force_visible']:
        visible = True

    cur = window_state(page)
    if cur is None:                # 读不到状态 —— 那就直接按意图设一次
        pass
    elif visible and cur != 'minimized':
        return                     # 已经是可见的
    elif not visible and cur == 'minimized':
        return                     # 已经是隐藏的

    try:
        page.set.window.normal() if visible else page.set.window.mini()
    except Exception as e:
        log(f'[窗口] 切换显示状态失败：{str(e)[:60]}')


def port_alive(port=PORT, timeout=0.5):
    """
    探测端口上有没有活着的浏览器。无副作用。

    注意：不能用 ChromiumPage(port) 来探测 —— 那个调用在端口没浏览器时
    会直接启动一个新浏览器，探测本身就有副作用。
    """
    try:
        with _opener.open(f'http://127.0.0.1:{port}/json/version', timeout=timeout) as r:
            return 'webSocketDebuggerUrl' in json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        # 连接被拒 / 超时 / 返回的不是 JSON —— 全都是「没有活着的浏览器」
        return False


def build_options():
    """只在需要『启动』浏览器时用。接管已有浏览器时这些配置一律不生效。"""
    return (ChromiumOptions()
            .set_browser_path(CHROME_EXE)      # 显式指定，免得找到 Edge
            .set_local_port(PORT)
            .set_user_data_path(PROFILE_DIR)   # ★ 独立 profile：既存登录态，又绕开 Chrome 136+ 对默认目录的限制
            .set_argument('--no-first-run')
            .set_argument('--no-default-browser-check'))


# ── 跨进程的浏览器锁 ──────────────────────────────────────
#
# 全机器只有一个浏览器，所以同一时刻只能有一个进程驱动它。
#
# 为什么需要：dsc（shim 进程）和 selftest（另一个进程）如果同时跑，
# 两边会互相把页面导航走 —— 表现是「答案莫名其妙不对」「找不到输入框」，
# 而且**看起来像 bug 其实是撞车**，非常难查。实测撞过一次：
# 自测三个联网用例失败，根因是用户正在用 dsc。
#
# 用「文件 + PID」做跨进程锁。持有者进程死了会自动接管，不会留下死锁。
LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locks')
BROWSER_LOCK = os.path.join(LOCK_DIR, 'main.lock')


def lock_path(key=None):
    """
    锁文件路径。key 为空 = 全局锁；有 key = 那个会话专属的锁。

    ★ 为什么按 key 分锁：多终端要能真并行。
      全局锁会把两个会话串起来（第二个等第一个跑完）。
      分锁之后，不同会话各跑各的 —— 因为它们用的是**不同的标签页**。
    """
    if not key:
        return BROWSER_LOCK
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', str(key))[:64] or 'x'
    return os.path.join(LOCK_DIR, f'{safe}.lock')


class BrowserBusy(RuntimeError):
    """浏览器被别的进程占着，等超时了。"""


def _pid_alive(pid):
    """那个 pid 的进程还在吗。查不到就保守地当作还活着。"""
    if not pid:
        return False
    try:
        import subprocess
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                             capture_output=True, text=True, timeout=5)
        return str(pid) in (out.stdout or '')
    except Exception:
        return True


@contextlib.contextmanager
def browser_lock(key=None, timeout=240.0, poll=0.5):
    """
    抢「驱动浏览器」的独占权。抢不到就排队，超时报 BrowserBusy。

    key 决定抢哪把锁：
      · key=None → 全局锁。只有一个页面，所有调用互相排队。
      · key=xxx  → 该会话专属的锁。不同会话用不同标签页，可以真并行。
    """
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = lock_path(key)

    deadline = time.time() + timeout
    fd = None
    waited = False
    last_check = 0.0

    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            if waited:
                log('[锁] 拿到浏览器')
            break
        except FileExistsError:
            if not waited:
                log('[锁] 浏览器被别的任务占着，排队等…')
                waited = True

            # 隔一阵子检查一次持有者是否还活着（tasklist 有点慢，别每轮都查）
            now = time.time()
            if now - last_check > 10:
                last_check = now
                owner = 0
                try:
                    with open(path) as f:
                        owner = int((f.read() or '0').strip() or 0)
                except Exception:
                    pass
                if owner and not _pid_alive(owner):
                    log(f'[锁] 持有者进程 {owner} 已经不在了，接管')
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    continue
            time.sleep(poll)

    if fd is None:
        raise BrowserBusy(
            f'浏览器被另一个任务占着超过 {timeout:.0f} 秒。\n'
            f'  如果确认没有别的任务在跑，删掉这个文件再试：\n'
            f'    {path}')

    try:
        yield
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(path)
        except Exception:
            pass


def browser_alive(page):
    """这个 page 对象的连接还活着吗。"""
    if page is None:
        return False
    try:
        _ = page.url
        return True
    except Exception:
        return False


_cached_page = None      # 全进程共用的浏览器连接


def ensure_browser(page=None, attempts=3):
    """
    确保手里有一个能用的浏览器连接 —— 断了就自己接回来。

    为什么需要：这套东西是长时间在后台跑的。浏览器可能被误关、崩掉，
    或者 CDP 连接自己断掉。没有自愈的话，后面每次请求都失败，只能人工重启。

    ★ page 传 None 时用进程内缓存的那一个。
      这一点很要紧：调用方（尤其是循环里的 do_chat）如果每次都把手里那个
      **已经失效的** page 传进来，就会变成「每轮都重连一遍」——
      多花约 1 秒/轮，日志里刷满「[自愈] 重连」。

    ⚠️ 只做「重连」，不做「重发」。重发有重复发送的风险（第一条其实已经
    发出去了），那个得靠 send_question 里的发送验证来兜，不能在这里盲目重试。
    """
    global _cached_page
    if page is None:
        page = _cached_page
    if browser_alive(page):
        _cached_page = page
        return page

    for i in range(attempts):
        try:
            log(f'[自愈] 浏览器连接不可用，第 {i+1}/{attempts} 次重连…')
            new_page, _ = connect()
            if browser_alive(new_page):
                log('[自愈] 重连成功')
                _cached_page = new_page
                return new_page
        except Exception as e:
            log(f'[自愈] 重连失败：{str(e)[:70]}')
        time.sleep(1.5 * (i + 1))
    raise RuntimeError('浏览器连接不上，重试多次仍失败。可能 Chrome 被关了，'
                       '或者配置目录被占用 —— 试试 ask --quit 再重来')


# ── 标签页池：一个会话一个标签页，多个终端就能真并行 ──────────
#
# 没有它的话，所有会话共用一个页面 —— 谁先抢到锁谁用，其余排队。
# 有了它，不同会话各用各的标签页，互不阻塞（实测两个标签页同时跑
# 8.3 秒，串行要 17 秒）。
#
# ★ 降级必须安全：标签页开不出来就返回 None，调用方退回「主页面 + 全局锁」——
#   慢，但正确。绝不能让并行这个「优化」把主流程搞挂。
MAX_TABS = 4                    # 每个标签页 50~100 MB，别开太多
_tabs = {}                      # key -> {'tab': 标签页, 'used': 最后使用时间}
_tabs_lock = threading.Lock()


def _evict_tabs_locked():
    """标签页到上限了就关掉最久没用的。调用方必须持 _tabs_lock。"""
    while len(_tabs) >= MAX_TABS:
        key, ent = min(_tabs.items(), key=lambda kv: kv[1]['used'])
        try:
            ent['tab'].close()
        except Exception:
            pass
        _tabs.pop(key, None)
        log(f'[标签页] 关掉最久没用的 {(key or "?")[:8]}（剩 {len(_tabs)} 个）')


def tab_for(key):
    """
    取这个 key 的专属标签页；没有就开一个。

    返回 None = 「用主页面」—— key 为空、或开标签页失败时的降级路径。
    """
    if not key:
        return None

    with _tabs_lock:
        ent = _tabs.get(key)
        if ent and browser_alive(ent['tab']):
            ent['used'] = time.time()
            return ent['tab']
        _tabs.pop(key, None)            # 那个标签页已经不在了

        try:
            main = ensure_browser()
            _evict_tabs_locked()
            tab = main.new_tab(URL)
            time.sleep(0.5)             # 给新标签页一点起页时间
            _tabs[key] = {'tab': tab, 'used': time.time()}
            log(f'[标签页] 给 {(key or "?")[:8]} 开了独立的（共 {len(_tabs)} 个）')
            return tab
        except Exception as e:
            log(f'[标签页] 开不出来（{str(e)[:60]}），退回主页面')
            return None


def close_extra_tabs():
    """关掉所有独立标签页，只留主页面。shim 启动时清一次遗留。"""
    with _tabs_lock:
        n = len(_tabs)
        for ent in _tabs.values():
            try:
                ent['tab'].close()
            except Exception:
                pass
        _tabs.clear()
    if n:
        log(f'[标签页] 清理了 {n} 个遗留标签页')
    return n


def connect():
    """
    优先接管已在运行的浏览器（快），没有就用同一个 profile 冷启动。

    刻意把「浏览器已经死了」当成一条正常路径 —— 官方文档说程序结束时浏览器不会
    主动关闭，但「VSCode 启动的除外」那个例外没有说明机制。所以这里不依赖它活着，
    『还活着』只是纯加速。任何情况下脚本都正确，只是冷启动慢几秒。
    """
    if port_alive():
        log('[浏览器] 接管已在运行的那个')
        return ChromiumPage(f'127.0.0.1:{PORT}'), False

    log('[浏览器] 启动中（首次会稍慢）…')
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return ChromiumPage(build_options()), True


# ============================================================
# 页面操作
# ============================================================

def answers(page):
    try:
        return page.eles(SEL['answer_body'], timeout=0.3)
    except Exception:
        return []


# 从 React fiber 里挖原始 markdown 的脚本。
#
# ★ 为什么非要从这里读，不能读 DOM：
#   网页版把 $...$ 当 LaTeX 行内公式渲染。实测输出
#       MARKER_XYZ $env:PATH D:\创业\test.txt
#   DOM 的 .text 读回来变成
#       MARKER_XYZ e n v : P A T H 还有 ...        ← 字母间被插了空格
#   而且多行内容会丢。PowerShell 命令里全是 $，所以这个 bug 会毁掉大量回答。
#   React 的 memoizedProps.content 是渲染前的原文，完全保真。
_JS_RAW_ANSWER = r'''
const els = document.querySelectorAll('.ds-assistant-message-main-content');
if (!els.length) return null;
const last = els[els.length - 1];
const fk = Object.keys(last).find(k => k.startsWith('__reactFiber'));
if (!fk) return null;
let f = last[fk];
for (let i = 0; f && i < 25; i++, f = f.return) {
  const c = f.memoizedProps && f.memoizedProps.content;
  if (typeof c === 'string' && c.length) return c;
}
return null;
'''


def last_answer_text(page):
    """
    取最后一条 AI 回答的文本。

    优先从 React 内部状态读【原始 markdown】；读不到才退回 DOM 渲染文本。
    理由见 _JS_RAW_ANSWER 上面的注释 —— DOM 那条路会被 LaTeX 渲染毁掉。
    """
    try:
        raw = page.run_js(_JS_RAW_ANSWER)
        if isinstance(raw, str) and raw.strip():
            return raw
    except Exception:
        pass

    items = answers(page)          # 兜底：DOM 渲染文本（可能被 LaTeX 破坏）
    if not items:
        return ''
    try:
        return items[-1].text or ''
    except Exception:
        return ''


def find_server_busy(page):
    """
    页面上有没有「服务器繁忙」这类提示。

    ★ 出现它就说明**这一轮根本没生成出回答**。实测此时回答气泡里只剩一个残渣
      （就是 `{"` 两个字），被当成正经回答交给上游后，用户界面上就是一个光秃秃的
      `{"`，会话卡死 —— 而且从现象里完全看不出原因。

    ★ 为什么必须配一个长度阈值（见调用处）：这类提示有可能是**留在对话历史里的
      旧气泡**，光看它在不在会误伤后面的正常短回答。
    """
    try:
        for pat in SEL['server_busy']:
            for ele in page.eles(pat, timeout=0.2):
                try:
                    if ele.states.is_displayed:
                        return ele
                except Exception:
                    pass
    except Exception:
        pass
    return None


def find_continue_buttons(page):
    """
    找「继续生成」按钮的**所有候选**，越像真按钮的越靠前。

    ★ 实测页面结构（2026-10-03 拿真实的截断回答量的）：
        <div class="ds-button ds-button--outlinedNeutral ...">   ← 真按钮，React 的 onClick 在这
          <span class="ds-button__content">继续生成</span>        ← 文字
    而 `text:继续生成` 只会命中**里层那个 span**。早先的代码按「class 里有
    ds-button」筛 —— span 的 `ds-button__content` **也含这个子串**，所以它拿到的
    其实一直是 span。（点 span 能用，事件会冒泡到父 div；但多留几个候选更稳。）

    ★ 排序：带 `ds-button ` / `ds-button--` 的（真按钮）排前面，`ds-button__`
    这种 BEM 元素（子元素）排后面。
    """
    out = []
    try:
        for ele in page.eles(SEL['continue_button'], timeout=0.3):
            try:
                cls = ele.attr('class') or ''
            except Exception:
                continue
            if 'ds-button' not in cls:
                continue
            try:
                if not ele.states.is_displayed:
                    continue
            except Exception:
                pass
            # 真按钮 vs BEM 子元素（__content 这种）
            out.append((0 if '__' not in cls else 1, ele))
    except Exception:
        pass
    out.sort(key=lambda x: x[0])
    return [e for _, e in out]


def find_continue_button(page):
    """第一个候选。保留这个名字是给自测和旧调用用的。"""
    btns = find_continue_buttons(page)
    return btns[0] if btns else None


def click_continue(page, tries=CONTINUE_CLICK_TRIES):
    """
    点「继续生成」。点上了返回 True，几种方式都点不上返回 False。

    ★ 为什么不能「抓个引用直接 click()，抛异常就算了」：
      实测真实报错是 DrissionPage 的 **「该元素没有位置及大小」** ——
      React 重渲染把节点换掉、或那一瞬间还没布局。**这是一次性抖动，不是「点不了」。**
      早先一次失败就认输 → 半截回答被当成完整回答交出去 → 上游报「缺 content」。
    ★ 还有更阴的一种：**点在空气上但 click() 不报错**（项目自己的坑 1）。
      那种只有「点完没反应」才知道 —— 所以调用方把「没反应」也当成失败重试。
    ★ 每次都重新抓元素：React 每次重渲染都换 DOM 节点，引用绝不能缓存。
    ★ JS 触发（by_js=True）不需要坐标，专治「没有位置及大小」。
    """
    last = ''
    for i in range(1, tries + 1):
        for ele in find_continue_buttons(page):
            try:
                ele.scroll.to_see()          # 真实点击更接近人的行为
            except Exception:
                pass
            # 第一遍先试真实点击（更像人），之后再试就直接上 JS —— 因为
            # 「重试」这个动作本身就说明上一次（多半是真实点击）没生效。
            for by_js in ((False, True) if i == 1 else (True, False)):
                try:
                    ele.click(by_js=by_js)
                    if i > 1 or by_js:
                        log(f'[续写] 点击成功（第 {i} 次尝试'
                            f'{"，改用了 JS 触发" if by_js else ""}）')
                    return True
                except Exception as e:
                    last = str(e).replace('\n', ' ')[:60]
        if i < tries:
            time.sleep(0.8)
    log(f'[续写] {tries} 次都没点上（{last}）')
    return False


def answer_done_rendered(page):
    """最后一条 AI 回答下方的操作栏渲染出来了没有 —— 渲染了说明这条写完了。"""
    items = answers(page)
    if not items:
        return False
    try:
        return bool(items[-1].parent().ele(SEL['action_button'], timeout=0))
    except Exception:
        return False


def clean(text):
    return THINK_HEADER_RE.sub('', text or '').strip()


def get_conversations(page):
    """
    读侧边栏的对话列表，返回 [(标题, href, 元素), ...]，按屏幕位置从上到下。

    按位置排序而不是按 DOM 顺序 —— 视觉顺序才是用户看到的顺序。

    注意：列表可能是虚拟滚动的，只有滚动到可见区域的条目才会出现在 DOM 里。
    所以这里只能列出当前加载出来的那些。
    """
    try:
        items = page.eles(SEL['conversation'], timeout=3)
    except Exception:
        return []

    out = []
    for it in items:
        try:
            y = it.rect.location[1]
            title = (it.text or '').strip().replace('\n', ' ')
            if title:
                out.append((y, title, it))
        except Exception:
            continue
    out.sort(key=lambda t: t[0])
    return [(t, ele) for _, t, ele in out]


def save_history(question, answer, think):
    """把这次问答追加到当天的 markdown 里。写失败也不能影响正常问答。"""
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        path = os.path.join(HISTORY_DIR, time.strftime('%Y-%m-%d') + '.md')
        tag = ' 〔深度思考〕' if think else ''
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'\n## {time.strftime("%H:%M:%S")}{tag}\n\n'
                    f'**问：** {question}\n\n**答：**\n\n{answer}\n')
    except Exception as e:
        log(f'[提示] 历史记录没写进去：{e}')


def is_logged_in(page, timeout=5):
    """
    能查到输入框 = 还处于登录态。

    timeout 别调太小：Chrome 会节流后台标签页，你在终端打字时浏览器窗口
    通常不在前台，DOM 查询偶尔会慢过 1 秒 —— 那会误判成「登录失效」。
    """
    return find_first(page, SEL['chat_input'], timeout=timeout) is not None


def diagnose_missing_input(page):
    """
    找不到输入框时，给一份能区分故障原因的诊断。

    ★ 为什么需要：「找不到输入框」至少是三种完全不同的情况 ——
        1. 登录态真失效了
        2. 网页改版，选择器失效了
        3. 页面还在加载 / 标签被节流
      只报一句「登录态失效，跑 ask --login」，真改版的时候用户会反复去扫码，
      方向完全错了。这里把选择器命中情况直接摆出来，一眼能分清。
    """
    lines = ['没找到输入框。诊断：']
    try:
        url = page.url or ''
        if 'chat.deepseek.com' not in url:
            lines.append(f'  · 当前不在 DeepSeek 页面：{url[:70]}')
            lines.append('    → 多半是网络问题，不是登录也不是选择器')
        else:
            hits = []
            for name, locs in SEL.items():
                first = locs if isinstance(locs, str) else locs[0]
                try:
                    hits.append(f'{name}={len(page.eles(first, timeout=0.5))}')
                except Exception:
                    hits.append(f'{name}=?')
            lines.append('  · 选择器命中数：' + '  '.join(hits))

            # 页面上有没有「登录」按钮？有 → 确实没登录；没有 → 更像改版
            try:
                login_btn = page.ele('text:登录', timeout=1) or page.ele('text:Log in', timeout=1)
            except Exception:
                login_btn = None
            if login_btn:
                lines.append('  · 页面上有「登录」入口 → 确实是登录态失效')
                lines.append('    → 跑 ask --login')
            else:
                lines.append('  · 页面上没有「登录」入口，但也没有输入框')
                lines.append('    → 更像网页改版导致选择器失效，跑 ask --probe 看')
    except Exception as e:
        lines.append(f'  · 诊断本身出错：{str(e)[:70]}')

    lines.append('  兜底：先试 ask --login，不行再 ask --probe')
    return '\n'.join(lines)


def ensure_page(page, new_chat):
    """
    确保停在一个可提问的页面。

    new_chat=True  → 导航到根地址，开一个全新对话。
    new_chat=False → 保持当前页面不动。绝不能在这里 page.get(URL)，
                     那就是开新对话，会把 --continue 的上下文清掉。
    """
    if new_chat or 'chat.deepseek.com' not in (page.url or ''):
        page.get(URL)
        time.sleep(1.0)
    if is_logged_in(page):
        return True

    # 没找到输入框。多半是页面正忙 / 后台标签被节流 —— 重新加载一次再试，
    # 而不是立刻判定「登录失效」把整个交互会话打断。
    log('[提示] 没找到输入框，重新加载页面再试一次…')
    page.get(URL)
    time.sleep(2.0)
    return is_logged_in(page)


# 附件「已就绪」的信号。
#
# 实测（png / txt / py 三种类型都验过）：上传完成后，输入框上方会多出一个
# .ds-animated-size-item —— 这是 DeepSeek 设计系统的类名，比哈希类名稳。
#   · 图片会额外渲染一张 <img> 缩略图，文本类没有
#   · 约 0.4 秒出现
#   · 消息发送后计数归零（说明附件被消费掉了）
# 早先的实现是按 [class*=file/attach/upload] 瞎猜再死等 —— 既可能误判
# （页面上本来就有别的元素带这些字样）又慢（最多干等 10 秒）。
ATTACH_READY_SEL = 'css:.ds-animated-size-item'
ATTACH_TIMEOUT = 15.0

# 一条消息**最多能挂多少个附件** —— 实测出来的（数 .ds-animated-size-item）
#
#   一次性投 50 个        → 50 个全进 ✅
#   先投 40、再加 10(→50) → 50 个 ✅
#   先投 40、再加 20(→60) → **卡在 40，一个都没进** ★
#   先投 40、再加 5 (→55) → **卡在 50，一个都没进** ★
#
# 两条结论：
#   ① 上限 = 50
#   ② 超限时是**整批拒绝**，不是「能塞多少塞多少」——
#      所以「先投 40 再加 20」会一个都进不去，光看这个很容易把上限误测成 40
#      （我第一遍就测错了，是用户指出来的）。
#
# 因为②，把多于剩余名额的一股脑丢过去，最坏结果是**一个都没传上**，
# 而提示词里还写着「附件就是内容本身」—— 模型只能回「我看不到图」。
# 所以在这里**主动按剩余名额截断并大声记日志**。
MAX_ATTACH = 50


def upload_attachments(page, paths):
    """
    通过网页隐藏的 input[type=file] 上传附件，**确认就绪后才返回**。

    这个 input 常驻 DOM（display:none，由按钮触发），accept 列表覆盖几乎所有
    代码/文本/文档/图片格式。DrissionPage 可以直接往它塞路径，多文件用换行分隔。
    """
    paths = [p for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        return False

    def ready_count():
        """已经就绪的附件挂件数量。读不到就返回 -1（当作未知）。"""
        try:
            return len(page.eles(ATTACH_READY_SEL, timeout=0.3))
        except Exception:
            return -1

    before = max(ready_count(), 0)
    want = before + len(paths)

    # ★ 超上限的部分必须**在这里截掉并说出来**，理由有两层：
    #
    #   ① 网页超限时是**整批拒绝**，不是部分接受。实测：框里已有 40 个时再投
    #      10 个（→50）能全进；再投 5 个（→55）则**一个都进不去**。
    #      所以我们要是把 45 个一股脑丢过去，可能落到「一个都没传上」，
    #      而调用方的提示词里还写着「附件就是内容本身」—— 模型只能回「我看不到图」。
    #
    #   ② 上限算的是「输入框里**同时挂着**的总数」，不只是这一批 ——
    #      上一次发送失败残留下来的附件也占名额。所以按**剩余名额**截，
    #      而不是按 len(paths) 截。
    room = max(0, MAX_ATTACH - before)
    if len(paths) > room:
        log(f'[附件] ⚠️ 框里已有 {before} 个，本批 {len(paths)} 个超出上限 '
            f'{MAX_ATTACH} —— 只传前 {room} 个，其余 {len(paths) - room} 个**没传**')
        paths = paths[:room]
    if not paths:
        log(f'[附件] ❌ 输入框里已经挂满 {MAX_ATTACH} 个，这一批传不进去')
        return False

    inp = page.ele('css:input[type="file"]', timeout=5)
    if not inp:
        log('[附件] ❌ 找不到上传入口，附件没传上去')
        return False

    try:
        inp.input('\n'.join(paths))
    except Exception as e:
        log(f'[附件] ❌ 投递失败：{str(e)[:80]}')
        return False

    # 等「就绪挂件数达标」，而不是等一个拍脑袋的固定时长
    t0 = time.time()
    while time.time() - t0 < ATTACH_TIMEOUT:
        n = ready_count()
        if n >= want:
            log(f'[附件] ✅ {len(paths)} 个已就绪（{time.time()-t0:.1f} 秒）')
            return True
        time.sleep(0.25)

    log(f'[附件] ❌ 等了 {ATTACH_TIMEOUT:.0f} 秒仍未就绪 '
        f'（当前 {ready_count()} / 期望 {want}）—— 附件可能没传上去')
    return False


def send_question(page, text, baseline='', attachments=None):
    """
    把问题填进输入框并发出去。

    ★ 两条纪律，都是踩坑换来的：

    1. **每次操作前重新抓元素，绝不缓存引用。** React 每次重渲染都会替换掉
       DOM 节点。拿着旧引用去点击，轻则点在空气上（click() 还不报错！），
       重则抛 NoRectError。这是最难查的一类故障 —— 它是间歇性的，只有恰好
       赶在重渲染的节骨眼上才炸，平时测几十次都正常。

    2. **发出去没有，用「输入框有没有清空」验证，不信 click 有没有报错。**
       React 提交后会立刻清空输入框，这是最可靠的信号。
    """
    # 干活的时候把窗口藏起来，别弹到前台抢焦点
    set_window_visible(page, False)

    if '\n' in text:
        # 输入框里换行可能被当成回车提前触发发送
        log('[提示] 问题里的换行已替换成空格')
        text = ' '.join(text.splitlines())

    # 附件必须在填字之前传 —— 传完输入框附近会长出附件条
    if attachments:
        # ★ 传失败必须**报错，不能照发**。
        #   踩过一整晚：附件没上去、消息却发出去了，而提示词里写着
        #   「★ 上面这个附件就是这个文件的内容本身」—— 模型找不到附件，
        #   只能回「我无法看到附件中的图片内容」，然后重读、再重试，死循环。
        #   宁可这一轮明确失败，也不要发一条**在骗模型**的消息。
        if not upload_attachments(page, attachments):
            raise RuntimeError(
                f'附件没传上去（{len(attachments)} 个）—— 消息没有发出。'
                f'重试一次；若反复失败，看 logs 里 [附件] 那几行。')

    box = find_first(page, SEL['chat_input'], timeout=15)
    if not box:
        raise RuntimeError('找不到输入框。可能没登录 —— 先跑 ask.cmd --login')

    box.click()
    try:
        # by_js=False 走真实按键（ctrl-a + del）。by_js=True 直接改 value
        # 不会触发 React 的 onChange，框里看着有字但发出去是空的。
        box.clear(by_js=False)
    except Exception:
        pass

    box.input(text)
    time.sleep(0.4)

    def still_pending():
        """输入框里还有没有没发出去的字。元素重新抓，不用旧引用。"""
        b = find_first(page, SEL['chat_input'], timeout=2)
        if b is None:
            return False                     # 输入框都没了 = 页面在跳转 = 已发出
        try:
            return bool((b.run_js('return this.value') or '').strip())
        except Exception:
            return False

    # 按可靠性顺序试。每种都用同一个判据验证。
    for name, act in (
        ('点发送按钮',
         lambda: find_first(page, SEL['send_button'], timeout=3).click()),
        ('JS 点发送按钮',
         lambda: find_first(page, SEL['send_button'], timeout=3).click(by_js=True)),
        ('输入框回车',
         lambda: find_first(page, SEL['chat_input'], timeout=3).input(Keys.ENTER)),
    ):
        try:
            act()
        except Exception as e:
            log(f'[发送] {name} 报错：{str(e)[:50]}')
            continue

        time.sleep(1.5)
        # 两个独立信号，任一成立就算发出去了：
        #   ① 输入框被清空  ② 最后一条回答的内容变了（新的回答开始覆盖它）
        # 特意不用「回答节点个数变多」—— 那个受虚拟滚动和渲染时序影响，不稳。
        if not still_pending() or last_answer_text(page) != baseline:
            return
        log(f'[发送] {name} 没生效，换一种方式')

    raise RuntimeError('三种发送方式都没能把消息发出去 —— 跑 ask --probe 看看选择器')


# ── 伪流式：增量回调 ──────────────────────────────────────
#
# ★ 为什么需要：网页版不给 token 流，我们只能轮询 DOM；但可以把轮询到的
#   增量实时吐给上层，用户看到的就是逐字冒出来，而不是「卡 30 秒 → 一大段」。
#   这是 dsc 和真 Claude Code 体感差距最大的一处。
#
# ★ 为什么用回调而不是返回值：这条线要贯穿五层。回调不传就是 None，
#   行为完全不变 —— 对 CLI / MCP / api_server 零影响。
#
# ★ 为什么每处都要 try/except：这是「展示」用的旁路，绝不能因为
#   用户代码报错就把取回答的主流程搞挂。
def _emit(on_delta, text):
    if not on_delta or not text:
        return
    try:
        on_delta(text)
    except Exception as e:
        log(f'[流式] 回调出错（忽略）：{str(e)[:60]}')


# 思考标题行**还没写完**时的样子：开头像标题，但整段还没有换行。
# 见 streamable_prefix 的说明 —— 这种时候先别吐。
_HEADER_PENDING_RE = re.compile(
    r'^\s*(已深度思考|深度思考|思考中|正在思考|已思考|Thought about|Thinking)[^\n]*$')


def streamable_prefix(text):
    """
    这段文本里，**能安全吐出去**的部分有多长。

    ★ 为什么要截：这一轮可能是工具调用，而工具调用**不能流** ——
      得等 JSON 完整了才能解析。所以一旦文本里出现 `{`，就从那里截断：
      前面的叙述（「我先看一下目录」）是给人看的，可以吐；
      后面的 JSON 没成型，吐出去就是垃圾。

    ★ 只认 `{` 不认 `[`：`[` 在正常回答里太常见（Markdown 链接、
      列表、代码里的下标），拿它当判据会频繁误截。而模型要调工具时
      **必然**写 `{`（`{"tool_use"...}`），所以 `{` 一个就够了。

    ★ 必须先 `clean()`：非流式路径吐给上游的是 `clean(text)`，流式这条路
      原先吐的是**原文** —— 同一份回答两条路产出不一样。开深度思考时，
      那行「已深度思考（用时 N 秒）」会被流出去，而普通路径会把它删掉。
      和 `streamable_prefix` 之外的每一处清洗对齐，才不会出现
      「流式看到的和最终落盘的不一致」。

    ★ 标题行没写完之前**压住不吐**：`clean()` 要等那行**带着换行**到齐了
      才删得掉，而流式是按前缀一次次吐的 —— 在它到齐之前把
      「已深度思考（用时 3」吐出去就收不回来了（SSE 的 delta 只能加不能减）。
      压住是对的：真等不到换行，`_finish_stream` 会把结尾补上。
    """
    text = clean(text)
    if _HEADER_PENDING_RE.match(text):
        return ''
    i = text.find('{')
    return text if i < 0 else text[:i]


def _poll_interval(t0, now=None):
    """
    轮询间隔：开头密、之后稀。

    ★ 首字延迟是体感关键（用户盯着空屏），所以前 POLL_FAST_WINDOW 秒用
      POLL_FAST；一旦开始出字，文本变化没那么频繁，改用 POLL_SLOW。
      实测一轮 30 秒的回答从 120 次 CDP 往返降到约 65 次。
    """
    return POLL_FAST if (now or time.time()) - t0 < POLL_FAST_WINDOW else POLL_SLOW


def wait_answer(page, baseline, think, start_limit=None, total_limit=None,
                search=False, on_delta=None):
    """
    等回答生成完。两道判据：

      主   回答下方的操作栏渲染出来了（生成结束才渲染）
      兜底 最后一条回答文本连续 N 秒不再变化

    护栏：必须「文本非空 + 稳定性达标」，且「操作栏就绪 或 稳定性超过两倍窗口」
    才收工。操作栏判据一旦失效，会自动退化到更保守的纯稳定性判定 ——
    不崩，只是慢一点。这是防止选择器失效时静默返回空结果的关键。

    baseline 是发送前最后一条回答的文本。**判断「新回答开始了没有」靠跟它比对
    内容，不靠数节点个数** —— 数个数会受虚拟滚动（对话变长后旧消息会被移出
    DOM）和渲染时序影响，在长对话里会间歇性失灵，而且失灵得很安静：
    不报错，只是干等到超时。
    """
    # 深度思考和智能搜索走同一套「慢档」预算 —— 搜索要先联网搜一轮，
    # 首字动辄 30 秒以上，用普通档会间歇性误报「回答没有开始」。
    slow = bool(think or search)
    stable_need = STABLE_THINK if slow else STABLE_NORMAL
    if start_limit is None:
        start_limit = START_THINK if slow else START_NORMAL
    if total_limit is None:
        total_limit = TOTAL_THINK if slow else TOTAL_NORMAL

    # --- 阶段 1：等回答开始。避免刚发出去就判空。 ---
    t_start = time.time()
    deadline = t_start + start_limit
    started = False
    while time.time() < deadline:
        txt = last_answer_text(page)
        if txt and txt != baseline:
            started = True
            break
        time.sleep(_poll_interval(t_start))
    if not started:
        return None, '回答没有开始 —— 可能没发出去，或者选择器失效了（跑 --probe 看看）'

    # --- 阶段 2：等生成结束。 ---
    last_text, last_change = '', time.time()
    continues = 0
    t_start = time.time()
    deadline = t_start + total_limit
    # ★ 两个缓存，都是为了省 CDP 往返：
    #   done_seen     —— answer_done_rendered 一旦 True 就不会变回 False
    #                    （操作栏渲染出来就一直在），没必要每轮重查。
    #   last_btn_scan —— 「继续生成」按钮降频，见 CONTINUE_SCAN_INTERVAL。
    cache = {'done_seen': False, 'last_btn_scan': 0.0}
    while time.time() < deadline:
        time.sleep(_poll_interval(t_start))

        txt = last_answer_text(page)
        if txt != last_text:
            # ★ 只在**变长**时吐（见 _emit 上面 ① 的说明）——
            #   网页重渲染会让文本变短，那种一律忽略，绝不收回已吐的。
            if len(txt) > len(last_text):
                _emit(on_delta, streamable_prefix(txt))
            last_text, last_change = txt, time.time()
            continue

        stable_for = time.time() - last_change
        if stable_for < stable_need:
            continue
        if not txt.strip():                          # ① 空结果不算数
            continue
        # ② 还没稳，继续等。操作栏查过一次是真的就不再重复查（见 cache 说明）。
        if not cache['done_seen']:
            cache['done_seen'] = answer_done_rendered(page)
        if not (cache['done_seen'] or stable_for >= stable_need * 2):
            continue

        # ★ 文本稳了，但**未必是真的答完了** —— 也可能是页面弹了
        #   「服务器繁忙，请稍后重试」：这一轮压根没生成出回答。
        #   实测那种情况下回答气泡里只剩 `{"` 两个字，被当成「正经回答」交给
        #   上游后，用户界面上就是一个光秃秃的 `{"`，会话直接卡死，
        #   而且从现象里完全看不出是服务端限流。
        #   宁可**明确报错**，也不要交一坨看不懂的残渣出去。
        if len(txt.strip()) < BUSY_MIN_CHARS and find_server_busy(page):
            log(f'[繁忙] 页面提示服务器繁忙，回答只有 {len(txt.strip())} 字 —— '
                f'这一轮没生成出来，按失败处理')
            return None, ('DeepSeek 服务器繁忙（页面提示「服务器繁忙，请稍后重试」），'
                          '这一轮没有生成出回答。稍等几秒重发一次即可。')

        # ★ 文本稳了，但**可能是被长度限制截断的** —— 回答下方会出现
        #   「继续生成」按钮。不处理的话我们只拿到半截，用户那边看着就是
        #   「输出莫名其妙断了」。实测踩过：一个 SVG 任务的回答被切在半句上。
        # ★ 降频：按钮一旦出现就不会瞬间消失，1 秒粒度足够（见
        #   CONTINUE_SCAN_INTERVAL）。这一处曾经是热路径上最贵的查询。
        _now = time.time()
        if _now - cache['last_btn_scan'] >= CONTINUE_SCAN_INTERVAL:
            cache['last_btn_scan'] = _now
            cache['has_btn'] = bool(find_continue_button(page))
        btn = cache.get('has_btn')
        if btn and continues < MAX_CONTINUES:
            continues += 1
            before = txt
            log(f'[续写] 回答被截断了，点「继续生成」（第 {continues} 次）')

            # ★ 点完之后**必须等新内容真的开始出来**，不能直接回到「文本不变
            #   就是写完了」那套判据。
            #
            #   踩过：这里原先只写了 `last_change = time.time()`，而稳定性窗口
            #   只有 2.5 秒。可网页点完「继续生成」要 5~30 秒才开始吐字 ——
            #   2.5 秒一到就判「又写完了」，于是再点一次……6 次点击挤在 28 秒
            #   内打完，**把正在进行的续写自己打断了**：文本长度从头到尾纹丝
            #   不动（实测 15270 → 15270 → 15273 字），永远写不完。
            #   后果不只是慢 —— 最后拿到的半截工具调用被当成完整的交给上游，
            #   报 "Error writing file"（content 是空的）。
            #
            #   判据与阶段 1 一致：文本必须**变得和点击前不一样**才算续上了。
            #
            # ★ 再套一层重试：「点了但没反应」也算失败，换种方式再点一次。
            #   实测点击有两种坏法 —— 抛「该元素没有位置及大小」（React 重渲染
            #   的瞬时抖动），以及**点在空气上却不报错**（坑 1）。
            #   早先两者都是直接认输 → 半截回答当完整回答交出去 → 上游报
            #   「缺 content」。最坏总耗时和早先一样（CONTINUE_WAIT × TRIES）。
            restarted = False
            for attempt in range(1, CONTINUE_CLICK_TRIES + 1):
                if not click_continue(page):
                    break
                wait_start = time.time()
                # ★ 这一圈是单次最贵的：CONTINUE_WAIT=45 秒、原先 POLL=0.25
                #   → 一次续写最长 **180 次** CDP 往返。而续写期间文本本来
                #   就是断断续续出的，密查纯属浪费。自适应后约减半。
                while time.time() - wait_start < CONTINUE_WAIT:
                    time.sleep(_poll_interval(wait_start))
                    new_txt = last_answer_text(page)
                    if new_txt and len(new_txt) > len(before):
                        _emit(on_delta, streamable_prefix(new_txt))
                    if new_txt and new_txt != before:
                        restarted = True
                        last_text, last_change = new_txt, time.time()
                        break
                if restarted:
                    break
                log(f'[续写] 第 {attempt} 次点完 {int(CONTINUE_WAIT)} 秒没动静，'
                    f'换种方式再点')
            if not restarted:
                # ★ 这里**不能**只返回 (txt, None) 当成品 —— 那正是缺陷 45：
                #   半截回答被当成完整回答交出去（实测 `[回答] 23 字` 那种）。
                #   但也**不能**直接报错：内容很可能其实是完整的，只是
                #   「继续生成」按钮没消失（误报）。这一层没有足够信息判断 ——
                #   要解析出工具调用才知道。所以只**报告事实**，
                #   判断交给 shim 侧（见 claude_shim 里对 partial 的处理）。
                log(f'[续写] 第 {continues} 次没续上，按已完成处理（但可能被截断）')
                return txt, TRUNCATED_WARN
            continue

        if continues >= MAX_CONTINUES:
            # ★ 这条**不报** TRUNCATED_WARN，和上面那条分支刻意区别开。
            #   能走到这里说明每次续写**都成功过**（文本一直在变长，否则
            #   会在 `if not restarted` 就 return 了），只是还没续完。
            #   内容在长 = 没被卡住，报「可能被截断」是误报。
            #   自测的钉子用例正是这个场景：MAX_CONTINUES=2 续满，
            #   断言 `err is None and 续写内容在`。
            log(f'[续写] 已经续了 {MAX_CONTINUES} 次还没完，就此打住')
            return txt, None
        return txt, None

    return None, f'生成超时（{int(total_limit)} 秒）'


def find_toggle(page, label, timeout=5):
    """按文字在多个 .ds-toggle-button 里认出目标开关。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            btns = page.eles(SEL['toggle_button'], timeout=0.3)
        except Exception:
            btns = []
        for b in btns:
            try:
                if label in (b.text or ''):
                    return b
            except Exception:
                continue
        time.sleep(0.2)
    return None


def set_toggle(page, label, on, name):
    """
    幂等：读当前状态，只在需要时点一下。DeepSeek 会记住上次的开关状态。

    ★ 为什么必须**每轮都调**，而不是只在「想开」时调：正因为网页会记住状态，
      用户手动开着的搜索会一直是开的。我们要是以为它是关的，就会在「没料到会联网」
      的情况下按普通档等超时 —— 那种失败很隐蔽。所以每轮都要**双向**对齐。
    ★ 不对称语义（沿用早先 set_think 的）：**强开却找不到开关 → 抛错**（整轮 500，
      让上游重试）；**强关找不到 → 记一行日志放过**。想开没开等于答非所问；
      想关没关最多是慢一点，不值得把整轮搞失败。
    """
    btn = find_toggle(page, label)
    if not btn:
        if on:
            raise RuntimeError(f'找不到「{name}」开关')
        log(f'[提示] 页面上没找到「{name}」开关，按默认（关）继续')
        return

    if toggle_state(btn) != on:
        btn.click()
        time.sleep(0.6)
        btn2 = find_toggle(page, label)
        if btn2 and toggle_state(btn2) != on:
            raise RuntimeError(f'「{name}」开关切换失败')
        # 只在真的切换了才打印 —— 否则交互模式下每轮都刷一行没意义的
        log(f'[模式] {name}已切到：{"开" if on else "关"}')


def set_think(page, on):
    set_toggle(page, THINK_LABEL, on, '深度思考')


def set_search(page, on):
    set_toggle(page, SEARCH_LABEL, on, '智能搜索')


def toggle_state(ele):
    """读开关状态。优先标准 aria 属性，退而求其次看 class。"""
    for attr in ('aria-pressed', 'aria-checked'):
        try:
            val = ele.attr(attr)
        except Exception:
            val = None
        if val is not None:
            return str(val).lower() == 'true'
    try:
        cls = ele.attr('class') or ''
    except Exception:
        cls = ''
    return any(k in cls for k in ('active', 'selected', 'checked'))


# ============================================================
# 子命令
# ============================================================

def do_login(page):
    set_window_visible(page, True)      # 扫码必须看得见
    page.get(URL)
    log('')
    log('=' * 60)
    log('请在弹出的浏览器窗口里完成登录（扫码 / 账号密码）。')
    log('登录成功后脚本会自动检测到，不用做别的。')
    log('=' * 60)

    deadline = time.time() + 600
    while time.time() < deadline:
        if is_logged_in(page):
            log('')
            log('✅ 登录成功，登录态已写入：')
            log(f'   {PROFILE_DIR}')
            log('   以后不用再登录了。')
            return 0
        time.sleep(2)

    log('❌ 等了 10 分钟还没检测到登录。重跑一次 --login 即可。')
    return 1


def do_probe(page):
    set_window_visible(page, True)      # 调选择器时要能看见页面
    print(f'URL: {page.url}')
    print()
    for name, locs in SEL.items():
        for loc in first_of(locs):
            try:
                n = len(page.eles(loc, timeout=0.6))
                print(f'{"OK " if n else "MISS"} {name:13} {loc:50} -> {n}')
            except Exception as e:
                print(f'ERR  {name:13} {loc:50} -> {e}')
        print()

    # 所有开关的当前状态 —— 顺便看「智能搜索」是不是开着（开着会联网，变慢）
    try:
        btns = page.eles(SEL['toggle_button'], timeout=1)
    except Exception:
        btns = []
    print(f'页面上的开关（{len(btns)} 个）：')
    for b in btns:
        try:
            label = (b.text or '').strip().replace('\n', ' ')
            print(f'   {label:10} aria-pressed={b.attr("aria-pressed")!r}')
        except Exception as e:
            print(f'   读不到：{e}')


def do_dump(page):
    set_window_visible(page, True)      # 排查时要能看见
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dump.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page.html)
    log(f'页面 HTML 已写入 {path}')
    log('在编辑器里搜 ds-markdown / 停止生成 / 深度思考，看真实结构。')


LIST_HINT = '没读到对话列表。多半是侧边栏被收起了 —— 在浏览器里展开它再试。'


def do_list(page):
    if not ensure_page(page, new_chat=False):
        log('❌ 没检测到输入框，登录态可能失效了。跑 ask --login。')
        return 1
    convs = get_conversations(page)
    if not convs:
        log(LIST_HINT)
        return 1
    for i, (title, _) in enumerate(convs, 1):
        print(f'{i:>3}  {title}')
    log(f'—— 共 {len(convs)} 个。用 ask --open <序号> 打开其中一个，'
        f'然后 ask -i 或 ask --continue 接着聊')
    return 0


def do_open(page, n):
    if not ensure_page(page, new_chat=False):
        log('❌ 没检测到输入框，登录态可能失效了。跑 ask --login。')
        return 1
    convs = get_conversations(page)
    if not convs:
        log(LIST_HINT)
        return 1
    if not 1 <= n <= len(convs):
        log(f'❌ 序号 {n} 超范围，当前只有 1-{len(convs)}')
        return 1
    title, ele = convs[n - 1]
    try:
        ele.click()
    except Exception as e:
        log(f'❌ 点不开：{e}')
        return 1
    time.sleep(2.0)
    log(f'已打开：{title}')
    return 0


def ask(page, question, think=False, attachments=None,
        start_limit=None, total_limit=None, search=False, on_delta=None):
    """
    **发一个问题并等答案。所有上层都必须走这里。**

    返回 (答案原文, 错误信息)。

    ★ 为什么要有这个函数：
      「取 baseline → 发送 → 等待 → clean」这套动作曾经在四个地方各写了一遍
      （CLI、MCP、shim、api_server）。结果是 MCP 那处把 baseline 传成了
      「节点个数」而不是「上一轮回答的文本」—— 字符串比整数恒为真，
      wait_answer 会立刻放行，然后**静默返回上一轮的答案**。
      抄三份改两份，这类错误必然会再犯。收敛成一个入口，就不会了。
    """
    baseline = last_answer_text(page)
    send_question(page, question, baseline, attachments)
    text, err = wait_answer(page, baseline, think,
                            start_limit=start_limit, total_limit=total_limit,
                            search=search, on_delta=on_delta)
    return (clean(text) if text else text), err


def ask_in_session(question, think=False, new_chat=True, attachments=None,
                   page=None, navigate_to=None,
                   start_limit=None, total_limit=None, key=None, search=False,
                   skip_toggles=False, on_delta=None):
    """
    **一轮完整的问答：拿连接 → 确保页面 → 设开关 → 发问 → 等答案。**

    返回 (page, 答案, 错误信息)。page 要接住，下次传回来就能复用连接。

    ★ 为什么又包一层（在 ask() 之上）：
      ask() 只收敛了「发送 + 等待」。但它前面那段
      「ensure_browser → ensure_page → set_think」在四个上层里各抄了一遍 ——
      CLI / MCP / shim / api_server 长得几乎一模一样。
      MCP 那个 bug 的**根源**就在这里：同样的前置序列抄了四份，其中一份写错。
      收敛到这一层之后，四个上层都只剩「调一次 + 处理返回值」。

    navigate_to 传 URL 时是「跳回某个已有的网页对话」（shim 用）；
    否则按 new_chat 决定开新对话还是接着当前的（CLI / MCP / api_server 用）。

    skip_toggles=True 时**跳过设开关那一步**。给谁用：shim 的**重试**路径。
    见下面那段说明 —— 重试是去修 JSON 格式的，跟开关状态毫无关系，
    而 set_toggle 的「强开找不到就抛错」语义会把整个重试废掉。
    """
    # ★ 全程持锁：多个进程同时驱动同一个页面会互相把它导航走。
    #   key 为空 → 全局锁（一个页面，大家排队）；
    #   key 有值 → 该会话专属的锁 + 专属标签页（不同会话真并行）。
    with browser_lock(key):
        return _ask_in_session_locked(
            question, think, new_chat, attachments, page, navigate_to,
            start_limit, total_limit, key, search, skip_toggles, on_delta)


def _ask_in_session_locked(question, think, new_chat, attachments, page,
                           navigate_to, start_limit, total_limit, key=None,
                           search=False, skip_toggles=False, on_delta=None):
    """ask_in_session 的实体，调用方必须已经持有 browser_lock。"""
    # 有 key 就用它的专属标签页；开不出来（或没给 key）就用主页面。
    tab = tab_for(key)
    page = tab if tab is not None else ensure_browser(page)

    if navigate_to:
        # 已经在这个对话里就别再导航 —— page.get() + 等待要花近 2 秒，
        # 而增量请求本身才几百字，这 2 秒比干活还长。
        if (page.url or '').rstrip('/') != navigate_to.rstrip('/'):
            page.get(navigate_to)
            time.sleep(1.5)
        if not is_logged_in(page):
            return page, None, ('跳不回那个网页对话了（可能被删了）。'
                                '先跑一次 ask --login 确认登录态')
    else:
        if not ensure_page(page, new_chat=new_chat):
            return page, None, diagnose_missing_input(page)

    # ★ 两个开关都**每轮双向对齐**，不是「想开才调」—— 网页会记住上次状态，
    #   用户手动开着的搜索会一直是开的，我们以为它是关的就会算错超时档。
    #
    # ★ skip_toggles 是给 shim 的**重试**路径用的（见 claude_shim 里的调用点）。
    #   为什么要能跳过：set_toggle 的语义是「强开找不到就抛错」，而重试是去修
    #   JSON 格式的，跟开关状态毫无关系 —— 开关一次瞬时抖动（React 重渲染、
    #   页面还没稳）就把整个重试废掉，用户直接吃 500。实测踩过：
    #       03:03:33 [重试] 第 1/2 次：Write 缺 content……重新问一次
    #       03:03:35 [重试] 失败：「智能搜索」开关切换失败
    #       03:03:35 [重试] 2 次都没修好，返回 500 交给上游重试
    #   重试复用的是同一个标签页，开关上一轮刚对齐过，再切一次纯属自找麻烦。
    if not skip_toggles:
        set_think(page, think)
        set_search(page, search)
    else:
        log('[重试] 跳过开关对齐（本轮只为修格式，不碰开关）')
    text, err = ask(page, question, think, attachments,
                    start_limit=start_limit, total_limit=total_limit, search=search,
                    on_delta=on_delta)
    return page, text, err


def ask_once(page, question, think, cont, cold_started):
    """问一次，返回 (答案, 错误)。do_ask 和 do_chat 共用。"""
    if cont:
        if cold_started:
            log('[提示] 浏览器是刚启动的，上一轮的对话不在了 —— 这次实际是新对话')
        else:
            log('[会话] 接着上一轮')
    else:
        log('[会话] 新对话')

    # 连接 / 页面 / 开关 / 发问 全由 ask_in_session 负责，别在这儿自己拼
    _page, text, err = ask_in_session(question, think, new_chat=not cont, page=page)
    if not err:
        save_history(question, text, think)
    return text, err


def do_ask(page, question, think, cont, cold_started):
    log(f'[提问] {question}')
    text, err = ask_once(page, question, think, cont, cold_started)
    if err:
        log(f'❌ {err}')
        return 1
    print(clean(text))
    return 0


def do_chat(page, cont, think, cold_started):
    """交互模式：同一个对话里持续追问。"""
    log('')
    log('=' * 56)
    log('交互模式 —— 直接打字提问，在同一个对话里持续追问。')
    log('  exit 或 quit  退出')
    log('=' * 56)

    first = True
    while True:
        try:
            sys.stdout.flush()      # 先把缓冲的答案吐出去，免得提示符插在答案前面
            q = input('\n> ').strip()
        except (EOFError, KeyboardInterrupt):
            log('\n已退出。')
            return 0

        if not q:
            continue
        if q.lower() in ('exit', 'quit'):
            log('已退出。')
            return 0

        # 第一轮尊重 --continue；之后一直留在同一个对话里
        text, err = ask_once(page, q, think, cont if first else True,
                             cold_started if first else False)
        first = False

        if err:
            log(f'❌ {err}')
            if '输入框' in err or '登录' in err:
                log('   交互模式退出 —— 修好后重跑。')
                return 1
            continue

        print()
        print(clean(text))


# ============================================================

def main():
    ap = argparse.ArgumentParser(
        prog='ask',
        description='在命令行问 DeepSeek 网页端',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='例：\n'
               '  ask.cmd "什么是快速排序"\n'
               '  ask.cmd --continue "再讲详细点"\n'
               '  ask.cmd --think "9.11 和 9.9 哪个大"',
    )
    ap.add_argument('question', nargs='*', help='要问的问题')
    ap.add_argument('--login', action='store_true', help='打开浏览器等你扫码登录')
    ap.add_argument('--probe', action='store_true', help='打印各选择器的命中数量（调参用）')
    ap.add_argument('--dump', action='store_true', help='把页面 HTML 落盘成 dump.html')
    ap.add_argument('--continue', dest='cont', action='store_true', help='接着上一轮对话追问')
    ap.add_argument('--think', action='store_true', help='打开深度思考（会慢很多）')
    ap.add_argument('-i', '--chat', action='store_true',
                    help='交互模式：在同一个对话里持续追问，exit 退出')
    ap.add_argument('--list', dest='list_', action='store_true',
                    help='列出侧边栏的历史对话')
    ap.add_argument('--open', dest='open_', type=int, metavar='N',
                    help='打开第 N 个历史对话（序号看 --list）。可配 -i 直接接着聊')
    ap.add_argument('--quit', action='store_true', help='关闭浏览器')
    ap.add_argument('--show', action='store_true',
                    help='这次别藏窗口（默认会藏起来，不抢焦点）')
    args = ap.parse_args()

    if args.show:
        _win_state['force_visible'] = True

    if args.quit:
        if not port_alive():
            log('没有正在运行的浏览器。')
            return 0
        ChromiumPage(f'127.0.0.1:{PORT}').quit()
        log('已关闭。')
        return 0

    question = ' '.join(args.question).strip()
    need_question = not (args.login or args.probe or args.dump or args.chat
                         or args.list_ or args.open_ is not None)
    if need_question and not question:
        ap.print_help()
        return 2

    if need_question:
        # 一行标记，让你随时知道这次走的是哪条路、花不花钱。
        log('[免费 · DeepSeek 网页版] 完全不经过 Claude，不消耗任何 API 额度')

    try:
        page, cold_started = connect()
    except Exception as e:
        log(f'❌ 连不上浏览器：{e}')
        log('   若提示 profile 被占用，说明同一个 profile 已经开着 —— 先 ask.cmd --quit')
        return 1

    if args.login:
        return do_login(page)
    if args.probe:
        do_probe(page)
        return 0
    if args.dump:
        do_dump(page)
        return 0
    if args.list_:
        return do_list(page)
    if args.open_ is not None:
        rc = do_open(page, args.open_)
        if rc != 0 or not args.chat:
            return rc
        # --open 3 -i ：打开第 3 个对话后直接进交互模式，接着聊
        return do_chat(page, True, args.think, False)
    if args.chat:
        return do_chat(page, args.cont, args.think, cold_started)
    return do_ask(page, question, args.think, args.cont, cold_started)


if __name__ == '__main__':
    # 刻意不在这里 quit() 浏览器 —— 留着让下一次运行能直接接管。
    # 要收尾请显式跑 --quit。
    sys.exit(main())
