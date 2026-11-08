# -*- coding: utf-8 -*-
"""端到端验证启动器的「启动服务」路径（VBS 本体，不是 Python 镜像）。

做三处改动，其余与桌面版逐字一致：
    PORT   8787 -> 8798      （避免和正在运行的真服务抢端口）
    logfile         -> 临时目录（避免污染真日志 / 触发轮转）
    最后一行        -> WScript.Echo（不要弹浏览器）
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC = r"C:\Users\34913\ownmem\scripts\ownmem-audit-console.vbs"
TMP = os.environ["TEMP"]
DST = os.path.join(TMP, "ownmem_launcher_startcheck.vbs")
TLOG = os.path.join(TMP, "ownmem_startcheck.log")

with open(SRC, encoding="utf-8") as f:
    text = f.read()

text = text.replace("Const PORT = 8787", "Const PORT = 8798")
text = text.replace('logfile = home & "\\webui.log"', 'logfile = "' + TLOG.replace("\\", "\\\\") + '"')
text = text.replace('logbak  = home & "\\webui.log.1"', 'logbak  = "' + TLOG + '.1"')
text = text.replace("ws.Run url, 1, False",
                    'WScript.Echo "START-PATH-" & IIf(ok, "OK", "FAIL") & ": " & url')

# VBScript 没有 IIf 函数，换成朴素写法
text = text.replace(
    'WScript.Echo "START-PATH-" & IIf(ok, "OK", "FAIL") & ": " & url',
    'If ok Then\n  WScript.Echo "START-PATH-OK: " & url\nElse\n  WScript.Echo "START-PATH-FAIL"\nEnd If',
)

if os.path.exists(TLOG):
    os.remove(TLOG)

with open(DST, "w", encoding="gbk") as f:
    f.write(text)
print("副本:", DST)

r = subprocess.run(["cscript", "//nologo", DST], capture_output=True, text=True,
                   encoding="gbk", errors="replace", timeout=90)
print("returncode:", r.returncode)
print("stdout:", (r.stdout or "(空)").strip()[:500])
print("stderr:", (r.stderr or "(空)").strip()[:500])
print("测试日志存在:", os.path.exists(TLOG))
if os.path.exists(TLOG):
    with open(TLOG, encoding="utf-8", errors="replace") as f:
        print("测试日志内容:", f.read().strip()[:200])
