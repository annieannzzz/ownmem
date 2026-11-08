# 模型网关（B 档适配器）

> 适用场景：你的 agent **不提供 hook**，但**能改 base_url**。
> 这是覆盖面最广的一档——国内主流的 Qoder / Trae / Codex / ZCode / CodeBuddy / Cline 都吃这一套。

## 一、为什么需要它

OwnMem 有两个投放记忆的通道：

| 通道 | 原理 | 前提 |
|---|---|---|
| MCP 工具 | 模型自己决定调用 `recall` | 模型得「觉得需要」——而它往往不觉得 |
| hook | agent 在用户发消息时执行命令 | agent 必须提供 hook |

现实是：**大多数 agent 不给 hook**，而 MCP 工具又指望模型的自觉——
于是记忆库建好了，却没人查。这是所有记忆产品的头号死穴。

但有个被忽略的事实：这些 agent **都要配 `base_url`**（国内用户更是如此，模型 API 全指向国产服务）。
把 `base_url` 指向 OwnMem 网关，就在「agent 与模型之间」拿到了一席之地——
**不需要 agent 支持任何东西**。

```
harness ──▶ 127.0.0.1:8790（OwnMem 网关）──▶ 真实模型 API
                  │
                  ├─ 取出这一轮的 user 输入
                  ├─ 查 OwnMem（走白纸/隐身闸门）
                  ├─ 注入 system / instructions
                  ├─ 记审计流水
                  └─ 转发 + 流式回传
```

## 二、五分钟接入

```bash
# 1. 起网关
ownmem gateway --port 8790 --upstream https://token.sensenova.cn/v1 --agent my-agent

# 也可以用环境变量代替参数
set OWNMEM_UPSTREAM=https://token.sensenova.cn/v1
set OWNMEM_AGENT=my-agent
ownmem gateway --port 8790
```

```
2. 改 harness 的 base_url：
   https://token.sensenova.cn/v1   →   http://127.0.0.1:8790/v1
   （apiKey、模型名、其它参数一律不动）
```

3. 正常用 agent。打开审计台 http://127.0.0.1:8787/ 就能看到 `网关注入` 流水。

## 三、各 harness 改哪里

| harness | 配置位置 | 改哪个字段 |
|---|---|---|
| **OpenCode** | `~/.config/opencode/opencode.json` → `provider.<名字>.options` | `baseURL` |
| **Codex CLI** | `~/.codex/config.toml` → `[model_providers.<名字>]` | `base_url`（注意它是 `wire_api = "responses"` 线格式，网关已支持） |
| **Cursor** | Settings → Models → OpenAI API Key 区 | `Override OpenAI Base URL` |
| **Cline / Roo Code** | 扩展设置里的模型配置 | `Base URL` |
| **Qoder / Trae / CodeBuddy 等 IDE 系** | 设置 → 模型 / 自定义模型 | 自定义端点（Base URL）填本地网关 |
| **其它 OpenAI 兼容客户端** | 任何叫 base_url / endpoint / api_base 的字段 | 指向网关 |

> 通用原则只有一句：**找那个写着模型服务地址的输入框，把域名换成 `http://127.0.0.1:8790`，路径保持 `/v1`。**

## 四、模式如何生效

网关走的是和 hook 完全相同的咽喉层，所以四种模式在这里一样成立：

| 模式 | 网关注入行为 |
|---|---|
| normal | 正常注入 |
| incognito（隐身） | 注入照常（可读），写入侧被 MCP 闸门拦截 |
| blank（白纸） | **不注入**，审计里记 `recall_blocked` |
| ghost（双断） | **不注入** |

切换方式（按会话或全局）：

```bash
ownmem mode blank                              # 改默认模式（影响所有未单独设模式的会话）
ownmem mode blank --session <会话ID>            # 只改某个会话（ID 在审计台「会话」页能查到）
```

网关的会话 ID 规则（按优先级）：

1. 请求头带 `X-OwnMem-Session` 就用它（脚本/测试指定用）
2. 否则按**对话指纹**自动分账：`proxy:<agent>:<10位哈希>`，哈希取自
   模型名 + 首条系统提示 + 首条用户消息——同一段对话逐轮稳定归入同一会话，
   不同对话互不混淆，且哈希不泄露内容
3. 取不到稳定前缀时退化为 `proxy:<agent>`（常驻会话）

会话 ID 在审计台「会话」页或 `ownmem sessions` 里能看到。

> 想给多个 harness 分别记账？起多个实例：`--port 8790 --agent opencode`、
> `--port 8791 --agent trae`，各自指向同一个记忆库，审计里区分得清清楚楚。

## 五、安全说明

- 只监听 `127.0.0.1`，局域网/外网访问不到；校验 Host 头，DNS 重绑定被拒
- `Authorization` 等头部**只做转发，不落盘、不写日志**
- 记忆库仍是本机 SQLite（`~/.ownmem`），没有任何数据外发
- **记忆侧出任何故障都放行原始请求**（fail-open）——网关坏了最多没记忆，
  不会卡死你正在跑的任务；但请求会失败，所以**别在关键任务中途改配置**
- 要摘掉网关，把 `base_url` 改回原值即可；网关活没活着可以探测：
  `curl http://127.0.0.1:8790/ownmem/health`

## 六、排错

| 现象 | 原因 / 处理 |
|---|---|
| 请求 502 / 连接被拒 | 网关没起，或被防火墙拦；先 `ownmem gateway --help` 确认参数，再看终端输出 |
| 想确认网关活着 | `curl http://127.0.0.1:8790/ownmem/health`（本地端点，不转发） |
| 记忆没注入 | ① 查询词与记忆无交集（BM25 正常不命中）② 会话是 blank/ghost 模式 ③ 记忆库为空 |
| 流式响应卡住 | 客户端要求 HTTP/2 或 TLS；本网关只提供 HTTP/1.1 明文，请确认 base_url 是 `http://` |
| 报「上游地址缺失」 | 没给 `--upstream` 也没设 `OWNMEM_UPSTREAM` |
| 想确认到底注入了什么 | 审计台 → 审计流水 → 筛 `网关注入`，能看到命中条数与注入方式 |

## 七、边界（不吹牛）

- 只对**本机能改 base_url** 的客户端有效；锁定官方端点的 SaaS 客户端管不了
- 只做**请求前注入**，不改写模型输出——**写回路不归它管**：只开网关的 agent
  记忆库不会增长，写入仍靠 MCP 工具（过闸门、进待定区）或 CLI。
  推荐「网关 + MCP 双开」：网关管读的强制性，MCP 管写的闸门
- 网关不解析 harness 的私有协议，只认标准 OpenAI 两种线格式；
  遇到自定义格式需要加适配
- 注入优先追加到 harness 自己的 system/developer 提示**之后**（记忆权威性
  低于 agent 自身指令）；payload 里两者都没有时才新插 system——个别只认
  developer 的模型（o1 系）若 harness 连 developer 都不发，可能要单独适配
- 「不改 harness 任何配置」做不到——`base_url` 总要改一次，这是它换取通用性的代价

## 八、自测

```bash
python scripts/test_gateway_proxy.py
```

会起一个假上游，验证：normal 注入 ✓ / blank 不注入 ✓ / ghost 不注入 ✓ /
Responses 格式 ✓ / 审计流水 ✓。全程本地，不打真实 API、不花钱。
