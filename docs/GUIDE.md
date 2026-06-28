# EverOS ↔ Hermes 集成指南

> 面向 AI Agent 阅读：本文档指导你完成 EverOS 记忆系统与 Hermes 的集成配置。
> 阅读后，你需要向用户收集必要信息（Provider、API Key 等），然后执行安装和配置。
>
> **本仓库**：https://github.com/moreoronce/EverOS-Hermes

---

## 架构概览

```
Hermes (Agent)                         EverOS Sidecar
┌─────────────┐    HTTP/localhost    ┌──────────────────────┐
│  everos-    │ ──────────────────► │  FastAPI :8765        │
│  local      │   /api/v1/memory/*  │  ├── LLM 抽取          │
│  plugin     │ ◄────────────────── │  ├── Embedding 向量化  │
│             │   episodes/facts    │  ├── Cascade 索引同步   │
└─────────────┘                     │  └── OME 离线记忆引擎   │
                                    │                        │
┌─────────────┐                     │  Markdown (真相源)      │
│  Codex MCP  │ ──────────────────► │  SQLite (队列/审计)     │
│  server.mjs │   同一 :8765        │  LanceDB (向量索引)     │
│  app_id:    │                     │                        │
│  "codex"    │                     └──────────────────────┘
└─────────────┘
```

一个 EverOS 实例可同时服务多个 Agent，通过 `app_id` 分区隔离。Hermes 用 `app_id="hermes"`，Codex 用 `app_id="codex"`，数据互不干扰。

---

## 1. 系统要求

### 1.1 操作系统

| OS | 支持 | 说明 |
|----|------|------|
| **Linux**（Debian/Ubuntu 等） | ✅ 原生支持 | inotify 文件监听，`ulimit -n` 默认 1024 足够 |
| **macOS** | ✅ 原生支持 | FSEvents 文件监听；索引缓存 >16MB 时需 `ulimit -n 1024`（默认 256 偏低） |
| **WSL2**（Windows Subsystem for Linux） | ✅ 支持 | runtime 跑在 WSL 内，完全兼容 |
| **Windows 原生** | ❌ **不支持** | EverOS 依赖 `fcntl.flock`（POSIX advisory locking），Windows 无此系统调用 |

> **Windows 用户：** EverOS 的真实 runtime 必须放在 WSL2 里运行。Hermes（Windows 原生）通过网络回环 `127.0.0.1` 连接 WSL 内的 EverOS sidecar。

### 1.2 运行环境

| 项目 | 要求 |
|------|------|
| Python | **3.12+**（通过 `uv` 管理的 venv 自动安装，不依赖系统 Python 版本） |
| uv | 推荐（`uv sync` 安装依赖） |
| 磁盘 | ≥500MB（源码 + venv + 索引） |
| 网络 | 需访问 LLM / Embedding Provider 的 API 端点 |

### 1.3 WSL2 特殊注意事项

- 文件监听（inotify）**在 WSL 文件系统内正常工作**（`~/...`）
- 但 Windows 侧文件（`/mnt/c/...`）的变更**不会触发 inotify 事件**——只影响直接编辑 Windows 侧 markdown 的场景（不常见）
- 解决：把 memory root 放在 WSL 文件系统内（如 `~/.everos-codex`），不要放 `/mnt/c/`

---

## 2. Agent 交互流程

> **本文档面向 AI Agent。** 你（Agent）需要先验证运行环境，然后向用户收集 Provider 信息，最后执行配置。

### 2.1 确认系统版本及系统支持情况

**在开始配置前，先运行以下检查，确认环境满足要求：**

```bash
# 1. 操作系统 — 确认是否 Linux/macOS/WSL（Windows 原生不支持）
cat /etc/os-release 2>/dev/null | head -2

# 2. WSL 检测 — 内核签名含 "microsoft-standard" 即为 WSL2
grep -i microsoft /proc/version 2>/dev/null && echo "→ WSL2 检测到" || echo "→ 非 WSL"

# 3. Python 版本 — uv venv 会自带 3.12，系统 Python 只需能跑 uv
python3 --version

# 4. uv 是否可用 — 推荐的依赖管理器
uv --version 2>/dev/null || echo "→ uv 未安装（后续步骤会安装）"

# 5. systemd 是否可用 — 用于开机自启服务
ps -p 1 -o comm= 2>/dev/null

# 6. 端口 8765 是否已被占用 — EverOS 默认端口
curl -s -m2 http://127.0.0.1:8765/health 2>/dev/null && echo "→ 端口已被占用（可能已安装）" || echo "→ 端口空闲"
```

**判断逻辑：**

| 检查项 | 通过条件 | 不通过的处理 |
|--------|---------|-------------|
| OS | Linux / macOS / WSL2 | Windows 原生 → 必须在 WSL2 内安装，无替代方案 |
| Python | 系统 Python ≥3.10 即可（uv venv 自带 3.12） | 无 Python → `sudo apt install python3`（Linux）或 `brew install python`（macOS） |
| uv | 已安装 | 未安装 → `curl -LsSf https://astral.sh/uv/install.sh \| sh`，然后 `source ~/.bashrc` |
| systemd | PID 1 = `systemd` | 无 systemd → 用 §3.5 替代方案（前台运行） |
| 端口 8765 | 空闲 | 被占用 → 检查是否已安装 EverOS；如需改端口，编辑 `everos.toml` 的 `[api] port` |

> **Windows 原生不支持的原因：** EverOS 依赖 `fcntl.flock`（POSIX advisory locking），这是 Unix 系统调用，Windows 没有。源码 `src/everos/core/persistence/locking.py` 明确声明了这一限制。WSL2 提供完整的 Linux 内核，POSIX 调用全部可用。

### 2.2 你需要向用户收集的信息

环境验证通过后，向用户确认以下内容（可以用一次性提问，也可以分步询问）：

**必需信息：**

1. **LLM Provider（记忆抽取用）**
   - 你使用哪个 LLM 服务？（SiliconFlow / OpenAI / DeepSeek 官方 / 其他 OpenAI 兼容端点）
   - API Key 是什么？
   - 该 Provider 的 base_url？（如 SiliconFlow 是 `https://api.siliconflow.cn/v1`）
   - 想用哪个模型？

2. **Embedding Provider（向量化用）**
   - 可以和 LLM 用同一个 Provider 吗？
   - 如果不同，API Key 和 base_url 是什么？
   - 想用哪个 Embedding 模型？

3. **用户标识**
   - 你的 user_id 是什么？（跨 Agent 共享的唯一标识，如 Telegram user ID）

**可选信息：**

4. **是否需要多模态解析？**（PDF/图片内容提取，需要支持视觉的模型）
5. **是否需要 Rerank？**（知识库搜索增强，个人使用通常不需要）

### 2.3 模型选择指导

当用户不确定选什么模型时，按以下原则推荐：

**LLM（记忆抽取）：**

| 原则 | 说明 |
|------|------|
| 必须支持 `chat.completions.create()` | TTS/Embedding/Whisper 模型不行，会导致 500 错误 |
| 选 Instruct 模型，不选推理模型 | 记忆抽取是信息提取任务，推理模型（R1/o3）延迟 14-15s 零增益 |
| 中文要好 | 对话内容是中文，模型需理解中文语义 |
| JSON 输出稳定 | 抽取结果需结构化为 JSON |
| 大小够用即可 | LLMStructBench 实测：14B 级别在结构化提取上和 70B 几乎没差距 |

> **SiliconFlow 推荐模型排序：**
> 1. `deepseek-ai/DeepSeek-V4-Flash` — MoE 架构，JSON 合规强，中文好，性价比极高（已实测验证）
> 2. `Qwen/Qwen2.5-14B-Instruct` — 免费档，中文原生，结构化输出成熟
> 3. `Qwen/Qwen2.5-7B-Instruct` — 免费档，速度最快，简单对话够用

**Embedding（向量化）：**

| 原则 | 说明 |
|------|------|
| 必须支持 `embeddings` 接口 | 标准 OpenAI 兼容 `/v1/embeddings` |
| 中文支持 | 检索内容是中文 |
| 维度一致性 | 同一个 server 必须始终用同一个 embedding 模型 |

> **SiliconFlow 推荐模型：**
> 1. `Qwen/Qwen3-Embedding-4B` — 1024 维，中文强（已实测验证）
> 2. `BAAI/bge-large-zh-v1.5` — 中文 embedding 经典模型

> **⚠️ 踩坑警告：** `[llm] model` 和 `[multimodal] model` 是两个独立配置段。LLM 段配文本模型，multimodal 段配多模态模型。**不要把 TTS 模型（如 `gpt-4o-mini-tts`）配到 `[llm]`——它不支持 chat completions，会导致所有记忆写入返回 500。**

### 2.4 收集到信息后

向用户确认你的理解，然后用 §3 的步骤执行安装和配置。确认格式示例：

```
我理解你要这样配置：
- LLM: deepseek-ai/DeepSeek-V4-Flash @ SiliconFlow
- Embedding: Qwen/Qwen3-Embedding-4B @ SiliconFlow
- user_id: 631288870184681503

确认后我开始配置。
```

---

## 3. 安装和配置

### 3.1 克隆本仓库

```bash
git clone https://github.com/moreoronce/EverOS-Hermes.git ~/src/everos-hermes
```

> **部署模板：** `deploy/` 目录提供了全部配置文件的模板。后续步骤会从模板复制，而不是从零手写。
>
> | 模板文件 | 用途 | 复制到 | 需替换的占位符 |
> |---------|------|--------|---------------|
> | `deploy/everos.toml.example` | EverOS server 配置 | `~/.everos-codex/everos.toml` | `<your-llm-api-key>`、`<your-embedding-api-key>` |
> | `deploy/everos-local.example.json` | Hermes 插件配置 | `~/.hermes/everos-local.json` | `<your-user-id>` |
> | `deploy/everos.service` | systemd user service | `~/.config/systemd/user/everos.service` | `<username>` |
> | `deploy/everos-codex-server` | 启动脚本 | `~/.local/bin/everos-codex-server` | 无（使用 `$HOME` 变量） |
>
> **占位符格式统一为 `<...>` 尖括号**——Agent 在配置阶段需将每一个替换为用户提供的信息，替换完成后确认无 `<` 字符残留。

### 3.2 安装 EverOS

```bash
git clone https://github.com/EverMind-AI/EverOS.git ~/src/EverOS
cd ~/src/EverOS
uv sync
```

验证：

```bash
uv run everos --help
# 期望：显示 CLI 帮助（init / server / cascade / config 子命令）
```

### 3.3 初始化 Memory Root + 配置 `everos.toml`

```bash
cd ~/src/EverOS
uv run everos init --root ~/.everos-codex
```

然后从模板复制并填入用户信息：

```bash
cp ~/src/everos-hermes/deploy/everos.toml.example ~/.everos-codex/everos.toml
```

编辑 `~/.everos-codex/everos.toml`，替换以下占位符为用户提供的信息：

| 占位符 | 替换为 |
|--------|--------|
| `<your-llm-api-key>` | 用户的 LLM API Key |
| `<your-embedding-api-key>` | 用户的 Embedding API Key |
| `deepseek-ai/DeepSeek-V4-Flash` | 用户选择的 LLM 模型（如不同） |
| `Qwen/Qwen3-Embedding-4B` | 用户选择的 Embedding 模型（如不同） |
| `https://api.siliconflow.cn/v1` | 用户的 Provider base_url（如不同） |

> **⚠️ 安全提示：** 读取或展示 `everos.toml` 内容时，必须对 `api_key` 字段脱敏（显示 `***`），禁止在对话中明文输出用户的密钥。

### 3.4 安装启动脚本

```bash
cp ~/src/everos-hermes/deploy/everos-codex-server ~/.local/bin/everos-codex-server
chmod +x ~/.local/bin/everos-codex-server
```

### 3.5 配置服务自启

#### 方式 A — systemd（推荐，适用于 PID 1 = systemd 的系统）

```bash
mkdir -p ~/.config/systemd/user
cp ~/src/everos-hermes/deploy/everos.service ~/.config/systemd/user/everos.service

# 替换 <username> 为实际用户名
sed -i "s/<username>/$(whoami)/g" ~/.config/systemd/user/everos.service

systemctl --user daemon-reload
systemctl --user enable --now everos.service
sudo loginctl enable-linger $(whoami)
```

> **`Environment=PATH=...` 是必须的**——systemd 的环境 PATH 很干净，不含 `~/.local/bin`（`uv` 所在目录），没有这行 server 会 exit 127。

#### 方式 B — 前台 / nohup（无 systemd 时）

旧版 WSL 或无 systemd 的系统，用 nohup 后台运行：

```bash
nohup ~/.local/bin/everos-codex-server > ~/.everos-codex/server.log 2>&1 &
```

或用 tmux 手动管理：

```bash
tmux new -s everos
~/.local/bin/everos-codex-server
# Ctrl+B D 脱离
# tmux attach -t everos 重新连接
```

> **注意：** nohup/tmux 方式不会自动重启崩溃的服务，WSL 重启后需手动重新运行。

### 3.6 验证 Server

```bash
# systemd 方式
systemctl --user is-active everos
# 期望: active

# 健康检查（两种方式通用）
curl -s http://127.0.0.1:8765/health
# 期望: {"status":"ok"}
```

---

## 4. 配置 Hermes 插件

### 4.1 `~/.hermes/config.yaml`

> **⚠️ 不要覆盖整个文件。** 将以下键**合并**到用户已有的 `config.yaml` 中——保留用户已有的其他插件、provider、model 等配置。

```yaml
# 合并到 memory: 段
memory:
  provider: everos-local
  memory_enabled: true
  flush_min_turns: 6
  memory_char_limit: 2800
  user_profile_enabled: true
  write_approval: false
  nudge_interval: 10

# 在 plugins: enabled: 列表中添加
plugins:
  enabled:
    - everos-local
    # ... 保留用户已有的其他插件
```

### 4.2 `~/.hermes/everos-local.json`

从模板复制并填入用户 user_id：

```bash
cp ~/src/everos-hermes/deploy/everos-local.example.json ~/.hermes/everos-local.json
# 编辑替换 <your-user-id> 为用户的实际 user_id
```

**字段说明：**

| 字段 | 作用 | 重要说明 |
|------|------|---------|
| `host` | EverOS server 地址 | 必须 server 已启动 |
| `user_id` | 用户唯一标识 | 跨 Agent 共享（Codex 和 Hermes 用同一个） |
| `agent_id` | Agent 标识 | 区分哪个 Agent 写的 |
| `app_id` | **数据分区键** | search 时必须匹配此值，否则搜不到 |
| `project_id` | 项目分区 | 默认 `default` |
| `prefetch_top_k` | 每轮注入记忆条数 | 5 条够用 |

> **`app_id` 是最关键的坑：** EverOS 按 `<app_id>/<project_id>` 在磁盘上硬分区。写入用 `app_id="hermes"`，搜索时不传 `app_id`（默认 `"default"`），将永远搜不到。Hermes 插件内部会自动传，但直接调 API 时务必注意。

### 4.3 安装插件文件

从本仓库复制插件到 Hermes 插件目录：

```bash
mkdir -p ~/.hermes/plugins/everos-local
cp ~/src/everos-hermes/plugin/__init__.py ~/.hermes/plugins/everos-local/__init__.py
cp ~/src/everos-hermes/plugin/plugin.yaml ~/.hermes/plugins/everos-local/plugin.yaml
cp ~/src/everos-hermes/docs/GUIDE.md ~/.hermes/plugins/everos-local/GUIDE.md
```

插件三个文件：

| 文件 | 说明 |
|------|------|
| `__init__.py` (20KB) | 核心逻辑：`_EverOSClient`（HTTP 客户端）+ `EverOSLocalPlugin`（Hermes 插件类）+ circuit breaker |
| `plugin.yaml` | 元数据 + `pip_dependencies: [httpx]` |
| `GUIDE.md` | 本文档 |

`__init__.py` 实现的 5 个接口：

- `system_prompt_block()` — 注入记忆系统提示
- `prefetch()` — 每轮被动召回历史记忆
- `sync_turn()` — 每轮异步写入对话（daemon 线程）
- `is_available()` — 健康检查
- `handle_tool_call()` — 分发 `everos_health`/`everos_search`/`everos_profile`/`everos_conclude` 四个工具

### 4.4 重启 Hermes

配置完成后重启使插件生效：

```bash
# Hermes Desktop / CLI：直接重启应用
# Gateway（如使用）：重启 gateway 进程
# 验证插件已加载：检查日志或调用 everos_health 工具
```

---

## 5. 记忆管线

插件在 Hermes 框架中接管四个角色，全部自动运行：

### 5.1 system_prompt_block

每轮注入 system prompt，告知 Agent 记忆系统可用。

### 5.2 prefetch（被动召回）

**每轮对话前自动执行。** 用当前用户消息作为 query，检索历史记忆，注入为 `<memory-context>` 块。

- 检索路径：`POST /api/v1/memory/search`（hybrid 方法）
- 注入格式：`[episode] summary... score=0.42`
- 上限：`prefetch_top_k` 条（默认 5）
- 带 circuit breaker（6 次连续失败后熔断 120s）

### 5.3 sync_turn（被动写入）

**每轮对话后自动执行。** 把 user + assistant 消息异步写入 EverOS。

- 写入路径：`POST /api/v1/memory/add` → `POST /api/v1/memory/flush`
- 线程：daemon 线程，不阻塞对话
- 消息格式：`sender_id` + `role` + `timestamp`(ms) + `content`
- flush 触发 LLM 抽取 → 生成 episode + atomic facts + foresights
- 上一轮还在处理时丢弃当前轮（`_sync_thread.is_alive()`）

### 5.4 工具集（主动调用）

插件暴露 4 个工具供 Agent 在对话中主动调用：

| 工具 | 触发时机 | 作用 |
|------|---------|------|
| `everos_health` | 排查问题时 | 检查 sidecar 连通性 |
| `everos_search` | 需要精确检索 | 语义/关键词搜索，支持 `owner=user/agent` |
| `everos_profile` | 需要用户画像 | 返回精简的用户偏好/特征样本 |
| `everos_conclude` | 学到重要事实 | 主动写入一条结构化记忆（触发 LLM 抽取） |

**`everos_conclude` 使用时机（强制）：**
- 查到有用信息后写回
- 学到新事实/配置/架构/踩坑
- 用户做了决定（选方案/确认偏好/改方向）
- 发现并修复问题
- 用户告知偏好或纠正了 Agent

**调用方式：** `everos_conclude(conclusion="结论文字")` → 内部走 `add_messages(flush=True)`。

---

## 6. API 速查

插件实际调用的三个端点。**所有 message 必须包含 `sender_id`、`role`、`timestamp`（Unix 毫秒）、`content`。**

### POST /api/v1/memory/add

```json
{
  "session_id": "hermes-turn-20260628-123456",
  "app_id": "hermes",
  "project_id": "default",
  "messages": [
    {
      "sender_id": "<user_id>",
      "sender_name": "User",
      "role": "user",
      "timestamp": 1751097600000,
      "content": "记住这个配置..."
    }
  ]
}
```

### POST /api/v1/memory/flush

```json
{
  "session_id": "hermes-turn-20260628-123456",
  "app_id": "hermes",
  "project_id": "default"
}
```

返回 `{"data": {"status": "extracted"}}` 表示 LLM 抽取成功。

### POST /api/v1/memory/search

```json
{
  "query": "搜索内容",
  "user_id": "<user_id>",
  "app_id": "hermes",
  "project_id": "default",
  "top_k": 5,
  "method": "hybrid"
}
```

**`user_id` 和 `agent_id` 互斥** —— 只能传一个。`method` 支持 `hybrid`（推荐）/ `vector` / `keyword`。

---

## 7. 记忆存储结构

记忆按 `<app_id>/<project_id>/users/<user_id>/` 分区存储：

```
~/.everos-codex/
├── hermes/                              ← app_id
│   └── default_project/                 ← project_id
│       └── users/
│           └── <user_id>/
│               ├── episodes/            ← 对话叙事（同步写入）
│               │   └── episode-<YYYY-MM-DD>.md
│               ├── .atomic_facts/       ← 原子事实（OME 异步）
│               │   └── atomic_fact-<YYYY-MM-DD>.md
│               ├── .foresights/         ← 预测笔记（OME 异步）
│               │   └── foresight-<YYYY-MM-DD>.md
│               └── user.md              ← 用户画像（OME 重写）
│       └── agents/
│           └── hermes/
│               ├── .cases/              ← Agent 轨迹
│               └── skills/              ← Agent 技能
├── codex/                               ← 第二个 Agent 的分区
│   └── default_project/
│       └── ...
├── .index/                              ← 可重建的索引（删了不丢数据）
│   ├── sqlite/
│   └── lancedb/
└── everos.toml                          ← 改了要重启 server
```

**Markdown 是唯一真相源。** 删掉 `.index/` 不丢任何记忆，重启后从 markdown 重建。

---

## 8. 添加 Codex 作为第二个 Agent（可选）

Codex 通过 MCP server 连接同一个 EverOS 实例，用不同的 `app_id` 分区。

### 8.1 Codex MCP Server

在 Windows 侧创建 `C:\Users\<用户名>\.codex\mcp\everos-shared\server.mjs`：

```javascript
const EVEROS_HOST = "http://127.0.0.1:8765";
const EVEROS_USER_ID = "<user_id>";      // 和 Hermes 用同一个
const EVEROS_AGENT_ID = "codex";         // 不同 agent_id
const EVEROS_APP_ID = "codex";           // 不同 app_id → 数据隔离
const EVEROS_PROJECT_ID = "default";

// MCP server 实现：封装 /health /search /add /flush 四个端点
// 暴露工具：everos_health / everos_search / everos_profile / everos_conclude / everos_add_turn
```

> 完整 `server.mjs` 实现参考本仓库或已有部署。

### 8.2 Codex 配置

在 `C:\Users\<用户名>\.codex\config.toml` 中注册 MCP server：

```toml
[mcp_servers.everos_shared]
command = "node"
args = ["C:\\Users\\<用户名>\\.codex\\mcp\\everos-shared\\server.mjs"]
```

### 8.3 验证

重启 Codex 会话后，调用 `everos_health` 工具确认连通。

---

## 9. 验证检查清单

部署完成后逐项验证：

```bash
# 1. Server 存活
systemctl --user is-active everos 2>/dev/null || echo "→ 非 systemd 方式，检查进程"
# 期望: active

# 2. 健康检查
curl -s http://127.0.0.1:8765/health
# 期望: {"status":"ok"}

# 3. Hermes 配置（只看 memory provider 行）
grep -A1 '^memory:' ~/.hermes/config.yaml | grep provider
# 期望: provider: everos-local

# 4. 插件加载
grep 'everos-local' ~/.hermes/config.yaml
# 期望: - everos-local

# 5. 配置文件存在（只检查 key 存在，不输出值）
test -f ~/.hermes/everos-local.json && echo "✅ everos-local.json 存在" || echo "❌ 缺失"

# 6. 写入测试（在 Hermes 对话中调工具）
# 调用: everos_conclude(conclusion="测试记忆写入")
# 期望: {"result":"Fact submitted to EverOS."}

# 7. 搜索测试（在 Hermes 对话中调工具）
# 调用: everos_search(query="测试记忆", top_k=3)
# 期望: 返回刚写入的 episode

# 8. 查看 markdown 验证记忆落盘
ls ~/.everos-codex/hermes/default_project/users/*/episodes/
# 期望: episode-<date>.md 文件存在
```

---

## 10. 排障指南

### add/flush 返回 500

**最可能原因：`[llm] model` 配了不能做 chat completions 的模型（如 TTS 模型）。**

```bash
journalctl --user -u everos --since "5 min ago" | grep -i "error\|llm\|500"
# 或查看 nohup 日志
tail -100 ~/.everos-codex/server.log | grep -i error
```

修复：换一个支持 `chat.completions.create()` 的 Instruct 模型，改完重启 server（`systemctl --user restart everos`）。

### search 返回空但数据存在

**最可能原因：`app_id` 不匹配。** 写入用 `app_id="hermes"`，搜索时传了 `app_id="default"`。

修复：确保搜索 payload 里 `app_id` 和写入时一致。插件内部已自动处理。

### search 刚 flush 完搜不到

**正常行为。** 写是强一致（flush 返回时 markdown 已落盘），读是最终一致（LanceDB 索引有亚秒到秒级延迟）。等待 2-3 秒后重试。

### sync_turn 不写入

检查 circuit breaker 是否打开（6 次连续失败触发，冷却 120s）。等冷却结束后首次成功调用会自动清零。

```bash
curl -s http://127.0.0.1:8765/health
```

### Server 启动失败 (exit 127)

systemd 环境的 PATH 不含 `~/.local/bin`。修复：确认 service 文件里有 `Environment=PATH=...`（见 §3.5）。

### `loginctl enable-linger` 报 Access denied

需要 sudo：`sudo loginctl enable-linger $(whoami)`

### 索引损坏 / 搜索异常

删除索引目录，重启 server 自动从 markdown 重建：

```bash
systemctl --user stop everos
rm -rf ~/.everos-codex/.index/
systemctl --user start everos
# 等待 cascade 重建（日志出现 cascade_lancedb_rebuilt）
```

---

## 11. 日常维护

### 更新 EverOS

```bash
cd ~/src/EverOS
git pull
uv sync
systemctl --user restart everos
```

### 更新本仓库（插件 + 模板）

```bash
cd ~/src/everos-hermes
git pull
# 如有更新，重新复制插件文件
cp plugin/__init__.py ~/.hermes/plugins/everos-local/__init__.py
cp plugin/plugin.yaml ~/.hermes/plugins/everos-local/plugin.yaml
cp docs/GUIDE.md ~/.hermes/plugins/everos-local/GUIDE.md
```

### 备份记忆

记忆就是 Markdown 文件，直接 rsync 即可：

```bash
# 完整备份（含索引）
rsync -av ~/.everos-codex/ /backup/everos-codex/

# 仅备份记忆（不含可重建的索引）
rsync -av --exclude='.index/' ~/.everos-codex/ /backup/everos-memories/
```

---

## 参考

- **本仓库**：https://github.com/moreoronce/EverOS-Hermes
- EverOS 源码：`~/src/EverOS`（https://github.com/EverMind-AI/EverOS）
- EverOS Memory Root：`~/.everos-codex`
- 本地架构文档：`~/src/EverOS/docs/how-memory-works.md`
- 本地配置文档：`~/src/EverOS/docs/configuration.md`
- 插件源码：`~/.hermes/plugins/everos-local/__init__.py`
- 线上文档站（注意：OSS 部分可能过时）：https://docs.evermind.ai
