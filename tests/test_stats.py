"""Estadística: valores de referencia conocidos (tablas de la t, Wilson) y casos límite."""

from __future__ import annotations

import math

import pytest

from swarmlab.stats import (
    auc,
    auc_permutation_p,
    icc,
    mean_sd,
    min_detectable_diff,
    normal_two_sided_p,
    spearman,
    t_quantile,
    t_two_sided_p,
    welch,
    wilson,
)


def test_wilson_matches_reference_values() -> None:
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson(9, 9)  # el «100 % de 9 clientes» de un informe
    assert lo == pytest.approx(0.7009, abs=1e-3) and hi == 1.0
    assert wilson(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize(("df", "q"), [(1, 12.706), (2, 4.303), (10, 2.228), (30, 2.042), (1000, 1.962)])
def test_t_quantiles_match_tables(df: float, q: float) -> None:
    assert t_quantile(0.975, df) == pytest.approx(q, abs=1e-3)
    assert t_two_sided_p(q, df) == pytest.approx(0.05, abs=1e-3)
    assert t_quantile(0.025, df) == pytest.approx(-q, abs=1e-3)


def test_normal_p() -> None:
    assert normal_two_sided_p(1.959964) == pytest.approx(0.05, abs=1e-6)
    assert normal_two_sided_p(0.0) == pytest.approx(1.0)


def test_welch_detects_clear_difference_and_not_noise() -> None:
    w = welch([0.10, 0.15, 0.12], [0.30, 0.25, 0.35])
    assert w.diff == pytest.approx(0.1767, abs=1e-3)
    assert w.significant and w.ci_low > 0
    assert w.df == pytest.approx(2.95, abs=0.01)
    noise = welch([0.10, 0.30, 0.20], [0.15, 0.35, 0.05])
    assert not noise.significant
    with pytest.raises(ValueError):
        welch([0.1], [0.2, 0.3])
    same = welch([0.2, 0.2], [0.3, 0.3])
    assert math.isnan(same.p) and not same.significant


def test_icc_separates_signal_from_noise() -> None:
    signal = icc([[0.10, 0.12, 0.11], [0.30, 0.28], [0.20, 0.25, 0.22]])
    assert signal is not None and signal.icc > 0.9 and signal.groups == 3 and signal.runs == 8
    noise = icc([[0.1, 0.3], [0.1, 0.3], [0.3, 0.1]])
    assert noise is not None and noise.icc == 0.0
    assert icc([[0.1], [0.2]]) is None  # sin réplicas no se puede
    assert icc([[0.1, 0.2]]) is None  # una sola idea tampoco


def test_min_detectable_diff_shrinks_with_replicas() -> None:
    assert min_detectable_diff(0.05, 1) == pytest.approx(0.198, abs=1e-3)
    assert min_detectable_diff(0.05, 3) < min_detectable_diff(0.05, 1)


def test_auc_and_exact_permutation_p() -> None:
    assert auc([3, 4], [1, 2]) == 1.0
    assert auc([1, 2], [1, 2]) == 0.5
    assert math.isnan(auc([], [1]))
    assert auc_permutation_p([3, 4], [1, 2]) == pytest.approx(1 / 6)
    assert auc_permutation_p([5, 6, 7, 8], [1, 2, 3, 4]) == pytest.approx(1 / 70)


def test_spearman_and_mean_sd() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert math.isnan(spearman([1, 2], [1, 2]))
    m, sd = mean_sd([1.0, 2.0, 3.0])
    assert m == 2.0 and sd == pytest.approx(1.0)
    assert mean_sd([5.0]) == (5.0, 0.0)
