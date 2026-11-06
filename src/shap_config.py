"""Shared configuration validation without the SHAP execution engine."""
from dataclasses import asdict,dataclass

@dataclass(frozen=True)
class ShapConfig:
    enabled: bool = True
    max_samples: int = 32
    background_samples: int = 8
    batch_size: int = 4
    nsamples: int = 64
    max_seconds: int = 60


def resolve_shap_config(config=None):
    if isinstance(config, ShapConfig):
        config = asdict(config)
    if config is not None and not isinstance(config, dict):
        raise ValueError("SHAP configuration must be an object.")
    try:
        options = ShapConfig(**(config or {}))
    except TypeError as error:
        raise ValueError("Unknown SHAP configuration field.") from error
    if type(options.enabled) is not bool:
        raise ValueError("SHAP enabled must be a boolean.")
    for name, upper in (("max_samples", 128), ("background_samples", 64),
                        ("batch_size", 16), ("nsamples", 256), ("max_seconds", 300)):
        value = getattr(options, name)
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(f"SHAP {name} must be an integer between 1 and {upper}.")
    return options
