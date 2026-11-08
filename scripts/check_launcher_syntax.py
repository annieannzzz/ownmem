# -*- coding: utf-8 -*-
"""用 cscript 校验启动器语法（副本，不开浏览器）。

把最后一行 ws.Run url 换成 WScript.Echo，其余完全一致。
语法错会让 cscript 直接报错，从而提前发现。
"""
import os
import subprocess

SRC = r"C:\Users\34913\ownmem\scripts\ownmem-audit-console.vbs"
DST = os.path.join(os.environ["TEMP"], "ownmem_launcher_syntaxcheck.vbs")

with open(SRC, encoding="utf-8") as f:
    text = f.read()

assert "ws.Run url, 1, False" in text
text = text.replace("ws.Run url, 1, False", 'WScript.Echo "PROBE-OK: 服务已就绪"')

with open(DST, "w", encoding="gbk") as f:
    f.write(text)
print("副本:", DST)

r = subprocess.run(["cscript", "//nologo", DST],
                   capture_output=True, text=True, encoding="gbk",
                   errors="replace", timeout=60)
print("returncode:", r.returncode)
print("stdout:", (r.stdout or "(空)").strip()[:400])
print("stderr:", (r.stderr or "(空)").strip()[:400])
