"""Граница ответа модели и фактов: дополнительные сценарии FR-1."""

import json

import pytest

from assistant.graphrag.extraction import extract_chunk
from assistant.infra.llm import LLMError
from assistant.infra.schemas import EXTRACTION_SCHEMA, Chunk
from assistant.infra.testing import FakeLLM

CHUNK = Chunk("example.md#0", "example.md", "Деталь P-1042: пусковое реле. Вместо неё P-1042B.")
RELATION = {
    "subject_type": "Part", "subject": "P-1042", "type": "REPLACED_BY",
    "object_type": "Part", "object": "P-1042B",
}


def response(entities=(), relations=()):
    return json.dumps({"entities": list(entities), "relations": list(relations)})


def test_relation_endpoints_are_grounded_even_when_entities_list_is_empty():
    result = extract_chunk(CHUNK, FakeLLM([response(relations=[RELATION])]))
    assert {e.node_id for e in result.entities} == {"Part:P-1042", "Part:P-1042B"}
    assert len(result.relations) == 1 and result.dropped == 0


def test_known_but_absent_name_and_its_relation_are_counted_separately():
    result = extract_chunk(CHUNK, FakeLLM([response(
        entities=[{"type": "Supplier", "name": "ВентПром"}],
        relations=[dict(RELATION, type="SUPPLIED_BY", object_type="Supplier", object="ВентПром")],
    )]))
    assert result.entities == [] and result.relations == [] and result.dropped == 2


def test_relation_with_wrong_direction_is_dropped_without_retry():
    llm = FakeLLM([response(relations=[dict(RELATION, subject_type="Model", subject="P-1042")])])
    result = extract_chunk(CHUNK, llm)
    assert result.relations == [] and result.dropped == 1 and len(llm.requests) == 1


def test_normalized_duplicates_are_not_counted_as_rejected_facts():
    result = extract_chunk(CHUNK, FakeLLM([response(
        entities=[{"type": "Part", "name": "P-1042"}, {"type": "Part", "name": "p 1042"}],
        relations=[RELATION, RELATION],
    )]))
    assert len(result.entities) == 2 and len(result.relations) == 1 and result.dropped == 0


@pytest.mark.parametrize("bad", [
    response(entities=[{"type": "Part", "name": 1042}]),
    response(relations=[dict(RELATION, object=None)]),
    response(relations=[{}]),
])
def test_bad_field_types_retry_the_whole_response(bad):
    llm = FakeLLM([bad, response()])
    result = extract_chunk(CHUNK, llm)
    assert result.entities == [] and result.dropped == 0 and len(llm.requests) == 2
    assert llm.requests[0]["messages"] != llm.requests[1]["messages"]


def test_empty_but_valid_response_does_not_retry():
    llm = FakeLLM([response()])
    assert extract_chunk(CHUNK, llm).entities == [] and len(llm.requests) == 1


def test_prompt_explains_every_signature_and_uses_the_exact_schema():
    from assistant.infra.schemas import RELATION_SIGNATURES

    llm = FakeLLM([response()])
    extract_chunk(CHUNK, llm)
    request = llm.requests[0]
    prompt = "\n".join(m["content"] for m in request["messages"])
    for kind, (subject, obj) in RELATION_SIGNATURES.items():
        assert f"{subject} -{kind}-> {obj}" in prompt
    assert "Пример" in prompt and CHUNK.text in prompt
    assert request["json_schema"] is EXTRACTION_SCHEMA


def test_model_transport_failure_is_not_reported_as_valid_empty_extraction():
    class OfflineLLM:
        def chat(self, *args, **kwargs):
            raise LLMError("offline")

    with pytest.raises(LLMError, match="offline"):
        extract_chunk(CHUNK, OfflineLLM())


def test_two_invalid_answers_log_the_failed_chunk(caplog):
    result = extract_chunk(CHUNK, FakeLLM(["broken", "broken"]))
    assert result.entities == [] and CHUNK.chunk_id in caplog.text
    assert "extraction_failed" in caplog.text


@pytest.mark.parametrize("script, attempts, status, errors", [
    (["broken", response()], 2, "ok", 1),
    (["broken", "broken"], 2, "invalid_response", 2),
])
def test_diagnostics_distinguish_recovery_from_failure(script, attempts, status, errors):
    from assistant.graphrag.extraction import extract_with_diagnostics

    result, diagnostics = extract_with_diagnostics(CHUNK, FakeLLM(script))
    assert (diagnostics.attempts, diagnostics.status) == (attempts, status)
    assert len(diagnostics.parse_errors) == errors
    assert result.dropped == len(diagnostics.rejected_entities) + len(diagnostics.rejected_relations)


def test_extraction_explicitly_increases_the_output_budget():
    class RecordingLLM(FakeLLM):
        def chat(self, *args, **kwargs):
            self.output_budget = kwargs["max_tokens"]
            return super().chat(*args, **kwargs)

    llm = RecordingLLM([response()])
    extract_chunk(CHUNK, llm)
    assert llm.output_budget == 8192


def test_a_part_cannot_replace_itself_even_if_both_spellings_are_grounded():
    from assistant.graphrag.extraction import extract_with_diagnostics

    result, diagnostics = extract_with_diagnostics(CHUNK, FakeLLM([
        response(relations=[dict(RELATION, object="p 1042")])
    ]))
    assert result.relations == [] and result.dropped == 1
    assert diagnostics.rejected_relations[0]["reason"] == "self_replacement"


@pytest.mark.parametrize("invalid", [
    response(entities=[{"type": "Unknown", "name": "P-1042"}]),
    response(relations=[dict(RELATION, subject_type="Unknown")]),
    response(relations=[dict(RELATION, object_type="Unknown")]),
    response(relations=[dict(RELATION, type="LIKES")]),
])
def test_unknown_schema_enum_requires_a_retry_instead_of_fact_filtering(invalid):
    llm = FakeLLM([invalid, response(relations=[RELATION])])
    result = extract_chunk(CHUNK, llm)
    assert len(llm.requests) == 2 and len(result.relations) == 1 and result.dropped == 0
