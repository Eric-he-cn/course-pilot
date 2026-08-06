"""模型接入协议的选择：配置怎么读、装配时挑哪个适配器。"""
from __future__ import annotations

import pytest

from adapters.llm import OpenAICompatibleChat, ResponsesApiChat
from app.bootstrap import RESPONSES_THINKING_TIERS, THINKING_TIERS, build_shared_runtime
from core.settings import ModelChoice, Settings, _read_models


def _env(**values: str):
    return lambda name, default="": values.get(name, default)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        data_dir=tmp_path, database_path=tmp_path / "db.sqlite", uploads_dir=tmp_path / "materials",
        text_provider="vendor", text_base_url="https://api.example.com", text_api_key="k",
        text_model="m", enable_remote_llm=True, chunk_size=600, chunk_overlap=120, top_k_results=6,
        rag_embedding_model="", rag_reranker_model="", **overrides,
    )


def test_protocol_defaults_to_chat():
    """不配这一项时行为与加它之前完全一致。"""
    models = _read_models(_env(TEXT_MODEL="m"))
    assert [choice.protocol for choice in models] == ["chat"]


def test_protocol_is_read_and_inherited_by_later_slots():
    models = _read_models(_env(TEXT_MODEL="m", TEXT_PROTOCOL="responses", TEXT_MODEL_2="m2",
                               TEXT_MODEL_3="m3", TEXT_PROTOCOL_3="chat"))
    assert [choice.protocol for choice in models] == ["responses", "responses", "chat"]


@pytest.mark.parametrize("env,name", [
    ({"TEXT_MODEL": "m", "TEXT_PROTOCOL": "grpc"}, "TEXT_PROTOCOL"),
    ({"TEXT_MODEL": "m", "TEXT_MODEL_2": "m2", "TEXT_PROTOCOL_2": "Responses API"}, "TEXT_PROTOCOL_2"),
])
def test_an_unknown_protocol_fails_at_startup_with_the_variable_name(env, name):
    with pytest.raises(ValueError) as caught:
        _read_models(_env(**env))
    assert name in str(caught.value) and "responses" in str(caught.value)


def test_the_single_model_fallback_carries_the_protocol(tmp_path):
    """直接构造 Settings（测试、脚本）时不填 text_models，兜出来的那个也要带上协议。"""
    assert _settings(tmp_path).models[0].protocol == "chat"
    assert _settings(tmp_path, text_protocol="responses").models[0].protocol == "responses"


def test_the_default_thinking_tier_reads_the_field_its_protocol_uses():
    responses = ModelChoice(key="1", label="模型一", provider="v", base_url="u", api_key="k", model="m",
                            protocol="responses", extra_body={"reasoning": {"effort": "none"}})
    assert responses.thinking_tier == "off"
    # Chat 那套的 thinking 字段在 Responses 协议下不代表任何档位。
    stale = ModelChoice(key="1", label="模型一", provider="v", base_url="u", api_key="k", model="m",
                        protocol="responses", extra_body={"thinking": {"type": "disabled"}})
    assert stale.thinking_tier == "adaptive"


def test_both_protocols_cover_every_thinking_tier():
    """档位 key 是前端下拉与请求共用的，两张表必须一一对应，否则切档会 KeyError。"""
    assert set(RESPONSES_THINKING_TIERS) == set(THINKING_TIERS)


def test_chat_stays_the_default_adapter(tmp_path):
    runtime = build_shared_runtime(_settings(tmp_path))
    assert all(isinstance(responder, OpenAICompatibleChat) for responder in runtime.responders.values())
    assert isinstance(runtime.classifier, OpenAICompatibleChat)


def test_responses_protocol_selects_the_responses_adapter(tmp_path):
    runtime = build_shared_runtime(_settings(tmp_path, text_protocol="responses"))
    assert runtime.responders and all(isinstance(r, ResponsesApiChat) for r in runtime.responders.values())
    assert isinstance(runtime.classifier, ResponsesApiChat)
    # 思考档位跟着协议换字段，否则界面上的开关在这条协议下是摆设。
    assert runtime.responders[("1", "off")]._extra_body == {"reasoning": {"effort": "none"}}
    assert runtime.responders[("1", "adaptive")]._extra_body == {}


def test_a_tier_overrides_the_effort_and_keeps_the_rest(tmp_path):
    """档位只管 effort 这一个键。adaptive 是「让服务端自己定」，连 effort 都不发；
    其余档位覆盖它——两种情况下 reasoning 下的其他字段都要留着，整块替换会把它们丢掉。"""
    settings = _settings(tmp_path, text_protocol="responses",
                         text_extra_body={"reasoning": {"effort": "medium", "summary": "auto"}})
    runtime = build_shared_runtime(settings)
    assert runtime.responders[("1", "adaptive")]._extra_body == {"reasoning": {"summary": "auto"}}
    assert runtime.responders[("1", "high")]._extra_body == {"reasoning": {"summary": "auto", "effort": "high"}}
    assert runtime.responders[("1", "off")]._extra_body == {"reasoning": {"summary": "auto", "effort": "none"}}


def test_a_stale_thinking_field_is_reported_when_switching_protocol(tmp_path, caplog):
    """切到 Responses 之后 thinking 字段被服务端静默忽略，不说一声用户查不出来。"""
    settings = _settings(tmp_path, text_protocol="responses",
                         text_extra_body={"thinking": {"type": "disabled"}})
    with caplog.at_level("WARNING"):
        build_shared_runtime(settings)
    assert any("thinking" in record.getMessage() and "reasoning.effort" in record.getMessage()
               for record in caplog.records)


def test_the_two_protocols_can_be_mixed_across_slots(tmp_path):
    settings = _settings(tmp_path, text_models=(
        ModelChoice(key="1", label="模型一", provider="v", base_url="https://api.example.com",
                    api_key="k", model="m"),
        ModelChoice(key="2", label="模型二", provider="v", base_url="https://api.example.com",
                    api_key="k", model="m2", protocol="responses"),
    ))
    runtime = build_shared_runtime(settings)
    assert isinstance(runtime.responders[("1", "high")], OpenAICompatibleChat)
    assert isinstance(runtime.responders[("2", "high")], ResponsesApiChat)


def test_the_commentary_split_reaches_the_responses_adapters(tmp_path):
    """开关默认开、配 0 关掉，两头都要真的传到适配器上——接线断了行为静默不变。"""
    on = build_shared_runtime(_settings(tmp_path, text_protocol="responses"))
    off = build_shared_runtime(_settings(tmp_path, text_protocol="responses",
                                         text_commentary_to_reasoning=False))
    assert all(responder._commentary_to_reasoning for responder in on.responders.values())
    assert not any(responder._commentary_to_reasoning for responder in off.responders.values())


def test_the_commentary_switch_is_read_from_the_environment(monkeypatch, tmp_path):
    """不配就是开着；配 0 关掉。空目录当 project_root，别读到本机 .env。"""
    monkeypatch.delenv("TEXT_COMMENTARY_TO_REASONING", raising=False)
    assert Settings.from_environment(tmp_path).text_commentary_to_reasoning
    monkeypatch.setenv("TEXT_COMMENTARY_TO_REASONING", "0")
    assert not Settings.from_environment(tmp_path).text_commentary_to_reasoning
