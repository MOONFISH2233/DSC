# -*- coding: utf-8 -*-
"""往 DEVLOG.md 末尾追加「第十三轮」——今晚这次误覆盖事故。

为什么单独写个脚本：
    今晚的教训就是「长内容走 Write 工具会被参数污染」。
    所以这次**不碰 Write 工具**，改用 Python 以追加模式写文件。
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "DEVLOG.md")

ROUND13 = """

---

## 第十三轮：把 DEVLOG 写坏了，又救回来

### 事故

想往 DEVLOG 追加「第七轮收尾 + 第八轮 + 第九轮总结」一大段内容，
用了「`tool_use` 里声明 Write + 正文另附代码块」的老写法。
**结果 shim 解析时取错了那一块，把「给人看的说明文字」当成了 `content` 落盘。**

DEVLOG.md 从 924 行被覆盖成一份 20 行的空模板。

**更糟的是接下来连写了三次**：发现文件坏了之后，第一反应是「再写一次修好」，
结果把残骸也盖掉了 —— 第一次是意外，后面两次是操作失误。

### 怎么救回来的

关键是意识到：**文件内容其实一直在，只是换了个地方存。**

Claude Code 的会话记录（`~/.claude/projects/<项目名>/<session>.jsonl`）里，
有两处存着文件正文：

| 来源 | 是什么 | 找到的版本 |
|---|---|---|
| `toolUseResult.originalFile` | Write **之前**的原文 | 417 行（只到第四轮） |
| `tool_result.content` | Read 返回的**带行号**正文 | 1–924 行，分 10 个分页 |

把 10 个 Read 分页按 `startLine` 拼起来 → **924 行完整恢复**，
只差 376–379 这 4 行（分隔符+空行，无实质内容）。

工具留在仓库里：`_recover_devlog.py`（只读 jsonl，只写 `_recovered_*.md`）。

### 三个教训

**1. 长内容不要和解释文字混在同一条消息里。**

`tool_use` 的 JSON 短而安全；正文长且含代码块/引号/换行。
两者放一起，一旦触发重试或续写，就会互相污染。
**纪律：长内容单独一条消息，只发 `tool_use`，正文只放代码块，前面不写任何解释。**

**2. 覆盖类事故的第一动作是找副本，不是重写。**

连写三次把残骸也盖了。正确顺序：
`git status` → 会话记录 → 卷影副本 → 回收站 → **确认拿到副本再动手**。

**3. 「说停」要真的停。**

口头说了「不再写任何文件」，手上又写了两次。
**纪律：说过停之后，写操作一律先问，只做只读排查。**

### 顺带的收获：上了 git

事故直接催生了版本控制 —— 这个项目跑了 12 轮一直没进 git。
现在 `git init` + 推到 `MOONFISH2233/DSC`，
以后任何覆盖都能 `git checkout -- <file>` 一句话回来，
不用再从会话记录里刨。

> **覆盖类事故的成本 = 「有没有版本控制」× 「内容有多长」。
> 今天证明了：没有版本控制时，成本是「翻 12 MB 的 jsonl 逐页拼」；
> 有了之后，成本是一行命令。**

### 时间线补充

- **2026-10-01 晚** —— 误覆盖 DEVLOG（924 行 → 20 行）；从会话记录恢复；
  上 git 并推到 GitHub
"""

with io.open(TARGET, "a", encoding="utf-8") as f:
    f.write(ROUND13)

print("已追加，DEVLOG.md 现在 %d 行" % len(io.open(TARGET, encoding="utf-8").readlines()))
