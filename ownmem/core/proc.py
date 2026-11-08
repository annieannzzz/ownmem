# -*- coding: utf-8 -*-
"""进程取证（纯 ctypes，零依赖）：谁拉起了这个进程、它会不会随会话一起消失。

为什么 OwnMem 需要它
-------------------
审计台是常驻服务，但「谁拉起它」决定了它的寿命：
  · 由 agent 会话拉起（后台任务 / 普通子进程）→ 进程在会话的 Job 里，会话结束即被回收；
  · 由桌面 shell 拉起（explorer.exe 走 ShellExecute，或直接双击 .vbs）→ 不在会话 Job 里，长期存活。
因此启动时把父进程链记进日志，事后（中间层进程早已退出、进程表上已看不出关系时）仍能判断
这次启动到底能不能活过会话。
"""

from __future__ import annotations

import ctypes
import datetime
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 由桌面 shell 托管的父进程名：链里出现它们，说明进程不是 agent 会话拉起的
SHELL_HOSTS = ("explorer.exe", "wscript.exe", "cscript.exe")


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _snapshot() -> dict[int, tuple[int, str]]:
    """一次性抓全进程表：{pid: (ppid, exe名)}。"""
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    out: dict[int, tuple[int, str]] = {}
    ok = k.Process32First(snap, ctypes.byref(entry))
    while ok:
        out[entry.th32ProcessID] = (
            entry.th32ParentProcessID,
            entry.szExeFile.decode("mbcs", "replace"),
        )
        ok = k.Process32Next(snap, ctypes.byref(entry))
    k.CloseHandle(snap)
    return out


def chain(pid: int, depth: int = 6, procs: dict | None = None) -> list[str]:
    """祖先链，形如 ["python.exe(24644)", "cmd.exe(1234)", "explorer.exe(5678)"]。

    链在「父进程已退出、进程表里查不到」处自然截断——这正是常驻服务启动一段时间后的常态，
    所以判断能否活过会话要靠启动瞬间记录下来的链，而不是事后追溯。
    """
    procs = _snapshot() if procs is None else procs
    out: list[str] = []
    cur = pid
    for _ in range(depth):
        if cur not in procs:
            break
        ppid, name = procs[cur]
        out.append(f"{name}({cur})")
        cur = ppid
    return out


def host(chain_list: list[str]) -> str:
    """从父链里认出托管者：shell | agent | unknown。"""
    lowered = [n.lower() for n in chain_list]
    if any(n.startswith(SHELL_HOSTS) for n in lowered):
        return "shell"
    if any(n.startswith(("sandbox-cli.exe", "workbuddy.exe")) for n in lowered):
        return "agent"
    return "unknown"


def birth(pid: int) -> str:
    """进程创建时间（本地时间，东八区），查不到返回空串。"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    created, _exit, _k, _u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
    ok = k.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(_exit),
                           ctypes.byref(_k), ctypes.byref(_u))
    k.CloseHandle(h)
    if not ok:
        return ""
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    epoch = 116444736000000000  # 1601-01-01 → 1970-01-01 的 100ns 数
    t = datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=(ticks - epoch) / 10)
    return (t + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def in_job(pid: int) -> bool | None:
    """该进程是否属于某个 Job 对象（None = 查不到）。

    hJob 传 NULL 时回答的是「在任意 Job 里吗」，只能与自身对照，不能断言「在同一个 Job 里」；
    Windows 会给 explorer 等 shell 进程也挂 Job，所以这个值单独看不能判寿命。
    """
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    res = wintypes.BOOL()
    ok = k.IsProcessInJob(h, None, ctypes.byref(res))
    k.CloseHandle(h)
    if not ok:
        return None
    return bool(res.value)


def self_chain() -> str:
    """本进程的父链，字符串形式（写日志用）。"""
    import os
    return " <- ".join(chain(os.getpid()))
