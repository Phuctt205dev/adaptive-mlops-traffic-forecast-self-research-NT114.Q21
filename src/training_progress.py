"""Optional structured progress; the default reporter is a no-op.

These hooks observe existing fits. They do not change data, estimators,
cross-validation, early stopping, selection or final-test policy.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_reporter = ContextVar("training_reporter", default=None)
_sequence = ContextVar("training_event_sequence", default=0)
_fold = ContextVar("training_fold", default={})
ALIASES = {"RandomForest": "random_forest_lag", "XGBoost": "xgboost_lag", "LightGBM": "lightgbm_lag",
           "LSTM": "lstm", "GRU": "gru"}


def enabled():
    return _reporter.get() is not None


@contextmanager
def reporting(callback):
    tokens = (_reporter.set(callback), _sequence.set(0), _fold.set({}))
    try:
        yield
    finally:
        _fold.reset(tokens[2]); _sequence.reset(tokens[1]); _reporter.reset(tokens[0])


def set_fold(fold=None, folds=None):
    _fold.set({"fold": fold, "folds": folds} if fold is not None else {})


def report(model, stage, **details):
    callback = _reporter.get()
    if callback is None:
        return
    seq = _sequence.get() + 1
    _sequence.set(seq)
    callback({"sequence": seq, "model": ALIASES.get(model, model), "stage": stage, **_fold.get(), **details})


def epoch_callbacks(model, epochs):
    if _reporter.get() is None:
        return []
    from tensorflow import keras
    scope = dict(_fold.get())

    class Progress(keras.callbacks.Callback):
        def on_epoch_begin(self, epoch, logs=None):
            report(model, "epoch_started", **scope, epoch=epoch + 1, epochs=int(epochs))

        def on_epoch_end(self, epoch, logs=None):
            details = {"epoch": epoch + 1, "epochs": int(epochs), **scope}
            loss = (logs or {}).get("loss")
            if loss is not None:
                import math
                value = float(loss)
                if math.isfinite(value):
                    details["loss"] = value
            report(model, "epoch_completed", **details)

    return [Progress()]
