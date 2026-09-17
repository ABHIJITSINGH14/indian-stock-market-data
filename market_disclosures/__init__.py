"""Official NSE/BSE disclosure acquisition and normalization."""

from .bse import BSEClient
from .models import (
    AcquisitionResult,
    DisclosureError,
    DocumentFormatError,
    HTTPSourceError,
    RawDocument,
)
from .nse import NSEClient
from .transport import ExchangeTransport, iter_date_windows
from .xbrl import (
    DII_MEMBERS,
    FII_FPI_MEMBERS,
    XbrlDocument,
    XbrlFact,
    aggregate_shareholding,
    parse_xbrl,
)

__all__ = [
    "AcquisitionResult",
    "BSEClient",
    "DII_MEMBERS",
    "DisclosureError",
    "DocumentFormatError",
    "ExchangeTransport",
    "FII_FPI_MEMBERS",
    "HTTPSourceError",
    "NSEClient",
    "RawDocument",
    "XbrlDocument",
    "XbrlFact",
    "aggregate_shareholding",
    "iter_date_windows",
    "parse_xbrl",
]
