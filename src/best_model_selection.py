import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd


def load_training_report(path):
    """Đọc một báo cáo train và kiểm tra các trường bắt buộc."""
    with open(path, "r", encoding="utf-8") as file:
        report = json.load(file)

    required_fields = {
        "variant",
        "family",
        "cv_metrics",
        "final_test_metrics",
        "split_policy",
        "production_used_for_training",
        "artifact_paths",
    }
    missing = sorted(required_fields - set(report))
    if missing:
        raise ValueError(
            f"Báo cáo {path} thiếu trường: {', '.join(missing)}."
        )
    if report["production_used_for_training"] is not False:
        raise ValueError(
            f"{report['variant']} đã dùng production để train."
        )
    if not report["artifact_paths"]:
        raise ValueError(
            f"{report['variant']} không có artifact để lưu."
        )
    for artifact_path in report["artifact_paths"]:
        if not os.path.exists(artifact_path):
            raise FileNotFoundError(
                f"Không tìm thấy artifact: {artifact_path}"
            )
    return report


def _split_signature(report):
    """Tạo chữ ký để chắc 8 model làm cùng một đề thời gian."""
    fold_signature = tuple(
        (
            fold["train"]["start"],
            fold["train"]["end"],
            fold["train"]["rows"],
            fold["validation"]["start"],
            fold["validation"]["end"],
            fold["validation"]["rows"],
        )
        for fold in report["folds"]
    )
    return (
        report["development"]["start"],
        report["development"]["end"],
        report["development"]["rows"],
        report["final_test"]["start"],
        report["final_test"]["end"],
        report["final_test"]["rows"],
        report["production_reserved"]["start"],
        report["production_reserved"]["end"],
        report["production_reserved"]["rows"],
        report["split_policy"]["cv_splits"],
        report["random_state"],
        fold_signature,
    )


def validate_comparable_reports(reports):
    """Từ chối xếp hạng nếu các model không dùng cùng split."""
    if not reports:
        raise ValueError("Không có báo cáo model để so sánh.")

    reference = _split_signature(reports[0])
    for report in reports[1:]:
        if _split_signature(report) != reference:
            raise ValueError(
                "Các model không dùng cùng Development, fold, "
                "Final Test hoặc production window."
            )


def rank_model_reports(reports):
    """
    Xếp hạng chủ yếu bằng CV Mean MAE.

    CV Std và kích thước chỉ là tie-break, không dùng Final Test để chọn.
    """
    validate_comparable_reports(reports)
    return sorted(
        reports,
        key=lambda report: (
            float(report["cv_metrics"]["MAE"]["mean"]),
            float(report["cv_metrics"]["MAE"]["std"]),
            sum(
                os.path.getsize(path)
                for path in report["artifact_paths"]
            ),
        ),
    )


def build_ranking_frame(ranked_reports):
    rows = []
    for rank, report in enumerate(ranked_reports, start=1):
        artifact_size = sum(
            os.path.getsize(path)
            for path in report["artifact_paths"]
        )
        rows.append(
            {
                "rank": rank,
                "variant": report["variant"],
                "model": report["model"],
                "family": report["family"],
                "cv_mean_MAE": report["cv_metrics"]["MAE"]["mean"],
                "cv_std_MAE": report["cv_metrics"]["MAE"]["std"],
                "final_test_MAE": report[
                    "final_test_metrics"
                ]["MAE"],
                "artifact_size_bytes": artifact_size,
                "production_used_for_training": report[
                    "production_used_for_training"
                ],
            }
        )
    return pd.DataFrame(rows)


def _save_json(data, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    os.replace(temporary_path, path)


def save_champion(
    best_report,
    champion_root="models/champion",
):
    """
    Sao chép model thắng vào thư mục version mới rồi cập nhật metadata.

    Model cũ không bị ghi đè nên có thể rollback bằng metadata của version cũ.
    """
    version = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version_directory = Path(champion_root) / "versions" / version
    version_directory.mkdir(parents=True, exist_ok=False)

    copied_artifacts = []
    for artifact_path in best_report["artifact_paths"]:
        source = Path(artifact_path)
        destination = version_directory / source.name
        shutil.copy2(source, destination)
        copied_artifacts.append(str(destination))

    champion_info = {
        "champion_version": version,
        "selected_at": datetime.now().isoformat(),
        "variant": best_report["variant"],
        "model": best_report["model"],
        "family": best_report["family"],
        "selection_metric": "cross_validation_mean_MAE",
        "cv_mean_MAE": best_report["cv_metrics"]["MAE"]["mean"],
        "cv_std_MAE": best_report["cv_metrics"]["MAE"]["std"],
        "final_test_MAE_for_reporting_only": best_report[
            "final_test_metrics"
        ]["MAE"],
        "artifact_paths": copied_artifacts,
        "source_report": best_report["report_path"],
        "production_used_for_training": False,
    }
    info_path = str(Path(champion_root) / "best_model_info.json")
    _save_json(champion_info, info_path)
    champion_info["info_path"] = info_path
    return champion_info


def select_and_save_best_model(
    report_paths,
    ranking_path=(
        "results/model_selection/eight_model_ranking.csv"
    ),
    selection_report_path=(
        "results/model_selection/best_model_selection.json"
    ),
    champion_root="models/champion",
    save_winner_as_champion=True,
):
    """Đọc báo cáo, xếp hạng 8 model và lưu Champion."""
    reports = [
        load_training_report(path)
        for path in report_paths
    ]
    ranked = rank_model_reports(reports)
    ranking_frame = build_ranking_frame(ranked)

    ranking_directory = os.path.dirname(ranking_path)
    if ranking_directory:
        os.makedirs(ranking_directory, exist_ok=True)
    temporary_ranking_path = f"{ranking_path}.tmp"
    ranking_frame.to_csv(temporary_ranking_path, index=False)
    os.replace(temporary_ranking_path, ranking_path)

    champion_info = None
    if save_winner_as_champion:
        champion_info = save_champion(
            ranked[0],
            champion_root=champion_root,
        )
    selection_report = {
        "created_at": datetime.now().isoformat(),
        "selection_metric": "cross_validation_mean_MAE",
        "tie_breakers": [
            "cross_validation_MAE_std",
            "artifact_size_bytes",
        ],
        "final_test_used_for_selection": False,
        "models_compared": len(ranked),
        "selection_complete": bool(save_winner_as_champion),
        "selected_variant": ranked[0]["variant"],
        "ranking_path": ranking_path,
        "champion": champion_info,
        "ranking": ranking_frame.to_dict(orient="records"),
    }
    _save_json(selection_report, selection_report_path)
    return selection_report
