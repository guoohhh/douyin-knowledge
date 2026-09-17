import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def database_url() -> str:
    value = os.getenv("DK_DATABASE_URL", "sqlite:///./data/douyin_knowledge.db")
    if value.startswith("sqlite:///") and value != "sqlite:///:memory:":
        Path(value.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return value


class Base(DeclarativeBase):
    pass


def make_engine(url=None):
    engine = create_engine(url or database_url(), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session
