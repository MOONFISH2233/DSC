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
           'errors': [], 'texts': [], 'final': '', 'outfile': outfile,
           # ★ 工具**结果**报错的次数 —— 「模型跑的命令失败了几次」。
           #   这是除轮数之外最能说明「谁更靠谱」的量：原生 API 那边几乎
           #   不该有，dsc 这边每失败一次就多烧一轮。
           'tool_errors': 0}
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
            if err:
                rec['tool_errors'] += 1
            print('        %-11s → %s%s'
                  % (tu.get('name'), out[:80].replace('\n', ' '),
                     '   ❌ 这步失败了' if err else ''))
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


def _med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def summarize(pairs):
    """
    多次对拍汇总。

    ★ 单次对拍**只能找 bug**（结构性错误一次就够定罪），
      要谈「谁更利索」必须有分布 —— 模型每次跑都不一样，
      一次 7:3、下一次可能 6:5，拿单次说倍数是没有统计意义的。
      这里给中位数和范围，让人自己看**分不分得开**。
    """
    def prep(recs):
        out = []
        for r in recs:
            r = dict(r)
            r['tools_n'] = sum(len(x) for x in r['tools'])
            r['patch'] = (r.get('retries', 0) + r.get('no_content', 0)
                          + r.get('continues', 0) + r.get('busy', 0))
            out.append(r)
        return out

    A = prep([a for a, _ in pairs])
    B = prep([b for _, b in pairs])
    n = len(pairs)
    rows = [
        ('轮数 中位', [_med([r['rounds'] for r in A]), _med([r['rounds'] for r in B])]),
        ('轮数 范围', ['%d~%d' % (min(r['rounds'] for r in A),
                                  max(r['rounds'] for r in A)),
                       '%d~%d' % (min(r['rounds'] for r in B),
                                  max(r['rounds'] for r in B))]),
        ('工具调用 中位', [_med([r['tools_n'] for r in A]),
                           _med([r['tools_n'] for r in B])]),
        ('墙钟 中位（秒）', ['%.0f' % _med([r['elapsed'] for r in A]),
                             '%.0f' % _med([r['elapsed'] for r in B])]),
        ('★ 工具结果报错 合计', [sum(r['tool_errors'] for r in A),
                                 sum(r['tool_errors'] for r in B)]),
        ('★ 补丁 合计（重试/缺content/续写/繁忙）',
         [sum(r['patch'] for r in A), sum(r['patch'] for r in B)]),
        ('工具调用原文泄漏 合计', [sum(r['leaked'] for r in A),
                                   sum(r['leaked'] for r in B)]),
        ('产物字节 中位', [_med([r['artifact'] for r in A]),
                           _med([r['artifact'] for r in B])]),
    ]
    print()
    print('=' * 78)
    print('  汇总（%d 轮对拍）' % n)
    print('=' * 78)
    print('  %-34s %-16s %-16s' % ('', 'A · dsc', 'B · 真实 API'))
    for name, (x, y) in rows:
        print('  %-34s %-16s %-16s' % (name, x, y))
    print()
    print('  token：A in %d / out %d      B in %d / out %d   ← B 这些要花钱'
          % (sum(r['usage_in'] for r in A), sum(r['usage_out'] for r in A),
             sum(r['usage_in'] for r in B), sum(r['usage_out'] for r in B)))
    print()
    print('  ★ 怎么读这张表：先看**分不分得开** —— 两个范围要是重叠，')
    print('    那点差异就是噪声，别当结论。看得开、又稳定的那几行才是真差距。')
    print('  逐轮叙述留在 %s\\runN\\' % TMP_DIR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--side', choices=['both', 'shim', 'api'], default='both')
    ap.add_argument('--task', choices=['full', 'short'], default='short',
                    help='short 省 token（默认）；full 是 e2e 那道长任务')
    ap.add_argument('--rounds', type=int, default=25)
    ap.add_argument('--repeat', type=int, default=1,
                    help='跑几轮对拍（≥3 才谈得上「系统性差距」）')
    ap.add_argument('--save', action='store_true',
                    help='保留产物（默认每轮开跑前清空，免得上一轮的成品'
                         '改变这一轮的起点 —— e2e 踩过这个坑）')
    args = ap.parse_args()

    task = SHORT_TASK if args.task == 'short' else task_text('%s')
    if args.task == 'full':
        # task_text 里已经把路径写死了，这里改成占位符好让两侧各写各的目录
        task = task.replace(os.path.join(HERE, '_e2e_tmp', 'gen_report.py'), '%s')

    print('=' * 78)
    print('  A/B 对拍 · 任务=%s · %d 轮' % (args.task, args.repeat))
    print('=' * 78)

    shim_used_8799 = shim_up('http://127.0.0.1:8799')
    if shim_used_8799 and args.side in ('both', 'shim'):
        print('\n⚠️  8799 上有一个 shim 在跑（可能是你的 dsc 会话）。')
        print('    对拍会用自己那个（%d），但**浏览器只有一个** ——' % SHIM_PORT)
        print('    你那边一有请求，两边就会互相把页面导航走。')
        print('    建议先确认没有别的 dsc 会话在跑。\n')

    ep = None
    if args.side in ('both', 'api'):
        ep = api_endpoint()
        if not ep:
            print('❌ 环境里没有 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN，'
                  '跑不了 B 侧')
            return 2
        print('  B 侧: %s  model=%s' % (ep['base'], ep['model']))

    pairs = []
    proc = None
    try:
        for i in range(1, args.repeat + 1):
            print('\n' + '━' * 78)
            print('  第 %d/%d 轮对拍' % (i, args.repeat))
            print('━' * 78)
            run_dir = os.path.join(TMP_DIR, 'run%d' % i)
            if not args.save and i > 1:
                shutil.rmtree(run_dir, ignore_errors=True)
            a = b = None

            if args.side in ('both', 'shim'):
                # ★ 每轮都要重起 shim 吗？不用 —— 但**必须让它用干净的
                #   目录**，而且不能复用上一轮的会话映射（那会让第二轮
                #   变成「接着上一轮聊」，任务就不是同一个起点了）。
                #   所以每轮换个新的 session_id，映射自然就分开了。
                print('[A] dsc（网页版 + 协议模拟）')
                if proc is None:
                    # ★ 传自己的目录：start_shim 默认往 e2e_check 的 _e2e_tmp
                    #   写，而那边不归我们管、也不保证存在（第一版就是这么炸的）。
                    proc, url, ownlog = start_shim(
                        SHIM_PORT, os.path.join(run_dir, 'shim'))
                # ★ shim 只起一次、连着跑多轮，所以它的 stderr 是**累积**的 ——
                #   必须只数这一轮新增的那一段，否则第 2 轮会把第 1 轮的重试
                #   再数一遍（数出来的是 1、2、3… 的累加，看着像越来越糟）。
                #   e2e 那边也踩过同类坑（读公共日志会把用户的会话算进来）。
                _before = read_own_log(ownlog)
                a = run_side('dsc', url, {'content-type': 'application/json'},
                             'deepseek-web', task,
                             os.path.join(run_dir, 'shim'), args.rounds)
                _log = read_own_log(ownlog)[len(_before):]
                a['retries'] = len(re.findall(r'\[重试\].*重新问一次', _log))
                a['no_content'] = len(re.findall(r'缺 content', _log))
                a['continues'] = len(re.findall(r'\[续写\].*点「继续生成」', _log))
                a['busy'] = len(re.findall(r'\[繁忙\]', _log))

            if args.side in ('both', 'api'):
                print('\n[B] 真实 API（原生工具调用）')
                # ★ 只传 base —— run_side 自己会接 '/v1/messages'。
                #   第一版在这儿又接了一次，拼成 .../v1/messages/v1/messages，
                #   于是 404 cave_route_not_found（还先去查了一轮请求头，白查）。
                b = run_side('api', ep['base'], ep['headers'], ep['model'],
                             task, os.path.join(run_dir, 'api'), args.rounds)

            if a and b:
                pairs.append((a, b))
                show(a, b)
    finally:
        if proc:
            proc.kill()

    if len(pairs) > 1:
        summarize(pairs)
    return 0


if __name__ == '__main__':
    sys.exit(main())
