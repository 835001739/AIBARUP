"""本地 GPT 连接层测试：端点归一化、模型发现、错误诊断与推理调用。"""

from __future__ import annotations

import pytest
import requests

from config import Config
from core import llm


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, json_error: bool = False):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _clean_cache():
    llm._model_cache.clear()
    yield
    llm._model_cache.clear()


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", True)
    monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "http://127.0.0.1:11434")
    monkeypatch.setattr(Config, "AI_PROVIDER_MODEL", "qwen2.5:7b")
    monkeypatch.setattr(Config, "AI_PROVIDER_API_KEY", "")
    monkeypatch.setattr(Config, "AI_PROVIDER_TIMEOUT", 30)
    monkeypatch.setattr(Config, "AI_PROVIDER_AUTO_MODEL", True)
    monkeypatch.setattr(Config, "AI_PROVIDER_MAX_TOKENS", 256)
    return Config


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
        ("127.0.0.1:1234", "http://127.0.0.1:1234/v1"),
        ("http://127.0.0.1:11434/api", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/api/chat", "http://127.0.0.1:11434/v1"),
        (
            "http://127.0.0.1:11434/v1/chat/completions",
            "http://127.0.0.1:11434/v1",
        ),
        ("http://127.0.0.1:8080/openai/v1", "http://127.0.0.1:8080/openai/v1"),
        ("https://api.example.com/v1?x=1", "https://api.example.com/v1"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert llm.normalize_base_url(raw) == expected


def test_normalize_rejects_non_http():
    with pytest.raises(llm.LLMError) as exc:
        llm.normalize_base_url("ftp://127.0.0.1/v1")
    assert exc.value.code == "invalid_endpoint"


def test_endpoint_info_masks_external_address(monkeypatch):
    local = llm.endpoint_info("http://127.0.0.1:11434")
    assert local["loopback"] is True
    assert local["label"] == "127.0.0.1:11434/v1"

    monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "https://api.example.com/v1")
    external = llm.endpoint_info()
    assert external["loopback"] is False
    assert external["label"] == "外部服务（地址已隐藏）"
    assert "example.com" not in external["label"]


def test_chat_normalizes_endpoint_and_sends_auth(monkeypatch, enabled):
    monkeypatch.setattr(Config, "AI_PROVIDER_API_KEY", "sk-local")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "body": json, "timeout": timeout})
        return FakeResponse({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "ping"}]) == "OK"
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-local"
    assert captured["body"]["model"] == "qwen2.5:7b"
    assert captured["body"]["max_tokens"] == 256
    assert captured["body"]["stream"] is False


def test_chat_accepts_parts_content(monkeypatch, enabled):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"choices": [{"message": {"content": [{"type": "text", "text": "你"}, {"type": "text", "text": "好"}]}}]}
        ),
    )
    assert llm.chat([{"role": "user", "content": "hi"}]) == "你好"


def test_chat_error_mapping(monkeypatch, enabled):
    def raise_timeout(*_a, **_k):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(requests, "post", raise_timeout)
    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])
    assert exc.value.code == "timeout"
    assert "超时" in exc.value.message
    assert "TIMEOUT" in exc.value.hint


@pytest.mark.parametrize(
    "status,payload,code",
    [
        (404, {"error": {"message": "model not found"}}, "model_not_found"),
        (404, {"error": {"message": "Not Found"}}, "endpoint_not_found"),
        (401, {"error": {"message": "unauthorized"}}, "auth_failed"),
        (429, {"error": {"message": "rate limit"}}, "rate_limited"),
        (500, {"error": {"message": "boom"}}, "server_error"),
        (400, {"error": {"message": "bad request"}}, "http_error"),
    ],
)
def test_chat_http_errors(monkeypatch, enabled, status, payload, code):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(payload, status_code=status))
    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])
    assert exc.value.code == code


def test_chat_bad_and_empty_responses(monkeypatch, enabled):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(None, json_error=True))
    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])
    assert exc.value.code == "bad_response"

    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse({"choices": [{"message": {"content": ""}}]})
    )
    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])
    assert exc.value.code == "empty_content"


def test_auto_model_discovery(monkeypatch, enabled):
    monkeypatch.setattr(Config, "AI_PROVIDER_MODEL", "")
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse({"data": [{"id": "qwen2.5:7b"}, {"id": "llama3.2:3b"}]})

    monkeypatch.setattr(requests, "get", fake_get)
    assert llm.resolve_model() == "qwen2.5:7b"
    assert captured["url"] == "http://127.0.0.1:11434/v1/models"
    # 第二次走缓存，不再发请求
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应再请求")))
    assert llm.resolve_model() == "qwen2.5:7b"


def test_provider_status_states(monkeypatch):
    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", False)
    status = llm.provider_status(probe=True)
    assert status["enabled"] is False
    assert status["error_code"] == "provider_disabled"

    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", True)
    monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "")
    status = llm.provider_status(probe=True)
    assert status["configured"] is False
    assert status["error_code"] == "endpoint_missing"


def test_provider_status_probe_and_test(monkeypatch, enabled):
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse({"data": [{"id": "qwen2.5:7b"}]})

    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)

    status = llm.provider_status(probe=True)
    assert status["reachable"] is True
    assert status["model_available"] is True
    assert status["models"] == ["qwen2.5:7b"]

    tested = llm.provider_test()
    assert tested["chat_ok"] is True
    assert tested["reply_len"] == 2
    assert tested["error_code"] == ""


def test_provider_test_reports_connection_failure(monkeypatch, enabled):
    def boom(*_a, **_k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    result = llm.provider_test()
    assert result["chat_ok"] is False
    assert result["error_code"] == "connection_refused"
    assert "11434" in result["hint"]


def test_status_never_leaks_key(monkeypatch, enabled):
    monkeypatch.setattr(Config, "AI_PROVIDER_API_KEY", "sk-super-secret")
    status = llm.provider_status(probe=False)
    assert "sk-super-secret" not in repr(status)


def test_provider_test_against_real_openai_compatible_server(monkeypatch, enabled):
    """用本机真实 HTTP 服务走一遍网络链路，验证根地址会自动补 /v1。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # 静默测试日志
            return

        def _send(self, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append("GET " + self.path)
            self._send({"data": [{"id": "stub-model"}]})

        def do_POST(self):
            seen.append("POST " + self.path)
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._send({"choices": [{"message": {"content": "OK"}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "http://127.0.0.1:%d" % port)
        monkeypatch.setattr(Config, "AI_PROVIDER_MODEL", "")
        result = llm.provider_test()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["chat_ok"] is True, result
    assert result["model"] == "stub-model"
    assert result["models"] == ["stub-model"]
    assert seen == ["GET /v1/models", "POST /v1/chat/completions"]


def test_prompt_routes_expose_ai_provider_status_and_test(monkeypatch):
    from flask import Flask
    from prompt.routes import bp as prompt_bp

    monkeypatch.setattr(
        llm,
        "provider_status",
        lambda probe=False: {"enabled": True, "reachable": bool(probe), "probe": probe},
    )
    monkeypatch.setattr(
        llm,
        "provider_test",
        lambda: {"enabled": True, "reachable": True, "chat_ok": True, "reply_len": 2},
    )

    app = Flask(__name__)
    app.register_blueprint(prompt_bp)
    with app.test_client() as client:
        status = client.get("/api/prompts/ai-provider/status?probe=1")
        assert status.status_code == 200
        payload = status.get_json()
        assert payload["ok"] is True
        assert payload["data"]["probe"] is True

        tested = client.post("/api/prompts/ai-provider/test")
        assert tested.status_code == 200
        assert tested.get_json()["data"]["chat_ok"] is True


def test_provider_status_reports_invalid_endpoint(monkeypatch):
    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", True)
    monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "ftp://127.0.0.1/v1")
    monkeypatch.setattr(Config, "AI_PROVIDER_MODEL", "x")
    status = llm.provider_status(probe=False)
    assert status["enabled"] is True
    assert status["configured"] is False
    assert status["error_code"] == "invalid_endpoint"
