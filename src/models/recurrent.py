from tensorflow import keras


RECURRENT_MODEL_NAMES = ("LSTM", "GRU")


def build_recurrent_model(
    model_name,
    input_shape,
    recurrent_units=48,
    dense_units=24,
    dropout_rate=0.2,
    learning_rate=0.001,
):
    """Build an LSTM or GRU model for hourly traffic sequences."""
    normalized_name = model_name.upper()
    if normalized_name not in RECURRENT_MODEL_NAMES:
        raise ValueError(
            f"Unsupported model: {model_name}. "
            f"Use one of: {', '.join(RECURRENT_MODEL_NAMES)}."
        )
    if len(input_shape) != 2:
        raise ValueError("input_shape must be shaped as (time_steps, features).")

    recurrent_layer = (
        keras.layers.LSTM
        if normalized_name == "LSTM"
        else keras.layers.GRU
    )
    inputs = keras.Input(shape=input_shape, name="traffic_history")
    hidden = recurrent_layer(
        recurrent_units,
        name=f"{normalized_name.lower()}_layer",
    )(inputs)
    hidden = keras.layers.Dense(
        dense_units,
        activation="relu",
        name="dense_features",
    )(hidden)
    hidden = keras.layers.Dropout(
        dropout_rate,
        name="regularization_dropout",
    )(hidden)
    outputs = keras.layers.Dense(1, name="scaled_traffic")(hidden)

    model = keras.Model(
        inputs=inputs,
        outputs=outputs,
        name=f"traffic_{normalized_name.lower()}",
    )
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=learning_rate),
        # Huber loss reduces the impact of traffic outliers.
        loss=keras.losses.Huber(),
        metrics=[keras.metrics.MeanAbsoluteError(name="mae")],
    )
    return model


def build_training_callbacks():
    """Stop early and reduce learning rate when validation stops improving."""
    return [
        keras.callbacks.EarlyStopping(
            monitor="val_loss",
            patience=4,
            restore_best_weights=True,
            verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss",
            factor=0.5,
            patience=2,
            min_lr=0.00001,
            verbose=1,
        ),
    ]
