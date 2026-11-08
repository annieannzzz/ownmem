# Claude Code 接入指南

OwnMem 与 Claude Code 的集成分两步：**MCP 工具**（让 agent 能读写记忆）和 **UserPromptSubmit hook**（让记忆自动进场）。第二步才是 OwnMem 的核心价值。

---

## 一、MCP 工具配置

编辑 `~/.claude.json`（全局）或项目根目录 `.mcp.json`：

```json
{
  "mcpServers": {
    "ownmem": {
      "command": "ownmem",
      "args": ["mcp"],
      "env": {
        "OWNMEM_AGENT": "claude-code"
      }
    }
  }
}
```

Windows 上如果 `ownmem` 不在 PATH，用模块方式：

```json
{
  "mcpServers": {
    "ownmem": {
      "type": "stdio",
      "command": "C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["-m", "ownmem.mcp_server"],
      "env": {
        "PYTHONPATH": "C:\\Users\\<你>\\ownmem",
        "PYTHONUTF8": "1",
        "OWNMEM_AGENT": "claude-code"
      }
    }
  }
}
```

配置后重启 Claude Code，用 `/mcp` 应该能看到 `ownmem` 以及 14 个工具。

---

## 二、Hook 自动召回配置

编辑 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ownmem recall --hook claude",
            "timeout": 10
          }
        ]
      }
    ]
  },
  "SessionEnd": [
    {
      "hooks": [
        { "type": "command", "command": "ownmem session-end" }
      ]
    }
  ]
}
```

### 它是怎么工作的

```
你按下回车
   ↓
Claude Code 把 {"session_id","prompt",...} 通过 stdin 交给 hook
   ↓
ownmem recall --hook claude
   ├─ 读会话模式（normal / incognito / blank / ghost）
   ├─ blank 或 ghost → 不注入，记一条 recall_blocked 审计
   ├─ BM25 检索相关记忆 → 按条数与字符预算裁剪
   └─ 输出 {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}
   ↓
Claude Code 把 additionalContext 注入这一轮上下文
   ↓
模型在「已经看到记忆」的状态下回答——它没有选择权，也没必要选择
```

这解决了 MCP 记忆工具的通病：**模型不主动查记忆**。工具调用权在模型手上，而模型通常不觉得自己需要翻记忆；hook 则把召回变成基础设施的强制动作。

### 手动验证 hook 是否工作

```bash
echo '{"session_id":"s_test","prompt":"我上次说的那个方案是什么","hook_event_name":"UserPromptSubmit"}' | ownmem recall --hook claude
```

正常应输出一行 JSON，`additionalContext` 里是你的记忆（没有相关记忆时该字段不出现）。

用 `ownmem audit` 可以看到刚才这次召回的流水。

---

## 三、会话身份与环境变量

| 变量 | 作用 | 默认 |
|------|------|------|
| `OWNMEM_AGENT` | 标记来源 agent，写入审计与记忆的 `agent` 字段 | `mcp` / `cli` |
| `OWNMEM_SESSION` | 指定会话 ID（用于跨进程共享同一个会话模式） | 从 hook payload 的 `session_id` 取 |
| `OWNMEM_HOME` | 数据目录 | `~/.ownmem` |
| `OWNMEM_MAX_MEMORIES` | 记忆容量上限 | 500 |
| `OWNMEM_DUP_THRESHOLD` | 判重阈值（0–1） | 0.6 |
| `OWNMEM_AUDIT_DETAIL` | 审计详情留存：`content`（默认，留摘录）/ `hash`（只留内容指纹） | `content` |

Claude Code 的 hook payload 自带 `session_id`，OwnMem 会用它作为会话 ID——所以同一会话里切模式、看回执都能对上。hook 每次召回还会刷新一个按 agent 隔离的桥接设置，同一对话里的 MCP 工具读写会自动对齐到同一会话，回执是一张完整的账。

---

## 四、常用操作

```bash
ownmem mode            # 当前模式
ownmem mode blank      # 这轮开始别用旧记忆回答我
ownmem mode incognito  # 这轮说什么都别记
ownmem receipt         # 本次会话的记忆账单
ownmem ui              # 审计台（看 agent 到底记了什么）
```

---

## 五、排错

**hook 没有任何输出**
1. 先跑 `echo '{"prompt":"test"}' | ownmem recall --hook claude`，确认命令本身可用
2. 检查 `ownmem doctor`
3. Claude Code 里用 `/hooks` 看 hook 是否被加载

**MCP 工具连不上**
1. `ownmem mcp` 手动运行，应打印 `ready: ownmem 0.1.0，工具 13 个（stdio）`（在 stderr）
2. 确认 `command` 路径正确（Windows 建议写绝对路径）
3. 若你同时使用其他基于 `mcp 2.0.0` 的 server，本机可能遇到 pydantic ≥2.9 冲突——OwnMem 自身已绕过（见 `ownmem/compat.py`）

**记忆没有注入**
1. `ownmem status` 看是否处于 `blank`/`ghost`（审计台设的默认模式对所有新会话生效）
2. `ownmem search "<关键词>"` 看记忆库是否真有内容
3. 输入分不出可检索词时 OwnMem 主动跳过召回，避免噪音（中文两三个字的有效词没问题）

**别忘了**：harness 自带的记忆（`CLAUDE.md` 等）不经过 OwnMem，其内容不受模式开关控制。
