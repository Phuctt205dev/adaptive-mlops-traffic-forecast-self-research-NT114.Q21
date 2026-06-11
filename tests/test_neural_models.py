import os
import tempfile
import unittest

import numpy as np
from sklearn.preprocessing import StandardScaler
from tensorflow import keras

from src.models.recurrent import build_recurrent_model
from src.neural_utils import inverse_scale_predictions


class NeuralModelTests(unittest.TestCase):
    def tearDown(self):
        keras.backend.clear_session()

    def test_build_lstm_with_expected_shape(self):
        model = build_recurrent_model("LSTM", (168, 30))

        self.assertEqual(model.input_shape, (None, 168, 30))
        self.assertEqual(model.output_shape, (None, 1))
        self.assertIsInstance(model.get_layer("lstm_layer"), keras.layers.LSTM)

    def test_build_gru_with_expected_shape(self):
        model = build_recurrent_model("GRU", (168, 30))

        self.assertEqual(model.input_shape, (None, 168, 30))
        self.assertEqual(model.output_shape, (None, 1))
        self.assertIsInstance(model.get_layer("gru_layer"), keras.layers.GRU)

    def test_reject_unsupported_model(self):
        with self.assertRaisesRegex(ValueError, "không được hỗ trợ"):
            build_recurrent_model("RNN", (168, 30))

    def test_inverse_target_scaling(self):
        scaler = StandardScaler().fit(
            np.array([[100.0], [200.0], [300.0]])
        )
        scaled = scaler.transform(
            np.array([[150.0], [250.0]])
        )

        restored = inverse_scale_predictions(scaled, scaler)

        np.testing.assert_allclose(restored, [150.0, 250.0])

    def test_model_can_train_save_and_load(self):
        random = np.random.default_rng(42)
        X = random.normal(size=(12, 4, 3)).astype(np.float32)
        y = random.normal(size=(12, 1)).astype(np.float32)
        model = build_recurrent_model(
            "GRU",
            (4, 3),
            recurrent_units=4,
            dense_units=2,
        )
        model.fit(X, y, epochs=1, batch_size=4, verbose=0)

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "model.keras")
            model.save(path)
            loaded = keras.models.load_model(path)
            predictions = loaded.predict(X[:2], verbose=0)

        self.assertEqual(predictions.shape, (2, 1))


if __name__ == "__main__":
    unittest.main()
