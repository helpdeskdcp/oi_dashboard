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


def _json_safe(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json_safe_trades(trades):
    """Trade dicts (e.g. from agents.quant_researcher.strategy_runner)
    carry pandas Timestamp entry_time/exit_time values -- not natively
    JSON-serializable -- so every value is coerced through isoformat()
    first. Milestone 6 addition (agent_memory_backtest_history.trades_json)
    so agents.risk_manager.risk_intelligence has real per-trade history to
    correlate a promotion candidate against, instead of only the
    aggregate stats Milestone 4/5 recorded."""
    if not trades:
        return None
    return [{k: _json_safe(v) for k, v in t.items()} for t in trades]


class SQLiteMemoryStore(MemoryStore):
    backend_name = "sqlite"

    def __init__(self, db_path: str = "oi_history.db"):
        self.db_path = db_path
        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # See agents/audit_log.py's _connect() for why -- same shared
        # oi_history.db, same concurrent-writer concern.
        conn.execute("PRAGMA busy_timeout=5000")
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
                    audit_log_id     INTEGER,
                    trades_json      TEXT
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

                CREATE TABLE IF NOT EXISTS agent_memory_market_regime (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT NOT NULL,
                    symbol         TEXT,
                    regime_type    TEXT,
                    observed_date  TEXT,
                    vix_level      REAL,
                    metrics_json   TEXT,
                    notes          TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_memory_trade_journal (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT NOT NULL,
                    symbol         TEXT,
                    entry_price    REAL,
                    exit_price     REAL,
                    entry_time     TEXT,
                    exit_time      TEXT,
                    screenshot     TEXT,
                    ai_reason      TEXT,
                    actual_result  TEXT,
                    learning       TEXT,
                    audit_log_id   INTEGER
                );

                CREATE TABLE IF NOT EXISTS agent_memory_institutional_patterns (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT NOT NULL,
                    symbol         TEXT,
                    pattern_type   TEXT,
                    observed_date  TEXT,
                    description    TEXT,
                    outcome        TEXT,
                    details_json   TEXT
                );
                """
            )
            # Every search_*/list_* method above filters on one of these
            # columns and always orders by ts DESC -- unindexed full-table
            # scans today, cheap while these tables are small, increasingly
            # not as research cycles and proposals accumulate rows daily.
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_bug_fixes_ts ON agent_memory_bug_fixes(ts);
                CREATE INDEX IF NOT EXISTS idx_backtest_history_symbol_ts
                    ON agent_memory_backtest_history(symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_performance_history_symbol_context_ts
                    ON agent_memory_performance_history(symbol, context, ts);
                CREATE INDEX IF NOT EXISTS idx_failed_experiments_ts ON agent_memory_failed_experiments(ts);
                CREATE INDEX IF NOT EXISTS idx_parameter_sets_strategy_symbol
                    ON agent_memory_parameter_sets(strategy_name, symbol, is_best);
                CREATE INDEX IF NOT EXISTS idx_strategy_evolution_strategy_ts
                    ON agent_memory_strategy_evolution(strategy_name, ts);
                CREATE INDEX IF NOT EXISTS idx_market_regime_symbol_type
                    ON agent_memory_market_regime(symbol, regime_type);
                CREATE INDEX IF NOT EXISTS idx_trade_journal_symbol_ts ON agent_memory_trade_journal(symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_institutional_patterns_symbol_type
                    ON agent_memory_institutional_patterns(symbol, pattern_type);
                """
            )
            # Migration (Milestone 6): agent_memory_backtest_history predates
            # trades_json -- an oi_history.db created before this milestone
            # has the table WITHOUT this column, and CREATE TABLE IF NOT
            # EXISTS above is a no-op against an existing table. Same
            # PRAGMA table_info() + conditional ALTER pattern app.py already
            # uses for its own schema migrations (e.g. strikes/paper_orders).
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory_backtest_history)")}
            if "trades_json" not in existing_cols:
                conn.execute("ALTER TABLE agent_memory_backtest_history ADD COLUMN trades_json TEXT")
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
                         branch=None, audit_log_id=None, trades=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_backtest_history "
                "(ts, symbol, date_from, date_to, branch, stats_json, comparison_json, audit_log_id, trades_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), symbol, date_from, date_to, branch,
                    json.dumps(stats) if stats is not None else None,
                    json.dumps(comparison) if comparison is not None else None,
                    audit_log_id,
                    json.dumps(_json_safe_trades(trades)) if trades else None,
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

    def record_market_regime(self, *, symbol, regime_type, observed_date=None, vix_level=None,
                              metrics=None, notes=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_market_regime "
                "(ts, symbol, regime_type, observed_date, vix_level, metrics_json, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), symbol, regime_type, observed_date, vix_level,
                    json.dumps(metrics) if metrics is not None else None, notes,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_trade_journal(self, *, symbol, entry_price, exit_price=None, entry_time=None,
                              exit_time=None, screenshot=None, ai_reason=None, actual_result=None,
                              learning=None, audit_log_id=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_trade_journal "
                "(ts, symbol, entry_price, exit_price, entry_time, exit_time, screenshot, "
                " ai_reason, actual_result, learning, audit_log_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), symbol, entry_price, exit_price, entry_time, exit_time, screenshot,
                    ai_reason, actual_result, learning, audit_log_id,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def record_institutional_pattern(self, *, symbol, pattern_type, description, observed_date=None,
                                      outcome=None, details=None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_memory_institutional_patterns "
                "(ts, symbol, pattern_type, observed_date, description, outcome, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), symbol, pattern_type, observed_date, description, outcome,
                    json.dumps(details) if details is not None else None,
                ),
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
        return [
            self._row(r, (("stats_json", "stats"), ("comparison_json", "comparison"), ("trades_json", "trades")))
            for r in rows
        ]

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

    def search_market_regime(self, *, symbol=None, regime_type=None, limit=10) -> list:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if regime_type:
            clauses.append("regime_type = ?")
            params.append(regime_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_market_regime {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r, (("metrics_json", "metrics"),)) for r in rows]

    def search_trade_journal(self, query=None, *, symbol=None, limit=10) -> list:
        clauses, params = [], []
        if query:
            like = f"%{query}%"
            clauses.append("(ai_reason LIKE ? OR actual_result LIKE ? OR learning LIKE ?)")
            params += [like, like, like]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_trade_journal {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def search_institutional_pattern(self, query=None, *, symbol=None, pattern_type=None, limit=10) -> list:
        clauses, params = [], []
        if query:
            like = f"%{query}%"
            clauses.append("(description LIKE ? OR outcome LIKE ?)")
            params += [like, like]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if pattern_type:
            clauses.append("pattern_type = ?")
            params.append(pattern_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_memory_institutional_patterns {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row(r, (("details_json", "details"),)) for r in rows]
