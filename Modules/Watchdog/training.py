import numpy as np

from Modules.Watchdog.dataset import build_dataset
from Modules.Watchdog.features import feature_names
from Modules.Watchdog.model import AnomalyDetector, RiskModel, auc
from Modules.Watchdog.scenarios import TARGET, run_episode
from Modules.Watchdog.watchdog import alarm_transitions

# =====================================================================
# Training and evaluating a watchdog on simulated episodes.
#
#   train_watchdog(train_seeds, validation_seeds, test_seeds) runs independent episodes, fits on the training
#   set, selects an alarm threshold under a false-alarm budget on validation episodes, and reports discrimination,
#   event recall, alert precision, false alarms/hour and lead time on test episodes. Splits are by episode, never
#   by window, to prevent neighbouring windows from one run leaking across sets.
# =====================================================================


def collect(seeds, target=TARGET, horizon=600.0, window=60, stride=3, sample_period=5.0, max_seconds=7200.0):
    """Run episodes and return a list of (X, y) per episode plus the channel list."""
    records = collect_recordings(seeds, sample_period, max_seconds)
    parts, channels = parts_from_recordings([r[1] for r in records], target, horizon, window, stride)
    return parts, channels


def collect_recordings(seeds, sample_period=5.0, max_seconds=7200.0):
    """Return seeded, held-out-capable records: (failure mode, (times, matrix, names))."""
    from Modules.Watchdog.scenarios import FAILURE_MODES
    return [(FAILURE_MODES[seed % len(FAILURE_MODES)],
             run_episode(seed, max_seconds=max_seconds, sample_period=sample_period)) for seed in seeds]


def stack(parts):
    return np.vstack([p[0] for p in parts]), np.concatenate([p[1] for p in parts])


def fit(parts, channels, window, sample_period, horizon, target, kind="boosted"):
    """Fit the RiskModel and AnomalyDetector on (X, y) parts. Returns (model, detector, train metrics)."""
    if not parts or channels is None:
        raise ValueError("No training windows were produced; provide longer recordings or a smaller window")
    X, y = stack(parts)
    if not np.isfinite(X).all() or not np.isfinite(y).all():
        raise ValueError("Training data contains non-finite values")
    if len(np.unique(y)) < 2:
        raise ValueError("Training data must contain both pre-failure and healthy windows")
    model = RiskModel(channels, feature_names(channels), window, sample_period, horizon, target, kind).fit(X, y)
    detector = AnomalyDetector().fit(X[y < 0.5])
    return model, detector, {"train_windows": len(y), "train_positive": int(y.sum()),
                             "train_auc": auc(y, model.predict_proba(X))}


def parts_from_recordings(recordings, target, horizon=600.0, window=60, stride=3):
    """Windows from recorded runs [(times, matrix, names)], e.g. loaded with recorder.load_csv."""
    recordings = list(recordings)
    if not recordings:
        return [], None
    available = []
    for _, _, names in recordings:
        if target not in names:
            raise ValueError(f"Target channel '{target}' is missing from a recording")
        available.append({name for name in names if name != target and not name.endswith(".Fault")
                          and not name.startswith(("EVENT.", "TRUTH."))})
    channels = sorted(set.intersection(*available))
    if not channels:
        raise ValueError("Recordings have no common PLC feedback channels besides the target")
    parts = []
    for times, matrix, names in recordings:
        keep = [names.index(target)] + [names.index(channel) for channel in channels]
        selected_names = [target] + channels
        selected_matrix = np.asarray(matrix)[:, keep]
        X, y, _ = build_dataset(times, selected_matrix, selected_names, target, horizon, window, stride)
        if len(X):
            parts.append((X, y))
    return parts, channels


def _evaluate_recordings(model, recordings, threshold, stride=3):
    """Evaluate complete episodes at an explicit score threshold; never mix windows across episodes."""
    from Modules.Watchdog.dataset import rising_edges

    y_all, score_all, event_count, detected, false_alarms, duration_hours, leads = [], [], 0, 0, 0, 0.0, []
    per_mode = {}
    for mode, (times, matrix, names) in recordings:
        required = [model.target] + model.channels
        missing = [name for name in required if name not in names]
        if missing:
            raise ValueError(f"Evaluation recording is missing model channels: {missing}")
        selected = np.asarray(matrix)[:, [names.index(name) for name in required]]
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
                   max_false_alarms_per_hour=0.1):
    """Returns (RiskModel, AnomalyDetector, metrics dict) from simulated episodes."""
    parts, channels = collect(train_seeds, target, horizon, window, stride, sample_period, max_seconds)
    model, detector, metrics = fit(parts, channels, window, sample_period, horizon, target, kind)
    selected_threshold = alarm_threshold
    if len(validation_seeds):
        validation_records = collect_recordings(validation_seeds, sample_period, max_seconds)
        selected_threshold, validation_metrics = select_alarm_threshold(
            model, validation_records, max_false_alarms_per_hour, stride)
        metrics["validation_selected_threshold"] = selected_threshold
        metrics["validation_false_alarms_per_hour"] = validation_metrics["false_alarms_per_hour"]
        metrics["validation_event_recall"] = validation_metrics["event_recall"]
        metrics["validation_alert_precision"] = validation_metrics["alert_precision"]
        metrics["validation_budget_met"] = (
            validation_metrics["false_alarms_per_hour"] is not None
            and validation_metrics["alarm_episodes"] > 0
            and validation_metrics["false_alarms_per_hour"] <= max_false_alarms_per_hour)
    metrics["selected_alarm_threshold"] = selected_threshold
    if len(test_seeds):
        test_records = collect_recordings(test_seeds, sample_period, max_seconds)
        test_parts, _ = parts_from_recordings([r[1] for r in test_records], target, horizon, window, stride)
        if test_parts:
            Xt, yt = stack(test_parts)
            metrics.update(test_windows=len(yt), test_positive=int(yt.sum()))
            evaluation = _evaluate_recordings(model, test_records, selected_threshold, stride)
            metrics.update({f"test_{k}": v for k, v in evaluation.items() if k != "by_failure_mode"})
            metrics["test_by_failure_mode"] = evaluation["by_failure_mode"]
    return model, detector, metrics
