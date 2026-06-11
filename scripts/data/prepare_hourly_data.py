import argparse

import pandas as pd

from src.time_series_preprocess import (
    prepare_hourly_time_series,
    save_hourly_dataset,
)


# Luôn đọc bản tải về để không biến các giờ suy luận thành dữ liệu quan sát.
DEFAULT_INPUT_PATH = (
    "data/raw/TrafficVolumeData_original_2012_2017.csv"
)
DEFAULT_OUTPUT_PATH = "data/processed/TrafficVolumeData_hourly.csv"
DEFAULT_AUDIT_PATH = "data/processed/TrafficVolumeData_hourly_audit.csv"
DEFAULT_REPORT_PATH = "data/processed/hourly_quality_report.json"


def parse_arguments():
    """Read input and output paths from the command line."""
    parser = argparse.ArgumentParser(
        description="Prepare one continuous traffic row per hour."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_PATH,
        help="Source CSV path.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help="Prepared hourly CSV path.",
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT_PATH,
        help="Quality report JSON path.",
    )
    parser.add_argument(
        "--audit",
        default=DEFAULT_AUDIT_PATH,
        help="Audit metadata CSV path.",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    raw_df = pd.read_csv(args.input)

    hourly_df, audit_df, report = prepare_hourly_time_series(raw_df)
    save_hourly_dataset(
        hourly_df=hourly_df,
        audit_df=audit_df,
        report=report,
        output_csv_path=args.output,
        output_audit_path=args.audit,
        output_report_path=args.report,
    )

    # Log runtime dùng ASCII để chạy ổn định trên cả Windows và Linux.
    print("\nTime-series data preparation completed.")
    print(f"Input file               : {args.input}")
    print(f"Original rows            : {report['source_rows']}")
    print(
        "Hours after deduplication: "
        f"{report['rows_after_duplicate_aggregation']}"
    )
    print(
        "Inserted missing hours   : "
        f"{report['inserted_missing_hours']}"
    )
    print(
        "Final hourly rows        : "
        f"{report['rows_after_hourly_reindex']}"
    )
    print(f"Output CSV               : {args.output}")
    print(f"Audit CSV                : {args.audit}")
    print(f"Quality report           : {args.report}")


if __name__ == "__main__":
    main()
