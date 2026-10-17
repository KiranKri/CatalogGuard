"""Engine and session factory. SQLite enables foreign keys and WAL, and
SQLAlchemy, not the driver, emits BEGIN so savepoints nest in the unit of work."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.orm import Base


def create_db_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = NullPool
    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.fetchone()
            cursor.close()
            # The sqlite3 driver's legacy mode skips BEGIN before SELECT and
            # SAVEPOINT, so a savepoint could open, then commit, its own
            # transaction. Turn that off; _sqlite_begin issues BEGIN instead.
            # The aiosqlite adapter's setter applies this to the underlying
            # sqlite3 connection on its worker thread.
            dbapi_connection.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin(connection) -> None:
            # IMMEDIATE takes the write lock up front. A deferred BEGIN would hold
            # a read snapshot, and a unit of work that reads, then writes after
            # another one committed, would fail with "database is locked" instead
            # of waiting busy_timeout for its turn.
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
