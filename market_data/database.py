"""SQLite schema, migrations, and transactional market-data upserts."""

import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from market_data.normalization import (
    canonical_security_id,
    clean_text,
    normalize_exchange,
    normalize_isin,
    normalize_symbol,
)


SCHEMA_VERSION = 2
metadata = MetaData()

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("description", String(255), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

securities = Table(
    "securities",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("canonical_id", String(64), nullable=False, unique=True),
    Column("isin", String(12), unique=True),
    Column("name", String(255), nullable=False),
    Column("normalized_name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(canonical_id) > 0", name="ck_security_canonical_id"),
)
Index("ix_securities_name", securities.c.normalized_name)

exchange_symbols = Table(
    "exchange_symbols",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "security_id",
        Integer,
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("exchange", String(3), nullable=False),
    Column("exchange_symbol", String(64), nullable=False),
    Column("normalized_symbol", String(64), nullable=False),
    Column("raw_symbol", String(128), nullable=False),
    Column("series", String(16), nullable=False, default=""),
    Column("scrip_code", String(32), nullable=False, default=""),
    Column("source", String(32), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("raw_data", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "exchange", "normalized_symbol", "series", name="uq_exchange_symbol_series"
    ),
    CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_symbol_exchange"),
)
Index("ix_exchange_symbols_security", exchange_symbols.c.security_id)
Index("ix_exchange_symbols_scrip_code", exchange_symbols.c.exchange, exchange_symbols.c.scrip_code)

daily_prices = Table(
    "daily_prices",
    metadata,
    Column(
        "security_id",
        Integer,
        ForeignKey("securities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("exchange", String(3), primary_key=True),
    Column("trading_date", Date, primary_key=True),
    Column("series", String(16), primary_key=True, default=""),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float, nullable=False),
    Column("last", Float),
    Column("previous_close", Float),
    Column("volume", Integer),
    Column("turnover", Float),
    Column("trades", Integer),
    Column("deliverable_quantity", Integer),
    Column("source", String(64), nullable=False),
    Column("raw_data", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_price_exchange"),
    CheckConstraint("volume IS NULL OR volume >= 0", name="ck_price_volume"),
)
Index("ix_daily_prices_exchange_date", daily_prices.c.exchange, daily_prices.c.trading_date)
Index("ix_daily_prices_date", daily_prices.c.trading_date)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(32), nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", String(16), nullable=False),
    Column("attempted", Integer, nullable=False, default=0),
    Column("succeeded", Integer, nullable=False, default=0),
    Column("failed", Integer, nullable=False, default=0),
    Column("rows_written", Integer, nullable=False, default=0),
    Column("message", Text),
)
Index("ix_ingestion_runs_source_dataset", ingestion_runs.c.source, ingestion_runs.c.dataset)

ingestion_checkpoints = Table(
    "ingestion_checkpoints",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("dataset", String(32), primary_key=True),
    Column("checkpoint_key", String(32), primary_key=True),
    Column("checkpoint_date", Date),
    Column("status", String(16), nullable=False),
    Column("row_count", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_ingestion_checkpoints_status",
    ingestion_checkpoints.c.source,
    ingestion_checkpoints.c.dataset,
    ingestion_checkpoints.c.status,
)

ingestion_errors = Table(
    "ingestion_errors",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("ingestion_runs.id", ondelete="CASCADE")),
    Column("source", String(32), nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("checkpoint_key", String(32)),
    Column("error_type", String(128), nullable=False),
    Column("message", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_ingestion_errors_run", ingestion_errors.c.run_id)

archive_availability = Table(
    "archive_availability",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("year_month", String(7), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("first_available_date", Date),
    Column("last_available_date", Date),
    Column("success_count", Integer, nullable=False, default=0),
    Column("not_published_count", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketDatabase:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._sqlite_path: Optional[Path] = None
        if database_url.startswith("sqlite:///"):
            path = Path(database_url[len("sqlite:///") :])
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
                self._sqlite_path = path
        self.engine = create_engine(database_url, future=True)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def initialize(self) -> None:
        if self.engine.dialect.name == "sqlite":
            with self._initialization_lock():
                with self.transaction() as connection:
                    self._initialize_schema(connection)
            return
        with self.engine.begin() as connection:
            self._initialize_schema(connection)

    def initialize_metadata(self, target_metadata: MetaData) -> None:
        if self.engine.dialect.name == "sqlite":
            with self._initialization_lock():
                with self.transaction() as connection:
                    target_metadata.create_all(connection)
            return
        target_metadata.create_all(self.engine)

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        with self._database_lock("init"):
            yield

    @contextmanager
    def _database_lock(self, name: str) -> Iterator[None]:
        if self._sqlite_path is None:
            yield
            return
        import fcntl

        lock_path = Path("{}-{}.lock".format(self._sqlite_path, name))
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _initialize_schema(connection: Connection) -> None:
        metadata.create_all(connection)
        applied = connection.execute(
            select(schema_migrations.c.version).where(
                schema_migrations.c.version == SCHEMA_VERSION
            )
        ).scalar_one_or_none()
        if applied is None:
            connection.execute(
                schema_migrations.insert().values(
                    version=SCHEMA_VERSION,
                    description="Historical backfill and archive coverage schema",
                    applied_at=utcnow(),
                )
            )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        if self.engine.dialect.name == "sqlite":
            with self._database_lock("write"):
                with self.engine.connect() as connection:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    try:
                        yield connection
                    except BaseException:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
            return
        with self.engine.begin() as connection:
            yield connection

    def upsert_security(
        self, connection: Connection, record: Dict[str, object]
    ) -> int:
        exchange = normalize_exchange(record["exchange"])
        raw_symbol = clean_text(record["symbol"])
        symbol = normalize_symbol(raw_symbol, exchange, record.get("alias_source"))
        series = clean_text(record.get("series")).upper()
        scrip_code = clean_text(record.get("scrip_code")).upper()
        isin = normalize_isin(record.get("isin"))
        canonical_id = canonical_security_id(
            isin, exchange, symbol, series=series, scrip_code=scrip_code
        )
        name = clean_text(record.get("name")) or symbol
        now = utcnow()

        mapping = connection.execute(
            select(exchange_symbols.c.id, exchange_symbols.c.security_id).where(
                exchange_symbols.c.exchange == exchange,
                exchange_symbols.c.normalized_symbol == symbol,
                exchange_symbols.c.series == series,
            )
        ).mappings().first()

        security_id = connection.execute(
            select(securities.c.id).where(securities.c.canonical_id == canonical_id)
        ).scalar_one_or_none()
        if security_id is None and isin:
            security_id = connection.execute(
                select(securities.c.id).where(securities.c.isin == isin)
            ).scalar_one_or_none()
        if security_id is None:
            result = connection.execute(
                securities.insert().values(
                    canonical_id=canonical_id,
                    isin=isin,
                    name=name,
                    normalized_name=name.upper(),
                    active=bool(record.get("active", True)),
                    created_at=now,
                    updated_at=now,
                )
            )
            security_id = int(result.inserted_primary_key[0])
        else:
            existing_name = connection.execute(
                select(securities.c.name).where(securities.c.id == security_id)
            ).scalar_one()
            preferred_name = existing_name
            if (
                not existing_name
                or existing_name.upper() == symbol
                or len(name) > len(existing_name)
            ):
                preferred_name = name
            connection.execute(
                securities.update()
                .where(securities.c.id == security_id)
                .values(
                    isin=isin if isin else securities.c.isin,
                    name=preferred_name,
                    normalized_name=preferred_name.upper(),
                    active=bool(record.get("active", True)),
                    updated_at=now,
                )
            )

        raw_data = record.get("raw_data")
        mapping_values = {
            "security_id": security_id,
            "exchange": exchange,
            "exchange_symbol": symbol,
            "normalized_symbol": symbol,
            "raw_symbol": raw_symbol,
            "series": series,
            "scrip_code": scrip_code,
            "source": clean_text(record.get("source")) or exchange.lower(),
            "active": bool(record.get("active", True)),
            "raw_data": json.dumps(raw_data, sort_keys=True, default=str)
            if raw_data is not None
            else None,
            "updated_at": now,
        }
        if exchange == "BSE" and scrip_code:
            connection.execute(
                exchange_symbols.update()
                .where(
                    exchange_symbols.c.exchange == "BSE",
                    exchange_symbols.c.scrip_code == scrip_code,
                    exchange_symbols.c.normalized_symbol != symbol,
                )
                .values(active=False, updated_at=now)
            )
        if mapping:
            connection.execute(
                exchange_symbols.update()
                .where(exchange_symbols.c.id == mapping["id"])
                .values(**mapping_values)
            )
            old_security_id = int(mapping["security_id"])
            if old_security_id != security_id:
                self._merge_security(connection, old_security_id, security_id)
        else:
            connection.execute(
                exchange_symbols.insert().values(created_at=now, **mapping_values)
            )
        return security_id

    @staticmethod
    def _merge_security(
        connection: Connection, old_security_id: int, new_security_id: int
    ) -> None:
        new_price = daily_prices.alias("new_price")
        matching_new_price = (
            select(new_price.c.security_id)
            .where(
                new_price.c.security_id == new_security_id,
                new_price.c.exchange == daily_prices.c.exchange,
                new_price.c.trading_date == daily_prices.c.trading_date,
                new_price.c.series == daily_prices.c.series,
            )
            .exists()
        )
        connection.execute(
            daily_prices.delete().where(
                daily_prices.c.security_id == old_security_id,
                matching_new_price,
            )
        )
        connection.execute(
            daily_prices.update()
            .where(daily_prices.c.security_id == old_security_id)
            .values(security_id=new_security_id)
        )
        for table_name in (
            "filings",
            "shareholding_patterns",
            "financial_facts",
            "financial_fact_instances",
            "corporate_actions",
            "board_meetings",
            "pit_disclosures",
            "sast_disclosures",
        ):
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).first()
            if exists:
                connection.exec_driver_sql(
                    'UPDATE "{}" SET security_id=? WHERE security_id=?'.format(
                        table_name
                    ),
                    (new_security_id, old_security_id),
                )
        mappings = connection.execute(
            select(func.count()).select_from(exchange_symbols).where(
                exchange_symbols.c.security_id == old_security_id
            )
        ).scalar_one()
        prices = connection.execute(
            select(func.count()).select_from(daily_prices).where(
                daily_prices.c.security_id == old_security_id
            )
        ).scalar_one()
        if mappings == 0 and prices == 0:
            connection.execute(
                securities.delete().where(securities.c.id == old_security_id)
            )

    def upsert_securities(self, records: Iterable[Dict[str, object]]) -> int:
        count = 0
        with self.transaction() as connection:
            for record in records:
                self.upsert_security(connection, record)
                count += 1
        return count

    def _resolve_security(
        self, connection: Connection, record: Dict[str, object]
    ) -> int:
        exchange = normalize_exchange(record["exchange"])
        symbol = normalize_symbol(record["symbol"], exchange, record.get("alias_source"))
        series = clean_text(record.get("series")).upper()
        scrip_code = clean_text(record.get("scrip_code")).upper()
        if normalize_isin(record.get("isin")):
            return self.upsert_security(connection, record)
        if scrip_code:
            security_id = connection.execute(
                select(exchange_symbols.c.security_id).where(
                    exchange_symbols.c.exchange == exchange,
                    exchange_symbols.c.scrip_code == scrip_code,
                ).order_by(
                    exchange_symbols.c.active.desc(),
                    exchange_symbols.c.updated_at.desc(),
                    exchange_symbols.c.id.desc(),
                ).limit(1)
            ).scalar_one_or_none()
            if security_id is not None:
                return int(security_id)
        security_id = connection.execute(
            select(exchange_symbols.c.security_id).where(
                exchange_symbols.c.exchange == exchange,
                exchange_symbols.c.normalized_symbol == symbol,
                exchange_symbols.c.series == series,
            )
        ).scalar_one_or_none()
        if security_id is None and series:
            security_id = connection.execute(
                select(exchange_symbols.c.security_id).where(
                    exchange_symbols.c.exchange == exchange,
                    exchange_symbols.c.normalized_symbol == symbol,
                    exchange_symbols.c.series == "",
                )
            ).scalar_one_or_none()
        if security_id is None:
            security_id = self.upsert_security(connection, record)
        return int(security_id)

    def upsert_prices(self, records: Sequence[Dict[str, object]]) -> int:
        if not records:
            return 0
        now = utcnow()
        values: List[Dict[str, object]] = []
        with self.transaction() as connection:
            for record in records:
                exchange = normalize_exchange(record["exchange"])
                security_id = self._resolve_security(connection, record)
                raw_data = record.get("raw_data")
                values.append(
                    {
                        "security_id": security_id,
                        "exchange": exchange,
                        "trading_date": record["trading_date"],
                        "series": clean_text(record.get("series")).upper(),
                        "open": record.get("open"),
                        "high": record.get("high"),
                        "low": record.get("low"),
                        "close": record["close"],
                        "last": record.get("last"),
                        "previous_close": record.get("previous_close"),
                        "volume": record.get("volume"),
                        "turnover": record.get("turnover"),
                        "trades": record.get("trades"),
                        "deliverable_quantity": record.get("deliverable_quantity"),
                        "source": clean_text(record.get("source")) or exchange.lower(),
                        "raw_data": json.dumps(raw_data, sort_keys=True, default=str)
                        if raw_data is not None
                        else None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            statement = sqlite_insert(daily_prices).values(values)
            update_columns = {
                column.name: getattr(statement.excluded, column.name)
                for column in daily_prices.c
                if column.name
                not in {"security_id", "exchange", "trading_date", "series", "created_at"}
            }
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        daily_prices.c.security_id,
                        daily_prices.c.exchange,
                        daily_prices.c.trading_date,
                        daily_prices.c.series,
                    ],
                    set_=update_columns,
                )
            )
        return len(values)

    def start_run(self, source: str, dataset: str) -> int:
        with self.transaction() as connection:
            result = connection.execute(
                ingestion_runs.insert().values(
                    source=source,
                    dataset=dataset,
                    started_at=utcnow(),
                    status="running",
                    attempted=0,
                    succeeded=0,
                    failed=0,
                    rows_written=0,
                )
            )
            return int(result.inserted_primary_key[0])

    def finish_run(
        self,
        run_id: int,
        status: str,
        attempted: int,
        succeeded: int,
        failed: int,
        rows_written: int,
        message: str = "",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                ingestion_runs.update()
                .where(ingestion_runs.c.id == run_id)
                .values(
                    finished_at=utcnow(),
                    status=status,
                    attempted=attempted,
                    succeeded=succeeded,
                    failed=failed,
                    rows_written=rows_written,
                    message=message,
                )
            )

    def record_checkpoint(
        self,
        source: str,
        dataset: str,
        checkpoint_key: str,
        status: str,
        checkpoint_date: Optional[date] = None,
        row_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        statement = sqlite_insert(ingestion_checkpoints).values(
            source=source,
            dataset=dataset,
            checkpoint_key=checkpoint_key,
            checkpoint_date=checkpoint_date,
            status=status,
            row_count=row_count,
            error=error,
            updated_at=utcnow(),
        )
        with self.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        ingestion_checkpoints.c.source,
                        ingestion_checkpoints.c.dataset,
                        ingestion_checkpoints.c.checkpoint_key,
                    ],
                    set_={
                        "checkpoint_date": statement.excluded.checkpoint_date,
                        "status": statement.excluded.status,
                        "row_count": statement.excluded.row_count,
                        "error": statement.excluded.error,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def record_error(
        self,
        run_id: int,
        source: str,
        dataset: str,
        checkpoint_key: Optional[str],
        error: Exception,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                ingestion_errors.insert().values(
                    run_id=run_id,
                    source=source,
                    dataset=dataset,
                    checkpoint_key=checkpoint_key,
                    error_type=type(error).__name__,
                    message=str(error),
                    created_at=utcnow(),
                )
            )

    def successful_checkpoint_dates(
        self, source: str, dataset: str, start: date, end: date
    ) -> set:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ingestion_checkpoints.c.checkpoint_date).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == dataset,
                    ingestion_checkpoints.c.status == "success",
                    ingestion_checkpoints.c.checkpoint_date >= start,
                    ingestion_checkpoints.c.checkpoint_date <= end,
                )
            ).scalars()
            return set(rows)

    def failed_checkpoint_dates(self, source: str, dataset: str, end: date) -> set:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ingestion_checkpoints.c.checkpoint_date).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == dataset,
                    ingestion_checkpoints.c.status == "failed",
                    ingestion_checkpoints.c.checkpoint_date <= end,
                )
            ).scalars()
            return {item for item in rows if item is not None}

    def checkpoint_dates(
        self,
        source: str,
        dataset: str,
        start: date,
        end: date,
        statuses: Sequence[str],
    ) -> set:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(ingestion_checkpoints.c.checkpoint_date).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == dataset,
                    ingestion_checkpoints.c.status.in_(statuses),
                    ingestion_checkpoints.c.checkpoint_date >= start,
                    ingestion_checkpoints.c.checkpoint_date <= end,
                )
            ).scalars()
            return {item for item in rows if item is not None}

    def unavailable_months(self, source: str) -> set:
        with self.engine.connect() as connection:
            return set(
                connection.execute(
                    select(archive_availability.c.year_month).where(
                        archive_availability.c.source == source,
                        archive_availability.c.status == "unavailable",
                    )
                ).scalars()
            )

    def refresh_month_availability(self, source: str, year_month: str) -> None:
        from market_data.calendar import trading_days

        start = date.fromisoformat(year_month + "-01")
        if start.month == 12:
            end = date(start.year + 1, 1, 1) - date.resolution
        else:
            end = date(start.year, start.month + 1, 1) - date.resolution
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    ingestion_checkpoints.c.status,
                    ingestion_checkpoints.c.checkpoint_date,
                ).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == "daily_prices",
                    ingestion_checkpoints.c.checkpoint_date >= start,
                    ingestion_checkpoints.c.checkpoint_date <= end,
                )
            ).all()
        successes = [item[1] for item in rows if item[0] == "success"]
        unavailable = sum(item[0] == "not_published" for item in rows)
        expected = len(list(trading_days(start, end)))
        status = (
            "available"
            if successes
            else "unavailable"
            if unavailable >= expected
            else "unknown"
        )
        statement = sqlite_insert(archive_availability).values(
            source=source,
            year_month=year_month,
            status=status,
            first_available_date=min(successes) if successes else None,
            last_available_date=max(successes) if successes else None,
            success_count=len(successes),
            not_published_count=unavailable,
            updated_at=utcnow(),
        )
        with self.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        archive_availability.c.source,
                        archive_availability.c.year_month,
                    ],
                    set_={
                        "status": statement.excluded.status,
                        "first_available_date": statement.excluded.first_available_date,
                        "last_available_date": statement.excluded.last_available_date,
                        "success_count": statement.excluded.success_count,
                        "not_published_count": statement.excluded.not_published_count,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def coverage_snapshot(self, source: str, start: date, end: date) -> Dict[str, object]:
        with self.engine.connect() as connection:
            prices = connection.execute(
                select(
                    func.min(daily_prices.c.trading_date),
                    func.max(daily_prices.c.trading_date),
                    func.count(),
                    func.count(func.distinct(daily_prices.c.security_id)),
                    func.count(func.distinct(daily_prices.c.trading_date)),
                    func.max(daily_prices.c.updated_at),
                ).where(
                    daily_prices.c.exchange == source.upper(),
                    daily_prices.c.trading_date >= start,
                    daily_prices.c.trading_date <= end,
                )
            ).one()
            status_rows = connection.execute(
                select(
                    ingestion_checkpoints.c.status,
                    func.count(),
                ).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == "daily_prices",
                    ingestion_checkpoints.c.checkpoint_date >= start,
                    ingestion_checkpoints.c.checkpoint_date <= end,
                ).group_by(ingestion_checkpoints.c.status)
            ).all()
            present = set(
                connection.execute(
                    select(daily_prices.c.trading_date).distinct().where(
                        daily_prices.c.exchange == source.upper(),
                        daily_prices.c.trading_date >= start,
                        daily_prices.c.trading_date <= end,
                    )
                ).scalars()
            )
        return {
            "exchange": source.upper(),
            "start": start,
            "end": end,
            "earliest": prices[0],
            "latest": prices[1],
            "rows": int(prices[2] or 0),
            "distinct_securities": int(prices[3] or 0),
            "present_days": int(prices[4] or 0),
            "last_update": prices[5],
            "status_counts": {status: count for status, count in status_rows},
            "present_dates": present,
        }

    def latest_successful_date(self, source: str, dataset: str) -> Optional[date]:
        with self.engine.connect() as connection:
            return connection.execute(
                select(func.max(ingestion_checkpoints.c.checkpoint_date)).where(
                    ingestion_checkpoints.c.source == source,
                    ingestion_checkpoints.c.dataset == dataset,
                    ingestion_checkpoints.c.status == "success",
                )
            ).scalar_one_or_none()
