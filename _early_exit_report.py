# -*- coding: utf-8 -*-
"""
把日志里 [早收工评估] 那几行汇总出来 —— 决定「结构性提前收工」开不开。

    py -3.11 _early_exit_report.py

背景见 DEVLOG「第十八轮补十」和 deepseek_ask.py 的 EARLY_EXIT_NOTE。
一句话：`STABLE_NORMAL` 那 4 秒是每轮最大的一块自有开销，想用
「结构收尾」这个正向信号提前收工省掉它 —— 但**先量再改**。

判读：
  ✅ 安全   —— 收尾就是最后一次变长。这种轮提前收工没问题。
  🟡 勉强   —— 收尾后又长过，但都在 1.5 秒内长完。按 1.5 秒收工来得及。
  ⚠️ 危险   —— 收尾之后**超过 1.5 秒还在长**。这种轮提前收工会截断。

结论怎么下：
  ⚠️ 一条都没有      → 可以开（先用 e2e_check 把关，它精确数「缺 content」）
  ⚠️ 占比很低但非零   → 再想想：是加一条更强的判据，还是干脆放弃这条优化
  ⚠️ 占比明显        → **这条优化否掉**，老老实实等满 4 秒
"""
import glob
import os
import re
import collections

LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
pat = re.compile(r'\[早收工评估\]\s*(✅|🟡|⚠️)(.*)$')

tally = collections.Counter()
samples = collections.defaultdict(list)
files = sorted(glob.glob(os.path.join(LOGS, '*.log')))
for p in files:
    b = os.path.basename(p)
    if not re.match(r'\d{4}-\d\d-\d\d\.log$', b):     # 跳过 selftest-*.log
        continue
    for ln in open(p, encoding='utf-8', errors='replace'):
        # ★ 跳过**旧格式**的行（改判读指标之前记的那几条）。
        #   它们记的是「收尾之后还长过没有」，而现在的判据是
        #   「收尾之后**超过 1.5 秒**还在长」—— 两回事，混在一起会误导结论。
        if '这种轮**不能**提前收工' in ln:
            continue
        m = pat.search(ln)
        if not m:
            continue
        tally[m.group(1)] += 1
        if len(samples[m.group(1)]) < 5:
            samples[m.group(1)].append((b, m.group(2).strip()[:90]))

total = sum(tally.values())
print(f'扫描 {len([f for f in files])} 个日志文件')
print(f'有「结构收尾」的轮次：{total}\n')
if not total:
    print('还没有样本 —— 正常跑几天 dsc 再看。')
    raise SystemExit

for k in ('✅', '🟡', '⚠️'):
    n = tally[k]
    if not n:
        continue
    print(f'  {k}  {n:>5} 次   {100 * n / total:5.1f}%')
    for b, s in samples[k]:
        print(f'        [{b[-10:-4]}] {s}')
    print()

print('=' * 66)
if total < 30:
    print(f'★ 样本只有 {total} 条，**还不够下结论**（这类判断至少要上百条）。')
    print('  继续跑 dsc，过几天再来看。')
elif not tally['⚠️']:
    print('★ 没有「危险」样本 —— 可以开提前收工了。')
    print('  步骤：① 先写用例把 D1~D4 四个危险形状钉红')
    print('        ② 实现时让 shim 传一个更强的判据（缺长字段就不许早收工）')
    print('        ③ 用 e2e_check 把关（它精确数「缺 content」）')
elif tally['⚠️'] / total < 0.02:
    print(f'★ 「危险」占比 {100 * tally["⚠️"] / total:.1f}% —— 很低但不是零。')
    print('  要么加更强的判据，要么放弃这条优化。**不要直接开。**')
else:
    print(f'★ 「危险」占比 {100 * tally["⚠️"] / total:.1f}% —— **这条优化否掉**，')
    print('  「结构收尾」不是个可靠信号，老老实实等满 STABLE_NORMAL。')
