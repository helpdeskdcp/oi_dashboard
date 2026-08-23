"""
test_db_busy_timeout.py -- regression tests for the "database is locked"
class of failure.

PRAGMA busy_timeout is PER-CONNECTION state, not persisted in the DB file
the way journal_mode=WAL is. app.py used to set it only on init_db()'s own
connection, leaving its other 57 connections at the SQLite default of 0ms:
with 14 symbol loops, the request handlers and the agent loops all writing
to the same oi_history.db, any writer that met a held write lock failed
immediately instead of waiting. These tests pin the fix (a single _connect()
helper) and the invariant that no new raw sqlite3.connect() call sneaks back
into app.py/billing.py.
"""
import os
import re
import sqlite3
import threading
import time

os.environ["SKIP_AUTOSTART"] = "1"

import app
import billing


def _busy_timeout_ms(conn):
    """SQLite has no PRAGMA to read busy_timeout back directly; it is
    exposed as the (undocumented but stable) `PRAGMA busy_timeout` query
    form, which returns the current value."""
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


class TestConnectHelper:
    def test_app_connect_sets_a_nonzero_busy_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "t.db"))
        conn = app._connect()
        try:
            assert _busy_timeout_ms(conn) == app.DB_BUSY_TIMEOUT_MS > 0
        finally:
            conn.close()

    def test_billing_connect_sets_a_nonzero_busy_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "t.db"))
        conn = billing._connect()
        try:
            assert _busy_timeout_ms(conn) == billing.DB_BUSY_TIMEOUT_MS > 0
        finally:
            conn.close()

    def test_app_connect_reads_db_path_at_call_time(self, tmp_path, monkeypatch):
        """Not at import time -- every test in this suite repoints
        app.DB_PATH, and so does manage.py."""
        target = tmp_path / "late-bound.db"
        monkeypatch.setattr(app, "DB_PATH", str(target))
        conn = app._connect()
        try:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
        assert target.exists()


class TestNoRawConnectRemains:
    """Source-level guard: the whole point of the helper is that nothing
    bypasses it. A new `sqlite3.connect(DB_PATH)` added later would silently
    reintroduce the 0ms-timeout bug, which only shows up under real
    concurrency (i.e. in production, during market hours)."""

    def _raw_connect_lines(self, module):
        source = open(module.__file__).read()
        return [
            line.strip() for line in source.splitlines()
            if re.search(r"\bsqlite3\.connect\(", line)
        ]

    def test_app_has_exactly_one_raw_connect_inside_the_helper(self):
        assert self._raw_connect_lines(app) == ["conn = sqlite3.connect(DB_PATH)"]

    def test_billing_has_exactly_one_raw_connect_inside_the_helper(self):
        assert self._raw_connect_lines(billing) == ["conn = sqlite3.connect(DB_PATH, timeout=timeout)"]


class TestConcurrentWriterWaits:
    def test_second_writer_waits_for_the_lock_instead_of_failing(self, tmp_path, monkeypatch):
        """The actual production symptom, reproduced: one connection holds
        the write lock briefly while another tries to write. With
        busy_timeout=0 this raises 'database is locked' instantly; with the
        helper's timeout it blocks and then succeeds."""
        db = str(tmp_path / "lock.db")
        monkeypatch.setattr(app, "DB_PATH", db)

        setup = app._connect()
        setup.execute("CREATE TABLE t (x INTEGER)")
        setup.commit()
        setup.close()

        holding = threading.Event()
        released = threading.Event()

        def hold_the_write_lock_briefly():
            """Own connection, own thread -- sqlite3 objects are not
            shareable across threads."""
            holder = app._connect()
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO t VALUES (1)")
            holding.set()
            time.sleep(0.5)
            holder.commit()
            holder.close()
            released.set()

        holder_thread = threading.Thread(target=hold_the_write_lock_briefly)
        holder_thread.start()
        try:
            assert holding.wait(timeout=5)
            writer = app._connect()
            try:
                writer.execute("INSERT INTO t VALUES (2)")   # would raise OperationalError at timeout 0
                writer.commit()
            finally:
                writer.close()
        finally:
            holder_thread.join()

        assert released.is_set()
        check = app._connect()
        try:
            assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        finally:
            check.close()

    def test_a_zero_timeout_connection_is_what_used_to_fail(self, tmp_path):
        """Control case documenting the pre-fix behavior, so this file
        fails loudly if a future SQLite/Python version changes it."""
        db = str(tmp_path / "control.db")
        setup = sqlite3.connect(db)
        setup.execute("CREATE TABLE t (x INTEGER)")
        setup.commit()
        setup.close()

        holder = sqlite3.connect(db)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO t VALUES (1)")

        writer = sqlite3.connect(db, timeout=0)
        writer.execute("PRAGMA busy_timeout=0")
        try:
            raised = False
            try:
                writer.execute("INSERT INTO t VALUES (2)")
            except sqlite3.OperationalError as exc:
                raised = "locked" in str(exc)
            assert raised
        finally:
            writer.close()
            holder.rollback()
            holder.close()
