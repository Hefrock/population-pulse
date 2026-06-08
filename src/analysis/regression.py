"""Multi-driver lagged regression for hospital demand.

Where ``correlate.lagged_cross_correlation`` answers "does *this one* driver,
at *this* lag, move with hospital demand?" one signal at a time, this module
asks the natural follow-up: with several drivers each contributing at its own
lag, how much of the weekly demand level can they jointly explain — and which
ones still matter once the others are accounted for?

``hospital_demand`` is a weekly **count** (ILI patients), not a continuous
measurement, so this fits a Poisson / Negative-Binomial GLM rather than OLS
linear regression: counts are non-negative and typically over-dispersed
(variance > mean), and plain linear regression mis-specifies both.

IMPORTANT CAVEATS (read before trusting a coefficient):
- This is still descriptive/exploratory, not causal — same caveat as
  ``correlate``. A significant lagged coefficient says the driver and demand
  move together at that lag, not that one causes the other.
- With roughly a year of weekly data (~50 points) and several lagged drivers,
  this model is easy to overfit — keep the driver list short and prefer fewer,
  better-justified lags (e.g. the ones ``lagged_cross_correlation`` already
  flagged) over a kitchen-sink specification.
- Weekly demand is heavily autocorrelated, so a random train/test split leaks
  future information into training. If you start using this for prediction,
  validate with walk-forward / expanding-window splits, not random ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import statsmodels.api as sm

_FAMILIES = {
    "poisson": sm.families.Poisson,
    "negative_binomial": sm.families.NegativeBinomial,
}


@dataclass
class CountRegressionResult:
    """Result of a multi-driver lagged count regression."""

    family: str
    n_obs: int
    coefficients: pd.Series   # one entry per driver, plus "const"
    pvalues: pd.Series
    aic: float
    pseudo_r_squared: float   # McFadden's pseudo-R^2 vs. an intercept-only model
    fitted: pd.Series         # in-sample predicted counts, indexed like the design matrix


def build_lagged_design_matrix(
    aligned: pd.DataFrame,
    response: str,
    driver_lags: dict[str, int],
) -> pd.DataFrame:
    """Shift each driver by its lag and join with the response, dropping NaNs.

    ``driver_lags`` maps a column of ``aligned`` to a lag in periods, using the
    same convention as ``lagged_cross_correlation``: a positive lag means the
    driver *leads* the response by that many periods. ``driver.shift(lag)``
    moves ``driver[t - lag]`` into row ``t`` — i.e. "the driver's value `lag`
    periods before this week's demand" — which is exactly the predictor a
    leading-indicator hypothesis implies.
    """
    if response not in aligned.columns:
        raise ValueError(f"{response!r} not in aligned columns: {list(aligned.columns)}")
    missing = [d for d in driver_lags if d not in aligned.columns]
    if missing:
        raise ValueError(f"Driver(s) not in aligned columns: {missing}")

    cols = {response: aligned[response]}
    for driver, lag in driver_lags.items():
        cols[driver] = aligned[driver].shift(lag)
    return pd.concat(cols, axis=1).dropna()


def fit_count_regression(
    aligned: pd.DataFrame,
    response: str,
    driver_lags: dict[str, int],
    family: str = "poisson",
) -> CountRegressionResult:
    """Fit a Poisson or Negative-Binomial GLM of ``response`` on lagged drivers.

    ``family`` is ``"poisson"`` (default — assumes variance equals the mean) or
    ``"negative_binomial"`` (allows over-dispersion, the more realistic choice
    for real syndromic counts, but needs more data to pin down the extra
    dispersion parameter — prefer it once you have multiple years of history).
    """
    if family not in _FAMILIES:
        raise ValueError(f"Unknown family {family!r}; use one of {sorted(_FAMILIES)}")

    design = build_lagged_design_matrix(aligned, response, driver_lags)
    min_obs = len(driver_lags) + 5
    if len(design) < min_obs:
        raise ValueError(
            f"Only {len(design)} overlapping weeks after lagging "
            f"{len(driver_lags)} driver(s) — need at least {min_obs}."
        )

    y = design[response].astype(float)
    X = sm.add_constant(design.drop(columns=[response]).astype(float))
    glm_family = _FAMILIES[family]()

    model = sm.GLM(y, X, family=glm_family).fit()

    # McFadden's pseudo-R^2: 1 - (log-likelihood of the fitted model / log-
    # likelihood of an intercept-only model). 0 = no better than predicting the
    # mean every week; values above ~0.2-0.4 are considered a strong fit for
    # count models (it is not directly comparable to OLS R^2).
    null_model = sm.GLM(y, np.ones((len(y), 1)), family=glm_family).fit()
    pseudo_r2 = 1.0 - (model.llf / null_model.llf)

    return CountRegressionResult(
        family=family,
        n_obs=int(model.nobs),
        coefficients=model.params,
        pvalues=model.pvalues,
        aic=float(model.aic),
        pseudo_r_squared=float(pseudo_r2),
        fitted=model.fittedvalues,
    )
