"""Multi-driver lagged regression for hospital demand.

Where ``correlate.lagged_cross_correlation`` answers "does *this one* driver,
at *this* lag, move with hospital demand?" one signal at a time, this module
asks the natural follow-up: with several drivers each contributing at its own
lag, how much of the weekly demand level can they jointly explain — and which
ones still matter once the others are accounted for?

``hospital_demand`` is a weekly **count** (ED visits / ILI patients), not a
continuous measurement, so this fits a Poisson / Negative-Binomial GLM rather
than OLS linear regression: counts are non-negative and typically
over-dispersed (variance > mean), and plain linear regression mis-specifies
both.

A second framing — ``build_surge_labels`` / ``fit_logistic_regression`` —
turns the count into a binary "is this week a surge *for the time of year*?"
label and asks which lagged drivers predict it, reporting AUC-ROC. This
trades the count model's information for an easier-to-communicate yes/no
question, and is most useful for drivers (like wastewater) hypothesized as
*leading* indicators.

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
from scipy.stats import rankdata
from statsmodels.stats.outliers_influence import variance_inflation_factor

from src.analysis.correlate import seasonal_residual

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


@dataclass
class LogisticRegressionResult:
    """Result of a multi-driver lagged "surge" logistic regression."""

    n_obs: int
    n_surge_weeks: int
    coefficients: pd.Series        # one entry per driver, plus "const"
    pvalues: pd.Series
    aic: float
    pseudo_r_squared: float         # McFadden's pseudo-R^2 vs. an intercept-only model
    auc: float                       # AUC-ROC of in-sample fitted probabilities
    surge_labels: pd.Series          # the binary labels the model was fit on
    fitted_probabilities: pd.Series  # in-sample P(surge), indexed like the design matrix


def build_surge_labels(response: pd.Series, quantile: float = 0.75, window: int | None = None) -> pd.Series:
    """Binary "surge" label: is this week elevated *for the time of year*?

    Hospital demand is strongly seasonal, so a label based on the raw level
    (e.g. "top 25% of all weeks") would just relabel "is it winter" — not
    useful as a target. Instead this deseasonalizes with
    ``correlate.seasonal_residual`` (the same rolling-mean filter
    ``lagged_cross_correlation`` uses) and labels the top ``1 - quantile``
    fraction of *residuals* as a surge: weeks running hotter than that time of
    year would predict, regardless of whether it's June or January.

    ``quantile=0.75`` (the default) labels roughly a quarter of weeks as
    surges — a reasonable balance for logistic regression with a modest
    sample size.
    """
    residual = seasonal_residual(response, window=window)
    threshold = residual.quantile(quantile)
    return (residual > threshold).astype(int)


def _roc_auc_score(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """AUC-ROC via the rank-sum (Mann-Whitney U) identity — avoids a
    scikit-learn dependency for one statistic."""
    n_pos = int(np.sum(y_true == 1))
    n_neg = int(np.sum(y_true == 0))
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = rankdata(y_score)
    sum_ranks_pos = ranks[y_true == 1].sum()
    return float((sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def fit_logistic_regression(
    aligned: pd.DataFrame,
    response: str,
    driver_lags: dict[str, int],
    surge_quantile: float = 0.75,
) -> LogisticRegressionResult:
    """Fit a logistic regression of a "surge" label on lagged drivers.

    The label is built by ``build_surge_labels`` from the *full* ``response``
    series (so deseasonalization isn't distorted by truncating to the lagged
    design matrix first), then aligned to whichever weeks survive lagging the
    drivers — same convention as ``fit_count_regression``.

    Same caveats as ``fit_count_regression`` apply (descriptive not causal,
    easy to overfit with few weeks and many drivers, autocorrelated weekly
    series). AUC-ROC here is an **in-sample** fit-quality measure, not a
    validated predictive score — see the module docstring on walk-forward
    validation before using this for prediction.
    """
    if response not in aligned.columns:
        raise ValueError(f"{response!r} not in aligned columns: {list(aligned.columns)}")

    surge = build_surge_labels(aligned[response], quantile=surge_quantile)
    design = build_lagged_design_matrix(aligned, response, driver_lags)
    min_obs = len(driver_lags) + 5
    if len(design) < min_obs:
        raise ValueError(
            f"Only {len(design)} overlapping weeks after lagging "
            f"{len(driver_lags)} driver(s) — need at least {min_obs}."
        )

    y = surge.loc[design.index]
    if y.nunique() < 2:
        raise ValueError(
            "Surge label has only one class over the overlapping weeks — "
            "try a different surge_quantile or a longer time range."
        )

    X = sm.add_constant(design.drop(columns=[response]).astype(float))
    model = sm.Logit(y, X).fit(disp=0)

    null_model = sm.Logit(y, np.ones((len(y), 1))).fit(disp=0)
    pseudo_r2 = 1.0 - (model.llf / null_model.llf)

    fitted = model.predict(X)
    auc = _roc_auc_score(y.to_numpy(), fitted.to_numpy())

    return LogisticRegressionResult(
        n_obs=int(model.nobs),
        n_surge_weeks=int(y.sum()),
        coefficients=model.params,
        pvalues=model.pvalues,
        aic=float(model.aic),
        pseudo_r_squared=float(pseudo_r2),
        auc=auc,
        surge_labels=y,
        fitted_probabilities=fitted,
    )


def driver_vif(
    aligned: pd.DataFrame,
    drivers: list[str] | None = None,
    deseasonalize: bool = True,
    min_obs: int = 20,
) -> pd.DataFrame:
    """Variance inflation factor for each driver, on weeks where all overlap.

    Companion to ``correlate.driver_correlation_matrix`` for the same
    confounding question, but stated in regression terms: VIF > 5 means a
    driver is largely explained by a linear combination of the others, so its
    own coefficient in a joint model (e.g. ``fit_count_regression`` given
    multiple ``driver_lags`` keys) is unreliable even if its univariate
    correlation against ``hospital_demand`` looked clean.

    Returns an empty frame (rather than raising) if there are fewer than two
    drivers or fewer than ``min_obs`` weeks where all of them overlap — VIF
    on a handful of points is noise, not signal.
    """
    if drivers is None:
        drivers = [c for c in aligned.columns if c != "hospital_demand"]
    cols = {
        d: seasonal_residual(aligned[d]) if deseasonalize else aligned[d].astype(float)
        for d in drivers
    }
    design = pd.DataFrame(cols).dropna()
    if len(drivers) < 2 or len(design) < min_obs:
        return pd.DataFrame(columns=["driver", "vif", "n_obs"])

    exog = sm.add_constant(design)
    rows = [
        {
            "driver": d,
            "vif": float(variance_inflation_factor(exog.to_numpy(), exog.columns.get_loc(d))),
            "n_obs": len(design),
        }
        for d in drivers
    ]
    return pd.DataFrame(rows).sort_values("vif", ascending=False).reset_index(drop=True)
