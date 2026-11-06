"""Select by existing CV policy; no fitting or model imports."""

def select_best_candidate(candidates):
    if not candidates:
        raise ValueError("No model candidates were trained.")
    return min(
        candidates,
        key=lambda item: (
            float(item["validation_MAE"]),
            float(item.get("validation_MAE_std") or 0.0),
            str(item["best_model_name"]),
        ),
    )
