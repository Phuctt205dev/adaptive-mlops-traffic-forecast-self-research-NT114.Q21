"""Shared holdout sizing for tree and recurrent training."""
import math


def final_test_size(total_rows, ratio=0.15):
    if not math.isfinite(ratio) or not 0 < ratio < 0.5:
        raise ValueError("final_test_ratio must be greater than 0 and less than 0.5.")
    size = max(1, int(round(total_rows * ratio)))
    if total_rows - size <= 1:
        raise ValueError("The training window is too short for development and final test data.")
    return size
