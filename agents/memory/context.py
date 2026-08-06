"""
agents/memory/context.py -- turns a MemoryStore search into the short,
prompt-ready text block detector.py and patcher.py splice into an LLM
prompt. Requirement: "Every AI proposal must search this memory before
generating code." This is the one place search-then-format happens, so
both modules stay in sync on what "relevant memory" means and neither
duplicates the other's formatting.

Deliberately compact: agents.config.MEMORY_SEARCH_LIMIT caps each
category so this stays a short excerpt an LLM can actually use, not a
full table dump that burns context for no benefit.
"""
from .. import config


def build_context(store, *, trigger: str, target_files: list | None = None,
                   symbol: str | None = None, strategy_name: str | None = None,
                   limit: int | None = None) -> str:
    limit = limit or config.MEMORY_SEARCH_LIMIT

    bug_fixes = store.search_bug_fixes(trigger, target_files=target_files, limit=limit)
    failed = store.search_failed_experiments(trigger, target_files=target_files, limit=limit)
    params = (
        store.search_parameter_sets(strategy_name=strategy_name, symbol=symbol, limit=limit)
        if (strategy_name or symbol) else []
    )
    evolution = store.search_strategy_evolution(strategy_name=strategy_name, limit=limit) if strategy_name else []

    if not (bug_fixes or failed or params or evolution):
        return "No relevant history found in memory for this trigger/these files."

    sections = []
    if bug_fixes:
        lines = [f"- {b['issue_summary']} -> fix: {b['fix_summary']}" for b in bug_fixes]
        sections.append("Past bugs & fixes on related files:\n" + "\n".join(lines))
    if failed:
        lines = [f"- {f['description']} (failed because: {f['reason']})" for f in failed]
        sections.append("Failed experiments -- do not repeat these:\n" + "\n".join(lines))
    if params:
        lines = [f"- {p['strategy_name']}/{p['symbol']}: {p['parameters']}" for p in params]
        sections.append("Known parameter sets:\n" + "\n".join(lines))
    if evolution:
        lines = [f"- {e['version_label']}: {e['change_summary']}" for e in evolution]
        sections.append("Strategy evolution history:\n" + "\n".join(lines))

    return "\n\n".join(sections)
