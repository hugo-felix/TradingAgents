"""Allocation and execution are separate judgments, not orders sent to a broker."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DecisionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    allocation_horizon: str = Field(min_length=1, description='Explicit allocation horizon; do not substitute next-day price direction.')
    execution_mode: Literal['conditional', 'wait', 'immediate'] = Field(description='Conditional triggers, wait/no action, or explicitly justified immediate action.')
    execution_conditions: str = Field(min_length=1, description='Complete triggers, stages and defensive conditions. No invented prices or automatic execution.')
    valid_until: str = Field(min_length=1, description='Execution plan expiry or explicit review event, separate from allocation horizon. State unknown if unsupported.')
    invalidation_conditions: str = Field(min_length=1, description='Observable evidence that invalidates the thesis or cancels the plan.')
    counterevidence: str = Field(min_length=1, description='Strongest contrary evidence and why it does or does not change the allocation.')
    catalysts: str = Field(min_length=1, description='Known upcoming/ongoing events, dates and source references; explicitly state calendar coverage gaps.')
    benchmark_context: str = Field(min_length=1, description='Same-date market/sector context from sources, or explicitly unavailable. No invented relative returns.')

    fact_ids: list[str] = Field(default_factory=list, description='Original available fact IDs supporting the plan, prices and contrary evidence; never invented.')
    source_ids: list[str] = Field(default_factory=list, description='Original usable source IDs supporting event dates and context.')

    @field_validator('allocation_horizon', 'execution_conditions', 'valid_until', 'invalidation_conditions',
                     'counterevidence', 'catalysts', 'benchmark_context')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('decision_plan fields cannot be blank')
        return value


PLAN_LABELS = (
    ('allocation_horizon', '配置期限'), ('execution_mode', '执行方式'),
    ('execution_conditions', '执行条件'), ('valid_until', '计划有效期'),
    ('invalidation_conditions', '失效条件'), ('counterevidence', '最强反向证据'),
    ('catalysts', '事件窗口'), ('benchmark_context', '市场与板块'),
)
MODES = {'conditional': '条件触发', 'wait': '等待，不执行', 'immediate': '即时执行建议，须核查依据'}


def render_decision_plan(plan: DecisionPlan | None) -> str:
    import html
    lines = []
    for key, label in PLAN_LABELS:
        value = getattr(plan, key) if plan else '未提供，需复核'
        if plan and key == 'execution_mode':
            value = MODES[value]
        lines.append(f'**{label}**：' + html.escape(value, quote=False))
    return '\n\n'.join(lines)


DECISION_DISCIPLINE = """
Decision discipline:
- Separate allocation horizon, next-day direction and conditional execution. A cautious allocation
  is not a next-day decline forecast and does not itself justify an immediate market order.
- Researchers, trader and risk analysts sharing sources are not independent votes. Repeated opinions
  or a unanimous debate are not new evidence. Explain changes from the original analyst views.
- Reconcile year-to-date and standalone quarter performance on compatible bases. Do not claim
  accelerating deterioration from a cumulative decline when the latest quarter improved or stabilized.
- A procurement award establishes a buyer/supplier relationship, not current-period expense recognition.
  Distinguish investment outlays, future growth expectations and realized issuer revenue.
- Attention rank and sparse social discussion do not measure marginal buyers or price direction.
- Distinguish known facts from what is already priced in; if expectations are unavailable, say so.
  Same-date market/sector evidence is required for relative-strength claims; otherwise mark it unavailable.
- Retain the strongest counterevidence. Never invent prices, event dates, benchmark returns, or units.
  Numeric source measurements are rendered in the source appendix; retain fact/source references.
- Review previously announced events still upcoming or ongoing, not just recently published headlines.
  Missing event coverage is not evidence of no catalysts. Future-publication content is unavailable as of analysis.
"""


def original_analyst_views(state) -> str:
    """Give the judges direct access to original views, not only a debate's digest."""
    labels = (('market_report', 'Technical'), ('fundamentals_report', 'Fundamentals'),
              ('news_report', 'News'), ('sentiment_report', 'Sentiment'))
    return '\n\n'.join(['Original analyst views (interpretations, not independent source confirmations):',
                         *[f'{label}:\n{state.get(key) or "Not provided"}' for key, label in labels]])
