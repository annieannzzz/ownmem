# -*- coding: utf-8 -*-
"""单独验证 LogTail()：从启动器源码里原样抽出该函数，喂一份带中文的日志。

顺带验证 ADODB.Stream 按 utf-8 读取时中文不乱码。
"""
import os
import re
import subprocess

SRC = r"C:\Users\34913\ownmem\scripts\ownmem-audit-console.vbs"
TMP = os.environ["TEMP"]
TLOG = os.path.join(TMP, "ownmem_logtail_test.log")
DST = os.path.join(TMP, "ownmem_logtail_check.vbs")

with open(SRC, encoding="utf-8") as f:
    text = f.read()

m = re.search(r"Function LogTail\(n\).*?\nEnd Function", text, re.S)
assert m, "没找到 LogTail 函数"
func = m.group(0)
print("抽出函数行数:", func.count("\n") + 1)

sample = (
    "OwnMem 审计台已启动：http://127.0.0.1:8787/\n"
    "（Ctrl+C 停止）\n"
    + "无意义行一\n" * 20 +
    "Traceback (most recent call last):\n"
    '  File "webui.py", line 282\n'
    "ConnectionAbortedError: [WinError 10053] 主机中的软件中止了一个已建立的连接。\n"
)
with open(TLOG, "w", encoding="utf-8") as f:
    f.write(sample)

body = f'''Const LOGN = 999
Dim fso, logfile
Set fso = CreateObject("Scripting.FileSystemObject")
logfile = "{TLOG}"

{func}

WScript.Echo "----- BEGIN -----"
WScript.Echo LogTail(4)
WScript.Echo "----- END -----"
'''
with open(DST, "w", encoding="gbk") as f:
    f.write(body)

r = subprocess.run(["cscript", "//nologo", DST], capture_output=True, text=True,
                   encoding="gbk", errors="replace", timeout=60)
print("returncode:", r.returncode)
print(r.stdout.strip()[:600])
if r.stderr.strip():
    print("stderr:", r.stderr.strip()[:300])
