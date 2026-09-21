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
