"""Fixtures that guarantee nothing is left open.

The project runs with warnings as errors, and an unclosed SQLite connection is a
``ResourceWarning`` — so a test that leaks one fails the build, which is the
behaviour S3 turned on deliberately and the reason these are fixtures rather
than helper functions. Closing by hand works right up until the test that
raises before it gets there.

Both openers are factories rather than a single object, because a few tests need
two connections to the same file on purpose: concurrency has to be observed from
outside one connection to be observed at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from clawdence.store import IN_MEMORY, Redactor, StateStore, connect, unscreened

ConnectionFactory = Callable[..., sqlite3.Connection]
StoreFactory = Callable[..., StateStore]


@pytest.fixture
def connections() -> Iterator[ConnectionFactory]:
    opened: list[sqlite3.Connection] = []

    def open_connection(path: Path | str = IN_MEMORY) -> sqlite3.Connection:
        connection = connect(path)
        opened.append(connection)
        return connection

    yield open_connection
    for connection in opened:
        connection.close()


@pytest.fixture
def db(connections: ConnectionFactory) -> sqlite3.Connection:
    return connections()


@pytest.fixture
def stores() -> Iterator[StoreFactory]:
    opened: list[StateStore] = []

    def open_store(
        path: Path | str = IN_MEMORY,
        *,
        redactor: Redactor = unscreened,
        conflict_window: Callable[[], None] = lambda: None,
    ) -> StateStore:
        store = StateStore.open(path, redactor=redactor, conflict_window=conflict_window)
        opened.append(store)
        return store

    yield open_store
    for store in opened:
        store.close()


@pytest.fixture
def state(stores: StoreFactory) -> StateStore:
    return stores()
