# EverOS ↔ Hermes

> 为 Hermes Agent 接入 EverOS 记忆系统。本地优先、Markdown 存储、跨 session 持久化。
>
> 中文 | [English](README.md)

**[EverOS](https://github.com/EverMind-AI/EverOS)** 是由 [EverMind](https://evermind.ai) 开发的开源 AI agent 记忆框架。  
**[Hermes Agent](https://github.com/NousResearch/hermes-agent)** 是由 [Nous Research](https://github.com/NousResearch) 打造的自我进化 AI agent。

本仓库将两者打通：Hermes 插件 + 部署模板 + 一份专为 AI Agent 编写的集成指南。

## 为什么用 EverOS

| 特性 | 说明 |
|------|------|
| **Markdown 即真相** | 记忆是磁盘上的 `.md` 文件，`cat` / `grep` / `vim` 直接看直接改，不是黑盒向量 |
| **零外部依赖** | 不需要 Qdrant / Milvus / Redis，全嵌入（SQLite + LanceDB）|
| **OME 离线引擎** | 自动聚类、反思合并碎片记忆，维护动态用户画像 |
| **多 Agent 共享** | 一个实例跑 Hermes + Codex，按 `app_id` 分区互不干扰 |
| **索引可重建** | 删掉整个 `.index/` 不丢任何记忆，重启自动从 Markdown 重建 |
| **Prefetch / sync_turn** | 每轮自动召回历史记忆注入上下文 + 异步写入对话 |

## 仓库内容

| 路径 | 说明 |
|------|------|
| `plugin/` | Hermes 插件（`everos-local`）— HTTP 客户端、prefetch、sync_turn、工具集 |
| `docs/GUIDE.md` | 完整集成指南，专为 AI Agent 编写，按步骤执行安装和配置 |
| `deploy/` | 部署模板（systemd service、配置示例、启动脚本）|

## 快速开始

1. **阅读 [`docs/GUIDE.md`](docs/GUIDE.md)** — 这份指南是写给 AI Agent 看的。
2. Agent 读完会自动执行：环境检查 → 向用户收集 Provider 信息 → 安装 → 配置 → 验证 → 排障。

```
Hermes (Agent)                         EverOS Sidecar
┌─────────────┐    HTTP/localhost    ┌──────────────────────┐
│  everos-    │ ──────────────────► │  FastAPI :8765        │
│  local      │   /api/v1/memory/*  │  LLM 抽取 + Embedding │
│  plugin     │ ◄────────────────── │  Cascade 索引 + OME   │
└─────────────┘                     │                       │
                                    │  Markdown (真相源)     │
                                    │  SQLite (队列)         │
                                    │  LanceDB (向量索引)    │
                                    └───────────────────────┘
```

一个 EverOS 实例可同时服务多个 Agent — Hermes 和 Codex 共享同一个 sidecar，通过 `app_id` 分区隔离。

## 系统要求

- **操作系统：** Linux / macOS / WSL2（Windows 原生不支持 — `fcntl.flock` POSIX 依赖）
- **Python：** 3.12+（通过 `uv` 自动管理 venv）
- **LLM Provider：** 任意 OpenAI 兼容端点（SiliconFlow / OpenAI / DeepSeek 等）
- **Embedding Provider：** 同上 — 需支持 `/v1/embeddings`

## 相关链接

- [EverOS（上游仓库）](https://github.com/EverMind-AI/EverOS) — 记忆框架本体
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Agent 框架
- [EverOS 官方文档](https://docs.evermind.ai)

## License

MIT
