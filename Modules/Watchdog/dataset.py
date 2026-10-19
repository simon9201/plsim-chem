import numpy as np

from Modules.Watchdog.features import sliding_features

# =====================================================================
# Labelled training data: "will this tag trip within the next `horizon` seconds?"
#
# The target is a channel that is 1 while the stop is active, for example "P101.Fault".
# A sample is positive when a rising edge of the target happens within (t, t + horizon].
# Rows where the target is already active are dropped (nothing left to predict), and the target
# channel is removed from the inputs so the model cannot cheat by looking at the answer.
# =====================================================================


def rising_edges(times, values):
    """Times at which a 0/1 channel goes from 0 to 1."""
    v = np.asarray(values) > 0.5
    idx = np.nonzero(~v[:-1] & v[1:])[0] + 1
    return np.asarray(times)[idx]


def build_dataset(times, matrix, names, target, horizon, window, stride=1, drop=()):
    """Returns (X, y, feature channel names). `window` is in samples; `horizon` in seconds."""
    if target not in names:
        raise ValueError(f"Target channel '{target}' is not in the recording")
    t_col = names.index(target)
    # A PLC fault bit is an operator-visible measurement, but using any trip bit as a predictor
    # would leak a failure label into a different target model. EVENT/TRUTH columns are evaluation-only.
    skip = {target, *drop}
    skip.update(n for n in names if n.endswith(".Fault") or n.startswith(("EVENT.", "TRUTH.")))
    keep = [i for i, n in enumerate(names) if n not in skip]
    channels = [names[i] for i in keep]
    edges = rising_edges(times, matrix[:, t_col])

    X, ends = sliding_features(matrix[:, keep], window, stride)
    if len(ends) == 0:
        return X, np.zeros(0), channels
    t_end = np.asarray(times)[ends]
    y = np.zeros(len(ends))
    for e in edges:
        y[(t_end < e) & (t_end >= e - horizon)] = 1.0
    active = matrix[ends, t_col] > 0.5
    return X[~active], y[~active], channels
