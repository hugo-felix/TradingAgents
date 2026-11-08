"""Grounded reports validate references; original numeric artifacts stay separate."""
import json
from types import SimpleNamespace

import pytest

from tradingagents.agents import grounding as g


def evidence():
    return {'filing': {'tool': 'filing', 'args': {}, 'status': 'available',
                       'content': 'original disclosure', 'facts': [
        {'key': 'net', 'label': '5G净增', 'value': '-1454.60', 'unit': '万户', 'period': '2026Q3',
             'basis': 'quarter flow', 'qualifiers': ['期末存量不等于期间净增']},
        {'key': 'plan', 'label': '开支计划', 'value': None, 'unit': 'CNY', 'period': '2026FY',
             'basis': 'plan', 'status': 'unverified', 'qualifiers': ['未披露，不得假定为零']}]}}


def payload(**changes):
    return dict(summary='经营变化仍需观察', points=[{'kind': 'analysis', 'text': '连接变化可能压制增长',
                'fact_ids': ['filing#net'], 'source_ids': []}], **changes)


def test_internal_report_retains_fact_references_and_original_values_stay_in_evidence():
    rows = evidence()
    report = g.validate_grounded_report(payload(), rows)
    rendered = g.render_grounded_report(report, rows)
    assert 'filing#net' in rendered and '推断' in rendered
    facts = g.facts_from_evidence(rows)
    assert str(facts['filing#net'].value) == '-1454.60'
    assert facts['filing#plan'].status == 'unverified'
    assert '期末存量不等于期间净增' in facts['filing#net'].qualifiers


@pytest.mark.parametrize('text', ['5G客户结构仍需观察', '2026Q3改善', '600941.SS 的客户结构', '增长10%', '增长１０％'])
def test_numeric_prose_is_not_confused_with_machine_verified_values(text):
    report = g.validate_grounded_report({**payload(), 'summary': text}, evidence())
    assert report.summary == text
    assert '推断' in g.render_grounded_report(report, evidence())


@pytest.mark.parametrize('change', [{'fact_ids': ['invented']}, {'source_ids': ['invented']},
                                    {'counter_fact_ids': ['invented']}, {'fact_ids': ['filing#plan']}])
def test_references_and_unknown_facts_cannot_be_proven_analysis(change):
    raw = payload()
    raw['points'][0].update(change)
    with pytest.raises(ValueError):
        g.validate_grounded_report(raw, evidence())


def test_limitation_can_reference_unknown_and_source_only_is_inference():
    raw = payload()
    raw['points'] = [{'kind': 'limitation', 'text': '计划尚缺证据', 'fact_ids': ['filing#plan']},
                     {'kind': 'analysis', 'text': '消息可能影响预期', 'source_ids': ['filing']}]
    report = g.validate_grounded_report(raw, evidence())
    assert '推断' in g.render_grounded_report(report, evidence())


@pytest.mark.parametrize('status', ['withheld', 'unavailable', 'error', 'empty'])
def test_known_unusable_source_can_only_explain_coverage_gap(status):
    rows = {'macro': {'status': status, 'content': 'Historical vintage unavailable', 'facts': []}}
    raw = {'summary': '宏观覆盖不足', 'points': [
        {'kind': 'limitation', 'text': '无法确认当时版本，不能判断宏观趋势', 'source_ids': ['macro']}]}
    report = g.validate_grounded_report(raw, rows)
    assert '证据局限' in g.render_grounded_report(report, rows)
    assert '来源文本解读' not in g.render_grounded_report(report, rows)
    for kind in ('analysis', 'scenario'):
        raw['points'][0]['kind'] = kind
        with pytest.raises(ValueError, match='unusable source'):
            g.validate_grounded_report(raw, rows)
    raw['points'][0].update(kind='limitation', source_ids=['invented'])
    with pytest.raises(ValueError, match='unknown or unusable source'):
        g.validate_grounded_report(raw, rows)


def test_ratings_require_fact_basis_and_exact_allowed_enum():
    for rating in ['BUY', None, 'Overweight']:
        with pytest.raises(ValueError):
            g.validate_grounded_report(payload(rating=rating), evidence(), ('Buy', 'Hold', 'Sell'))
    raw = payload(rating='Buy')
    raw['points'] = [{'kind': 'analysis', 'text': '消息可能改善预期', 'source_ids': ['filing']}]
    with pytest.raises(ValueError, match='available fact'):
        g.validate_grounded_report(raw, evidence(), ('Buy', 'Hold', 'Sell'))
    assert g.validate_grounded_report(payload(rating='Buy'), evidence(), ('Buy', 'Hold')).rating == 'Buy'


@pytest.mark.parametrize('status,content', [('error', 'some text'), ('empty', ''), ('available', '  ')])
def test_unusable_source_cannot_ground_prose_or_values(status, content):
    rows = evidence()
    rows['filing'].update(status=status, content=content)
    raw = payload()
    raw['points'] = [{'kind': 'analysis', 'text': '消息可能改善预期', 'source_ids': ['filing']}]
    with pytest.raises(ValueError):
        g.validate_grounded_report(raw, rows)
    with pytest.raises(ValueError):
        g.validate_grounded_report(payload(), rows)


def test_malformed_or_duplicate_facts_fail_closed():
    rows = evidence()
    rows['filing']['facts'][0].pop('unit')
    with pytest.raises(ValueError):
        g.facts_from_evidence(rows)
    rows = evidence()
    rows['filing']['facts'].append(rows['filing']['facts'][0])
    with pytest.raises(ValueError, match='duplicate'):
        g.facts_from_evidence(rows)


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=value)


def test_single_repair_then_fail_closed_never_publishes_invalid_draft():
    llm = Model(['INVALID DRAFT'])
    result = g.finish_grounded_report(llm, 'INVALID DRAFT', evidence(), ('Buy', 'Hold', 'Sell'))
    assert len(llm.calls) == 1
    assert result.status == 'failed' and result.rating == 'REVIEW'
    assert 'INVALID DRAFT' not in result.content
    assert '-1454.60' not in result.content and result.issues


def test_repair_can_succeed_and_initial_generation_has_two_attempt_bound():
    llm = Model(['bad', json.dumps(payload(rating='Buy'), ensure_ascii=False)])
    result = g.invoke_grounded_report(llm, 'Analyze', evidence(), ('Buy', 'Hold', 'Sell'))
    assert result.status == 'passed' and result.repaired and result.rating == 'Buy'
    assert len(llm.calls) == 2


def test_rounded_ratio_fails_and_qualitative_repair_keeps_original_fact():
    rows = {'market': {'tool': 'snapshot', 'args': {}, 'status': 'available',
                      'content': 'verified volume comparison', 'facts': [
                          {'key': 'volume_ratio', 'label': 'Volume comparison',
                           'value': '1.258963002196035583162184908', 'unit': 'ratio',
                           'period': '2026-11-06', 'basis': 'previous seven sessions'}]}}
    raw = {'summary': '量能值得关注', 'points': [
        {'kind': 'analysis', 'text': '量比为1.26倍', 'fact_ids': ['market#volume_ratio']}]}
    with pytest.raises(ValueError, match='measurement value/unit'):
        g.validate_grounded_report(raw, rows, verify_measurements=True)
    fixed = {**raw, 'points': [{**raw['points'][0], 'text': '量能较近期均量放大，需观察持续性'}]}
    llm = Model([json.dumps(fixed, ensure_ascii=False)])
    result = g.finish_grounded_report(llm, raw, rows, verify_measurements=True)
    assert result.status == 'passed' and result.repaired
    assert '1.26倍' not in result.content
    assert 'market#volume_ratio' in result.content
    assert rows['market']['facts'][0]['value'] == '1.258963002196035583162184908'


def test_no_model_call_when_valid_and_repair_exception_is_closed():
    llm = Model([])
    result = g.finish_grounded_report(llm, payload(), evidence())
    assert result.status == 'passed' and not result.repaired and not llm.calls
    llm = Model([RuntimeError('provider failed')])
    result = g.finish_grounded_report(llm, 'bad', evidence())
    assert result.status == 'failed'


def test_schema_contract_explains_references_and_semantic_scope():
    instruction = g.grounding_instruction(evidence())
    for term in ['qualitative', 'untrusted', 'fact_ids', 'counter_fact_ids', 'filing#net', 'not machine-verified']:
        assert term in instruction


def test_prompt_catalog_shares_metadata_without_losing_fact_semantics():
    rows = evidence()
    original = rows['filing']['facts'][0]
    original['source_url'] = 'https://example.com/disclosure'
    rows['filing']['facts'].append({**original, 'key': 'other', 'value': '0.00'})
    facts = g.facts_from_evidence(rows)
    catalog = json.loads(g._table(facts))
    assert len(catalog['metadata']) == 2
    assert g._table(facts).count(original['source_url']) == 1
    for row in catalog['facts']:
        fact_id, label, value, period, status, index = row
        unit, currency, basis, qualifiers, url = catalog['metadata'][index]
        fact = facts[fact_id]
        assert [label, value, period, status] == [fact.label,
            format(fact.value, 'f') if fact.value is not None else None, fact.period, fact.status]
        assert [unit, currency, basis, qualifiers, url] == [fact.unit, fact.currency,
            fact.basis, list(fact.qualifiers), fact.source_url]


def test_fact_keys_may_contain_reference_separator():
    rows = evidence()
    rows['filing']['facts'][0]['key'] = 'net#flow'
    raw = payload()
    raw['points'][0]['fact_ids'] = ['filing#net#flow']
    assert g.validate_grounded_report(raw, rows)


def test_summary_and_point_schema_cannot_be_bypassed_by_constructed_models():
    invalid = g.GroundedReport.model_construct(summary='Good', points=[], rating=None)
    with pytest.raises(ValueError):
        g.validate_grounded_report(invalid, evidence())


def test_evidence_failure_does_not_publish_values_as_verified():
    rows = evidence()
    rows['filing']['status'] = 'error'
    result = g.finish_grounded_report(Model([]), 'bad', rows, ('Buy', 'Hold', 'Sell'))
    assert result.status == 'failed' and '-1454.60' not in result.content


@pytest.mark.parametrize('version', [2, '1', True, None])
def test_declared_unknown_artifact_schema_versions_fail_closed(version):
    rows = evidence()
    rows['filing']['fact_schema_version'] = version
    with pytest.raises(ValueError, match='schema version'):
        g.facts_from_evidence(rows)
    result = g.finish_grounded_report(Model([]), payload(), rows)
    assert result.status == 'failed' and '-1454.60' not in result.content


def test_declared_schema_requires_fact_list():
    rows = evidence()
    rows['filing']['fact_schema_version'] = 1
    rows['filing'].pop('facts')
    with pytest.raises(ValueError, match='fact list'):
        g.facts_from_evidence(rows)


@pytest.mark.parametrize('status', ['missing', 'unverified'])
@pytest.mark.parametrize('source_status', ['unavailable', 'error', 'empty'])
def test_unknown_fact_on_unusable_source_can_explain_limitation(status, source_status):
    rows = evidence()
    rows['filing']['status'] = source_status
    rows['filing']['facts'] = [{**rows['filing']['facts'][1], 'status': status}]
    raw = {'summary': '覆盖不足，无法形成方向判断', 'rating': 'REVIEW', 'points': [
        {'kind': 'limitation', 'text': '披露信息尚无法取得', 'fact_ids': ['filing#plan']}]}
    report = g.validate_grounded_report(raw, rows, ('Buy', 'Hold', 'Sell'))
    assert 'filing#plan' in g.render_grounded_report(report, rows)
    assert g.facts_from_evidence(rows)['filing#plan'].qualifiers == ('未披露，不得假定为零',)
    raw['points'][0]['kind'] = 'analysis'
    with pytest.raises(ValueError, match='requires limitation'):
        g.validate_grounded_report(raw, rows, ('Buy', 'Hold', 'Sell'))


def test_repair_receives_original_source_and_task_when_no_numeric_facts_exist():
    from unittest.mock import Mock
    rows = {'news': {'tool': 'news', 'args': {}, 'status': 'available',
                     'content': 'Original tender notice: supplier award, no accounting recognition evidence.'}}
    llm = Mock(spec=['invoke'])
    llm.invoke.return_value = json.dumps({'summary': 'The notice requires verification.', 'points': [
        {'kind': 'analysis', 'text': 'An award does not establish cost recognition.', 'source_ids': ['news']}]})
    result = g.finish_grounded_report(llm, 'invalid draft', rows,
                                     task_context='Analyze issuer news, distinguishing supplier awards from issuer sales.')
    assert result.status == 'passed' and result.repaired
    prompt = llm.invoke.call_args.args[0]
    assert rows['news']['content'] in prompt
    assert 'distinguishing supplier awards from issuer sales' in prompt


def test_prose_accepts_metric_names_dates_and_qualified_ticker_without_repair():
    raw = payload()
    raw['summary'] = '600941.SS 的5G客户结构在2026Q3仍需观察'
    llm = Model([])
    result = g.finish_grounded_report(llm, raw, evidence())
    assert result.status == 'passed' and not result.repaired and not llm.calls


def test_internal_report_keeps_references_without_copying_fact_tables():
    result = g.finish_grounded_report(Model([]), payload(), evidence())
    assert result.status == 'passed'
    assert 'filing#net' in result.content
    assert '| Fact ID |' not in result.content
    assert '-1454.60' not in result.content

@pytest.mark.parametrize('fence', ['```json\n{}\n```', '```\n{}\n```'])
def test_whole_json_fence_is_validated_without_a_repair(fence):
    raw = fence.replace('{}', json.dumps(payload(), ensure_ascii=False))
    model = Model([])
    result = g.finish_grounded_report(model, raw, evidence())
    assert result.status == 'passed'
    assert not model.calls


@pytest.mark.parametrize('raw', ['explanation\n```json\n{}\n```', '```json\n{}', '{} trailing'])
def test_fence_tolerance_does_not_accept_prose_or_truncation(raw):
    with pytest.raises(ValueError):
        g.validate_grounded_report(raw.replace('{}', json.dumps(payload())), evidence())


def test_report_generation_and_repair_use_provider_schema_mode():
    from langchain_core.messages import AIMessage

    class StructuredModel(Model):
        def with_structured_output(self, schema, **kwargs):
            assert schema is g.GroundedReport
            assert kwargs == {'include_raw': True}
            return SimpleNamespace(invoke=lambda prompt: {
                'raw': AIMessage(content=self.invoke(prompt).content), 'parsed': None,
            })

    model = StructuredModel(['not json', json.dumps(payload())])
    result = g.invoke_grounded_report(model, 'research', evidence())
    assert result.status == 'passed' and result.repaired
    assert len(model.calls) == 2


def test_raw_truncated_json_is_rejected_even_if_provider_parser_accepts_it():
    from langchain_core.messages import AIMessage

    class PartialModel:
        def with_structured_output(self, schema, **kwargs):
            return SimpleNamespace(invoke=lambda prompt: {
                'raw': AIMessage(content=json.dumps(payload())[:-1]),
                'parsed': g.GroundedReport.model_validate(payload()),
            })

    result = g.invoke_grounded_report(PartialModel(), 'research', evidence())
    assert result.status == 'failed'


def test_function_calling_report_is_still_checked_against_source_references():
    from langchain_core.messages import AIMessage

    class ToolModel:
        def with_structured_output(self, schema, **kwargs):
            raw = {**payload(), 'points': [
                {'kind': 'analysis', 'text': 'Unsupported interpretation', 'fact_ids': ['invented#id']}]}
            return SimpleNamespace(invoke=lambda prompt: {
                'raw': AIMessage(content='', tool_calls=[{
                    'name': 'GroundedReport', 'args': raw, 'id': 'report',
                }]),
                'parsed': g.GroundedReport.model_validate(raw),
            })

    result = g.invoke_grounded_report(ToolModel(), 'research', evidence())
    assert result.status == 'failed'
    assert 'unknown fact ID' in result.issues[-1]


@pytest.mark.parametrize('truncated', [False, True])
def test_invalid_function_arguments_reach_validation_and_repair_unchanged(truncated):
    from langchain_core.messages import AIMessage

    invalid = {**payload(), 'points': payload()['points'] * 9}
    arguments = json.dumps(invalid)
    if truncated:
        arguments = arguments[:-1]

    class ToolModel:
        def with_structured_output(self, schema, **kwargs):
            return SimpleNamespace(invoke=lambda prompt: {
                'raw': AIMessage(content='', additional_kwargs={'tool_calls': [{
                    'id': 'report', 'type': 'function', 'function': {
                        'name': 'GroundedReport', 'arguments': arguments,
                    },
                }]}),
                'parsed': None,
            })

    draft = g.invoke_report_model(ToolModel(), 'research').content
    assert draft == arguments
    with pytest.raises(ValueError, match='Invalid grounded report JSON/schema'):
        g.validate_grounded_report(draft, evidence())
