# -*- coding: utf-8 -*-
"""命令行查看某个进程的出身：创建时间、是否在 Job 里、父进程链。

用法：python scripts/whopid.py <pid> [pid...]

判定口径（同 ownmem.core.proc）：
  父链里出现 explorer/wscript/cscript → 由桌面 shell 拉起，能活过 agent 会话；
  出现 sandbox-cli.exe / WorkBuddy.exe → 由 agent 会话拉起，会话结束即被回收；
  父链断在自己 → 中间层进程已退出（常驻服务的常态），得靠启动时记下的链来判断。

实现在包里（ownmem/core/proc.py），这里只做命令行外壳，避免两份逻辑各说各话。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ownmem.core import proc  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    procs = proc._snapshot()
    for arg in sys.argv[1:]:
        pid = int(arg)
        lineage = proc.chain(pid, procs=procs)
        who = proc.host(lineage)
        print(f"PID {pid}｜创建于 {proc.birth(pid)}｜在 Job 中={proc.in_job(pid)}｜托管者={who}")
        print("  父链: " + (" <- ".join(lineage) if lineage else "(空)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
