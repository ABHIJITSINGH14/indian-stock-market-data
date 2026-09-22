"""Reviewed identity exceptions; no fuzzy aliases or successor-series splicing."""
from datetime import date

INFOSYS_SOURCE = 'https://www.infosys.com/investors/shareholder-services/faqs.html'
HDFC_MERGER_SOURCE = (
    'https://www.hdfc.bank.in/press-release/2023/q2/'
    'hdfc-ltd-to-merge-into-hdfc-bank-effective-july-1-2023'
)
HDFC_MERGER_EFFECTIVE = date(2023, 7, 1)


def reject_known_typo(symbol: str) -> None:
    """Require an explicit corrected request, rather than silently rewriting input."""
    if symbol == 'INFOSY.NS':
        raise ValueError('INFOSY.NS is a configuration typo; request INFY.NS explicitly. '
                         + INFOSYS_SOURCE)


def current_snapshot_deferral(symbol: str, as_of: date) -> dict | None:
    """A merged issuer needs archival financials, not today's successor snapshot.

    This is a current-standalone-financials boundary, NOT its last trading date.
    HDFC's requested history remains in scope; no replacement symbol is returned.
    An unlisted symbol here is not certified active or independently verified.
    """
    if symbol == 'HDFC.NS' and as_of >= HDFC_MERGER_EFFECTIVE:
        return {
            'status': 'archival_required',
            'reason': 'merged_issuer_has_no_current_standalone_snapshot',
            'effective_date': HDFC_MERGER_EFFECTIVE.isoformat(),
            'evidence_url': HDFC_MERGER_SOURCE,
            'historical_obligation_preserved': True,
            'replacement_symbol': None,
        }
    return None


HDFC_PRICE_REVIEWED_ON = date(2026, 9, 22)
HDFC_PRICE_OBSERVATION_SOURCE = (
    'https://github.com/ABHIJITSINGH14/indian-stock-market-data/'
    'actions/runs/35685081036'
)


def historical_price_deferral(symbol: str, start: date, end: date,
                              as_of: date) -> dict | None:
    """Explicit retrieval routing, NOT a trading-calendar or price-history cutoff.

    Today's unavailable HDFC adapter needs a separately verified archive even
    for a pre-merger request. Keep the ENTIRE requested interval outstanding.
    The review date is execution knowledge, not an historical market signal.
    No other issuer or arbitrary empty response is eligible for this exception.
    """
    if start >= end:
        raise ValueError('Start must precede exclusive end date')
    if symbol == 'HDFC.NS' and as_of >= HDFC_PRICE_REVIEWED_ON:
        return {
            'policy_id': 'hdfc-yahoo-price-archive-v1',
            'status': 'archival_required',
            'provider_symbol': symbol,
            'source_adapter': 'yahoo_finance',
            'data_family': 'daily_ohlcv',
            'reason': 'reviewed_merged_issuer_and_unavailable_provider_history',
            'requested_start': start.isoformat(),
            'requested_end_exclusive': end.isoformat(),
            'reviewed_on': HDFC_PRICE_REVIEWED_ON.isoformat(),
            'evidence_urls': [HDFC_MERGER_SOURCE, HDFC_PRICE_OBSERVATION_SOURCE],
            'historical_obligation_preserved': True,
            'historical_completeness': 'not_verified',
            'replacement_symbol': None,
            'last_trading_date': None,
            'archive_source': None,
            'archive_fetch_status': 'awaiting_verified_source',
        }
    return None
