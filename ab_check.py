# -*- coding: utf-8 -*-
"""
A/B 对拍：**同一个任务，两条路各跑一遍，把过程和结果摆在一起比。**

    路 A（dsc）    Claude Code → 本地 shim → DeepSeek 网页版（我们的协议模拟）
    路 B（api）    Claude Code → 付费中转   → 原生工具调用

★ 为什么这个对比有意义：两边是**同一个模型家族**（中转那边也是
  deepseek-v4.x），差别只在「工具调用是怎么喂给模型的」——
  A 靠一段文字协议 + 我们从回复里抠，B 靠 API 原生的 tool_use。
  所以比出来的差异，基本就是**我们这一层（协议模拟）的代价**，
  而不是「模型不同」。这正是能拿来改进的东西。

★ 两边发**完全相同**的请求体（同一个 system、同一份工具、同一段任务、
  同样的 metadata）—— 唯一的变量就是那个 URL。不这么控制的话，
  比出来的差异说不清是谁造成的。

用法：
    py -3.11 ab_check.py                 # 两边都跑，出对比报告
    py -3.11 ab_check.py --side api      # 只跑真实 API（快，但**要花钱**）
    py -3.11 ab_check.py --side shim     # 只跑 dsc 那侧
    py -3.11 ab_check.py --task short    # 小任务（省 token，看趋势用）

★ 成本：路 B **按 token 计费**，报告末尾会打总量。
★ 抢浏览器：路 A 要独占浏览器 —— 跑之前确认没有别的 dsc 会话在跑，
  否则联网用例会假红（DEVLOG 缺陷 19）。
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ★ 复用 e2e_check 的驱动件，**不抄一份** —— 同一个职责两份实现，
#   这个项目已经栽过六次（DEVLOG 里记着）。
from e2e_check import (SYS, TOOLS, task_text, run_tool, shim_up, start_shim,
                       read_own_log)

TMP_DIR = os.path.join(HERE, '_ab_tmp')
SHIM_PORT = 8798                 # 和 e2e 一样避开 8799

# 小任务：够短，能看出「谁更利索」，又不至于烧掉太多 token。
SHORT_TASK = (
    '请写一个 Python 脚本到 %s。\n'
    '脚本功能：读一个文本文件，统计每个词出现的次数，输出前 10 名。\n'
    '要有函数拆分、有 __main__ 入口、有错误处理。写完**运行它**验证能跑通，'
    '再告诉我文件路径。\n'
    '自己造一个测试用的文本文件，别去读别的路径。'
)


def api_endpoint():
    """真实 API 那侧的地址和请求头（从环境变量来，和 claude 用的是同一套）。"""
    base = (os.environ.get('ANTHROPIC_BASE_URL') or '').rstrip('/')
    tok = os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY')
    model = os.environ.get('ANTHROPIC_MODEL') or 'claude-sonnet-4-5'
    if not base or not tok:
        return None
    return {'base': base, 'model': model,
            'headers': {'content-type': 'application/json',
                        'x-api-key': tok,
                        'authorization': 'Bearer ' + tok,
                        'anthropic-version': '2023-06-01'}}


def post(url, headers, body, timeout=900):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def looks_like_leaked_call(text):
    """
    这一段文字是不是**本该是工具调用、却被当成回答交出来**了。
    这正是 dsc 那侧独有的坏法（原生 API 不可能有），所以要单独数。
    """
    if not text:
        return False
    t = text.strip()
    if not t.startswith('{'):
        return False
    return ('"tool_use"' in t or '"tool_calls"' in t or
            ('"name"' in t and ('"input"' in t or '"arguments"' in t)))


def run_side(label, url, headers, model, task, workdir, max_rounds=25):
    """跑一侧，返回过程记录。两边用的是同一段循环，所以差异只来自后端。"""
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    outfile = os.path.join(workdir, 'gen_report.py')

    sid = 'ab-%s-%d' % (label, int(time.time()))
    body_common = {
        'model': model, 'max_tokens': 8192, 'system': SYS, 'tools': TOOLS,
        # ★ 带上 session_id，和真实 Claude Code 一样 —— 这样 dsc 那侧走的是
        #   **增量**路径（每轮只发新消息），而不是每轮重发全部历史。
        #   两边发的是同一个 body，唯一的变量是 URL。
        'metadata': {'user_id': json.dumps({'session_id': sid})},
    }

    msgs = [{'role': 'user', 'content': task % outfile.replace('\\', '\\\\')}]
    rec = {'label': label, 'rounds': 0, 'tools': [], 'elapsed': 0.0,
           'leaked': 0, 'narrated': 0, 'usage_in': 0, 'usage_out': 0,
           'errors': [], 'texts': [], 'final': '', 'outfile': outfile}
    t0 = time.time()
    for rnd in range(1, max_rounds + 1):
        rec['rounds'] = rnd
        body = dict(body_common, messages=msgs)
        try:
            resp = post(url.rstrip('/') + '/v1/messages', headers, body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:200]
            rec['errors'].append('第 %d 轮 HTTP %s：%s' % (rnd, e.code, detail))
            print('  [%s] 第 %2d 轮 HTTP %s %s' % (label, rnd, e.code, detail[:80]))
            break
        except Exception as e:
            rec['errors'].append('第 %d 轮请求失败：%s' % (rnd, str(e)[:150]))
            print('  [%s] 第 %2d 轮失败 %s' % (label, rnd, str(e)[:80]))
            break

        u = resp.get('usage') or {}
        rec['usage_in'] += u.get('input_tokens') or 0
        rec['usage_out'] += u.get('output_tokens') or 0

        blocks = resp.get('content') or []
        text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        uses = [b for b in blocks if b.get('type') == 'tool_use']
        names = [b.get('name') for b in uses]
        rec['tools'].append(names)
        if text.strip():
            rec['narrated'] += 1
            rec['texts'].append(text.strip())
        if looks_like_leaked_call(text):
            rec['leaked'] += 1
            rec['errors'].append('第 %d 轮：回答里是一坨工具调用原文' % rnd)
        rec['final'] = text or rec['final']

        print('  [%s] 第 %2d 轮  %s%s'
              % (label, rnd, names or ('文本 %d 字' % len(text)),
                 ('   ⚠️ 疑似 JSON 泄漏' if looks_like_leaked_call(text) else '')))
        if not uses:
            break

        results = []
        for tu in uses:
            out, err = run_tool(tu.get('name'), tu.get('input') or {})
            print('        %-11s → %s' % (tu.get('name'), out[:80].replace('\n', ' ')))
            results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                            'content': [{'type': 'text', 'text': out}],
                            **({'is_error': True} if err else {})})
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': results})

    rec['elapsed'] = time.time() - t0
    rec['artifact'] = (os.path.getsize(outfile)
                       if os.path.exists(outfile) else 0)
    with open(os.path.join(workdir, 'transcript.txt'), 'w',
              encoding='utf-8') as f:
        for i, t in enumerate(rec['texts'], 1):
            f.write('─── 叙述 %d ───\n%s\n\n' % (i, t))
    return rec


def show(a, b):
    """把两侧摆一起。只打**能直接指向改动的**那些量。"""
    def seq(rec):
        """把工具序列压成 Tool×N，一眼能看出「谁在反复调同一个」。"""
        comp = []
        for n in [x for names in rec['tools'] for x in names]:
            if comp and comp[-1][0] == n:
                comp[-1][1] += 1
            else:
                comp.append([n, 1])
        return ' → '.join('%s×%d' % (n, c) if c > 1 else n for n, c in comp)

    rows = [
        ('轮数', a['rounds'], b['rounds']),
        ('墙钟（秒）', '%.0f' % a['elapsed'], '%.0f' % b['elapsed']),
        ('工具调用总数', sum(len(x) for x in a['tools']),
         sum(len(x) for x in b['tools'])),
        ('叙述轮数（会说话）', a['narrated'], b['narrated']),
        ('★ 工具调用原文泄漏', a['leaked'], b['leaked']),
        ('报错/异常', len(a['errors']), len(b['errors'])),
        ('产物 gen_report.py（字节）', a['artifact'], b['artifact']),
        # 下面这几行 B 侧**恒为 0 且不可能非 0** —— 原生 API 不存在这些补救机制。
        # 它们量的正是「协议模拟这一层的代价」。
        ('补丁：重试次数', a.get('retries', '—'), '0（不需要）'),
        ('补丁：缺 content', a.get('no_content', '—'), '0（不需要）'),
        ('补丁：续写生成', a.get('continues', '—'), '0（不需要）'),
        ('补丁：服务器繁忙', a.get('busy', '—'), '0（不需要）'),
    ]
    w = max(len(r[0]) for r in rows) + 2
    print()
    print('=' * 78)
    print('  对比结果')
    print('=' * 78)
    print('  %-*s %-16s %-16s' % (w, '', 'A · dsc（协议模拟）', 'B · 真实 API'))
    for name, x, y in rows:
        flag = ''
        if name.startswith('★') and x:
            flag = '   ← 只有 dsc 会这样'
        print('  %-*s %-16s %-16s%s' % (w, name, x, y, flag))
    print()
    print('  A 工具序列: %s' % (seq(a) or '（无）'))
    print('  B 工具序列: %s' % (seq(b) or '（无）'))
    print()
    print('  A token: in %d / out %d' % (a['usage_in'], a['usage_out']))
    print('  B token: in %d / out %d   ← 这一段是要花钱的'
          % (b['usage_in'], b['usage_out']))
    for rec in (a, b):
        if rec['errors']:
            print()
            print('  [%s] 异常记录：' % rec['label'])
            for e in rec['errors'][:8]:
                print('     ·', e)
    print()
    print('  逐轮叙述留在 %s' % TMP_DIR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--side', choices=['both', 'shim', 'api'], default='both')
    ap.add_argument('--task', choices=['full', 'short'], default='short',
                    help='short 省 token（默认）；full 是 e2e 那道长任务')
    ap.add_argument('--rounds', type=int, default=25)
    args = ap.parse_args()

    task = SHORT_TASK if args.task == 'short' else task_text('%s')
    if args.task == 'full':
        # task_text 里已经把路径写死了，这里改成占位符好让两侧各写各的目录
        task = task.replace(os.path.join(HERE, '_e2e_tmp', 'gen_report.py'), '%s')

    print('=' * 78)
    print('  A/B 对拍 · 任务=%s' % args.task)
    print('=' * 78)

    a = b = None
    proc = None
    try:
        if args.side in ('both', 'shim'):
            if shim_up('http://127.0.0.1:8799'):
                print('\n⚠️  8799 上有一个 shim 在跑（可能是你的 dsc 会话）。')
                print('    对拍会用自己那个（%d），但**浏览器只有一个** ——' % SHIM_PORT)
                print('    你那边一有请求，两边就会互相把页面导航走。')
                print('    建议先确认没有别的 dsc 会话在跑。\n')
            print('[A] dsc（网页版 + 协议模拟）—— 自己起一个 shim（端口 %d）'
                  % SHIM_PORT)
            # ★ 传自己的目录：start_shim 默认往 e2e_check 的 _e2e_tmp 写，
            #   而那边不归我们管、也不保证存在（第一版就是这么炸的）。
            proc, url, ownlog = start_shim(SHIM_PORT, os.path.join(TMP_DIR, 'shim'))
            a = run_side('dsc', url, {'content-type': 'application/json'},
                         'deepseek-web', task,
                         os.path.join(TMP_DIR, 'shim'), args.rounds)
            # ★ 重试/续写只有 dsc 这侧才有（原生 API 不需要这些补救机制）——
            #   从**自己那个 shim 的 stderr** 里数，不读公共日志（那是所有
            #   shim 共写的，会把用户 dsc 会话的行算到我们头上）。
            _log = read_own_log(ownlog)
            a['retries'] = len(re.findall(r'\[重试\].*重新问一次', _log))
            a['no_content'] = len(re.findall(r'缺 content', _log))
            a['continues'] = len(re.findall(r'\[续写\].*点「继续生成」', _log))
            a['busy'] = len(re.findall(r'\[繁忙\]', _log))
        if args.side in ('both', 'api'):
            ep = api_endpoint()
            if not ep:
                print('❌ 环境里没有 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN，'
                      '跑不了 B 侧')
                return 2
            print('\n[B] 真实 API（原生工具调用）—— %s  model=%s'
                  % (ep['base'], ep['model']))
            # ★ 只传 base —— run_side 自己会接 '/v1/messages'。
            #   第一版在这儿又接了一次，拼成 .../v1/messages/v1/messages，
            #   于是 404 cave_route_not_found（还先去查了一轮请求头，白查）。
            b = run_side('api', ep['base'], ep['headers'],
                         ep['model'], task, os.path.join(TMP_DIR, 'api'),
                         args.rounds)
    finally:
        if proc:
            proc.kill()

    if a and b:
        show(a, b)
    return 0


if __name__ == '__main__':
    sys.exit(main())
