import numpy as np

# =====================================================================
# Turning a window of recent samples into the numbers a model can learn from.
#
# For every channel in the window we compute four features:
#   last   the newest value                      (where is it now)
#   mean   the average over the window           (typical level)
#   std    the spread over the window            (how noisy / unstable)
#   trend  the change over the window by linear fit (is it drifting up or down)
# The trend features are what catch slow degradation: winding temperature creeping up,
# current rising as insulation ages, vibration growing with wear.
# =====================================================================

FEATURES = ("last", "mean", "std", "trend")


def feature_names(channels):
    return [f"{c}:{f}" for c in channels for f in FEATURES]


def window_features(window):
    """window: array (samples, channels) -> flat feature vector (channels * 4,)"""
    window = np.asarray(window, dtype=float)
    n = window.shape[0]
    x = np.arange(n) - (n - 1) / 2.0
    denom = float((x * x).sum()) or 1.0
    mean = window.mean(axis=0)
    slope = (x[:, None] * (window - mean)).sum(axis=0) / denom
    feats = np.stack([window[-1], mean, window.std(axis=0), slope * (n - 1)], axis=1)
    return feats.reshape(-1)


def sliding_features(matrix, window, stride=1):
    """Features for every window of `window` rows. Returns (features, index of the last row of each window)."""
    ends = np.arange(window - 1, len(matrix), stride)
    if len(ends) == 0:
        return np.zeros((0, matrix.shape[1] * len(FEATURES))), ends
    return np.array([window_features(matrix[e - window + 1:e + 1]) for e in ends]), ends
