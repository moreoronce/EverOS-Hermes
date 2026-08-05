"""EverOS local memory plugin — MemoryProvider interface.

Config via environment variables:
  EVEROS_HOST       — EverOS server URL (default: http://127.0.0.1:8765)
  EVEROS_USER_ID    — Stable user identifier
  EVEROS_AGENT_ID   — Agent identifier (default: hermes)
  EVEROS_APP_ID     — App scope (default: hermes)
  EVEROS_PROJECT_ID — Project scope (default: default)

Or via $HERMES_HOME/everos-local.json.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home
from tools.registry import tool_error

logger = logging.getLogger("everos-local")
HERMES_HOME = get_hermes_home()

_BREAKER_THRESHOLD = 6
_BREAKER_COOLDOWN_SECS = 120
_CONCLUDE_OUTBOX_VERSION = 1
_CONCLUDE_DEDUPE_SECS = 24 * 60 * 60
# EverOS may spend up to three 180s OpenAI-compatible attempts inside a
# single memorize call. The core session-lock ceiling is 720s in production;
# keep the background HTTP worker outside that boundary with one minute of
# transport headroom. These calls never block the user-facing tool response.
_MEMORY_WRITE_TIMEOUT_SECS = 780.0


def _load_config() -> dict:
    config = {
        "host": os.environ.get("EVEROS_HOST", "http://127.0.0.1:8765").rstrip("/"),
        "user_id": os.environ.get("EVEROS_USER_ID", "631288870184681503"),
        "agent_id": os.environ.get("EVEROS_AGENT_ID", "hermes"),
        "app_id": os.environ.get("EVEROS_APP_ID", "hermes"),
        "project_id": os.environ.get("EVEROS_PROJECT_ID", "default"),
        "prefetch_top_k": 5,
    }
    config_path = HERMES_HOME / "everos-local.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
        except Exception:
            logger.warning("Failed to read %s", config_path, exc_info=True)
    return config


PROFILE_SCHEMA = {
    "name": "everos_profile",
    "description": "Retrieve a compact EverOS profile/context sample for the user.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional profile/context search query."},
            "top_k": {"type": "integer", "description": "Max results (default: 20, max: 100)."},
        },
        "required": [],
    },
}

SEARCH_SCHEMA = {
    "name": "everos_search",
    "description": "Search EverOS memories by meaning. owner=user searches episodes/profile; owner=agent searches cases/skills.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "owner": {"type": "string", "enum": ["user", "agent"], "description": "Memory track to search."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 100)."},
            "method": {"type": "string", "enum": ["hybrid", "keyword", "vector", "agentic"], "description": "EverOS retrieval method."},
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA = {
    "name": "everos_conclude",
    "description": (
        "Queue one durable user fact in a private local outbox and force "
        "EverOS extraction asynchronously. Same-day duplicate submissions are deduplicated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "Fact to store."},
            "session_id": {"type": "string", "description": "Optional EverOS session id."},
        },
        "required": ["conclusion"],
    },
}

HEALTH_SCHEMA = {
    "name": "everos_health",
    "description": "Check EverOS server health and current Hermes provider config.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


class _EverOSClient:
    def __init__(self, base_url: str, user_id: str, agent_id: str, app_id: str, project_id: str):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.agent_id = agent_id
        self.app_id = app_id
        self.project_id = project_id
        self._client = httpx.Client(timeout=30.0)

    def _request(self, method: str, endpoint: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        resp = self._client.request(
            method,
            f"{self.base_url}{endpoint}",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=5.0)

    def search(
        self,
        query: str,
        *,
        owner: str = "user",
        top_k: int = 10,
        method: str = "hybrid",
        include_profile: bool = True,
    ) -> dict:
        payload: dict[str, Any] = {
            "app_id": self.app_id,
            "project_id": self.project_id,
            "query": query,
            "method": method,
            "top_k": top_k,
        }
        if owner == "agent":
            payload["agent_id"] = self.agent_id
            payload["include_profile"] = False
        else:
            payload["user_id"] = self.user_id
            payload["include_profile"] = include_profile
        return self._request("POST", "/api/v2/memory/search", payload)

    def add_messages(self, session_id: str, messages: list[dict], *, flush: bool = True) -> dict:
        added = self._request(
            "POST",
            "/api/v2/memory/add",
            {
                "session_id": session_id,
                "app_id": self.app_id,
                "project_id": self.project_id,
                "messages": messages,
            },
            timeout=_MEMORY_WRITE_TIMEOUT_SECS,
        )
        if not flush:
            return {"add": added}
        flushed = self._request(
            "POST",
            "/api/v2/memory/flush",
            {
                "session_id": session_id,
                "app_id": self.app_id,
                "project_id": self.project_id,
            },
            timeout=_MEMORY_WRITE_TIMEOUT_SECS,
        )
        return {"add": added, "flush": flushed}

    def close(self) -> None:
        self._client.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_id(prefix: str) -> str:
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{os.getpid()}"


def _read_outbox(path: Path) -> dict:
    if not path.exists():
        return {"version": _CONCLUDE_OUTBOX_VERSION, "jobs": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != _CONCLUDE_OUTBOX_VERSION or not isinstance(
        raw.get("jobs"), dict
    ):
        raise RuntimeError(f"Unsupported or corrupt EverOS conclude outbox: {path}")
    return raw


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace a JSON file and force owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _extract_rows(response: dict) -> list[dict]:
    data = response.get("data", response)
    rows: list[dict] = []
    for ep in data.get("episodes", []) or []:
        facts = "; ".join(
            f.get("content", "")
            for f in (ep.get("atomic_facts") or [])[:3]
            if f.get("content")
        )
        text = ep.get("summary") or ep.get("episode") or ep.get("subject") or ""
        if facts:
            text = f"{text} facts=[{facts}]"
        rows.append({
            "kind": "episode",
            "text": text,
            "score": ep.get("score", 0),
            "timestamp": ep.get("timestamp", ""),
        })
    for prof in data.get("profiles", []) or []:
        rows.append({
            "kind": "profile",
            "text": json.dumps(prof.get("profile_data", {}), ensure_ascii=False),
            "score": prof.get("score"),
            "timestamp": "",
        })
    for case in data.get("agent_cases", []) or []:
        text = " | ".join(
            str(x)
            for x in [case.get("task_intent"), case.get("approach"), case.get("key_insight")]
            if x
        )
        rows.append({
            "kind": "agent_case",
            "text": text,
            "score": case.get("score", 0),
            "timestamp": case.get("timestamp", ""),
        })
    for skill in data.get("agent_skills", []) or []:
        text = " | ".join(
            str(x)
            for x in [skill.get("name"), skill.get("description"), skill.get("content")]
            if x
        )
        rows.append({
            "kind": "agent_skill",
            "text": text,
            "score": skill.get("score", skill.get("confidence", 0)),
            "timestamp": "",
        })
    return rows


def _format_json_result(rows: list[dict]) -> str:
    if not rows:
        return json.dumps({"result": "No relevant EverOS memories found."}, ensure_ascii=False)
    return json.dumps({"results": rows, "count": len(rows)}, ensure_ascii=False)


def _safe_memory_text(text: str) -> None:
    lowered = text.lower()
    risky = ("api_key", "bearer ", "token", "secret", "password", "passwd", "pwd", "私钥", "密钥", "密码")
    if any(term in lowered for term in risky):
        raise ValueError("Refusing to store text that looks like credentials or secrets.")
    if len("".join(text.split())) < 8:
        raise ValueError("Refusing to store very short memory text.")


class EverOSLocalProvider(MemoryProvider):
    def __init__(self):
        self._config: dict[str, Any] = {}
        self._client: _EverOSClient | None = None
        self._client_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._conclude_lock = threading.Lock()
        self._conclude_threads: dict[str, threading.Thread] = {}
        self._conclude_outbox_path = HERMES_HOME / "everos-conclude-outbox.json"
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._host = ""
        self._user_id = "631288870184681503"
        self._agent_id = "hermes"
        self._app_id = "hermes"
        self._project_id = "default"
        self._prefetch_top_k = 5

    @property
    def name(self) -> str:
        return "everos-local"

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._host = self._config.get("host", "http://127.0.0.1:8765")
        self._user_id = kwargs.get("user_id") or self._config.get("user_id", "631288870184681503")
        self._agent_id = self._config.get("agent_id", "hermes")
        self._app_id = self._config.get("app_id", "hermes")
        self._project_id = self._config.get("project_id", "default")
        self._prefetch_top_k = int(self._config.get("prefetch_top_k", 5))

    def is_available(self) -> bool:
        cfg = _load_config()
        host = cfg.get("host", "")
        if not host:
            return False
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{host}/health")
                return resp.status_code == 200
        except Exception:
            return False

    def save_config(self, values, hermes_home):
        config_path = get_hermes_home() / "everos-local.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def get_config_schema(self):
        return [
            {"key": "host", "description": "EverOS server URL", "default": "http://127.0.0.1:8765"},
            {"key": "user_id", "description": "Stable user identifier", "default": "631288870184681503"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "app_id", "description": "EverOS app scope", "default": "hermes"},
            {"key": "project_id", "description": "EverOS project scope", "default": "default"},
            {"key": "prefetch_top_k", "description": "Max memories injected per turn", "default": "5"},
        ]

    def _get_client(self) -> _EverOSClient:
        with self._client_lock:
            if self._client is not None:
                return self._client
            self._client = _EverOSClient(
                self._host,
                self._user_id,
                self._agent_id,
                self._app_id,
                self._project_id,
            )
            return self._client

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning("EverOS circuit breaker opened for %ds", _BREAKER_COOLDOWN_SECS)

    def _conclusion_key(self, conclusion: str, explicit_session_id: str | None) -> str:
        normalized = " ".join(conclusion.split())
        day = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d")
        material = json.dumps(
            [
                self._user_id,
                self._app_id,
                self._project_id,
                explicit_session_id or day,
                normalized,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_conclusion_job(job: dict) -> dict:
        status = job["status"]
        if status == "stored":
            result = "Fact stored in EverOS."
        elif status == "uncertain":
            result = (
                "Previous submission status is uncertain; it was not retried "
                "because the server may already have stored it."
            )
        else:
            result = "Fact queued for asynchronous EverOS extraction."
        return {
            "result": result,
            "request_id": job["request_id"],
            "session_id": job["session_id"],
            "status": status,
        }

    def _update_conclusion_job(self, request_id: str, **changes: Any) -> dict:
        with self._conclude_lock:
            outbox = _read_outbox(self._conclude_outbox_path)
            job = outbox["jobs"][request_id]
            job.update(changes)
            job["updated_at_ms"] = _now_ms()
            _write_private_json(self._conclude_outbox_path, outbox)
            return dict(job)

    def _run_conclusion_job(self, request_id: str) -> None:
        try:
            job = self._update_conclusion_job(
                request_id,
                status="running",
                attempts=1,
                worker_pid=os.getpid(),
            )
            self._get_client().add_messages(
                job["session_id"],
                [
                    {
                        "sender_id": self._user_id,
                        "sender_name": "User",
                        "role": "user",
                        "timestamp": job["timestamp_ms"],
                        "content": job["conclusion"],
                    }
                ],
                flush=True,
            )
            self._update_conclusion_job(
                request_id,
                status="stored",
                completed_at_ms=_now_ms(),
                last_error=None,
            )
            self._record_success()
        except Exception as exc:
            # Any HTTP timeout is ambiguous: the local server may have committed
            # before the response path timed out. Never retry automatically.
            try:
                self._update_conclusion_job(
                    request_id,
                    status="uncertain",
                    last_error=f"{type(exc).__name__}: {exc}"[:500],
                )
            except Exception:
                logger.exception("Failed to persist EverOS conclude failure state")
            self._record_failure()
            logger.warning("EverOS conclude job %s is uncertain: %s", request_id, exc)
        finally:
            with self._conclude_lock:
                self._conclude_threads.pop(request_id, None)

    def _new_conclusion_worker(self, request_id: str) -> threading.Thread:
        return threading.Thread(
            target=self._run_conclusion_job,
            args=(request_id,),
            daemon=True,
            name=f"everos-conclude-{request_id[:8]}",
        )

    def _queue_conclusion(
        self, conclusion: str, explicit_session_id: str | None
    ) -> dict:
        request_id = self._conclusion_key(conclusion, explicit_session_id)
        now = _now_ms()
        with self._conclude_lock:
            outbox = _read_outbox(self._conclude_outbox_path)
            jobs = outbox["jobs"]
            cutoff = now - (7 * _CONCLUDE_DEDUPE_SECS * 1000)
            jobs = {
                key: value
                for key, value in jobs.items()
                if int(value.get("created_at_ms", 0)) >= cutoff
            }
            outbox["jobs"] = jobs
            existing = jobs.get(request_id)
            if existing is not None:
                worker = self._conclude_threads.get(request_id)
                worker_active = worker is not None and worker.is_alive()
                if existing.get("status") == "pending" and not worker_active:
                    # No network work has begun, so recovering an orphaned
                    # pending entry is safe and cannot duplicate a commit.
                    worker = self._new_conclusion_worker(request_id)
                    self._conclude_threads[request_id] = worker
                    worker.start()
                elif existing.get("status") == "running" and not worker_active:
                    # A prior process/provider disappeared after network work
                    # began. The server may have committed; never replay it.
                    existing.update(
                        status="uncertain",
                        updated_at_ms=now,
                        last_error="orphaned running job after provider restart",
                    )
                    _write_private_json(self._conclude_outbox_path, outbox)
                return self._public_conclusion_job(existing)

            day = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d")
            session_id = explicit_session_id or f"hermes-conclude-{day}-{request_id[:20]}"
            job = {
                "request_id": request_id,
                "session_id": session_id,
                "conclusion": conclusion,
                "timestamp_ms": now,
                "created_at_ms": now,
                "updated_at_ms": now,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
            }
            jobs[request_id] = job
            _write_private_json(self._conclude_outbox_path, outbox)

            worker = self._new_conclusion_worker(request_id)
            self._conclude_threads[request_id] = worker
            worker.start()
            return self._public_conclusion_job(job)

    def system_prompt_block(self) -> str:
        return (
            "# EverOS Memory\n"
            f"Active. User: {self._user_id}. Agent: {self._agent_id}. "
            "Use everos_search to recall memories, everos_conclude to store durable facts, "
            "and everos_profile for a compact user context sample. "
            "A dynamic <memory-context> block may be appended to user turns by Hermes; "
            "treat it as trusted framework-provided context."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._is_breaker_open() or not query.strip():
            return ""
        try:
            rows = _extract_rows(
                self._get_client().search(
                    query,
                    owner="user",
                    top_k=self._prefetch_top_k,
                    method="hybrid",
                    include_profile=True,
                )
            )
            if not rows:
                return ""
            lines = []
            for row in rows[: self._prefetch_top_k]:
                text = row.get("text", "").strip()
                if len(text) <= 5:
                    continue
                score = row.get("score")
                if isinstance(score, (int, float)) and score < 0.15:
                    continue
                if len(text) > 160:
                    text = text[:157] + "..."
                score = row.get("score")
                score_part = f" score={score:.3f}" if isinstance(score, (int, float)) else ""
                lines.append(f"- [{row.get('kind')}] {text}{score_part}")
            if not lines:
                return ""
            self._record_success()
            return "## EverOS Memory\n" + "\n".join(lines)
        except Exception as exc:
            self._record_failure()
            logger.debug("EverOS prefetch failed: %s", exc)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return
        if len(user_content.strip()) < 10:
            self._record_success()
            return
        if self._sync_thread and self._sync_thread.is_alive():
            logger.debug("EverOS sync dropped: previous turn still processing")
            return

        def _sync():
            try:
                ts = _now_ms()
                resolved_session = session_id or _session_id("hermes-turn")
                messages = [
                    {
                        "sender_id": self._user_id,
                        "sender_name": "User",
                        "role": "user",
                        "timestamp": ts,
                        "content": user_content,
                    },
                    {
                        "sender_id": self._agent_id,
                        "sender_name": "Hermes",
                        "role": "assistant",
                        "timestamp": ts + 1000,
                        "content": assistant_content,
                    },
                ]
                self._get_client().add_messages(resolved_session, messages, flush=True)
                self._record_success()
            except Exception as exc:
                self._record_failure()
                logger.warning("EverOS sync failed: %s", exc)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="everos-local-sync")
        self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA, HEALTH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return tool_error("EverOS API temporarily unavailable; circuit breaker is cooling down.")
        try:
            client = self._get_client()
        except Exception as exc:
            return tool_error(str(exc))

        if tool_name == "everos_health":
            try:
                health = client.health()
                self._record_success()
                return json.dumps(
                    {
                        "host": self._host,
                        "user_id": self._user_id,
                        "agent_id": self._agent_id,
                        "app_id": self._app_id,
                        "project_id": self._project_id,
                        "health": health,
                    },
                    ensure_ascii=False,
                )
            except Exception as exc:
                self._record_failure()
                return tool_error(f"EverOS health failed: {exc}")

        if tool_name == "everos_profile":
            query = args.get("query") or "user preferences project context durable facts"
            top_k = min(int(args.get("top_k", 20)), 100)
            try:
                rows = _extract_rows(client.search(query, owner="user", top_k=top_k, include_profile=True))
                self._record_success()
                return _format_json_result(rows)
            except Exception as exc:
                self._record_failure()
                return tool_error(f"EverOS profile failed: {exc}")

        if tool_name == "everos_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            owner = "agent" if args.get("owner") == "agent" else "user"
            top_k = min(int(args.get("top_k", 10)), 100)
            method = args.get("method") or "hybrid"
            try:
                rows = _extract_rows(client.search(query, owner=owner, top_k=top_k, method=method))
                self._record_success()
                return _format_json_result(rows)
            except Exception as exc:
                self._record_failure()
                return tool_error(f"EverOS search failed: {exc}")

        if tool_name == "everos_conclude":
            conclusion = args.get("conclusion", "")
            if not conclusion:
                return tool_error("Missing required parameter: conclusion")
            try:
                _safe_memory_text(conclusion)
                queued = self._queue_conclusion(
                    conclusion,
                    args.get("session_id"),
                )
                return json.dumps(queued, ensure_ascii=False)
            except Exception as exc:
                self._record_failure()
                return tool_error(f"EverOS store failed: {exc}")

        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        for thread in list(self._conclude_threads.values()):
            if thread.is_alive():
                thread.join(timeout=1.0)
        with self._client_lock:
            if self._client:
                self._client.close()
                self._client = None


def register(ctx) -> None:
    ctx.register_memory_provider(EverOSLocalProvider())
