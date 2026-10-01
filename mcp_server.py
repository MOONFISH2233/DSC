# -*- coding: utf-8 -*-
"""
把 deepseek_ask 包装成 MCP server，让 Claude Code 能直接调用。

挂上之后就能在会话里说「问一下 DeepSeek：xxx」，由 Claude 替你调、拿到答案继续干活。

注册（user scope，任何目录都能用）：
    claude mcp add -s user deepseek -- "<PY>" "D:/创业/deepseek_ask/mcp_server.py"

⚠️ stdio MCP 的 stdout 是 JSON-RPC 专用通道。
   print() 到 stdout 会污染协议流，客户端表现为「连不上」或「Unexpected character」。
   所以这里绝不调用 deepseek_ask 里那些 do_* 函数（它们全是往 stdout print 的），
   只用返回值的那些。日志一律走 logging → stderr。
"""

import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 日志走 stderr —— stdio MCP 的标准日志通道
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(asctime)s [deepseek-mcp] %(levelname)s %(message)s',
)
log = logging.getLogger('deepseek-mcp')

import deepseek_ask as ds                      # noqa: E402  (有 __main__ 保护，import 不会跑 main)
from mcp.server.mcpserver import MCPServer     # noqa: E402

mcp = MCPServer('deepseek-web')

# 只有一个浏览器、一个输入框，并发调用会互相踩。串行化。
_lock = threading.Lock()


@mcp.tool()
def ask_deepseek(question: str, think: bool = False, follow_up: bool = False) -> str:
    """向 DeepSeek 网页端提问，返回回答的纯文本。

    走的是浏览器自动化驱动 chat.deepseek.com，用的是本机已登录的那个账号，
    不消耗 API 额度。

    Args:
        question: 要问的问题。
        think: 是否打开「深度思考」。打开后回答质量更高，但可能要等几分钟。
        follow_up: 是否接着上一轮对话追问。默认 False = 开新对话。

    Returns:
        DeepSeek 的回答文本。
    """
    with _lock:
        log.info('收到提问: %s (think=%s, follow_up=%s)',
                 question[:60], think, follow_up)

        # ★ 走公共入口 ds.ask_in_session()，不要自己拼
        #   connect → ensure_page → set_think → ask。
        #   这里曾经传的是 len(ds.answers(page))（节点个数），而 wait_answer 的
        #   baseline 语义是「上一轮回答的文本」—— 字符串比整数恒为真，
        #   阶段 1 直接放行，于是**静默返回上一轮的答案**，还不报错。
        #   根源就是这段前置序列在四个上层各抄了一遍。
        try:
            _, text, err = ds.ask_in_session(question, think,
                                             new_chat=not follow_up)
        except Exception as e:
            raise RuntimeError(
                f'连不上浏览器：{e}。'
                '若提示 profile 被占用，先在终端跑一次 ask.cmd --quit'
            )
        if err:
            raise RuntimeError(err)

        log.info('回答完成，%d 字', len(text or ''))
        return text


if __name__ == '__main__':
    log.info('deepseek MCP server 启动 (stdio)')
    mcp.run()      # 不传 transport 参数 = stdio
