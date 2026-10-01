# -*- coding: utf-8 -*-
"""
把 DeepSeek 网页版包装成一个「OpenAI 兼容」的本地 API。

启动：
    python api_server.py                 # 监听 http://127.0.0.1:8899
    python api_server.py --port 9000
    python api_server.py --think         # 所有请求都开深度思考

然后任何支持「OpenAI 兼容接口」的客户端都能用它：

    Base URL : http://127.0.0.1:8899/v1
    API Key  : 随便填（本地服务，不校验）
    Model    : deepseek-web          （想要深度思考就用 deepseek-web-think）

自测：
    curl http://127.0.0.1:8899/v1/chat/completions ^
      -H "Content-Type: application/json" ^
      -d "{\"model\":\"deepseek-web\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"

⚠️ 三条必须知道的：
  1. 慢。一次请求要驱动浏览器点一遍网页，10-60 秒。API 是毫秒，这里差三个数量级。
  2. 一次只能处理一个请求（只有一个浏览器），用锁串行化了。
  3. 仍然违反 DeepSeek 服务条款。这是本地自用工具，别对外暴露。
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_ask as ds          # noqa: E402

MODEL_PLAIN = 'deepseek-web'
MODEL_THINK = 'deepseek-web-think'

_lock = threading.Lock()           # 只有一个浏览器，请求串行
_default_think = False


def build_prompt(messages):
    """
    把 OpenAI 的 messages 数组压成一段话。

    为什么要压：网页版只有「一个输入框」，没有 system / user / assistant 的角色概念，
    也没有结构化的多轮上下文。所以只能把整段对话历史拼成一段文字喂进去。

    代价：客户端每轮都把完整历史发过来，这里就每轮都重拼一遍 ——
    对话越长越慢、越贵（占用网页输入框）。所以超长会被截断。
    """
    parts = []
    for m in messages:
        role = (m.get('role') or 'user').lower()
        content = m.get('content')
        if isinstance(content, list):       # 有些客户端发分段内容
            content = ' '.join(c.get('text', '') for c in content
                               if isinstance(c, dict) and c.get('text'))
        content = (content or '').strip()
        if not content:
            continue
        if role == 'system':
            parts.append(f'【系统指令】{content}')
        elif role == 'assistant':
            parts.append(f'【你之前的回答】{content}')
        else:
            parts.append(content)

    # 用公共的拼接函数：超长时保头（系统指令）+ 保尾（最近几轮）。
    # 早先这里是 text[-6000:] —— 只保尾巴，带 system prompt 的客户端
    # 系统指令会被整段砍掉，等于这个通道在真实使用下是残的。
    return ds.join_prompt(parts, head_parts=1)


def ask_web(prompt, think):
    """驱动浏览器问一次。连接/页面/开关/发问全交给 ds.ask_in_session。"""
    with _lock:
        # 每个 API 请求都开新对话 —— 无状态，跟 OpenAI 接口的语义一致
        _, text, err = ds.ask_in_session(prompt, think, new_chat=True)
        if err:
            raise RuntimeError(err)
        return text


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        ds.log('[HTTP] ' + (fmt % args))

    # ---------- 工具 ----------

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg, kind='invalid_request_error'):
        self._json(code, {'error': {'message': msg, 'type': kind, 'code': None}})

    def _sse(self, answer, model=MODEL_PLAIN):
        """流式返回。网页版本身是流式的，但这里一次性给完整答案 —— 够客户端用了。"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        created = int(time.time())

        def chunk(delta, finish=None):
            return {'id': 'chatcmpl-web', 'object': 'chat.completion.chunk',
                    # 用请求里实际指定的模型名 —— 写死 MODEL_PLAIN 的话，
                    # 请求 deepseek-web-think 时响应会报成 deepseek-web，
                    # 拿这个字段做路由/显示的客户端会被误导。
                    'created': created, 'model': model,
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

        for payload in (
            chunk({'role': 'assistant', 'content': answer}),
            chunk({}, 'stop'),
        ):
            self.wfile.write(f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode('utf-8'))
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.rstrip('/')
        if path in ('/v1/models', '/models'):
            self._json(200, {'object': 'list', 'data': [
                {'id': MODEL_PLAIN, 'object': 'model', 'created': 0, 'owned_by': 'deepseek-web'},
                {'id': MODEL_THINK, 'object': 'model', 'created': 0, 'owned_by': 'deepseek-web'},
            ]})
        elif path in ('', '/health'):
            self._json(200, {'status': 'ok', 'models': [MODEL_PLAIN, MODEL_THINK]})
        else:
            self._error(404, f'没有这个路径: {self.path}')

    def do_POST(self):
        if self.path.rstrip('/') not in ('/v1/chat/completions', '/chat/completions'):
            self._error(404, f'没有这个路径: {self.path}')
            return

        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception as e:
            self._error(400, f'请求体不是合法 JSON：{e}')
            return

        messages = req.get('messages') or []
        if not messages:
            self._error(400, 'messages 不能为空')
            return

        prompt = build_prompt(messages)
        if not prompt:
            self._error(400, 'messages 里没有可用的文本内容')
            return

        model = str(req.get('model') or MODEL_PLAIN)
        think = _default_think or ('think' in model.lower())

        ds.log(f'[请求] {len(prompt)} 字{" · 深度思考" if think else ""}')

        try:
            answer = ask_web(prompt, think)
        except Exception as e:
            ds.log(f'[失败] {e}')
            self._error(500, str(e), 'upstream_error')
            return

        if req.get('stream'):
            self._sse(answer, model)
            return

        self._json(200, {
            'id': 'chatcmpl-web',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': model,
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': answer},
                'finish_reason': 'stop',
            }],
            # 网页版不给 token 数，填 0 占位。有些客户端会拿它算钱，填 0 最诚实。
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
        })


def main():
    global _default_think

    ap = argparse.ArgumentParser(description='把 DeepSeek 网页版变成 OpenAI 兼容 API')
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--host', default='127.0.0.1',
                    help='默认只监听本机。改成 0.0.0.0 会暴露到局域网 —— 别这么干')
    ap.add_argument('--think', action='store_true', help='所有请求都开深度思考')
    args = ap.parse_args()

    _default_think = args.think

    # 挂到非回环地址 = 把这台机器对外开门。这个工具本身违反 ToS，
    # 再对外提供的话风险全在账号上 —— 给个明确警告和确认。
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        ds.log('')
        ds.log('⚠️⚠️⚠️  警告  ⚠️⚠️⚠️')
        ds.log(f'  你正在把服务挂到 {args.host} —— 不是本机回环地址。')
        ds.log('  同网络的别人可以拿你的账号白嫖 DeepSeek，风险全在你身上。')
        ds.log('  自己用的话请去掉 --host（默认 127.0.0.1）。')
        ds.log('')
        try:
            if input('  确定要继续吗？输入 yes 继续：').strip().lower() != 'yes':
                ds.log('  已取消。')
                return 1
        except Exception:
            ds.log('  非交互环境，视为取消。')
            return 1

    ds.log('=' * 60)
    ds.log('  DeepSeek 网页版 → OpenAI 兼容 API')
    ds.log(f'  Base URL : http://{args.host}:{args.port}/v1')
    ds.log(f'  API Key  : 随便填（本地服务，不校验）')
    ds.log(f'  Model    : {MODEL_PLAIN}' + (f' / {MODEL_THINK}' if not args.think else ''))
    ds.log('')
    ds.log('  ⚠️ 一次请求要 10-60 秒（要驱动浏览器点网页）')
    ds.log('  ⚠️ 一次只能处理一个请求')
    ds.log('  ⚠️ 违反 DeepSeek ToS，仅限本机自用')
    ds.log('=' * 60)

    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        ds.log('\n已停止。')
    except OSError as e:
        ds.log(f'❌ 起不来：{e}')
        ds.log(f'   端口 {args.port} 可能被占用，换个端口： --port 8898')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
