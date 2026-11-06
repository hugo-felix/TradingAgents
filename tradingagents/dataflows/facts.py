"""Typed source measurements shared by tool artifacts and report rendering."""
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Fact(BaseModel):
    """A value always travels with its unit, period, basis and qualifications."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: Decimal | None
    unit: str = Field(min_length=1)
    period: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    currency: str | None = None
    status: Literal['available', 'missing', 'unverified'] = 'available'
    qualifiers: tuple[str, ...] = ()
    source_url: str | None = None

    @model_validator(mode='after')
    def value_matches_status(self):
        if self.status == 'available' and (self.value is None or not self.value.is_finite()):
            raise ValueError('Available facts require a finite value')
        if self.status != 'available' and self.value is not None:
            raise ValueError('Missing/unverified facts cannot contain a value')
        return self


@dataclass(frozen=True)
class SourceResult:
    """The existing human source text plus a separate, machine-readable artifact."""

    content: str
    facts: tuple[Fact, ...] = ()

    def __post_init__(self):
        if len({fact.key for fact in self.facts}) != len(self.facts):
            raise ValueError('Source fact keys must be unique')

    def as_tool_output(self) -> tuple[str, dict]:
        return self.content, {'fact_schema_version': 1,
                              'facts': [fact.model_dump(mode='json') for fact in self.facts]}


def render_facts(facts: dict[str, Fact]) -> str:
    """Render exact source values; models do not supply or convert these cells."""
    if not facts:
        return ''
    lines = ['| Fact ID | 指标 | 数值 | 单位/币种 | 期间 | 口径 | 状态 |',
             '| --- | --- | ---: | --- | --- | --- | --- |']
    notes = []
    for fact_id, fact in facts.items():
        value = format(fact.value, 'f') if fact.value is not None else '—'
        cells = [fact_id, fact.label, value, fact.unit + (f' / {fact.currency}' if fact.currency else ''),
                 fact.period, fact.basis, fact.status]
        lines.append('| ' + ' | '.join(escape(str(cell), quote=False).replace('|', '\\|').replace('\n', ' ') for cell in cells) + ' |')
        for note in fact.qualifiers:
            notes.append(f'\n{escape(fact_id, quote=False)} 限定条件：{escape(note, quote=False)}')
        if fact.source_url:
            notes.append(f'\n{escape(fact_id, quote=False)} 来源：{escape(fact.source_url, quote=False)}')
    return '\n'.join(lines + notes)
