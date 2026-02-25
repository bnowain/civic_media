"""
SQLAlchemy engine, session factory, and FastAPI dependency.
"""

import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

# SQLite requires check_same_thread=False for multithreaded FastAPI
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    # WAL mode significantly improves concurrent read performance with SQLite
    echo=False,
)

# Enable WAL journal mode and foreign key enforcement
@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI dependency that provides a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def validate_schema_columns(base):
    """Compare ORM model columns against the live database and auto-add
    any missing nullable columns.  Logs errors for non-nullable columns
    that require manual migration.  Safe to call on every startup.
    """
    from app.models import Base  # noqa: local import to avoid circular

    if base is None:
        base = Base

    with engine.connect() as conn:
        for table_name, table in base.metadata.tables.items():
            rows = conn.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
            if not rows:
                # Table doesn't exist yet — create_all() will handle it
                continue

            existing_cols = {row[1] for row in rows}
            for col in table.columns:
                if col.name in existing_cols:
                    continue

                # Map SQLAlchemy type to SQLite type affinity
                col_type = str(col.type)

                if col.nullable:
                    conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"
                    ))
                    conn.commit()
                    logger.warning(
                        "Schema drift fixed: added %s.%s (%s)",
                        table_name, col.name, col_type,
                    )
                else:
                    logger.error(
                        "Schema drift detected: %s.%s (%s) is NOT NULL — "
                        "requires manual migration",
                        table_name, col.name, col_type,
                    )
