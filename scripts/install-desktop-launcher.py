# -*- coding: utf-8 -*-
"""把启动器源码装到桌面（并做 UTF-8 -> GBK 编码转换）。

为什么要这一步：
    Windows 脚本宿主(wscript) 读取 .vbs 时按「系统 ANSI 代码页」解码。
    中文 Windows 是 GBK(936)，而项目里的源码是 UTF-8。
    若直接把 UTF-8 文件丢到桌面，脚本里的中文（弹窗文字）会变乱码。
    所以：源码存 UTF-8（便于编辑/版本管理），部署时转成 GBK。

用法：
    python scripts/install-desktop-launcher.py            # 装到桌面
    python scripts/install-desktop-launcher.py --check    # 只校验，不写入
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ownmem-audit-console.vbs")
DESKTOP_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
DST = os.path.join(DESKTOP_DIR, "OwnMem 审计台.vbs")
# 项目内备一份（桌面文件被误删时可从这里恢复）
LOCAL_COPY = os.path.join(os.path.dirname(HERE), "ownmem-ui.vbs")


def main() -> int:
    check_only = "--check" in sys.argv

    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    gbk_data = text.encode("gbk", errors="replace")  # 中文 Windows 的 ANSI 就是 GBK
    utf8_bytes = text.encode("utf-8")
    print(f"源码   : {SRC}  ({len(text)} 字符)")
    print(f"编码   : utf-8 -> gbk  ({len(gbk_data)} 字节，桌面)；项目备份存 utf-8")

    if check_only:
        print("(--check 模式，未写入任何文件)")
        return 0

    for path, data, enc in ((DST, gbk_data, "gbk"), (LOCAL_COPY, utf8_bytes, "utf-8")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        # 回读校验：按目标编码解回来，说明中文没坏
        with open(path, encoding=enc) as f:
            back = f.read()
        flag = "OK" if back == text else "内容不一致!"
        print(f"已写入 : {path}  ({enc})  [校验 {flag}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
