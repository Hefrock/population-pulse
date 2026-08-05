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


def build_surge_labels(
    response: pd.Series, quantile: float = 0.75, window: int | None = None, causal: bool = False,
) -> pd.Series:
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

    ``causal=False`` (default) uses ``seasonal_residual``'s centered window —
    correct for retrospective description, the normal use here. Note that
    even with ``causal=True``, ``quantile`` is still computed over the *whole*
    series passed in — a global quantile leaks future information the same
    way a centered window does, just at the threshold instead of the
    residual. ``walk_forward_validate_logistic`` doesn't call this function
    directly for that reason; it recomputes the threshold per fold from only
    that fold's training data.
    """
    residual = seasonal_residual(response, window=window, causal=causal)
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


def _expanding_window_splits(n: int, n_splits: int, min_train: int) -> list[tuple[slice, slice]]:
    """Generate ``n_splits`` expanding-window (train, test) positional slices.

    Each split trains on rows ``[0, k)`` and tests on the next block of rows
    immediately after — train grows every split, test never overlaps train,
    and no test fold ever precedes the training data used to fit it. This is
    the "not a random split" walk-forward validation every fit_* docstring in
    this module has warned about: weekly demand is heavily autocorrelated, so
    a random train/test split would leak future information into training.
    """
    if n < min_train + n_splits:
        raise ValueError(
            f"Only {n} rows, need at least {min_train + n_splits} for "
            f"{n_splits} walk-forward splits with a {min_train}-row minimum training window."
        )
    test_size = (n - min_train) // n_splits
    splits = []
    train_end = min_train
    for _ in range(n_splits):
        test_end = train_end + test_size
        splits.append((slice(0, train_end), slice(train_end, test_end)))
        train_end = test_end
    return splits


@dataclass
class WalkForwardCountResult:
    """Out-of-sample validation of ``fit_count_regression`` via expanding-window CV."""

    family: str
    n_splits: int                    # folds that actually produced a result (some may be skipped)
    fold_n_test: list[int]
    fold_mean_absolute_error: list[float]
    mean_out_of_sample_mae: float
    mean_in_sample_mae: float        # from fitting once on the full series, for a same-units comparison
    in_sample_aic: float


def walk_forward_validate_count(
    aligned: pd.DataFrame,
    response: str,
    driver_lags: dict[str, int],
    family: str = "negative_binomial",
    n_splits: int = 5,
    min_train_weeks: int = 52,
) -> WalkForwardCountResult:
    """Expanding-window out-of-sample validation of ``fit_count_regression``.

    Every AIC/pseudo-R^2 this module (and the dashboard) has ever reported is
    an **in-sample** fit-quality measure — the model graded on the same data
    it was trained on. This answers the different, harder question: refit the
    model on only the past, and see how well it predicts weeks it never saw.
    Mean absolute error (in raw weekly-count units) is reported per fold and
    averaged, alongside the in-sample MAE from a single full-series fit for a
    directly comparable, same-units before/after picture — a wide gap between
    the two is the signature of overfitting.
    """
    if family not in _FAMILIES:
        raise ValueError(f"Unknown family {family!r}; use one of {sorted(_FAMILIES)}")

    design = build_lagged_design_matrix(aligned, response, driver_lags)
    splits = _expanding_window_splits(len(design), n_splits, min_train_weeks)
    glm_family = _FAMILIES[family]()

    fold_n_test, fold_mae = [], []
    for train_slice, test_slice in splits:
        train, test = design.iloc[train_slice], design.iloc[test_slice]
        if test.empty:
            continue
        y_train = train[response].astype(float)
        X_train = sm.add_constant(train.drop(columns=[response]).astype(float), has_constant="add")
        X_test = sm.add_constant(test.drop(columns=[response]).astype(float), has_constant="add")
        model = sm.GLM(y_train, X_train, family=glm_family).fit()
        y_pred = model.predict(X_test)
        y_test = test[response].astype(float)
        fold_n_test.append(len(test))
        fold_mae.append(float((y_test - y_pred).abs().mean()))

    if not fold_mae:
        raise ValueError(
            "No usable walk-forward folds — try fewer splits or a smaller min_train_weeks."
        )

    full_result = fit_count_regression(aligned, response, driver_lags, family=family)
    in_sample_actual = design.loc[full_result.fitted.index, response].astype(float)
    in_sample_mae = float((in_sample_actual - full_result.fitted).abs().mean())

    return WalkForwardCountResult(
        family=family,
        n_splits=len(fold_mae),
        fold_n_test=fold_n_test,
        fold_mean_absolute_error=fold_mae,
        mean_out_of_sample_mae=float(np.mean(fold_mae)),
        mean_in_sample_mae=in_sample_mae,
        in_sample_aic=full_result.aic,
    )


@dataclass
class WalkForwardLogisticResult:
    """Out-of-sample validation of ``fit_logistic_regression`` via expanding-window CV."""

    n_splits: int                    # folds that actually produced a result (some may be skipped)
    fold_n_test: list[int]
    fold_n_surge_test: list[int]
    fold_auc: list[float]
    mean_out_of_sample_auc: float
    in_sample_auc: float             # from fitting once on the full series, for comparison


def walk_forward_validate_logistic(
    aligned: pd.DataFrame,
    response: str,
    driver_lags: dict[str, int],
    surge_quantile: float = 0.75,
    n_splits: int = 5,
    min_train_weeks: int = 52,
) -> WalkForwardLogisticResult:
    """Expanding-window out-of-sample validation of ``fit_logistic_regression``.

    Unlike ``walk_forward_validate_count``, this can't just reuse
    ``build_surge_labels`` per fold: the surge label depends on
    ``seasonal_residual``'s rolling window *and* a quantile threshold, and
    both can leak future information into a training fold if computed the
    normal (in-sample-safe, not walk-forward-safe) way --

    - The residual uses a **causal** (trailing-only) window here, not the
      default centered one: a centered window looks at future weeks when
      deseasonalizing a "past" week. A causal window's value at any row only
      depends on that row and earlier ones, so it's safe to compute once over
      the whole series and reuse across folds without recomputing it per fold.
    - The surge **threshold**, unlike the residual, is *not* safe to compute
      once globally — ``residual.quantile(q)`` over the whole series still
      leaks future information into every fold's labels. It's recomputed per
      fold from only that fold's training-window residuals, then applied to
      both the training and test weeks in that fold.

    AUC-ROC is reported per fold and averaged, alongside the in-sample AUC
    from a single full-series fit (using the normal, centered-window,
    global-threshold ``fit_logistic_regression``) for a directly comparable
    before/after picture.
    """
    if response not in aligned.columns:
        raise ValueError(f"{response!r} not in aligned columns: {list(aligned.columns)}")

    residual = seasonal_residual(aligned[response], causal=True)
    design = build_lagged_design_matrix(aligned, response, driver_lags)
    splits = _expanding_window_splits(len(design), n_splits, min_train_weeks)

    fold_n_test, fold_n_surge_test, fold_auc = [], [], []
    for train_slice, test_slice in splits:
        train, test = design.iloc[train_slice], design.iloc[test_slice]
        if test.empty:
            continue

        threshold = residual.loc[train.index].quantile(surge_quantile)
        surge = (residual.loc[train.index.union(test.index)] > threshold).astype(int)
        y_train, y_test = surge.loc[train.index], surge.loc[test.index]
        if y_train.nunique() < 2:
            continue  # can't fit a logistic model on a single-class training fold

        X_train = sm.add_constant(train.drop(columns=[response]).astype(float), has_constant="add")
        X_test = sm.add_constant(test.drop(columns=[response]).astype(float), has_constant="add")
        model = sm.Logit(y_train, X_train).fit(disp=0)
        fitted_test = model.predict(X_test)
        auc = _roc_auc_score(y_test.to_numpy(), fitted_test.to_numpy())
        if np.isnan(auc):
            continue  # degenerate test fold (single-class) -- AUC undefined

        fold_n_test.append(len(test))
        fold_n_surge_test.append(int(y_test.sum()))
        fold_auc.append(auc)

    if not fold_auc:
        raise ValueError(
            "No usable walk-forward folds — try fewer splits, a larger "
            "min_train_weeks, or a different surge_quantile."
        )

    full_result = fit_logistic_regression(aligned, response, driver_lags, surge_quantile=surge_quantile)

    return WalkForwardLogisticResult(
        n_splits=len(fold_auc),
        fold_n_test=fold_n_test,
        fold_n_surge_test=fold_n_surge_test,
        fold_auc=fold_auc,
        mean_out_of_sample_auc=float(np.mean(fold_auc)),
        in_sample_auc=full_result.auc,
    )
