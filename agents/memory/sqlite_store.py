"""
agents/memory/sqlite_store.py -- the SQLite implementation of MemoryStore.
Same tables-in-oi_history.db, per-call connect/close, dt.datetime.now()
timestamp, json.dumps() payload convention agents/audit_log.py already
established -- this package is new, but it isn't a new persistence
pattern in this codebase.

Search is deliberately simple: case-insensitive LIKE matching against a
handful of text columns, plus a LIKE-based file-overlap check against a
space-joined target_files column (never SQLite's JSON1 extension --
LIKE-on-a-flattened-column works on every SQLite build, with no optional
extension to depend on). This is the seam a future PostgresMemoryStore
would replace with full-text search (tsvector/trigram) -- the interface
(MemoryStore) doesn't change, only what's behind search_*() does.
"""
import datetime as dt
import json
import sqlite3

from .base import MemoryStore


def _now() -> str:
    return dt.datetime.now().isoformat()


def _files_text(files) -> str:
    return " ".join(files) if files else ""


class SQLiteMemoryStore(MemoryStore):
    backend_name = "sqlite"

    def __init__(self, db_path: str = "oi_history.db"):
        self.db_path = db_path
        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_memory_bug_fixes (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                TEXT NOT NULL,
                    trigger           TEXT,
                    issue_summary     TEXT,
                    root_cause        TEXT,
                    fix_summary       TEXT,
                    target_files      TEXT,
                    target_files_json TEXT,
                    confidence_score  INTEGER,
                    audit_log_id      INTEGER,
                    outcome           TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_memory_backtest_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               TEXT NOT NULL,
                    symbol           TEXT,
                    date_from        TEXT,
                    date_to          TEXT,
                    branch           TEXT,
                    stats_json       TEXT,
                    comparison_json  TEXT,
                    audit_log_id     INTEGER
                );

                CREATE TABLE IF NOT EXISTS agent_memory_performance_history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT NOT NULL,
                    symbol       TEXT,
                    context      TEXT,
                    metrics_json TEXT,
                    notes        TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_memory_failed_experiments (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                TEXT NOT NULL,
                    trigger           TEXT,
                    description       TEXT,
                    target_files      TEXT,
                    target_files_json TEXT,
                    parameters_json   TEXT,
                    reason            TEXT NOT NULL,
                    audit_log_id      INTEGER
                );

                CREATE TABLE IF NOT EXISTS agent_memory_parameter_sets (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               TEXT NOT NULL,
                    strategy_name    TEXT,
                    symbol           TEXT,
                    parameters_json  TEXT,
                    performance_json TEXT,
                    is_best          INTEGER NOT NULL DEFAULT 0,
                    notes            TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_memory_strategy_evolution (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT NOT NULL,
                    strategy_name  TEXT,
                    version_label  TEXT,
                    change_summary TEXT,
                    rationale      TEXT,
                    audit_log_id   INTEGER
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # --- writes -------------------------------------------------------------

    def record_bug_fix(self, *, trigger, issue_summary, root_cause, fix_summary,
                        target_files=None, confidence_score=None, audit_log_id=None,
                        outcome=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_bug_fixes "
                "(ts, trigger, issue_summary, root_cause, fix_summary, target_files, "
                " target_files_json, confidence_score, audit_log_id, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), trigger, issue_summary, root_cause, fix_summary,
                    _files_text(target_files), json.dumps(target_files) if target_files else None,
                    confidence_score, audit_log_id, outcome,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_backtest(self, *, symbol, date_from, date_to, stats, comparison=None,
                         branch=None, audit_log_id=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_backtest_history "
                "(ts, symbol, date_from, date_to, branch, stats_json, comparison_json, audit_log_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), symbol, date_from, date_to, branch,
                    json.dumps(stats) if stats is not None else None,
                    json.dumps(comparison) if comparison is not None else None,
                    audit_log_id,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_performance(self, *, symbol, context, metrics, notes=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_performance_history (ts, symbol, context, metrics_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now(), symbol, context, json.dumps(metrics) if metrics is not None else None, notes),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_failed_experiment(self, *, trigger, description, reason, target_files=None,
                                  parameters=None, audit_log_id=None) -> int:
        if not reason:
            raise ValueError("record_failed_experiment requires a non-empty reason")
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_failed_experiments "
                "(ts, trigger, description, target_files, target_files_json, parameters_json, reason, audit_log_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), trigger, description, _files_text(target_files),
                    json.dumps(target_files) if target_files else None,
                    json.dumps(parameters) if parameters is not None else None,
                    reason, audit_log_id,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_parameter_set(self, *, strategy_name, symbol, parameters, performance=None,
                              is_best=False, notes=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_parameter_sets "
                "(ts, strategy_name, symbol, parameters_json, performance_json, is_best, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), strategy_name, symbol, json.dumps(parameters),
                    json.dumps(performance) if performance is not None else None,
                    1 if is_best else 0, notes,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_strategy_evolution(self, *, strategy_name, version_label, change_summary,
                                   rationale=None, audit_log_id=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_strategy_evolution "
                "(ts, strategy_name, version_label, change_summary, rationale, audit_log_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), strategy_name, version_label, change_summary, rationale, audit_log_id),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    # --- reads / search -------------------------------------------------------

    @staticmethod
    def _row(row, json_fields=()) -> dict:
        d = dict(row)
        for src, dest in json_fields:
            raw = d.pop(src, None)
            d[dest] = json.loads(raw) if raw else None
        return d

    def _text_and_file_search(self, table, text_columns, query, target_files, limit):
        like = f"%{query}%" if query else "%"
        text_clause = "(" + " OR ".join(f"{c} LIKE ?" for c in text_columns) + ")"
        params = [like] * len(text_columns)
        clause = text_clause
        if target_files:
            file_ors = " OR ".join(["target_files LIKE ?"] * len(target_files))
            clause = f"({text_clause} OR ({file_ors}))"
            params += [f"%{f}%" for f in target_files]
        sql = f"SELECT * FROM {table} WHERE {clause} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def search_bug_fixes(self, query, *, target_files=None, limit=10) -> list:
        rows = self._text_and_file_search(
            "agent_memory_bug_fixes",
            ("trigger", "issue_summary", "root_cause", "fix_summary"),
            query, target_files, limit,
        )
        return [self._row(r, (("target_files_json", "target_files"),)) for r in rows]

    def search_failed_experiments(self, query, *, target_files=None, limit=10) -> list:
        rows = self._text_and_file_search(
            "agent_memory_failed_experiments",
            ("trigger", "description", "reason"),
            query, target_files, limit,
        )
        return [
            self._row(r, (("target_files_json", "target_files"), ("parameters_json", "parameters")))
            for r in rows
        ]

    def search_parameter_sets(self, *, strategy_name=None, symbol=None, limit=10) -> list:
        clauses, params = [], []
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_parameter_sets {where} ORDER BY is_best DESC, ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            self._row(r, (("parameters_json", "parameters"), ("performance_json", "performance")))
            for r in rows
        ]

    def search_strategy_evolution(self, *, strategy_name=None, limit=10) -> list:
        clauses, params = [], []
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_strategy_evolution {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def list_backtest_history(self, *, symbol=None, limit=10) -> list:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_backtest_history {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r, (("stats_json", "stats"), ("comparison_json", "comparison"))) for r in rows]

    def list_performance_history(self, *, symbol=None, context=None, limit=10) -> list:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if context:
            clauses.append("context = ?")
            params.append(context)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_performance_history {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r, (("metrics_json", "metrics"),)) for r in rows]
