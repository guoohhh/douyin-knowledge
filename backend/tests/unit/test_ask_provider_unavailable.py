from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from douyin_knowledge.ai.adapters import deepseek_adapter
from douyin_knowledge.api import create_app
from douyin_knowledge.api.deps import get_conversation_manager
from douyin_knowledge.config import Settings


def test_upstream_503_reaches_ask_client_as_safe_retryable_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SDKStatusError(Exception):
        status_code = 503

    calls = 0

    def complete(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise SDKStatusError("private provider body")

    monkeypatch.setattr(deepseek_adapter, "APIStatusError", SDKStatusError)
    model = object.__new__(deepseek_adapter.DeepSeekChatModel)
    model.model = "test"
    model._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete))
    )
    app = create_app(settings)
    app.dependency_overrides[get_conversation_manager] = lambda: SimpleNamespace(
        ask=lambda *args, **kwargs: model.generate([])
    )
    response = TestClient(app).post("/api/conversations/ask", json={"query": "test question"})
    assert response.status_code == 503
    assert response.json() == {"error": {
        "code": "model_unavailable",
        "message": "AI 服务暂时繁忙，请稍后重试",
        "retryable": True,
        "context": {},
    }}
    assert calls == 1
