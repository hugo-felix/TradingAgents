"""A-share statements, news, dividends and China macro via the optional AKShare SDK."""

from .dividends import get_dividends
from .fundamentals import get_balance_sheet, get_cashflow, get_income_statement
from .macro import get_macro_data
from .news import get_event_candidates, get_news
from .valuation import get_valuation_comparison
