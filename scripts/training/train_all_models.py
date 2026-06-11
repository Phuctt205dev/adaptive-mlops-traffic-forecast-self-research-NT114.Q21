import argparse
import subprocess
import sys
from pathlib import Path

from src.best_model_selection import select_and_save_best_model
from src.training_variants import VARIANT_SPECS


TRAIN_MODULES = {
    "random_forest_no_lag": (
        "scripts.training.train_random_forest_no_lag"
    ),
    "random_forest_lag": "scripts.training.train_random_forest_lag",
    "xgboost_no_lag": "scripts.training.train_xgboost_no_lag",
    "xgboost_lag": "scripts.training.train_xgboost_lag",
    "lightgbm_no_lag": "scripts.training.train_lightgbm_no_lag",
    "lightgbm_lag": "scripts.training.train_lightgbm_lag",
    "lstm": "scripts.training.train_lstm",
    "gru": "scripts.training.train_gru",
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Train eight variants and select the best by CV MAE."
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=tuple(VARIANT_SPECS),
        default=list(VARIANT_SPECS),
        help="Run selected variants; default runs all eight.",
    )
    parser.add_argument("--cv-splits", type=int, default=5)
    parser.add_argument("--max-epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Select from existing reports without retraining.",
    )
    parser.add_argument(
        "--quiet-neural",
        action="store_true",
        help="Hide LSTM/GRU epoch logs.",
    )
    return parser.parse_args()


def report_path_for_variant(variant):
    return str(
        Path("results/time_series_cross_validation")
        / f"{variant}_report.json"
    )


def run_trainer(variant, args):
    """Gọi trainer bằng đúng Python của môi trường hiện tại."""
    project_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        "-m",
        TRAIN_MODULES[variant],
        "--cv-splits",
        str(args.cv_splits),
        "--max-epochs",
        str(args.max_epochs),
        "--batch-size",
        str(args.batch_size),
        "--random-state",
        str(args.random_state),
    ]
    if args.quiet_neural and variant in ("lstm", "gru"):
        command.append("--quiet")

    print(f"\n=== Training {variant} ===", flush=True)
    subprocess.run(
        command,
        check=True,
        cwd=project_root,
    )


def main():
    args = parse_arguments()
    variants = list(dict.fromkeys(args.only))

    if not args.skip_training:
        for variant in variants:
            run_trainer(variant, args)

    report_paths = [
        report_path_for_variant(variant)
        for variant in variants
    ]
    complete_selection = set(variants) == set(VARIANT_SPECS)
    result = select_and_save_best_model(
        report_paths,
        save_winner_as_champion=complete_selection,
    )

    print("\n=== Best model selection completed ===")
    for row in result["ranking"]:
        print(
            f"{row['rank']}. {row['variant']:24} | "
            f"CV MAE={row['cv_mean_MAE']:.4f} | "
            f"CV std={row['cv_std_MAE']:.4f} | "
            f"Final Test MAE={row['final_test_MAE']:.4f}"
        )

    print(f"\nSelected variant : {result['selected_variant']}")
    if result["champion"] is not None:
        champion = result["champion"]
        print(f"Champion version : {champion['champion_version']}")
        print(f"Champion info    : {champion['info_path']}")
    else:
        print(
            "Champion not saved: this run did not include all "
            "eight variants."
        )
    print(f"Ranking          : {result['ranking_path']}")


if __name__ == "__main__":
    main()
