"""Structured measurements travel through actual tool artifacts, not prose parsing."""
from langchain_core.messages import ToolMessage

from tradingagents.agents.evidence import collect_tool_evidence
from tradingagents.dataflows.facts import Fact, SourceResult


def test_evidence_preserves_fact_artifact_separate_from_bounded_source_text():
    fact = Fact(key='net', label='净增', value='-1454.6', unit='万户', period='2026Q3', basis='quarter flow')
    content, artifact = SourceResult('Long raw text ' * 5000, (fact,)).as_tool_output()
    row = next(iter(collect_tool_evidence('fundamentals', [ToolMessage(
        content, name='get_company_operating_history', tool_call_id='x', artifact=artifact,
    )]).values()))
    assert row['fact_schema_version'] == 1
    assert Fact.model_validate(row['facts'][0]) == fact
    assert len(row['content']) < len(content)
