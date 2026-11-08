# -*- coding: utf-8 -*-
"""清理本轮排障在 TEMP 留下的探针脚本与卡住的脚本宿主进程。

背景：把 ownmem-ui.vbs（UTF-8 源码）复制到 %TEMP% 用 explorer 拉起时，
wscript 按 ANSI(GBK) 解码，中文注释把字符串字面量吞了 →「未结束的字符串常量」
编译错误弹窗（用户看到了）。这个弹窗没有「超时自动关」，会一直挂着等点击。
本脚本：结束残留的脚本宿主、删掉探针 vbs，然后校验 TEMP 里不再有残留。

用法：python scripts/cleanup-probe-temp.py
"""
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ownmem.core import proc  # noqa: E402


def main() -> int:
    tmp = os.environ.get("TEMP", "")
    if not tmp:
        print("TEMP 未设置，退出")
        return 1

    # 1) 结束还在挂着的脚本宿主（弹窗进程），避免继续占着用户屏幕
    snap = proc._snapshot()
    for pid, (_ppid, name) in snap.items():
        if name.lower() == "wscript.exe":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, text=True)
            print(f"结束脚本宿主 PID {pid} → rc={r.returncode}")

    # 2) 删掉本轮生成的探针 vbs
    probes = sorted(glob.glob(os.path.join(tmp, "probe*.vbs")))
    for f in probes:
        try:
            os.remove(f)
            print("已删除", f)
        except OSError as e:
            print("删除失败", f, e)

    # 3) 复核
    left = sorted(glob.glob(os.path.join(tmp, "probe*.vbs")))
    print("TEMP 残留探针:", left if left else "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
