"""Typed source measurements shared by tool artifacts and report rendering."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class FactProvenance(BaseModel):
    """Source dates are distinct from acquisition; unknown dates stay unknown.

    Publication refers to this revision, not an assumed first release. Date-only
    publication does not establish availability at any intraday execution time.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')
    period_end: date
    period_start: date | None = None
    published_date: date | None = None
    first_published_date: date | None = None
    fetched_at: AwareDatetime | None = None
    revision: str | None = Field(default=None, min_length=1)
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def dates_are_ordered(self):
        if self.period_start and self.period_start > self.period_end:
            raise ValueError('Period start cannot follow period end')
        if (self.first_published_date and self.published_date
                and self.first_published_date > self.published_date):
            raise ValueError('First publication cannot follow revision publication')
        return self


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
    provenance: FactProvenance | None = None

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
        if fact.provenance:
            metadata = fact.provenance.model_dump(mode='json', exclude_none=True)
            for key, value in metadata.items():
                notes.append(f'\n{escape(fact_id, quote=False)} {key}：'
                             f'{escape(str(value), quote=False)}')
    return '\n'.join(lines + notes)
