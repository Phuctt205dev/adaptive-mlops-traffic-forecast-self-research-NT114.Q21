"""Optional structured progress; the default reporter is a no-op.

These hooks observe existing fits. They do not change data, estimators,
cross-validation, early stopping, selection or final-test policy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import time

_reporter = ContextVar("training_reporter", default=None)
_sequence = ContextVar("training_event_sequence", default=0)
_fold = ContextVar("training_fold", default={})
_origin = ContextVar("training_clock_origin", default=None)
_fit = ContextVar("training_fit", default={})
_fit_sequence = ContextVar("training_fit_sequence", default=0)
ALIASES = {"RandomForest": "random_forest_lag", "XGBoost": "xgboost_lag", "LightGBM": "lightgbm_lag",
           "LSTM": "lstm", "GRU": "gru"}


def enabled():
    return _reporter.get() is not None


@contextmanager
def reporting(callback):
    tokens = (_reporter.set(callback), _sequence.set(0), _fold.set({}),
              _origin.set(time.monotonic()), _fit.set({}), _fit_sequence.set(0))
    try:
        yield
    finally:
        _fold.reset(tokens[2]); _sequence.reset(tokens[1]); _reporter.reset(tokens[0])
        _origin.reset(tokens[3]); _fit.reset(tokens[4]); _fit_sequence.reset(tokens[5])


def set_fold(fold=None, folds=None):
    _fold.set({"fold": fold, "folds": folds} if fold is not None else {})
    _fit.set({})


def start_fit(model, purpose):
    if not enabled():
        return
    if purpose not in {"cv_full", "cv_reduced", "cv_reused", "final"}:
        raise ValueError("Unknown training fit purpose.")
    number = _fit_sequence.get() + 1
    _fit_sequence.set(number)
    _fit.set({"fit_id": number, "fit_purpose": purpose})
    report(model, "fit_started")


def report(model, stage, **details):
    callback = _reporter.get()
    if callback is None:
        return
    seq = _sequence.get() + 1
    _sequence.set(seq)
    callback({"sequence": seq, "model": ALIASES.get(model, model), "stage": stage,
              **_fold.get(), **_fit.get(), **details,
              "source_time": datetime.now(UTC).isoformat(), "source_clock": "ml_child_monotonic_v1",
              "source_elapsed_seconds": round(max(0, time.monotonic() - _origin.get()), 6)})


def epoch_callbacks(model, epochs):
    if _reporter.get() is None:
        return []
    from tensorflow import keras
    scope = dict(_fold.get())

    class Progress(keras.callbacks.Callback):
        completed = 0

        def on_epoch_begin(self, epoch, logs=None):
            report(model, "epoch_started", **scope, epoch=epoch + 1, epochs=int(epochs))

        def on_epoch_end(self, epoch, logs=None):
            self.completed = epoch + 1
            details = {"epoch": epoch + 1, "epochs": int(epochs), **scope}
            loss = (logs or {}).get("loss")
            if loss is not None:
                import math
                value = float(loss)
                if math.isfinite(value):
                    details["loss"] = value
            report(model, "epoch_completed", **details)

        def on_train_end(self, logs=None):
            report(model, "fit_completed", **scope, epochs=int(epochs), epochs_completed=self.completed,
                   early_stopped=self.completed < int(epochs))

    return [Progress()]
