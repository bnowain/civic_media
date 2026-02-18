"""
SQLAlchemy engine, session factory, and FastAPI dependency.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL

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
