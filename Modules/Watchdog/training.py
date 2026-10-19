import numpy as np

from Modules.Watchdog.dataset import build_dataset
from Modules.Watchdog.features import feature_names
from Modules.Watchdog.model import RiskModel, SubspaceDetector, auc
from Modules.Watchdog.scenarios import FAULT_FREE_MODES, TARGET, run_episode
from Modules.Watchdog.watchdog import alarm_transitions

# =====================================================================
# Training and evaluating a watchdog on simulated episodes.
#
#   train_watchdog(train_seeds, validation_seeds, test_seeds) runs independent episodes, fits on the training
#   set, selects an alarm threshold under a false-alarm budget on validation episodes, and reports discrimination,
#   event recall, alert precision, false alarms/hour and lead time on test episodes. Splits are by episode, never
#   by window, to prevent neighbouring windows from one run leaking across sets.
# =====================================================================


def collect(seeds, target=TARGET, horizon=600.0, window=60, stride=3, sample_period=5.0, max_seconds=7200.0,
            corpus=None, workers=1):
    """Run episodes and return a list of (X, y) per episode plus the channel list."""
    records = collect_recordings(seeds, sample_period, max_seconds, corpus, workers)
    parts, channels = parts_from_recordings([r[1] for r in records], target, horizon, window, stride)
    return parts, channels


def collect_recordings(seeds, sample_period=5.0, max_seconds=7200.0, corpus=None, workers=1):
    """Return seeded, held-out-capable records: (failure mode, (times, matrix, names)).

    With `corpus` (a folder, see corpus.py) the episodes are generated once into it, in parallel, and read back
    on later runs; without it they are simulated in memory every time."""
    from Modules.Watchdog.corpus import build_corpus, generate_recordings, load_corpus
    seeds = list(seeds)
    if corpus:
        build_corpus(corpus, seeds, sample_period, max_seconds, workers)
        records = load_corpus(corpus, seeds)
    else:
        records = generate_recordings(seeds, sample_period, max_seconds, workers)
    return [(label[0], arrays) for label, arrays in records]


def collect_campaigns(seeds, sample_period=5.0, hours=6.0, workers=1):
    """Long multi-incident runs (scenarios.run_campaign) as records labelled "campaign"."""
    from concurrent.futures import ProcessPoolExecutor
    from functools import partial
    from Modules.Watchdog.scenarios import run_campaign
    seeds = list(seeds)
    run = partial(run_campaign, hours=hours, sample_period=sample_period)
    if workers > 1 and len(seeds) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            arrays = list(pool.map(run, seeds))
    else:
        arrays = [run(seed) for seed in seeds]
    return [("campaign", a) for a in arrays]


def stack(parts):
    return np.vstack([p[0] for p in parts]), np.concatenate([p[1] for p in parts])


def fit(parts, channels, window, sample_period, horizon, target, kind="boosted", healthy_parts=None, optional=()):
    """Fit the RiskModel and the normal-only SubspaceDetector. Returns (model, detector, train metrics).

    The detector learns from `healthy_parts` (windows of fault-free operation); without them it falls back to
    the windows not labelled pre-failure. It is None if there are too few healthy windows."""
    if not parts or channels is None:
        raise ValueError("No training windows were produced; provide longer recordings or a smaller window")
    X, y = stack(parts)
    if not np.isfinite(X).all() or not np.isfinite(y).all():
        raise ValueError("Training data contains non-finite values")
    if len(np.unique(y)) < 2:
        raise ValueError("Training data must contain both pre-failure and healthy windows")
    names = feature_names(channels)
    model = RiskModel(channels, names, window, sample_period, horizon, target, kind, optional).fit(X, y)
    healthy = np.vstack([p[0] for p in healthy_parts]) if healthy_parts else X[y < 0.5]
    detector = None
    if len(healthy) >= 20:
        detector = SubspaceDetector(channels, names, window, sample_period, horizon, target).fit(healthy)
    return model, detector, {"train_windows": len(y), "train_positive": int(y.sum()),
                             "train_auc": auc(y, model.predict_proba(X)),
                             "detector_healthy_windows": int(len(healthy)) if detector else 0}


class _DetectorAsModel:
    """Lets the evaluation and threshold selection written for the RiskModel score a normal-only detector."""

    def __init__(self, detector, optional_channels=()):
        self.detector = detector
        self.target, self.horizon, self.window = detector.target, detector.horizon, detector.window
        self.channels, self.period = detector.channels, detector.period
        self.optional_channels = tuple(optional_channels)

    def predict_proba(self, X):
        return self.detector.predict_proba(X)


def parts_from_recordings(recordings, target, horizon=600.0, window=60, stride=3):
    """Windows from recorded runs [(times, matrix, names)], e.g. loaded with recorder.load_csv.

    The model channels are every feedback channel found in any recording. Plants differ (a reactor loop has
    a reactor temperature, a plain loop does not), so a channel missing from a recording is read as 0.0 there;
    `optional_channels` lists which ones that applies to."""
    recordings = list(recordings)
    if not recordings:
        return [], None
    channels = _channels_of(recordings, target)
    parts = []
    for times, matrix, names in recordings:
        required = [target] + channels
        X, y, _ = build_dataset(times, _columns(matrix, names, required), required, target, horizon, window, stride)
        if len(X):
            parts.append((X, y))
    return parts, channels


def _channels_of(recordings, target):
    available = []
    for _, _, names in recordings:
        if target not in names:
            raise ValueError(f"Target channel '{target}' is missing from a recording")
        available.append({name for name in names if name != target and not name.endswith(".Fault")
                          and not name.startswith(("EVENT.", "TRUTH."))})
    channels = sorted(set.union(*available))
    if not channels:
        raise ValueError("Recordings have no PLC feedback channels besides the target")
    return channels


def optional_channels(recordings, target):
    """Channels that some recordings have and others lack (filled with 0.0 where absent)."""
    recordings = list(recordings)
    channels = _channels_of(recordings, target)
    return [c for c in channels if any(c not in names for _, _, names in recordings)]


def _columns(matrix, names, required):
    """The wanted columns of a recording in order; a channel the recording lacks is zero."""
    names, matrix = list(names), np.asarray(matrix)
    out = np.zeros((len(matrix), len(required)))
    for j, name in enumerate(required):
        if name in names:
            out[:, j] = matrix[:, names.index(name)]
    return out


def _evaluate_recordings(model, recordings, threshold, stride=3):
    """Evaluate complete episodes at an explicit score threshold; never mix windows across episodes."""
    from Modules.Watchdog.dataset import rising_edges

    y_all, score_all, event_count, detected, false_alarms, duration_hours, leads = [], [], 0, 0, 0, 0.0, []
    per_mode = {}
    for mode, (times, matrix, names) in recordings:
        required = [model.target] + model.channels
        optional = set(getattr(model, "optional_channels", ()))
        missing = [name for name in required if name not in names and name not in optional]
        if missing:
            raise ValueError(f"Evaluation recording is missing model channels: {missing}")
        selected = _columns(matrix, names, required)
        X, y, _ = build_dataset(times, selected, required, model.target, model.horizon, model.window, stride=stride)
        target_col = 0
        ends = np.arange(model.window - 1, len(times), stride)
        ends = ends[selected[ends, target_col] <= 0.5]
        if len(X) != len(ends):
            raise ValueError("Window labels and timestamps do not align")
        scores = model.predict_proba(X).reshape(-1)
        events = rising_edges(times, selected[:, target_col])
        _, onset_indices = alarm_transitions(scores, threshold)
        alarm_edges = times[ends[onset_indices]]
        assigned, episode_leads, episode_false = set(), [], 0
        for alarm_time in alarm_edges:
            candidates = [(i, event - alarm_time) for i, event in enumerate(events)
                          if i not in assigned and 0.0 < event - alarm_time <= model.horizon]
            if candidates:
                i, lead = min(candidates, key=lambda item: item[1])
                assigned.add(i)
                episode_leads.append(float(lead))
            else:
                episode_false += 1
        episode_detected = len(assigned)
        episode_hours = max(0.0, float(times[-1] - times[0])) / 3600.0 if len(times) > 1 else 0.0
        y_all.extend(y.tolist())
        score_all.extend(scores.tolist())
        event_count += len(events)
        detected += episode_detected
        false_alarms += episode_false
        duration_hours += episode_hours
        leads.extend(episode_leads)
        stats = per_mode.setdefault(mode, {"episodes": 0, "events": 0, "detected": 0, "false_alarms": 0,
                                           "duration_hours": 0.0, "lead_times": []})
        stats["episodes"] += 1
        stats["events"] += len(events)
        stats["detected"] += episode_detected
        stats["false_alarms"] += episode_false
        stats["duration_hours"] += episode_hours
        stats["lead_times"].extend(episode_leads)

    def summarize(y, scores, events, found, false, hours, lead_times):
        alarm_count = found + false
        roc_auc = auc(np.asarray(y), np.asarray(scores)) if y else float("nan")
        return {
            "auc": roc_auc if np.isfinite(roc_auc) else None,
            "event_recall": found / events if events else None,
            "alert_precision": found / alarm_count if alarm_count else None,
            "false_alarms_per_hour": false / hours if hours else None,
            "median_lead_time_s": float(np.median(lead_times)) if lead_times else None,
            "events": events,
            "detected_events": found,
            "false_alarm_episodes": false,
            "alarm_episodes": alarm_count,
            "operating_hours": hours,
        }

    result = summarize(y_all, score_all, event_count, detected, false_alarms, duration_hours, leads)
    result["threshold"] = float(threshold)
    result["alarm_samples"] = 2
    result["clear_samples"] = 3
    result["by_failure_mode"] = {
        mode: summarize([], [], s["events"], s["detected"], s["false_alarms"], s["duration_hours"], s["lead_times"])
        for mode, s in sorted(per_mode.items())
    }
    return result


def select_alarm_threshold(model, recordings, max_false_alarms_per_hour=0.1, stride=3):
    """Choose an operating threshold on a held-out validation set, never on the final test set.

    Among thresholds meeting the false-alarm budget, maximize event recall, then alert precision.
    If no alerting threshold meets the budget, return a no-alert sentinel (>1) rather than presenting
    an unvalidated threshold as acceptable.
    """
    if max_false_alarms_per_hour < 0:
        raise ValueError("false-alarm budget cannot be negative")
    candidates = list(np.linspace(0.0, 1.0, 101)) + [1.000001]
    evaluated = [(threshold, _evaluate_recordings(model, recordings, threshold, stride)) for threshold in candidates]
    feasible = [item for item in evaluated
                if item[1]["false_alarms_per_hour"] is not None
                and item[1]["false_alarms_per_hour"] <= max_false_alarms_per_hour
                and item[1]["alarm_episodes"] > 0]
    if not feasible:
        return 1.000001, _evaluate_recordings(model, recordings, 1.000001, stride)
    threshold, metrics = max(
        feasible,
        key=lambda item: (item[1]["event_recall"] if item[1]["event_recall"] is not None else -1.0,
                          item[1]["alert_precision"] if item[1]["alert_precision"] is not None else -1.0,
                          -item[0]))
    return float(threshold), metrics


def train_watchdog(train_seeds, test_seeds=(), target=TARGET, horizon=600.0, window=60, stride=3, sample_period=5.0,
                   max_seconds=7200.0, kind="boosted", alarm_threshold=0.8, validation_seeds=(),
                   max_false_alarms_per_hour=0.1, corpus=None, workers=1, campaigns=0, campaign_hours=6.0):
    """Returns (RiskModel, SubspaceDetector or None, metrics dict) from simulated episodes (cached in `corpus` if given).

    `campaigns` adds that many long multi-incident runs (scenarios.run_campaign) to training, and a third as many
    to each of the validation and test sets.
    The detector learns only from the fault-free episodes (baseline, benign transients, sensor nuisance).
    It gets its own validation-selected threshold under the same false-alarm budget (`anomaly_*` metrics)."""
    records = collect_recordings(train_seeds, sample_period, max_seconds, corpus, workers)
    campaign_hours_args = dict(sample_period=sample_period, hours=campaign_hours, workers=workers)
    if campaigns:
        records += collect_campaigns(range(5000, 5000 + campaigns), **campaign_hours_args)
    recordings = [r[1] for r in records]
    parts, channels = parts_from_recordings(recordings, target, horizon, window, stride)
    optional = optional_channels(recordings, target)
    healthy_parts = [_align(recording, channels, target, horizon, window, stride)
                     for mode, recording in records if mode in FAULT_FREE_MODES]
    model, detector, metrics = fit(parts, channels, window, sample_period, horizon, target, kind,
                                   [p for p in healthy_parts if p is not None] or None, optional)
    metrics["optional_channels"] = optional
    scorers = [("", model)] + ([("anomaly_", _DetectorAsModel(detector, optional))] if detector is not None else [])
    selected_threshold, anomaly_threshold = alarm_threshold, 0.75
    if len(validation_seeds):
        validation_records = collect_recordings(validation_seeds, sample_period, max_seconds, corpus, workers)
        if campaigns:
            validation_records += collect_campaigns(range(6000, 6000 + max(1, campaigns // 3)), **campaign_hours_args)
        for prefix, scorer in scorers:
            chosen, found = select_alarm_threshold(scorer, validation_records, max_false_alarms_per_hour, stride)
            if prefix:
                anomaly_threshold = chosen
            else:
                selected_threshold = chosen
            key = "validation_" if not prefix else "anomaly_validation_"
            metrics[f"{key}selected_threshold"] = chosen
            metrics[f"{key}false_alarms_per_hour"] = found["false_alarms_per_hour"]
            metrics[f"{key}event_recall"] = found["event_recall"]
            metrics[f"{key}alert_precision"] = found["alert_precision"]
            metrics[f"{key}budget_met"] = (
                found["false_alarms_per_hour"] is not None and found["alarm_episodes"] > 0
                and found["false_alarms_per_hour"] <= max_false_alarms_per_hour)
    metrics["selected_alarm_threshold"] = selected_threshold
    if detector is not None:
        metrics["selected_anomaly_threshold"] = anomaly_threshold
    if len(test_seeds):
        test_records = collect_recordings(test_seeds, sample_period, max_seconds, corpus, workers)
        if campaigns:
            test_records += collect_campaigns(range(7000, 7000 + max(1, campaigns // 3)), **campaign_hours_args)
        test_parts, _ = parts_from_recordings([r[1] for r in test_records], target, horizon, window, stride)
        if test_parts:
            Xt, yt = stack(test_parts)
            metrics.update(test_windows=len(yt), test_positive=int(yt.sum()))
            evaluation = _evaluate_recordings(model, test_records, selected_threshold, stride)
            metrics.update({f"test_{k}": v for k, v in evaluation.items() if k != "by_failure_mode"})
            metrics["test_by_failure_mode"] = evaluation["by_failure_mode"]
            if detector is not None:
                found = _evaluate_recordings(_DetectorAsModel(detector, optional), test_records, anomaly_threshold,
                                             stride)
                metrics.update({f"anomaly_test_{k}": v for k, v in found.items() if k != "by_failure_mode"})
                metrics["anomaly_test_by_failure_mode"] = found["by_failure_mode"]
    return model, detector, metrics


def _align(recording, channels, target, horizon, window, stride):
    """Windows of one recording on the shared channel list (channels it lacks are zero)."""
    times, matrix, names = recording
    required = [target] + channels
    X, y, _ = build_dataset(times, _columns(matrix, names, required), required, target, horizon, window, stride)
    return (X, y) if len(X) else None
