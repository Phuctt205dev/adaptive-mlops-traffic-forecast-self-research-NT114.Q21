import argparse

import pandas as pd

from src.time_series_features import (
    DEFAULT_LAG_HOURS,
    DEFAULT_ROLLING_WINDOWS,
    create_time_series_features,
    save_time_series_features,
)


DEFAULT_INPUT_PATH = "data/processed/TrafficVolumeData_hourly.csv"
DEFAULT_AUDIT_PATH = "data/processed/TrafficVolumeData_hourly_audit.csv"
DEFAULT_OUTPUT_PATH = "data/processed/TrafficVolumeData_features.csv"
DEFAULT_REPORT_PATH = "data/processed/time_series_feature_report.json"


def _positive_integer_list(value):
    """Convert a comma-separated string to positive integers."""
    try:
        values = [int(item.strip()) for item in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use comma-separated integers."
        ) from error

    if not values or min(values) <= 0:
        raise argparse.ArgumentTypeError(
            "Every value must be greater than zero."
        )
    return values


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Create lag and rolling features for hourly traffic."
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH)
    parser.add_argument("--audit", default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--lags",
        type=_positive_integer_list,
        default=list(DEFAULT_LAG_HOURS),
        help="Hourly lags, for example: 1,2,3,24,168.",
    )
    parser.add_argument(
        "--rolling-windows",
        type=_positive_integer_list,
        default=list(DEFAULT_ROLLING_WINDOWS),
        help="Rolling windows, for example: 3,6,24,168.",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    hourly_df = pd.read_csv(args.input)
    audit_df = pd.read_csv(args.audit)

    feature_df, report = create_time_series_features(
        hourly_df,
        audit_df,
        lag_hours=args.lags,
        rolling_windows=args.rolling_windows,
    )
    save_time_series_features(
        feature_df,
        report,
        output_csv_path=args.output,
        output_report_path=args.report,
    )

    print("\nTime-series feature preparation completed.")
    print(f"Input rows               : {report['source_rows']}")
    print(f"Training-ready rows      : {report['output_training_rows']}")
    print(f"Lag hours                : {report['lag_hours']}")
    print(f"Rolling windows          : {report['rolling_windows']}")
    print(f"Output CSV               : {args.output}")
    print(f"Feature report           : {args.report}")


if __name__ == "__main__":
    main()
