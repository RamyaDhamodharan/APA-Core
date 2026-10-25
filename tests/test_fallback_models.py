import httpx
import pytest

from src.services.llm_pipeline_services import (
    DEFAULT_EXTRACTION_FALLBACK_MODELS,
    _groq_extract,
    _is_openrouter_model,
    extract_document,
)
from src.services.llm_matching_services import (
    DEFAULT_MATCHING_FALLBACK_MODELS,
    _call_groq_match,
    _is_openrouter_matching_model,
)


def test_default_5_fallback_models_configured():
    expected_models = [
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "meta-llama/llama-3.3-70b-instruct",
        "mistralai/mistral-small-24b-instruct-2501",
        "qwen/qwen-2.5-72b-instruct",
    ]
    assert len(DEFAULT_EXTRACTION_FALLBACK_MODELS) == 5
    assert DEFAULT_EXTRACTION_FALLBACK_MODELS == expected_models

    assert len(DEFAULT_MATCHING_FALLBACK_MODELS) == 5
    assert DEFAULT_MATCHING_FALLBACK_MODELS == expected_models


def test_is_openrouter_model_detection():
    assert _is_openrouter_model("meta-llama/llama-3.3-70b-instruct") is True
    assert _is_openrouter_model("mistralai/mistral-small-24b-instruct-2501") is True
    assert _is_openrouter_model("qwen/qwen-2.5-72b-instruct") is True
    assert _is_openrouter_model("openrouter/custom-model") is True
    assert _is_openrouter_model("openai/gpt-oss-120b") is False
    assert _is_openrouter_model("qwen/qwen3.8-27b") is False

    assert _is_openrouter_matching_model("meta-llama/llama-3.3-70b-instruct") is True
    assert _is_openrouter_matching_model("mistralai/mistral-small-24b-instruct-2501") is True
    assert _is_openrouter_matching_model("qwen/qwen-2.5-72b-instruct") is True
    assert _is_openrouter_matching_model("openai/gpt-oss-120b") is False


def test_extraction_iterates_through_5_fallbacks_until_success(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv(
        "EXTRACTION_FALLBACK_MODELS",
        "openai/gpt-oss-20b,qwen/qwen3.8-27b,meta-llama/llama-3.3-70b-instruct,mistralai/mistral-small-24b-instruct-2501,qwen/qwen-2.5-72b-instruct",
    )

    attempted_calls = []

    def mock_post(url, *args, **kwargs):
        model = kwargs["json"]["model"]
        auth = kwargs["headers"]["Authorization"]
        attempted_calls.append({"url": str(url), "model": model, "auth": auth})

        # Fail primary + first 4 fallbacks
        if len(attempted_calls) < 6:
            raise httpx.HTTPStatusError("Rate limited 429", request=httpx.Request("POST", str(url)), response=httpx.Response(429))

        # 5th fallback succeeds
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"invoice_number": "INV-FALLBACK-5", "total": 999}'}}]},
            request=httpx.Request("POST", str(url)),
        )

    monkeypatch.setattr("src.services.llm_pipeline_services.httpx.post", mock_post)

    result = extract_document("Invoice INV-FALLBACK-5", "invoice")

    assert result["invoice_number"] == "INV-FALLBACK-5"
    assert result["total"] == 999
    assert len(attempted_calls) == 6

    # Verify order of primary and 5 fallbacks
    models = [c["model"] for c in attempted_calls]
    assert models == [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "meta-llama/llama-3.3-70b-instruct",
        "mistralai/mistral-small-24b-instruct-2501",
        "qwen/qwen-2.5-72b-instruct",
    ]

    # Verify endpoint routing
    assert "api.groq.com" in attempted_calls[0]["url"]
    assert "api.groq.com" in attempted_calls[1]["url"]
    assert "api.groq.com" in attempted_calls[2]["url"]
    assert "openrouter.ai" in attempted_calls[3]["url"]
    assert "openrouter.ai" in attempted_calls[4]["url"]
    assert "openrouter.ai" in attempted_calls[5]["url"]

    # Verify correct auth key was used per provider
    assert attempted_calls[0]["auth"] == "Bearer test-groq-key"
    assert attempted_calls[3]["auth"] == "Bearer test-openrouter-key"


def test_matching_call_routes_endpoints_correctly(monkeypatch):
    calls = []

    def mock_post(url, *args, **kwargs):
        calls.append({"url": str(url), "model": kwargs["json"]["model"], "auth": kwargs["headers"]["Authorization"]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"is_match": true, "outcome": "ready_to_pay", "match_score": 100}'}}]},
            request=httpx.Request("POST", str(url)),
        )

    monkeypatch.setattr("src.services.llm_matching_services.httpx.post", mock_post)

    # Call with Groq model
    res1 = _call_groq_match(
        {"invoice_number": "INV-1"},
        [],
        [],
        "openai/gpt-oss-20b",
        "groq-key-123",
        "openrouter-key-456",
    )
    assert res1["is_match"] is True
    assert "api.groq.com" in calls[0]["url"]
    assert calls[0]["auth"] == "Bearer groq-key-123"

    # Call with OpenRouter fallback model
    res2 = _call_groq_match(
        {"invoice_number": "INV-1"},
        [],
        [],
        "meta-llama/llama-3.3-70b-instruct",
        "groq-key-123",
        "openrouter-key-456",
    )
    assert res2["is_match"] is True
    assert "openrouter.ai" in calls[1]["url"]
    assert calls[1]["auth"] == "Bearer openrouter-key-456"
