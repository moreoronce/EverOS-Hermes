from __future__ import annotations

import json
import stat
import threading

import httpx

import plugin


class _BlockingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def add_messages(self, session_id, messages, *, flush=True):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"add": {}, "flush": {}}


class _FailingClient:
    def __init__(self) -> None:
        self.calls = 0

    def add_messages(self, session_id, messages, *, flush=True):
        self.calls += 1
        raise httpx.ReadTimeout("ambiguous timeout")


def _provider(tmp_path, client):
    provider = plugin.EverOSLocalProvider()
    provider._client = client
    provider._host = "http://127.0.0.1:8765"
    provider._user_id = "user-test"
    provider._agent_id = "hermes"
    provider._app_id = "hermes"
    provider._project_id = "default"
    provider._conclude_outbox_path = tmp_path / "outbox.json"
    return provider


def _seed_job(provider, conclusion: str, status: str) -> dict:
    now = plugin._now_ms()
    request_id = provider._conclusion_key(conclusion, None)
    job = {
        "request_id": request_id,
        "session_id": f"hermes-conclude-test-{request_id[:20]}",
        "conclusion": conclusion,
        "timestamp_ms": now,
        "created_at_ms": now,
        "updated_at_ms": now,
        "status": status,
        "attempts": 1 if status == "running" else 0,
        "last_error": None,
    }
    plugin._write_private_json(
        provider._conclude_outbox_path,
        {"version": plugin._CONCLUDE_OUTBOX_VERSION, "jobs": {request_id: job}},
    )
    return job


def test_client_uses_v2_memory_endpoints() -> None:
    client = plugin._EverOSClient(
        "http://127.0.0.1:8765", "user", "agent", "app", "project"
    )
    calls = []

    def fake_request(method, endpoint, payload=None, timeout=None):
        calls.append((method, endpoint, payload, timeout))
        return {"data": {}}

    client._request = fake_request
    client.search("needle")
    client.add_messages(
        "session",
        [
            {
                "sender_id": "user",
                "role": "user",
                "timestamp": 1,
                "content": "durable fact",
            }
        ],
    )
    client.close()

    assert [call[1] for call in calls] == [
        "/api/v2/memory/search",
        "/api/v2/memory/add",
        "/api/v2/memory/flush",
    ]


def test_conclude_is_async_private_and_deduplicated(tmp_path) -> None:
    client = _BlockingClient()
    provider = _provider(tmp_path, client)
    args = {"conclusion": "The production deployment uses API version two."}

    first = json.loads(provider.handle_tool_call("everos_conclude", args))
    assert first["status"] == "pending"
    assert client.started.wait(timeout=2)

    duplicate = json.loads(provider.handle_tool_call("everos_conclude", args))
    assert duplicate["request_id"] == first["request_id"]
    assert duplicate["status"] in {"pending", "running"}
    assert client.calls == 1

    mode = stat.S_IMODE(provider._conclude_outbox_path.stat().st_mode)
    assert mode == 0o600

    client.release.set()
    for thread in list(provider._conclude_threads.values()):
        thread.join(timeout=5)

    stored = json.loads(provider.handle_tool_call("everos_conclude", args))
    assert stored["status"] == "stored"
    assert stored["request_id"] == first["request_id"]
    assert client.calls == 1


def test_ambiguous_timeout_is_not_retried(tmp_path) -> None:
    client = _FailingClient()
    provider = _provider(tmp_path, client)
    args = {"conclusion": "The timeout result may still have reached the server."}

    queued = json.loads(provider.handle_tool_call("everos_conclude", args))
    for thread in list(provider._conclude_threads.values()):
        thread.join(timeout=5)

    uncertain = json.loads(provider.handle_tool_call("everos_conclude", args))
    assert uncertain["request_id"] == queued["request_id"]
    assert uncertain["status"] == "uncertain"
    assert client.calls == 1


def test_orphaned_pending_job_is_safely_resumed(tmp_path) -> None:
    client = _BlockingClient()
    provider = _provider(tmp_path, client)
    conclusion = "A pending outbox record has not started network work."
    seeded = _seed_job(provider, conclusion, "pending")

    resumed = json.loads(
        provider.handle_tool_call("everos_conclude", {"conclusion": conclusion})
    )
    assert resumed["request_id"] == seeded["request_id"]
    assert client.started.wait(timeout=2)
    assert client.calls == 1
    client.release.set()
    for thread in list(provider._conclude_threads.values()):
        thread.join(timeout=5)

    stored = json.loads(
        provider.handle_tool_call("everos_conclude", {"conclusion": conclusion})
    )
    assert stored["status"] == "stored"
    assert client.calls == 1


def test_orphaned_running_job_becomes_uncertain_without_retry(tmp_path) -> None:
    client = _FailingClient()
    provider = _provider(tmp_path, client)
    conclusion = "A running job may already have committed before restart."
    seeded = _seed_job(provider, conclusion, "running")

    result = json.loads(
        provider.handle_tool_call("everos_conclude", {"conclusion": conclusion})
    )
    assert result["request_id"] == seeded["request_id"]
    assert result["status"] == "uncertain"
    assert client.calls == 0
