import numpy as np

# =====================================================================
# BoostedTrees: gradient boosted decision trees (Newton boosting, logistic loss) in plain numpy.
# It is the stronger sibling of the logistic regression in model.py: it can learn thresholds and
# interactions ("winding temperature high AND voltage low"), which is how most equipment failures behave.
#
#   fit(X, y)          class-balanced, histogram based splits, small trees (depth 3) with shrinkage
#   decision(X)        raw score (log-odds)
#   contributions(x)   per-feature share of one prediction (path decomposition: how much each split
#                      on a feature moved the score), so the watchdog can say WHY it is worried
#
# Trees are stored as arrays in heap layout (children of node i are 2i+1 and 2i+2). A node that did not
# split has feature -1 and simply stays where it is during prediction.
# If scikit-learn is installed, a HistGradientBoosting model could replace this behind the same interface.
# =====================================================================


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _leaf_values(X, feat, thr, val):
    """Walk every row of X down one tree; returns the value of the node each row ends in."""
    depth = int(np.log2(len(feat) + 1)) - 1
    rows = np.arange(len(X))
    idx = np.zeros(len(X), dtype=np.int64)
    for _ in range(depth):
        f = feat[idx]
        split = f >= 0
        x = X[rows, np.where(split, f, 0)]
        idx = np.where(split, 2 * idx + 1 + (x > thr[idx]), idx)
    return val[idx]


class BoostedTrees:
    def __init__(self, rounds=60, depth=3, learning_rate=0.15, bins=32, l2=2.0, min_child_weight=2.0,
                 subsample=0.7, seed=0):
        self.rounds, self.depth, self.learning_rate = rounds, depth, learning_rate
        self.bins, self.l2, self.min_child_weight = bins, l2, min_child_weight
        self.subsample, self.seed = subsample, seed
        self.base = 0.0
        self.feature = self.threshold = self.value = None       # (rounds, nodes) arrays
        self.importance = None

    # ------------------------------------------------------------ training
    def fit(self, X, y):
        X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
        n, n_features = X.shape
        rng = np.random.default_rng(self.seed)
        edges = [np.unique(np.quantile(X[:, f], np.linspace(0, 1, self.bins + 1)[1:-1])) for f in range(n_features)]
        Xb = np.stack([np.searchsorted(edges[f], X[:, f], side="left") for f in range(n_features)], axis=1)
        n_pos, n_neg = max(y.sum(), 1.0), max((1 - y).sum(), 1.0)
        w = np.where(y > 0.5, 0.5 / n_pos, 0.5 / n_neg) * n         # balance the classes
        n_nodes = 2 ** (self.depth + 1) - 1
        feats = np.full((self.rounds, n_nodes), -1, dtype=np.int64)
        thrs = np.zeros((self.rounds, n_nodes))
        vals = np.zeros((self.rounds, n_nodes))
        self.importance = np.zeros(n_features)
        score = np.zeros(n)
        for r in range(self.rounds):
            p = _sigmoid(score)
            g, h = w * (p - y), w * np.maximum(p * (1 - p), 1e-6)
            rows = np.nonzero(rng.random(n) < self.subsample)[0] if self.subsample < 1.0 else np.arange(n)
            self._grow(Xb, g, h, rows, edges, feats[r], thrs[r], vals[r])
            score += _leaf_values(X, feats[r], thrs[r], vals[r])
        self.feature, self.threshold, self.value = feats, thrs, vals
        return self

    def _grow(self, Xb, g, h, rows, edges, feat, thr, val):
        """Build one tree level by level on the sample `rows`."""
        hess = np.zeros(len(feat))
        level = {0: rows}
        for depth in range(self.depth + 1):
            next_level = {}
            for node, idx in level.items():
                gi, hi = g[idx], h[idx]
                G, H = gi.sum(), hi.sum()
                hess[node] = H
                val[node] = -G / (H + self.l2) * self.learning_rate
                if depth == self.depth or len(idx) < 2:
                    continue
                best_gain, best_f, best_b = 1e-9, -1, 0
                for f in range(Xb.shape[1]):
                    k = len(edges[f])
                    if k == 0:
                        continue
                    col = Xb[idx, f]
                    GL = np.cumsum(np.bincount(col, weights=gi, minlength=k + 1)[:k])
                    HL = np.cumsum(np.bincount(col, weights=hi, minlength=k + 1)[:k])
                    GR, HR = G - GL, H - HL
                    ok = (HL >= self.min_child_weight) & (HR >= self.min_child_weight)
                    if not ok.any():
                        continue
                    gain = np.where(ok, GL ** 2 / (HL + self.l2) + GR ** 2 / (HR + self.l2) - G ** 2 / (H + self.l2), -np.inf)
                    b = int(np.argmax(gain))
                    if gain[b] > best_gain:
                        best_gain, best_f, best_b = float(gain[b]), f, b
                if best_f < 0:
                    continue
                feat[node], thr[node] = best_f, edges[best_f][best_b]
                self.importance[best_f] += best_gain
                go_left = Xb[idx, best_f] <= best_b
                next_level[2 * node + 1], next_level[2 * node + 2] = idx[go_left], idx[~go_left]
            level = next_level
        for node in range(len(feat) - 1, -1, -1):           # a split node's value = hessian weighted mean of its children
            if feat[node] >= 0:
                l, r = 2 * node + 1, 2 * node + 2
                val[node] = (val[l] * hess[l] + val[r] * hess[r]) / (hess[l] + hess[r])

    # ------------------------------------------------------------ prediction
    def decision(self, X):
        X = np.atleast_2d(np.asarray(X, dtype=float))
        total = np.full(len(X), self.base)
        for r in range(len(self.feature)):
            total += _leaf_values(X, self.feature[r], self.threshold[r], self.value[r])
        return total

    def predict_proba(self, X):
        return _sigmoid(self.decision(X))

    def contributions(self, x):
        """Per-feature contribution (log-odds) to the score of ONE window."""
        x = np.asarray(x, dtype=float).reshape(-1)
        out = np.zeros(len(x))
        for r in range(len(self.feature)):
            feat, thr, val = self.feature[r], self.threshold[r], self.value[r]
            node = 0
            while feat[node] >= 0:
                f = feat[node]
                child = 2 * node + 1 + int(x[f] > thr[node])
                out[f] += val[child] - val[node]
                node = child
        return out

    # ------------------------------------------------------------ persistence
    def arrays(self):
        return {"bt_feature": self.feature, "bt_threshold": self.threshold, "bt_value": self.value,
                "bt_importance": self.importance, "bt_base": np.array(self.base)}

    @classmethod
    def from_arrays(cls, data):
        model = cls()
        model.feature, model.threshold, model.value = data["bt_feature"], data["bt_threshold"], data["bt_value"]
        model.importance, model.base = data["bt_importance"], float(data["bt_base"])
        return model
