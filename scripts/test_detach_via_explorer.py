# -*- coding: utf-8 -*-
"""验证「用 explorer.exe 当启动器」能否让子进程脱离当前 agent 会话的 Job 对象。

要解决的坑
----------
agent 会话里用 run_in_background 拉起的服务寄生在会话的 Job 里，会话一结束就被回收；
subprocess 的 CREATE_BREAKAWAY_FROM_JOB 在本机报 WinError 5（Job 未授予 breakaway 权限），
所以「脱离」实际没成功，浏览器会莫名其妙 ERR_CONNECTION_REFUSED。

思路
----
explorer.exe 收到文件参数时会走 ShellExecute，由 shell 自己去创建目标进程——
新进程的父进程是 explorer.exe，天然不在 agent 的 Job 里。
本脚本不做任何推断，直接起一个真进程，读它的真实父进程链来验证。

判定
----
子进程祖先链里出现 explorer.exe / wscript.exe → 脱离成立（PASS）。
子进程祖先链只到本脚本或本会话 shell → 脱离失败（FAIL）。
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whopid  # noqa: E402

PY = r"C:\Users\34913\AppData\Local\Programs\Python\Python312\python.exe"
SLEEP_SEC = 25


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ownmem-detach-")
    if " " in tmp or " " in PY:
        print(f"路径含空格，本测试的引号假设不成立，换目录再跑：{tmp}")
        return 2

    marker = os.path.join(tmp, "pid.txt")
    child = os.path.join(tmp, "child.py")
    cmd = os.path.join(tmp, "run.cmd")
    vbs = os.path.join(tmp, "launch.vbs")

    with open(child, "w", encoding="utf-8") as f:
        f.write("import os, time\n")
        f.write(f"open(r'{marker}', 'w').write(str(os.getpid()))\n")
        f.write(f"time.sleep({SLEEP_SEC})\n")

    # cmd 只做一件事：拉起 python 子进程（本测试要观察的就是这个 python 的父进程）
    with open(cmd, "w", encoding="ascii") as f:
        f.write(f'@echo off\r\n"{PY}" "{child}"\r\n')

    # 外层多加一层引号，绕开 CreateProcess 对「首尾都是引号」的裁剪规则。
    # 末尾的 Sleep 只为把 wscript 留住——真实启动器跑完就退出，中间层会消失，
    # 祖先链就断了、无从取证；这里让中间层活着，才能看到完整的父子关系。
    with open(vbs, "w", encoding="ascii") as f:
        f.write('Set ws = CreateObject("WScript.Shell")\r\n')
        f.write(f'ws.Run """" & "{cmd}" & """", 0, False\r\n')
        f.write("WScript.Sleep 15000\r\n")

    subprocess.run(["explorer.exe", vbs], check=False, timeout=15)

    pid = None
    for _ in range(40):  # 最多等 10 秒
        time.sleep(0.25)
        if os.path.exists(marker):
            txt = open(marker, encoding="utf-8").read().strip()
            if txt.isdigit():
                pid = int(txt)
                break

    if pid is None:
        print("FAIL：10 秒内没等到子进程写回 PID —— explorer.exe 这条路没把进程拉起来")
        print(f"  现场保留在 {tmp}")
        return 1

    ch = whopid.chain(pid)
    print(f"子进程 PID {pid} 创建于 {whopid.birth(pid)}")
    print("  祖先链: " + " <- ".join(ch))

    escaped = any(n.lower().startswith(("explorer.exe", "wscript.exe", "cscript.exe")) for n in ch)
    if escaped:
        print("PASS：父进程链里有 explorer/wscript —— 该进程不在 agent 会话的 Job 里，会话结束不会被回收")
    else:
        print("FAIL：父进程链里没有 explorer/wscript —— 这类进程会随会话结束一起消失")

    subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for p in (marker, child, cmd, vbs):
            os.remove(p)
        os.rmdir(tmp)
    except OSError:
        pass
    return 0 if escaped else 1


if __name__ == "__main__":
    sys.exit(main())
