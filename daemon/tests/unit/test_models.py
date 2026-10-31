"""Model registry: catalog enrichment, recommendations, cost, caching."""

from daemon.models import (
    ModelRegistry,
    RemoteModel,
    estimate_cost_usd,
    pick_role_model,
    recommend,
)
from daemon.providers.types import ModelCapabilities, Usage


def _model(model_id: str, **caps) -> RemoteModel:
    return RemoteModel(model_id=model_id, provider="test",
                       capabilities=ModelCapabilities(**caps))


def test_enrichment_fills_known_models():
    model = RemoteModel(model_id="gpt-4o", provider="openai")
    from daemon.models import enrich

    enriched = enrich(model)
    assert enriched.capabilities.context_window == 128_000
    assert enriched.capabilities.tool_calling is True
    assert enriched.capabilities.input_price_per_mtok == 2.5


def test_enrichment_handles_gateway_prefixed_ids():
    from daemon.models import enrich

    enriched = enrich(RemoteModel(model_id="openai/gpt-4o-mini",
                                  provider="openrouter"))
    assert enriched.capabilities.context_window == 128_000


def test_enrichment_never_overwrites_known_values():
    from daemon.models import enrich

    model = RemoteModel(model_id="gpt-4o", provider="openai",
                        capabilities=ModelCapabilities(
                            context_window=999_999, tool_calling=False))
    enriched = enrich(model)
    assert enriched.capabilities.context_window == 999_999
    assert enriched.capabilities.tool_calling is False


def test_enrichment_leaves_unknown_models_unknown():
    from daemon.models import enrich

    enriched = enrich(RemoteModel(model_id="totally-unknown-model",
                                  provider="example"))
    assert enriched.capabilities.context_window is None
    assert enriched.capabilities.tool_calling is None


def test_recommend_prefers_tools_for_coding():
    models = [
        _model("no-tools", tool_calling=False, context_window=64_000),
        _model("coder", tool_calling=True, context_window=128_000,
               output_price_per_mtok=1.0),
    ]
    ranking = recommend(models, "coding")
    assert ranking[0].model.model_id == "coder"
    assert any("no tool calling" in reason for reason in ranking[1].reasons)


def test_recommend_reasons_mention_unknowns():
    ranking = recommend([_model("mystery")], "coding")
    assert any("unverified" in reason or "unknown" in reason
               for reason in ranking[0].reasons)


def test_recommend_flags_expensive_models():
    ranking = recommend([_model("premium", tool_calling=True,
                                context_window=200_000,
                                output_price_per_mtok=75.0)], "coding")
    assert any("expensive" in reason for reason in ranking[0].reasons)


def test_cost_estimation():
    model = _model("priced", input_price_per_mtok=2.5, output_price_per_mtok=10.0)
    cost = estimate_cost_usd(model, Usage(input_tokens=1_000_000,
                                          output_tokens=500_000))
    assert cost == pytest_approx(2.5 + 5.0)


def test_cost_estimation_none_without_pricing():
    assert estimate_cost_usd(_model("mystery"), Usage(100, 100)) is None


def test_registry_cache_roundtrip(home):
    registry = ModelRegistry(home)
    models = [_model("cached-model", context_window=128_000)]
    registry._write_cache("profile-x", models)
    loaded = registry.cached("profile-x")
    assert loaded and loaded[0].model_id == "cached-model"
    assert loaded[0].capabilities.context_window == 128_000


def test_registry_cache_expires(home, monkeypatch):
    registry = ModelRegistry(home)
    registry._write_cache("profile-y", [_model("old")])
    import time

    future = time.time() + 10 * 24 * 3600
    monkeypatch.setattr(time, "time", lambda: future)
    assert registry.cached("profile-y") is None


def test_pick_role_model_uses_config(config):
    config.set_user("models.coding", "mock-coder")
    models = [_model("mock-coder"), _model("mock-fast")]
    assert pick_role_model(config, "coding", models).model_id == "mock-coder"


def test_pick_role_model_fails_loudly_on_missing(config):
    config.set_user("models.coding", "not-there")
    import pytest

    from daemon.errors import ModelError

    with pytest.raises(ModelError):
        pick_role_model(config, "coding", [_model("a")])


def pytest_approx(value, tol=1e-6):
    class Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol

    return Approx()
