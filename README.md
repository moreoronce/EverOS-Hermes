# EverOS ↔ Hermes

> EverOS memory system integration for Hermes Agent. Local-first, markdown-backed memory that persists across sessions.

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

## Requirements

- **OS:** Linux / macOS / WSL2 (Windows native not supported — `fcntl.flock` POSIX dependency)
- **Python:** 3.12+ (via `uv` venv)
- **LLM Provider:** Any OpenAI-compatible endpoint (SiliconFlow, OpenAI, DeepSeek, etc.)
- **Embedding Provider:** Same — needs `/v1/embeddings` support

## License

MIT
