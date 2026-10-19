import json
import os

import numpy as np

from Modules.Watchdog.trees import BoostedTrees

MODEL_FORMAT_VERSION = 2
MODEL_VERSION = "plsim-watchdog-v2"

# =====================================================================
# The models. Both are plain numpy so the project needs no extra dependency.
#
#   RiskModel        supervised: window features -> risk of a stop within the horizon. Two backends:
#                      kind="boosted"   gradient boosted trees (trees.py), the default for real use: learns
#                                       thresholds and interactions, explains with per-feature contributions
#                      kind="logistic"  a linear model, fast and easy to inspect, a good baseline
#                    Both are class balanced because stops are rare. The score ranks risk but is not a
#                    calibrated probability; operating thresholds must be selected on held-out validation data.
#                    `explain()` lists the features pushing a prediction up the most.
#   AnomalyDetector  a simple max |z-score| per feature (used by residuals.py)
#   SubspaceDetector unsupervised and normal-only: learns the directions along which healthy windows vary
#                    (PCA) and scores how far a window falls outside them (SPE) or along them (Hotelling T2).
#                    It needs no failure data, so it can flag failures nobody recorded. It is saved next to the
#                    model and shown live as an advisory "this is not how the plant normally behaves" flag.
#
# To use a stronger learner later (scikit-learn gradient boosting, an LSTM in PyTorch, ...) keep this
# interface: fit(X, y), predict_proba(X), save(path), load(path). The rest of the watchdog does not care.
# =====================================================================


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _average_ranks(a):
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a))
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def auc(y, score):
    """Area under the ROC curve (rank based). 0.5 = random, 1.0 = perfect."""
    y, score = np.asarray(y), np.asarray(score, dtype=float)
    n_pos, n_neg = int((y > 0.5).sum()), int((y <= 0.5).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = _average_ranks(score)
    return float((ranks[y > 0.5].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


class RiskModel:
    def __init__(self, channels=(), feature_names=(), window=0, period=1.0, horizon=0.0, target="", kind="logistic",
                 optional_channels=()):
        self.channels = list(channels)
        self.optional_channels = list(optional_channels)    # channels some training plants lacked (read as 0.0 if absent)
        self.feature_names = list(feature_names)
        self.window, self.period, self.horizon, self.target = window, period, horizon, target
        self.kind = kind
        self.trees = None
        self.mean = self.std = self.weights = None
        self.bias = 0.0
        self.model_version = MODEL_VERSION
        self.metadata = {}

    def fit(self, X, y, epochs=600, lr=0.2, l2=1e-2, **tree_options):
        X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
        if self.kind == "boosted":
            self.trees = BoostedTrees(**tree_options).fit(X, y)
            return self
        if self.kind != "logistic":
            raise ValueError(f"Unknown model kind '{self.kind}' (use 'logistic' or 'boosted')")
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std < 1e-9] = 1.0
        Z = (X - self.mean) / self.std
        n_pos, n_neg = max(y.sum(), 1.0), max((1 - y).sum(), 1.0)
        sample_w = np.where(y > 0.5, 0.5 / n_pos, 0.5 / n_neg) * len(y)     # balance the classes
        w = np.zeros(Z.shape[1])
        b = 0.0
        for _ in range(epochs):
            p = _sigmoid(Z @ w + b)
            err = (p - y) * sample_w
            w -= lr * (Z.T @ err / len(y) + l2 * w)
            b -= lr * err.mean()
        self.weights, self.bias = w, b
        return self

    def _z(self, X):
        return (np.asarray(X, dtype=float) - self.mean) / self.std

    def predict_proba(self, X):
        """Calibrated only up to the class balance: read it as a risk score, not a literal probability."""
        if self.kind == "boosted":
            return self.trees.predict_proba(X)
        return _sigmoid(self._z(np.atleast_2d(X)) @ self.weights + self.bias)

    def explain(self, x, top=3):
        """The features pushing this window's risk up the most: [(name, contribution)]"""
        contrib = self.trees.contributions(x) if self.kind == "boosted" else self._z(x).reshape(-1) * self.weights
        order = np.argsort(contrib)[::-1][:top]
        return [(self.feature_names[i], float(contrib[i])) for i in order if contrib[i] > 0]

    def save(self, path):
        meta = json.dumps({"channels": self.channels, "feature_names": self.feature_names, "window": self.window,
                           "period": self.period, "horizon": self.horizon, "target": self.target, "kind": self.kind,
                           "format_version": MODEL_FORMAT_VERSION, "model_version": self.model_version,
                           "optional_channels": self.optional_channels, "metadata": self.metadata}, sort_keys=True)
        if self.kind == "boosted":
            np.savez(path, meta=meta, **self.trees.arrays())
        else:
            np.savez(path, mean=self.mean, std=self.std, weights=self.weights, bias=self.bias, meta=meta)

    @classmethod
    def load(cls, path):
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        model = cls(meta["channels"], meta["feature_names"], meta["window"], meta["period"], meta["horizon"], meta["target"],
                    meta.get("kind", "logistic"), meta.get("optional_channels", ()))
        model.model_version = meta.get("model_version", "legacy/unspecified")
        model.metadata = meta.get("metadata", {})
        if model.kind == "boosted":
            model.trees = BoostedTrees.from_arrays(data)
        else:
            model.mean, model.std, model.weights, model.bias = data["mean"], data["std"], data["weights"], float(data["bias"])
        return model


class AnomalyDetector:
    def __init__(self):
        self.mean = self.std = None

    def fit(self, X_healthy):
        X = np.asarray(X_healthy, dtype=float)
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std < 1e-6] = 1e-6 + 0.05 * np.abs(self.mean[self.std < 1e-6])
        return self

    def score(self, X):
        """Largest |z-score| over the features of each window (about 3+ is unusual)."""
        return np.abs((np.atleast_2d(X) - self.mean) / self.std).max(axis=1)


def anomaly_path(model_path):
    """Where the normal-only detector of a model file is kept: watchdog_model.npz -> watchdog_model.anomaly.npz"""
    return os.path.splitext(str(model_path))[0] + ".anomaly.npz"


class SubspaceDetector:
    """Normal-only detector: it never sees a failure, so it can flag failures nobody has recorded yet.

    Fit on windows of healthy operation it learns the few directions along which the features normally vary
    (principal components). A new window is scored two ways, each divided by its own 99th percentile on healthy
    data so that 1.0 means "at the edge of normal":
      SPE  how much of the window lies OUTSIDE the normal directions (a relationship between signals broke:
           a flow that no longer matches the pump speed, a valve that stopped moving the pressure)
      T2   how far the window is along the normal directions (an unusual but self-consistent operating point)
    The score is the larger of the two. It says "this is not how this plant normally behaves", not what is
    wrong or when it will stop, so it complements the supervised RiskModel rather than replacing it.
    `predict_proba` squashes the score into 0..1 (0.5 at the edge of normal) so the same evaluation and
    threshold selection as the RiskModel apply; `explain` lists the features that deviate most."""

    def __init__(self, channels=(), feature_names=(), window=0, period=1.0, horizon=0.0, target="", variance=0.95,
                 max_components=40):
        self.channels, self.feature_names = list(channels), list(feature_names)
        self.window, self.period, self.horizon, self.target = window, period, horizon, target
        self.variance, self.max_components = variance, max_components
        self.mean = self.std = self.components = self.eigenvalues = None
        self.spe_scale = self.t2_scale = 1.0
        self.trained_windows = 0

    def _z(self, X):
        return np.clip((np.atleast_2d(np.asarray(X, dtype=float)) - self.mean) / self.std, -10.0, 10.0)

    def fit(self, X_healthy):
        X = np.asarray(X_healthy, dtype=float)
        if len(X) < 20:
            raise ValueError("The normal-only detector needs at least 20 healthy windows")
        self.mean = X.mean(axis=0)
        spread = X.std(axis=0)
        floor = np.maximum(1e-6, 0.02 * np.abs(self.mean))
        self.std = np.where(spread < floor, floor, spread)       # a near-constant feature must not explode on tiny noise
        Z = self._z(X)
        _, singular, vt = np.linalg.svd(Z - Z.mean(axis=0), full_matrices=False)
        variance = singular ** 2 / max(len(Z) - 1, 1)
        share = np.cumsum(variance) / max(variance.sum(), 1e-12)
        k = int(min(self.max_components, np.searchsorted(share, self.variance) + 1, len(variance)))
        self.components, self.eigenvalues = vt[:k], np.maximum(variance[:k], 1e-9)
        self.spe_scale = self.t2_scale = 1.0
        spe, t2 = self._statistics(X)
        self.spe_scale = max(float(np.percentile(spe, 99)), 1e-9)
        self.t2_scale = max(float(np.percentile(t2, 99)), 1e-9)
        self.trained_windows = len(X)
        return self

    def _statistics(self, X):
        Z = self._z(X)
        scores = Z @ self.components.T
        residual = Z - scores @ self.components
        return (residual ** 2).sum(axis=1) / self.spe_scale, ((scores ** 2) / self.eigenvalues).sum(axis=1) / self.t2_scale

    def score(self, X):
        """1.0 = the edge of normal; large = far outside anything seen in healthy operation."""
        spe, t2 = self._statistics(X)
        return np.maximum(spe, t2)

    def predict_proba(self, X):
        s = self.score(X)
        return s / (s + 1.0)

    def explain(self, x, top=3):
        """Features that deviate most from normal: [(name, z-score)]."""
        z = self._z(x).reshape(-1)
        order = np.argsort(-np.abs(z))[:top]
        return [(self.feature_names[i], float(z[i])) for i in order if abs(z[i]) > 3.0]

    def save(self, path):
        meta = json.dumps({"channels": self.channels, "feature_names": self.feature_names, "window": self.window,
                           "period": self.period, "horizon": self.horizon, "target": self.target,
                           "variance": self.variance, "trained_windows": self.trained_windows,
                           "kind": "subspace-normal-only"}, sort_keys=True)
        np.savez(path, meta=meta, mean=self.mean, std=self.std, components=self.components,
                 eigenvalues=self.eigenvalues, scales=np.array([self.spe_scale, self.t2_scale]))

    @classmethod
    def load(cls, path):
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        det = cls(meta["channels"], meta["feature_names"], meta["window"], meta["period"], meta["horizon"],
                  meta["target"], meta.get("variance", 0.95))
        det.mean, det.std, det.components, det.eigenvalues = data["mean"], data["std"], data["components"], data["eigenvalues"]
        det.spe_scale, det.t2_scale = (float(v) for v in data["scales"])
        det.trained_windows = meta.get("trained_windows", 0)
        return det
