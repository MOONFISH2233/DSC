# -*- coding: utf-8 -*-
"""从 Claude Code 的会话记录里，把 DEVLOG.md 的原文抠出来。

关键事实（前两版摸清的）：
  · DEVLOG.md 被覆盖前有 **924 行**
  · Write 的 toolUseResult.originalFile 存的是「改之前的全文」，
    但只到第四轮（417 行）—— 那是更早的版本
  · 更晚的版本，存在 **Read 的分页结果**里：
        tool_result.content = "400\t...\n401\t...\n"，带 startLine / totalLines
    一页读不完会分页，把各页按 startLine 拼起来就是全文

本脚本：找出所有「DEVLOG 的 Read 分页」，按 startLine 拼成完整正文。
只读 jsonl、只写 _recovered_full.md，**绝不碰 DEVLOG.md**。

用法：
    python _recover_devlog.py --list     # 列出所有分页（默认）
    python _recover_devlog.py --export   # 拼接并导出
"""
import json
import os
import re
import sys
import glob

PROJ = r"C:\Users\MOONFISH\.claude\projects"
CANDIDATE_DIRS = [r"D----", r"D-----deepseek-ask"]


def iter_lines():
    for d in CANDIDATE_DIRS:
        for path in glob.glob(os.path.join(PROJ, d, "*.jsonl")):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        yield path, i, line
            except OSError as e:
                print("  跳过 %s: %s" % (path, e))


def parse_page(text):
    """把 "400\txxx\n401\tyyy" 解析成 {行号: 内容}。

    只有当**每一行**都是「数字 + Tab + 内容」时才认，
    否则返回 None（说明这不是带行号的 Read 结果）。
    """
    out = {}
    for raw in text.split("\n"):
        m = re.match(r"^(\d+)\t(.*)$", raw)
        if not m:
            return None
        out[int(m.group(1))] = m.group(2)
    return out or None


def is_devlog_page(page):
    """这一页是不是 DEVLOG 的内容（而不是别的文件的 Read）。"""
    if not page:
        return False
    joined = "\n".join(page.values())
    # DEVLOG 的特征：开头是「# 开发日志」，或正文里有这些章节
    if 1 in page and page[1].startswith("# 开发日志"):
        return True
    for marker in ("演进过程", "踩过的坑", "## 第九轮", "## 第十轮", "缺陷 2"):
        if marker in joined:
            return True
    return False


def main():
    export = "--export" in sys.argv
    pages = []   # (start, end, total, path, lineno, {行号:内容})

    for path, lineno, line in iter_lines():
        if "DEVLOG" not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            c = block.get("content")
            if not isinstance(c, str):
                continue
            page = parse_page(c)
            if not is_devlog_page(page):
                continue
            start = min(page)
            end = max(page)
            pages.append((start, end, len(page), path, lineno, page))

    if not pages:
        print("没找到 DEVLOG 的 Read 分页")
        return

    # 合并所有页到一个大字典（行号相同就覆盖，后面的版本更新）
    merged = {}
    for start, end, n, path, lineno, page in pages:
        for k, v in page.items():
            merged[k] = v

    print("找到 %d 个 DEVLOG 的 Read 分页：" % len(pages))
    for start, end, n, path, lineno, page in sorted(pages):
        print("  %4d–%-4d (%d 行)  %s:%d" % (start, end, n, os.path.basename(path), lineno))

    print("")
    if not merged:
        return
    lo, hi = min(merged), max(merged)
    missing = [i for i in range(lo, hi + 1) if i not in merged]
    print("合并后覆盖 %d–%d 行（共 %d 行，缺 %d 行）" % (lo, hi, len(merged), len(missing)))
    if missing:
        # 只报告缺口区间，不逐行刷屏
        gaps, s = [], None
        for i in range(lo, hi + 2):
            if i in merged:
                if s is not None:
                    gaps.append((s, i - 1))
                    s = None
            else:
                if s is None:
                    s = i
        print("  缺口: %s" % "、".join("%d–%d" % g for g in gaps[:20]))

    if not export:
        print("")
        print("（--list 模式，没写文件。加 --export 导出）")
        return

    outdir = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(outdir, "_recovered_full.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(merged.get(i, "") for i in range(lo, hi + 1)))
    print("")
    print("★ 已导出：%s（%d 行）" % (out, hi - lo + 1))
    print("  **没有**写回 DEVLOG.md")


if __name__ == "__main__":
    main()
