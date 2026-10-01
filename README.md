# deepseek_ask

> 📖 **第一次接触这个项目？** 先看 **[INTRO.md](INTRO.md)** —— 五分钟了解它是干什么的。
> 🔧 **要接手维护？** 看 **[DEVLOG.md](DEVLOG.md)** —— 踩过的坑和关键决策都在里面。
> 本文是**使用手册 + 维护指南**。

**把免费的 DeepSeek 网页版当成 AI 后端用。**

不开会员、不充 API 额度 —— 驱动你已登录的浏览器去问 `chat.deepseek.com`，
然后把这个能力接到终端、接到 Claude Code、接到任何支持 OpenAI 接口的软件上。

---

## 它能给你什么

| 用法 | 命令 | 场景 |
|---|---|---|
| **终端直接问** | `ask "问题"` | 随手问一句，零成本 |
| **终端连续聊** | `ask -i` | 在同一个对话里持续追问 |
| **Claude Code 用它当大脑** | `dsc` | **最像产品的用法** —— 完整的 agent 能力，但不花 API 钱 |
| **给别的软件用** | `start-api-server.cmd` | LobeChat / ChatBox / Dify 等任何 OpenAI 兼容客户端 |
| **让 Claude Code 调用它** | `/ds`、`/build` | 我帮你问，答案回来接着干活 |

---

## 快速开始

```powershell
# 1. 首次：扫码登录（只需一次，登录态会存下来）
ask --login

# 2. 随便问点什么
ask "什么是快速排序"

# 3. 连续聊
ask -i
```

**在做上面任何事之前**，确认 `ask` 命令可用（新开一个 PowerShell 窗口即可）。

---

## 最核心的用法：`dsc`

让 **Claude Code 跑在免费的 DeepSeek 网页版上**。

```powershell
dsc                     # 开新会话
dsc -r                  # 恢复上次的对话
dsc -p "读一下 README"   # 一次性任务

claude                  # 回到你原来的付费中转
claude -r               # 恢复对话（付费）
```

### 为什么这能行

Claude Code 是个 agent —— 它需要模型能**发出结构化的工具调用**（"调用 Read 工具读某个文件"）。
网页版本身没有这个能力，但 `claude_shim.py` 补上了这一层：

```
Claude Code
    ↓  Anthropic Messages API（/v1/messages）
claude_shim.py           ← 把请求转成一段带工具说明的提示词
    ↓  浏览器自动化
DeepSeek 网页版（免费）
    ↓  从回答里解析出 tool_use
claude_shim.py           ← 转回 Anthropic 格式
    ↓
Claude Code 执行工具（读文件、跑命令）
    ↓  结果喂回去
……循环，直到任务完成
```

### 付费和免费可以随时换着用

因为**两边共享同一个 `session_id`**：

```powershell
claude      # 用付费的干，干到一半嫌贵
# Ctrl+C 退出
dsc -r      # 接着干，不花钱 —— 同一个对话、同一份上下文
```

### 深度思考开关

```powershell
dscthink            # 看当前状态
dscthink on         # 开（复杂推理质量明显更高，但每轮要等几分钟）
dscthink off        # 关
dscthink toggle     # 翻转
```

**实时生效，不用重启。**

什么时候用：

| 场景 | 建议 |
|---|---|
| 复杂推理（架构选型、算法设计） | ✅ 开 |
| 工具调用循环（读文件、改代码） | ❌ 别开 —— 十几轮累积起来没法等 |
| 日常问答 | ❌ 普通模式够快 |

---

## 支持图片和 PDF

Claude Code 读图片/PDF 时，`claude_shim.py` 会把内容**上传成附件**给网页版
（而不是当文本发过去 —— 那样只会是一坨乱码）。

实测有效：给一张左红右蓝的图，问它有什么颜色，答「红色，白色，蓝色」。

| 类型 | 怎么走 |
|---|---|
| 文本文件 | 内容进提示词（上限 30 万字） |
| 图片 / PDF | 存成临时文件 → 网页的附件上传接口 |

---

## 成本：说清楚

| 路径 | 花谁的钱 |
|---|---|
| 终端 `ask` | **零** —— 完全不经过任何 API |
| `dsc` / `/ds` / `/build` | **零**（DeepSeek 那边）—— 但见下面的注意 |
| `claude`（普通） | 你的付费中转，按量计费 |

⚠️ **在 Claude Code 里用 MCP（`/ds`）不是完全免费的** —— 那一轮编排仍然花中转的钱，
而且 DeepSeek 的回答会进上下文被反复计费。**问完不需要了记得 `/clear`。**

⚠️ **`dsc` 是真的免费** —— 它把整个模型后端都换掉了。

---

## 自测（改完代码先跑这个）

```powershell
python selftest.py            # 全部（约 1 分钟）
python selftest.py --fast     # 只跑离线的单元测试（秒级）
python selftest.py --heal     # 额外测自愈（会真的杀掉 Chrome）
```

**58 项自动检查**，覆盖三层：

| 层 | 内容 |
|---|---|
| 单元 | 回答解析、提示词构造、会话指纹、图片提取、坏输出识别 |
| 集成 | 选择器存活、发消息、取原文、附件上传、窗口控制、shim HTTP |
| 自愈 | 杀掉浏览器后能否自动恢复 |

**别跳过这一步。** 这套东西的 bug 几乎都是间歇性的，人工测两轮通过不算数。
开发期间这套自测抓到 **6 个真 bug**，其中 2 个是「编辑代码时误删了函数」。

## 运行日志

所有操作都会记到 `logs\YYYY-MM-DD.log`：

```powershell
Get-Content "logs\$(Get-Date -Format 'yyyy-MM-dd').log" -Tail 50
```

后台跑出问题时先看这个。

## 出问题了怎么办

### `ask` / `dsc` 报「找不到输入框」「回答没有开始」

**八成是 DeepSeek 改版了，选择器失效。** 跑：

```powershell
ask --probe
```

看每个选择器的命中数量。`-> 0` 就是错的。

详细排查见下面「选择器维护」一节。

### `dsc` 没反应 / 一直转圈

```powershell
# 看 shim 日志
Get-Content "$env:TEMP\shim.err" -Tail 30
```

### 登录态失效

```powershell
ask --login
```

---

# 技术档案

以下是维护用的。日常使用不用看。

---

## 文件职责

| 文件 | 作用 |
|---|---|
| `deepseek_ask.py` | **核心**。选择器定义、浏览器生命周期、发消息/等回答、附件上传 |
| `claude_shim.py` | 把网页版伪装成 Anthropic API，给 Claude Code 用 |
| `mcp_server.py` | MCP server，让 Claude Code 能主动调 `ask` |
| `api_server.py` | OpenAI 兼容 API，给第三方客户端用 |
| `ask-profile.ps1` | PowerShell 函数：`ask` / `dsc` / `dscthink` |
| `ask.cmd` | 命令行入口（固定 Python 路径） |
| `start-api-server.cmd` | 双击启动 API 服务 |
| `dsclaude-settings.json` | `dsc` 用的 Claude Code 配置 |

**注册到 Claude Code 的**：
- MCP server `deepseek`（`~/.claude.json`）
- slash command `/ds`、`/build`（`~/.claude/commands/`）

---

## 选择器维护（网页改版后）

**这是最需要定期维护的部分。** 所有选择器集中在 `deepseek_ask.py` 顶部的 `SEL` 字典。

### 三条铁律

| 优先级 | 用什么锚 | 例子 |
|---|---|---|
| **最稳** | 产品级属性 | `href`、`placeholder`、`role`、`aria-*` |
| **中等** | `ds-` 前缀的设计系统类名 | `.ds-toggle-button`、`.ds-assistant-message-main-content` |
| **绝对别用** | 纯哈希类名 | `_6dbc175`、`f6d670` —— 每次发版都变 |

### 现有选择器

| 键 | 选择器 | 说明 |
|---|---|---|
| `chat_input` | `css:textarea[placeholder*="DeepSeek"]` | 品牌名不会被翻译。注意 `#chat-input` **不存在** |
| `send_button` | `css:div[role="button"].ds-button--primary` | 纯图标按钮，没文字没 aria-label |
| `answer_body` | `css:.ds-assistant-message-main-content` | 只匹配 AI 回答。注意 `.ds-markdown--block` **不存在** |
| `action_button` | `css:.ds-button--iconLabelTertiary` | 回答下方的操作栏，生成结束才渲染 → 判完成用 |
| `toggle_button` | `css:.ds-toggle-button` | 带 `aria-pressed` 的是**外层 div** |
| `conversation` | `css:a[href^="/a/chat/s/"]` | 侧边栏对话列表 |
| `continue_button` | `text:继续生成` | 回答被长度限制截断时才出现。**有文字**，所以能靠文案定位（发送/停止按钮就没有） |

### 修复流程

```powershell
ask --probe     # 1. 看命中数量：0 = 选择器错，5 = 太宽
ask --dump      # 2. 落盘 HTML，在编辑器里搜关键词
```

**最有效的调试方式** —— 交互式接管浏览器，改一行试一行：

```powershell
& "C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe"
```
```python
import sys; sys.path.insert(0, r'D:\创业\deepseek_ask')
import deepseek_ask as d
page, _ = d.connect()
len(page.eles('css:textarea'))          # 试选择器
page.eles('css:textarea')[0].attrs      # 看它的所有属性，找稳定锚点
```

改完 `SEL` 再跑 `ask --probe` 确认，然后 `ask "测试"` 端到端验证。

---

## 踩过的坑（改代码前必读）

### 坑 1：元素引用会失效

React 每次重渲染都会**替换 DOM 节点**。拿着旧引用去 `click()`：
- 轻则按**过期坐标**点击 → 点在空气上 → **但 `click()` 不报错**
- 重则抛 `NoRectError`

**纪律：每次操作前重新抓元素，绝不缓存。**

### 坑 2：别用「数节点个数」判断状态

「回答节点数变多了 = 新回答开始」—— 这个判据不稳（虚拟滚动会让数量波动）。

**纪律：用内容比对。** 发送前记下最后一条回答的文本，新回答一开始必然变。

### 坑 3：读 DOM 文本会被 LaTeX 毁掉

网页版把 `$...$` 当公式渲染：

```
模型实际输出 : MARKER_XYZ $env:PATH D:\创业\test.txt
DOM .text    : MARKER_XYZ e n v : P A T H 还有 ...     ← 字母间插空格，第二行还丢
```

PowerShell 命令里全是 `$`，所以这个坑会毁掉大量回答。

**解法：从 React 内部状态读原始 markdown。**

```javascript
const el = document.querySelectorAll('.ds-assistant-message-main-content');
const last = el[el.length - 1];
let f = last[Object.keys(last).find(k => k.startsWith('__reactFiber'))];
for (let i = 0; f && i < 25; i++, f = f.return) {
  if (typeof f.memoizedProps?.content === 'string') return f.memoizedProps.content;
}
```

### 坑 4：长回答不要包 JSON

工具调用很短，包 JSON 稳妥；但**最终回答可能几千字**，塞进 JSON 字符串会因为
换行和引号转义而报废。

**纪律：只有工具调用用 JSON，最终回答就是普通 Markdown。**

### 坑 5：等待上限是「最长等待」不是「固定等待」

「智能搜索」开着时 DeepSeek 要先联网搜，首字可能要 30 秒以上。
上限设太小会间歇性误报「回答没有开始」。

**纪律：宁可给 90 秒** —— 检测到文本变化就立刻继续，调大不花代价。

### 怎么验证这类修改

这些 bug 的共同特征是**间歇性**。**至少连测 10 轮，而且要包含长回答。**
测 2 轮通过不算数。

---

## 实测数据（改参数时的依据）

| 项 | 实测值 |
|---|---|
| 输入框容量 | **100 万字以上**（没探到底） |
| 发送速度 | ~200 万字符/秒（不是逐字敲，是瞬间灌入） |
| DOM 开销 | 约 2.6 秒/轮 |
| 模型开销 | 约 5.9 秒/轮 |
| **绕开 DOM 最多省** | **31%** —— 所以**不值得**为了它去破解反自动化 |

---

## 为什么不用 DeepSeek 的接口直接调

抓包能找到真接口：

```
POST https://chat.deepseek.com/api/v0/chat/completion
{
  "chat_session_id": "...",
  "prompt": "...",
  "thinking_enabled": false,
  "search_enabled": true,
  ...
}
```

**但发消息这个接口有 `x-ds-pow-response` 头 —— 工作量证明（Proof of Work），
是 DeepSeek 明确的反自动化措施。**

会话创建接口不需要（纯 Python 能打通），发消息需要。

**我们没有绕过它。** 理由：
1. 绕过反自动化措施和「模拟人手点击」性质不同 —— 前者是在**破解安全控制**
2. 实测收益只有 31%，不值得

现在的做法是**在页面的保护下工作** —— PoW 由页面自己算。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **速度** | 每轮 3-60 秒（模型思考时间），比真 API 慢 |
| **可靠性** | 网页改版会导致选择器失效，需要维护 |
| **合规** | **违反 DeepSeek 服务条款**，风险在你的账号 |
| **并发** | 一次只能一个请求（只有一个浏览器） |
| **深度思考** | 开启后每轮几分钟，不适合长任务 |

**这是个人自用工具，不要对外提供，不要挂公网。**
