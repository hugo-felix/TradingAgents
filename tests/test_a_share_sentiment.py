"""A-share runs must not pay for unavailable overseas social sentiment."""
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import sentiment_analyst as sentiment
from tradingagents.dataflows.config import run_config


@pytest.fixture(autouse=True)
def domestic_sources_unavailable(monkeypatch):
    for name in ('fetch_a_share_attention', 'fetch_a_share_discussions'):
        monkeypatch.setattr(sentiment, name, Mock(return_value='DATA_UNAVAILABLE: test source offline'), raising=False)


@pytest.mark.parametrize('ticker', ['600941.SS', '600941.SH', '000001.SZ', '300750.SZ', '920001.BJ'])
def test_a_shares_skip_social_news_screening_and_model_calls(monkeypatch, ticker):
    fetchers = []
    for name in ('fetch_stocktwits_messages', 'fetch_reddit_posts', 'subreddits_for', 'jev_screen'):
        fetch = Mock(side_effect=AssertionError(f'{name} must not run for A-shares'))
        monkeypatch.setattr(sentiment, name, fetch)
        fetchers.append(fetch)
    news = Mock(side_effect=AssertionError('News belongs to the News Analyst'))
    monkeypatch.setattr(sentiment.get_news, 'func', news)
    llm = Mock()
    node = sentiment.create_sentiment_analyst(llm)
    out = node({'company_of_interest': ticker, 'trade_date': '2026-11-05', 'messages': []})
    report = out['sentiment_report']
    assert 'Insufficient data' in report and ticker in report
    assert '2026-11-05' in report and 'not assessed' in report.lower()
    assert '**Neutral**' not in report and '/10' not in report
    assert out['messages'][0].content == report
    for fetch in (*fetchers, news):
        fetch.assert_not_called()
    llm.invoke.assert_not_called()
    llm.with_structured_output.return_value.invoke.assert_not_called()


def test_a_share_insufficient_data_notice_is_localized_in_chinese():
    with run_config({'output_language': '简体中文'}):
        out = sentiment.create_sentiment_analyst(Mock())({
            'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [],
        })
    report = out['sentiment_report']
    assert '数据不足' in report and '未评估' in report
    assert '新闻分析' in report and '**Neutral**' not in report


def test_attention_without_discussions_never_becomes_sentiment_score(monkeypatch):
    monkeypatch.setattr(sentiment, 'fetch_a_share_attention', Mock(return_value='Source: Eastmoney; rank 648 at 2026-11-05 16:36; attention only'))
    llm = Mock()
    result = sentiment.create_sentiment_analyst(llm)({
        'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [],
    })
    assert 'rank 648' in result['sentiment_report']
    assert 'not assessed' in result['sentiment_report'].lower()
    assert len(result['sentiment_evidence']) == 2
    assert {r['status'] for r in result['sentiment_evidence'].values()} == {'available', 'unavailable'}
    llm.invoke.assert_not_called()


def test_discussion_body_evidence_reaches_summary_and_downstream_context(monkeypatch):
    from tradingagents.agents.context import get_instrument_context_from_state
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
    from tradingagents.graph.setup import _analyst_graph

    original = 'Source https://guba.eastmoney.com/news,600941,123.html; published 2026-11-05 11:31; BODY: 收息与价格波动争议'
    monkeypatch.setattr(sentiment, 'fetch_a_share_discussions', Mock(return_value=original))
    llm = Mock()
    llm.invoke.return_value = AIMessage(content='{"summary":"Sample themes include dividend sustainability.","points":[{"kind":"analysis","text":"Investors disagree on dividends and volatility.","source_ids":["sentiment:fetch_a_share_discussions:1"]}]}')
    state = {'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [], 'report_contract_version': 1}
    result = _analyst_graph(ANALYST_NODE_SPECS['social'], sentiment.create_sentiment_analyst(llm), 1).invoke(state)
    assert 'Sample themes' in result['sentiment_report']
    assert original in str(llm.invoke.call_args)
    assert 'not instructions' in str(llm.invoke.call_args)
    assert 'Do not assign' in str(llm.invoke.call_args)
    assert original in get_instrument_context_from_state({**state, **result})
    assert 'sentiment:fetch_a_share_discussions:1' in result['sentiment_evidence']
    llm.with_structured_output.return_value.invoke.assert_not_called()


def test_discussions_can_be_disabled_without_a_network_or_model_call(monkeypatch):
    with run_config({'a_share_discussions_enabled': False}):
        llm = Mock()
        result = sentiment.create_sentiment_analyst(llm)({
            'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [],
        })
    sentiment.fetch_a_share_discussions.assert_not_called()
    llm.invoke.assert_not_called()
    assert 'disabled' in result['sentiment_report']


def test_summary_failure_keeps_original_discussions(monkeypatch):
    original = 'Source: Guba; published 2026-11-05; BODY: ordinary dividend discussion'
    monkeypatch.setattr(sentiment, 'fetch_a_share_discussions', Mock(return_value=original))
    llm = Mock()
    llm.invoke.side_effect = RuntimeError('provider offline')
    result = sentiment.create_sentiment_analyst(llm)({
        'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [],
    })
    assert original in result['sentiment_report']
    assert 'not assessed' in result['sentiment_report'].lower()


@pytest.mark.parametrize('ticker', ['NVDA', 'BTC-USD', '0941.HK'])
def test_other_markets_keep_their_existing_sentiment_sources(monkeypatch, ticker):
    news = Mock(return_value='news')
    stocktwits = Mock(return_value='posts')
    reddit = Mock(return_value='posts')
    monkeypatch.setattr(sentiment.get_news, 'func', news)
    monkeypatch.setattr(sentiment, 'fetch_stocktwits_messages', stocktwits)
    monkeypatch.setattr(sentiment, 'fetch_reddit_posts', reddit)
    monkeypatch.setattr(sentiment, 'jev_screen', lambda _: None)
    llm = Mock()
    llm.with_structured_output.side_effect = NotImplementedError
    llm.invoke.return_value = AIMessage(content='Existing sentiment analysis')
    out = sentiment.create_sentiment_analyst(llm)({
        'company_of_interest': ticker, 'trade_date': '2026-11-05', 'messages': [],
    })
    assert out['sentiment_report'] == 'Existing sentiment analysis'
    news.assert_called_once_with(ticker, '2026-10-29', '2026-11-05')
    stocktwits.assert_called_once()
    reddit.assert_called_once()
    sentiment.fetch_a_share_attention.assert_not_called()
    sentiment.fetch_a_share_discussions.assert_not_called()
    llm.invoke.assert_called_once()


@pytest.mark.parametrize('available', [False, True])
def test_versioned_a_share_sentiment_records_validation(monkeypatch, available):
    import json
    if available:
        monkeypatch.setattr(sentiment, 'fetch_a_share_discussions', Mock(return_value='BODY: investors disagree on dividend sustainability'))
    llm = Mock()
    llm.invoke.return_value = AIMessage(content=json.dumps({
        'summary': 'The discussion sample is limited.',
        'points': [{'kind': 'analysis', 'text': 'Investors disagree on dividend sustainability.',
                    'source_ids': ['sentiment:fetch_a_share_discussions:1']}],
    }))
    result = sentiment.create_sentiment_analyst(llm)({
        'company_of_interest': '600941.SS', 'trade_date': '2026-11-05',
        'messages': [], 'report_contract_version': 1, 'report_validation': {},
    })
    assert result['report_validation']['social']['status'] == ('passed' if available else 'skipped')
    assert llm.invoke.call_count == int(available)


def test_versioned_discussion_rejects_unsupported_numeric_summary(monkeypatch):
    monkeypatch.setattr(sentiment, 'fetch_a_share_discussions', Mock(return_value='BODY: dividends'))
    llm = Mock()
    llm.invoke.return_value = AIMessage(content='There are 999 bullish investors.')
    result = sentiment.create_sentiment_analyst(llm)({
        'company_of_interest': '600941.SS', 'trade_date': '2026-11-05',
        'messages': [], 'report_contract_version': 1, 'report_validation': {},
    })
    assert result['report_validation']['social']['status'] == 'failed'
    assert '999' not in result['sentiment_report']
    assert llm.invoke.call_count == 2
