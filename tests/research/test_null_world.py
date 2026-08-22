"""TRD §15.3 / Milestone 0: the pipeline must not invent discoveries when run
against data with no alpha by construction. This is a *permanent regression
test* — it must be re-run after any change to `evaluate.py`, the scoring
rule, or any profile (TRD §15.3)."""

import types

import pytest

from aqrl.research import evaluate, strategy


def _module():
    return types.SimpleNamespace(
        generate_signals=strategy.generate_signals,
        params=strategy.PARAMS,
        param_grid=None,  # skip tuning here purely for test speed
    )


@pytest.mark.parametrize("generator", ["synthetic_path", "permuted", "block_bootstrap"])
def test_null_world_fdr_is_low(generator):
    out = evaluate.run_null_world(generator, n_replications=12, n_days=252 * 6, module=_module())
    assert out["fdr"] <= 1 / 12, (
        f"{generator}: {out['n_discoveries']}/{out['n_replications']} replications cleared the bar "
        f"on data with no alpha by construction (max_score_observed={out['max_score_observed']:.3f})"
    )
