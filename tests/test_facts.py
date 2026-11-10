"""Source facts keep numbers and their interpretation bases together."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.facts import Fact, FactProvenance, SourceResult, render_facts


@pytest.mark.parametrize('changes', [
    {'period_start': '2027-01-01'},
    {'fetched_at': '2026-01-01T12:00:00'},
    {'published_date': '2026-01-01', 'first_published_date': '2026-01-02'},
    {'snapshot_sha256': 'not-a-hash'},
])
def test_provenance_rejects_ambiguous_or_inconsistent_metadata(changes):
    with pytest.raises(ValidationError):
        FactProvenance.model_validate({
            'period_end': '2025-12-31', 'snapshot_sha256': 'a' * 64, **changes})


def fact(**changes):
    return Fact(key='connections.net_additions', label='连接净增', value='-1454.6',
                unit='万户', period='2026Q3', basis='quarter flow', **changes)


def test_exact_values_and_units_survive_artifact_serialization():
    original = fact(qualifiers=('期末存量与期间净增分别列示',))
    content, artifact = SourceResult('Original source', (original,)).as_tool_output()
    restored = Fact.model_validate(artifact['facts'][0])
    assert content == 'Original source'
    assert restored.value == Decimal('-1454.6')
    assert restored.unit == '万户' and restored.period == '2026Q3'
    assert restored.qualifiers == original.qualifiers
    assert '-1454.6' in render_facts({'source#net': restored})


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_numbers_cannot_become_available_facts(value):
    with pytest.raises(ValidationError):
        Fact(key='metric', label='Metric', value=value, unit='CNY', period='2026H1', basis='reported')


def test_unknown_is_distinct_from_zero_and_cannot_carry_a_number():
    unknown = Fact(key='capex.plan', label='资本开支计划', value=None, unit='CNY',
                   period='2026FY', basis='plan', status='unverified')
    assert 'unverified' in render_facts({'source#plan': unknown})
    with pytest.raises(ValidationError):
        Fact.model_validate({**unknown.model_dump(), 'value': 0})
    zero = Fact(key='metric', label='Metric', value=0, unit='万户', period='2026Q3', basis='quarter flow')
    assert zero.value == 0 and zero.status == 'available'


def test_source_fact_keys_are_unique():
    with pytest.raises(ValueError, match='unique'):
        SourceResult('Source', (fact(), fact()))
