import json

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
#   AnomalyDetector  unsupervised: learns what healthy windows look like and scores how far a new window
#                    is from that (largest z-score). Useful before you have any recorded failures.
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
    def __init__(self, channels=(), feature_names=(), window=0, period=1.0, horizon=0.0, target="", kind="logistic"):
        self.channels = list(channels)
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
                           "metadata": self.metadata}, sort_keys=True)
        if self.kind == "boosted":
            np.savez(path, meta=meta, **self.trees.arrays())
        else:
            np.savez(path, mean=self.mean, std=self.std, weights=self.weights, bias=self.bias, meta=meta)

    @classmethod
    def load(cls, path):
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        model = cls(meta["channels"], meta["feature_names"], meta["window"], meta["period"], meta["horizon"], meta["target"],
                    meta.get("kind", "logistic"))
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
