# EverOS ↔ Hermes

> EverOS memory system integration for Hermes Agent. Local-first, markdown-backed memory that persists across sessions.
>
> [中文](README.zh-CN.md) | English

**EverOS** is an open-source memory framework for AI agents, developed by [EverMind](https://evermind.ai).  
**Hermes Agent** is a self-improving AI agent built by [Nous Research](https://github.com/NousResearch).

This repo bridges the two: a Hermes plugin + deployment templates + a guide written for AI agents to follow.

## What's in this repo

| Path | What |
|------|------|
| `plugin/` | Hermes plugin (`everos-local`) — HTTP client, prefetch, sync_turn, tools |
| `docs/GUIDE.md` | Full integration guide for AI agents to install & configure |
| `deploy/` | Deployment templates (systemd service, config examples, launch script) |

## Quick start

1. **Read [`docs/GUIDE.md`](docs/GUIDE.md)** — it's written for AI agents to follow step by step.
2. The guide covers: system requirements → provider selection → installation → Hermes config → verification → troubleshooting.

## Architecture

```
Hermes (Agent)                         EverOS Sidecar
┌─────────────┐    HTTP/localhost    ┌──────────────────────┐
│  everos-    │ ──────────────────► │  FastAPI :8765        │
│  local      │   /api/v1/memory/*  │  LLM + Embedding +    │
│  plugin     │ ◄────────────────── │  Cascade + OME        │
└─────────────┘                     │                       │
                                    │  Markdown (truth)     │
                                    │  SQLite (queue)       │
                                    │  LanceDB (vector)     │
                                    └───────────────────────┘
```

One EverOS instance can serve multiple agents simultaneously — Hermes and Codex share the same sidecar, partitioned by `app_id`.

## Requirements

- **OS:** Linux / macOS / WSL2 (Windows native not supported — `fcntl.flock` POSIX dependency)
- **Python:** 3.12+ (via `uv` venv)
- **LLM Provider:** Any OpenAI-compatible endpoint (SiliconFlow, OpenAI, DeepSeek, etc.)
- **Embedding Provider:** Same — needs `/v1/embeddings` support

## Links

- [EverOS (upstream)](https://github.com/EverMind-AI/EverOS) — the memory framework
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent harness
- [EverOS Docs](https://docs.evermind.ai) — official documentation

## License

MIT

<p align="center">
  <a href="https://x.com/moreoronce">
    <img src="https://img.shields.io/badge/X-Follow_@moreoronce-black?style=for-the-badge&logo=x&logoColor=white" height="40" alt="X @moreoronce">
  </a>
</p>
