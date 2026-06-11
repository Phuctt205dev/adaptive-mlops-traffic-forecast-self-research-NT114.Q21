import numpy as np
import tensorflow as tf
from tensorflow import keras


DEFAULT_RANDOM_STATE = 42


def configure_reproducibility(random_state=DEFAULT_RANDOM_STATE):
    """Cố định seed để các lần train neural ít chênh lệch hơn."""
    keras.utils.set_random_seed(random_state)
    try:
        tf.config.experimental.enable_op_determinism()
    except (AttributeError, RuntimeError):
        # Một số bản TensorFlow hoặc thiết bị không hỗ trợ chế độ này.
        pass


def inverse_scale_predictions(scaled_predictions, target_scaler):
    """Đổi dự đoán đã scale về đơn vị traffic_volume ban đầu."""
    values = np.asarray(
        scaled_predictions,
        dtype=np.float32,
    ).reshape(-1, 1)
    return target_scaler.inverse_transform(values).reshape(-1)
