@echo off
REM ASCII ONLY in this file. cmd.exe reads .cmd in the OEM codepage (GBK on
REM this machine), so UTF-8 Chinese comments get mangled and eat the commands
REM that follow them.
REM
REM Pin the interpreter path so PATH's old Python 3.8.6 is never used. The bare
REM "py" launcher is broken here too: its 3.13 registry entry points at a
REM nonexistent C:\python.exe.
chcp 65001 > nul
"C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 "%~dp0deepseek_ask.py" %*
