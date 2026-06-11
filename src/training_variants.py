import argparse

import pandas as pd

from src.time_series_cross_validation import (
    run_model_time_series_cross_validation,
)
from src.time_series_splits import DEFAULT_CV_SPLITS


VARIANT_SPECS = {
    "random_forest_no_lag": {
        "model_name": "RandomForest",
        "tree_profile": "no_lag",
    },
    "random_forest_lag": {
        "model_name": "RandomForest",
        "tree_profile": "autoregressive",
    },
    "xgboost_no_lag": {
        "model_name": "XGBoost",
        "tree_profile": "no_lag",
    },
    "xgboost_lag": {
        "model_name": "XGBoost",
        "tree_profile": "autoregressive",
    },
    "lightgbm_no_lag": {
        "model_name": "LightGBM",
        "tree_profile": "no_lag",
    },
    "lightgbm_lag": {
        "model_name": "LightGBM",
        "tree_profile": "autoregressive",
    },
    "lstm": {
        "model_name": "LSTM",
        "tree_profile": None,
    },
    "gru": {
        "model_name": "GRU",
        "tree_profile": None,
    },
}

DEFAULT_FEATURE_PATH = (
    "data/processed/TrafficVolumeData_features.csv"
)
DEFAULT_HOURLY_PATH = "data/TrafficVolumeData.csv"
DEFAULT_AUDIT_PATH = (
    "data/processed/TrafficVolumeData_hourly_audit.csv"
)


def build_variant_parser(variant_name):
    """Tạo CLI giống nhau cho cả 8 file train."""
    parser = argparse.ArgumentParser(
        description=f"Train variant: {variant_name}."
    )
    parser.add_argument(
        "--features",
        default=DEFAULT_FEATURE_PATH,
    )
    parser.add_argument(
        "--hourly",
        default=DEFAULT_HOURLY_PATH,
    )
    parser.add_argument(
        "--audit",
        default=DEFAULT_AUDIT_PATH,
    )
    parser.add_argument(
        "--cv-splits",
        type=int,
        default=DEFAULT_CV_SPLITS,
    )
    parser.add_argument(
        "--sequence-length",
        type=int,
        default=168,
    )
    parser.add_argument("--max-epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide neural epoch logs.",
    )
    return parser


def run_training_variant(variant_name, args):
    """Chạy đúng một biến thể và trả về báo cáo chuẩn hóa."""
    try:
        spec = VARIANT_SPECS[variant_name]
    except KeyError as error:
        raise ValueError(
            f"Biến thể không được hỗ trợ: {variant_name}."
        ) from error

    feature_df = pd.read_csv(args.features)
    hourly_df = None
    audit_df = None
    needs_hourly = (
        spec["tree_profile"] == "no_lag"
        or spec["model_name"] in ("LSTM", "GRU")
    )
    if needs_hourly:
        hourly_df = pd.read_csv(args.hourly)
        audit_df = pd.read_csv(args.audit)

    return run_model_time_series_cross_validation(
        model_name=spec["model_name"],
        feature_df=feature_df,
        hourly_df=hourly_df,
        audit_df=audit_df,
        n_splits=args.cv_splits,
        sequence_length=args.sequence_length,
        max_epochs=args.max_epochs,
        batch_size=args.batch_size,
        random_state=args.random_state,
        verbose=0 if args.quiet else 2,
        tree_profile=(
            spec["tree_profile"] or "autoregressive"
        ),
        artifact_name=variant_name,
    )


def print_variant_result(report):
    """In kết quả quan trọng để người mới dễ theo dõi."""
    print("\nTraining completed.")
    print(f"Variant                  : {report['variant']}")
    for fold in report["folds"]:
        print(
            f"Fold {fold['fold']} MAE               : "
            f"{fold['validation_metrics']['MAE']:.4f}"
        )
    print(
        "CV mean MAE              : "
        f"{report['cv_metrics']['MAE']['mean']:.4f}"
    )
    print(
        "CV MAE standard deviation: "
        f"{report['cv_metrics']['MAE']['std']:.4f}"
    )
    print(
        "Final Test MAE           : "
        f"{report['final_test_metrics']['MAE']:.4f}"
    )
    print(f"Report                   : {report['report_path']}")
    print(f"Model                    : {report['model_path']}")


def run_variant_cli(variant_name):
    parser = build_variant_parser(variant_name)
    args = parser.parse_args()
    report = run_training_variant(variant_name, args)
    print_variant_result(report)
