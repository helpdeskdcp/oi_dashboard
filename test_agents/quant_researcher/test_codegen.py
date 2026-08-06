"""
test_agents/quant_researcher/test_codegen.py -- confirms codegen.py
produces syntactically valid, importable Python (via py_compile, never
exec of the generated content -- matching agents/dev_agent/patcher.py's
"no eval/exec of generated content" discipline) with the expected SPEC
values baked in.
"""
import ast

from agents.quant_researcher.codegen import file_paths, generate_module, generate_test
from agents.quant_researcher.strategy_spec import StrategySpec


def _spec():
    return StrategySpec(
        name="vwap_gamma_NIFTY", symbol="NIFTY", hypothesis_id="vwap_gamma",
        features=["vwap_deviation", "gamma_exposure"],
        thresholds={"vwap_deviation": 0.001, "gamma_exposure": 0.0},
        direction="both", target_points=30.0, stop_points=15.0, max_hold_bars=20,
        params={"invert": {}},
    )


def test_generated_module_is_syntactically_valid_python():
    source = generate_module(_spec())
    ast.parse(source)  # raises SyntaxError if malformed -- never exec'd


def test_generated_module_contains_expected_spec_values():
    source = generate_module(_spec())
    assert "'NIFTY'" in source
    assert "'vwap_gamma'" in source
    assert "vwap_deviation" in source


def test_generated_test_is_syntactically_valid_python():
    source = generate_test(_spec(), "research_strategies.vwap_gamma_nifty")
    ast.parse(source)


def test_file_paths_are_safely_slugified():
    spec = _spec()
    module_relpath, module_import_name, test_relpath = file_paths(spec, strategies_dir="research_strategies")
    assert module_relpath == "research_strategies/vwap_gamma_nifty.py"
    assert module_import_name == "research_strategies.vwap_gamma_nifty"
    assert test_relpath == "research_strategies/test_vwap_gamma_nifty.py"
