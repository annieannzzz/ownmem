# -*- coding: utf-8 -*-
"""验证启动器里 BuildCmd 拼出来的命令行在 cmd /c 下真的能跑。

为什么需要这个测试：
    ws.Run(cmd, ...) 在 Windows 上走的是 CreateProcess(lpCommandLine=cmd)。
    Python 的 subprocess.Popen(cmd_string) 走的是同一条路。
    所以这里能用 Python 精确复现 VBS 的行为，验证 cmd /c 的引号规则有没有被绕开。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

HOME = r"C:\Users\34913\ownmem"
PY = r"C:\Users\34913\AppData\Local\Programs\Python\Python312\python.exe"
PORT = "8799"
LOG = os.path.join(os.environ["TEMP"], "ownmem_quote_test.log")


def build_cmd(p: str, port: str, logfile: str) -> str:
    """与 VBS 的 BuildCmd 逐字对应。"""
    # 注意：VBS 源码里写的是 """""" / """" ，解码后分别等于 2 个 / 1 个引号。
    # 这里是 Python，直接写真实字符即可。
    c = 'cmd /c ' + '""' + p + '"'
    c = c + ' -m ownmem.cli ui --port ' + port + ' --no-browser > '
    c = c + '"' + logfile + '"'
    c = c + ' 2>&1' + '"'
    return c


def main() -> int:
    cmd = build_cmd(PY, PORT, LOG)
    expected = (
        'cmd /c ""' + PY + '" -m ownmem.cli ui --port ' + PORT
        + ' --no-browser > "' + LOG + '" 2>&1"'
    )
    print("拼出的命令 :", cmd)
    print("期望的命令 :", expected)
    print("字符串一致 :", cmd == expected)
    if cmd != expected:
        print("!! 拼接逻辑有误，终止")
        return 1

    if os.path.exists(LOG):
        os.remove(LOG)

    env = {
        **os.environ,
        "PYTHONPATH": HOME,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }

    # 用字符串形式启动 = 复现 ws.Run 的行为
    p = subprocess.Popen(cmd, env=env, cwd=HOME,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("cmd pid    :", p.pid)

    ok = False
    for _ in range(24):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/overview", timeout=2) as r:
                body = r.read().decode("utf-8")
            if r.status == 200 and '"stats"' in body:
                ok = True
                break
        except Exception:
            pass

    print("端口响应   :", "200 + OwnMem 特征 OK" if ok else "无响应 (失败)")
    print("日志文件   :", "已生成" if os.path.exists(LOG) else "未生成")
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8", errors="replace") as f:
            print("日志内容   :", f.read().strip()[:200])

    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
