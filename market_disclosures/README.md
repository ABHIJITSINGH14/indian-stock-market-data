# Official exchange disclosure feeds

`market_disclosures` is a persistence-free acquisition layer for official NSE
and BSE JSON/XML feeds. `NSEClient` exports shareholding, corporate-action,
board-meeting, PIT, SAST Reg 29, and market-wide FII/DII records.
`BSEClient` provides numeric-scripcode fallbacks for corporate actions, board
meetings, current/legacy insider trades, and SAST. Every call returns an
`AcquisitionResult` containing normalized dictionaries, the immutable
`RawDocument` (URL, UTC retrieval time, status, content type, bytes, SHA-256),
and any fetched linked documents. Call `to_dataframe()` at the adapter boundary.

Shareholding retains NSE's official public percentage separately from
institutional subsets. Linked XBRL aggregation uses the
`InstitutionsDomesticMember` and `InstitutionsForeignMember` parent facts when
present. Otherwise DII is the sum of mutual funds/UTI, AIFs, banks, insurers,
provident/pension funds, domestic sovereign wealth funds, RBI-registered NBFCs,
and other financial institutions. FII/FPI is the sum of category I FPIs,
category II FPIs, and other foreign institutions. Parent and child facts are
never added together; NRI, foreign-company, and foreign-national holdings are
excluded.

These website-facing APIs are not versioned contracts. Date ranges should be
split with `iter_date_windows`; callers remain responsible for scheduling.
Linked HTML/iXBRL viewers are retained as URLs rather than scraped. BSE
`CorporateAction/w` uses canonical `Table2` rows to avoid duplicate summary
tables. No database schema, persistence, secrets, or downloaded datasets are
included.
