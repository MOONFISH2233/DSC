# DeepSeek 网页版命令行助手 —— 唯一定义处
# 由 Claude 生成于 2026-09-25
#
# 为什么单独放一份：PowerShell 7 和 Windows PowerShell 5.1 读的是两个不同
# 路径的 profile。与其在两个文件里各抄一遍（以后改一处漏一处），不如都来
# dot-source 这一份。
#
# ⚠️ 本文件必须存成 UTF-8 with BOM。PowerShell 5.1 读不带 BOM 的 UTF-8
#    会按 GBK 解析，下面的中文全变乱码，路径里的「创业」也会被搞坏。

function ask {
    <#
    .SYNOPSIS
        问 DeepSeek 网页版，免费，完全不经过 Claude，不消耗任何 API 额度。

    .DESCRIPTION
        驱动本机已登录的 Chrome 去问 chat.deepseek.com，答案直接打印。
        答案走 stdout，日志走 stderr，所以：
            ask "问题" > 答案.txt      # 文件里只有答案

    .EXAMPLE
        ask "什么是快速排序"
        ask -i                                 # 交互模式：持续追问，exit 退出
        ask --think "9.11 和 9.9 哪个大"      # 深度思考，会慢几分钟
        ask --continue "再讲详细点"            # 单次追问（接着上一轮）
        ask --list                             # 列出历史对话
        ask --open 3 -i                        # 打开第 3 个历史对话接着聊
        ask --login                            # 登录态失效时重登
        ask --probe                            # DeepSeek 改版后，重调选择器
        ask --quit                             # 关掉那个浏览器
    #>
    # 直接调 python.exe，不经过 ask.cmd。
    # 原因：多行参数经 cmd.exe 传递时会被截断（只传第一行）——
    # 粘贴一段多行的提示词进去就会踩到。绕开 cmd.exe 就没这问题。
    # ask.cmd 仍然保留，给「从别的地方调用」用。
    & "C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe" `
        -X utf8 "D:\创业\deepseek_ask\deepseek_ask.py" @args
}

Set-Alias -Name ds -Value ask -Force


function dsc {
    <#
    .SYNOPSIS
        像用 claude 一样用 Claude Code，但大脑是免费的 DeepSeek 网页版。

    .DESCRIPTION
        自动拉起本地 shim（把网页版伪装成 Anthropic API），然后启动 Claude Code。
        参数原样透传，所以 -r / -c / -p 这些都能用。

        和普通 claude 的区别：
          普通 claude  →  你的付费中转（快、稳、全能）
          dsc          →  DeepSeek 网页版（免费、慢、受限）

        两者的对话是【同一个】—— session_id 一样，所以可以随时换着用：
            claude      → 干到一半嫌贵
            dsc -r      → 接着干，不花钱

        深度思考用 dscthink on / off 中途切，不用重启。

    .EXAMPLE
        dsc                        # 开新会话，用免费网页版
        dsc -r                     # 恢复上次的对话，用免费网页版
        dsc -p "读一下 README"      # 一次性任务
        claude                     # 回到付费中转
    #>
    # 刻意不用 [CmdletBinding()]/param()：那会把 -p、-r 这些当成 PowerShell
    # 自己的参数去解析（-p 会和 -ProgressAction 歧义而报错）。
    # 用自动变量 $args 原样透传给 claude。
    $py   = "C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe"
    $shim = "D:\创业\deepseek_ask\claude_shim.py"
    $conf = "D:\创业\deepseek_ask\dsclaude-settings.json"
    $port = 8799

    # shim 没起就拉起来
    $alive = $false
    try {
        Invoke-WebRequest "http://127.0.0.1:$port/" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop | Out-Null
        $alive = $true
    } catch { }
    if (-not $alive) {
        Write-Host '[dsc] 启动 shim…' -ForegroundColor DarkGray
        Start-Process -FilePath $py `
            -ArgumentList '-u', $shim, '--port', $port `
            -RedirectStandardOutput "$env:TEMP\deepseek_shim.out" `
            -RedirectStandardError  "$env:TEMP\deepseek_shim.err" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 4
    }

    Write-Host '[dsc] 后端 = DeepSeek 网页版（免费，每轮约 10-60 秒）' -ForegroundColor DarkCyan
    # --strict-mcp-config：不加载任何 MCP。
    # 你的 10 个 MCP 会贡献 60+ 个工具定义，每轮都要重发，白白吃掉上下文。
    & claude --settings $conf --strict-mcp-config @args
}

Set-Alias -Name dsclaude -Value dsc -Force   # 旧名字留着，不影响


function dssessions {
    <#
    .SYNOPSIS
        看 Claude Code 会话 ↔ DeepSeek 网页对话 的对应关系。

    .DESCRIPTION
        排查「接着问，浏览器却开了另一个对话」这类问题的第一站。
        每行是一个 Claude Code 会话，显示它绑到了哪个网页对话、已经发了多少条、
        聊了几轮。

        最近用过的排在最前面。

    .EXAMPLE
        dssessions
    #>
    $f = Join-Path $env:USERPROFILE '.deepseek_shim_sessions.json'
    if (-not (Test-Path $f)) {
        Write-Host '还没有任何会话记录。' -ForegroundColor Yellow
        return
    }
    $j = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json

    $rows = $j.PSObject.Properties | ForEach-Object {
        $v = $_.Value
        [PSCustomObject]@{
            会话     = $_.Name.Substring(0, 8)
            轮次     = $v.turns
            已发消息 = $v.sent
            最后使用 = [DateTimeOffset]::FromUnixTimeSeconds([long]$v.updated).LocalDateTime.ToString('MM-dd HH:mm')
            网页对话 = ($v.web_url -replace '^.*/a/chat/s/', '' )
        }
    } | Sort-Object 最后使用 -Descending

    $rows | Format-Table -AutoSize

    Write-Host '提示：网页对话那一列是 DeepSeek 侧的会话 id。' -ForegroundColor DarkGray
    Write-Host '      在浏览器地址栏里能看到同一个 id —— 对不上就是映射出问题了。' -ForegroundColor DarkGray
}


function _dstoggle {
    # dscthink / dscsearch 共用的实体。
    # 两个开关除了端点、返回字段名、文案以外一模一样 —— 抄两份的话，
    # 下次再加开关又要抄第三份（这个项目在别处栽过「抄三份改两份」的跟头）。
    param([string]$Path, [string]$Field, [string]$Name,
          [string]$Mode, [string]$Hint)

    $q = if ($Mode) { "?set=$Mode" } else { "" }
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:8799/$Path$q" -TimeoutSec 5
        $on = [bool]$r.$Field
        $verb = if ($Mode) { '已切到' } else { '当前' }
        Write-Host "[$Name] $verb ：$(if ($on) { '开' } else { '关' })" -ForegroundColor Cyan
        if ($on -and $Hint) {
            Write-Host "           $Hint" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "[$Name] shim 没在跑 —— 先开一个 dsc。" -ForegroundColor Yellow
    }
}

function dscthink {
    <#
    .SYNOPSIS
        中途切 DeepSeek 网页版的「深度思考」，不用重启 dsc。

    .DESCRIPTION
        开启后网页版会先推理再回答，复杂问题质量明显更高，
        但**每轮要等几分钟**。适合想清楚一个难题，不适合日常连问。

    .EXAMPLE
        dscthink           # 看当前状态
        dscthink on        # 开
        dscthink off       # 关
        dscthink toggle    # 翻转
    #>
    param([string]$Mode = "")
    _dstoggle -Path 'think' -Field 'thinking' -Name 'dscthink' `
              -Mode $Mode -Hint '注意：每轮要等几分钟'
}

function dscsearch {
    <#
    .SYNOPSIS
        中途切 DeepSeek 网页版的「智能搜索」（联网），不用重启 dsc。

    .DESCRIPTION
        开启后网页版会先联网搜一轮再回答，能拿到最新信息。
        但**每轮首字要等 30 秒以上**，长任务会明显变慢。

        平时不用管它 —— 模型自己判断需要联网时，会在回复里写 [[SEARCH]]，
        shim 看到就自动打开并重发一次。这个命令是给你手动兜底 / 强制用的。

    .EXAMPLE
        dscsearch           # 看当前状态
        dscsearch on        # 强制一直开着
        dscsearch off       # 关（默认）
        dscsearch toggle    # 翻转
    #>
    param([string]$Mode = "")
    _dstoggle -Path 'search' -Field 'search' -Name 'dscsearch' `
              -Mode $Mode -Hint '注意：每轮首字要等 30 秒以上'
}
