import math
import unittest
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dependencies import get_current_user
from backend.app.api.routes.model_quality import router
from backend.app.core.errors import ApplicationError, register_exception_handlers
from backend.app.db.models import ModelVersion, Region, UserRole
from backend.app.db.session import get_db
from backend.app.schemas.model_quality import ModelQualityReport
from backend.app.services import model_quality as quality


def fixture():
    region = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Ho_Chi_Minh", is_active=True)
    model = SimpleNamespace(id=uuid.uuid4(), version="model_v1", variant="RandomForest", artifact_uri="model.pkl")
    region.active_model_version_id = model.id
    training = SimpleNamespace(configuration_json={"train_end_date": "2026-06-01 08:00:00"})
    dataset = SimpleNamespace(id=uuid.uuid4(), original_filename="traffic.csv", sha256="a" * 64)
    rows = []
    for index, target in enumerate([1000, 2000, 4000, 3000]):
        rows.append({
            "date_time": f"2026-06-01 {8 + index:02}:00:00", "traffic_volume": target,
            "is_holiday": None, "air_pollution_index": 100, "humidity": 60,
            "wind_speed": 4, "wind_direction": 180, "visibility_in_miles": 10,
            "dew_point": 10, "temperature": 20, "rain_p_h": 0, "snow_p_h": 0,
            "clouds_all": 30, "weather_type": "Clear", "weather_description": "clear",
        })
    return region, model, training, dataset, pd.DataFrame(rows)


def fake_predictions(frame):
    return {pd.Timestamp(when).to_pydatetime(): value for when, value in zip(frame.date_time, [1100, 1800, 3500, 3200])}


class QualityReportTests(unittest.TestCase):
    def setUp(self):
        self.region, self.model, self.training, self.dataset, self.frame = fixture()
        quality._reports.clear()

    def report(self, mode="reference", frame=None, start_at=None, hours=4):
        with patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)):
            return quality._build_report(self.region, self.model, self.training, self.dataset,
                                         self.frame if frame is None else frame, mode, start_at, hours)

    def test_reference_metrics_match_hand_calculation_and_never_claim_observed(self):
        report = self.report()
        self.assertEqual(report["metrics"]["mae"], 250)
        self.assertAlmostEqual(report["metrics"]["rmse"], math.sqrt(85000))
        self.assertEqual(report["coverage"]["unknown_labels"], 4)
        self.assertEqual(report["coverage"]["observed_labels"], 0)
        self.assertEqual(report["reference_source"], "dataset_traffic_volume")
        self.assertIn("traffic_volume", " ".join(report["warnings"]))
        ModelQualityReport.model_validate(report)

    def test_unknown_labels_have_no_observed_metrics_but_keep_predictions(self):
        report = self.report(mode="observed")
        self.assertIsNone(report["metrics"]["mae"])
        self.assertIsNone(report["metrics"]["rmse"])
        self.assertEqual(report["coverage"]["paired_points"], 0)
        self.assertTrue(all(point["reference"] is None for point in report["points"]))
        self.assertEqual(report["coverage"]["predicted_points"], 4)

    def test_dst_wall_time_ambiguity_is_a_controlled_error_not_http_500(self):
        self.region.timezone='America/New_York'
        self.training.configuration_json['train_end_date']='2026-01-01'
        for start in ('2026-11-01 01:00:00','2026-03-08 02:00:00'):
            with self.subTest(start=start):
                frame=self.frame.copy()
                frame['date_time']=pd.date_range(start,periods=4,freq='h')
                with patch.object(quality,'_predict_points',return_value={}):
                    with self.assertRaises(ApplicationError) as raised:
                        quality._build_report(self.region,self.model,self.training,self.dataset,frame,'reference',start,4)
                self.assertEqual(raised.exception.code,'quality_timeline_invalid')
                self.assertEqual(raised.exception.status_code,409)

    def test_imputed_and_unknown_rows_are_excluded_from_observed_score(self):
        self.frame["target_observed"] = [True, False, None, True]
        self.frame["target_is_imputed"] = [False, True, None, True]
        report = self.report(mode="observed")
        self.assertEqual(report["coverage"]["paired_points"], 1)
        self.assertEqual(report["metrics"]["mae"], 100)
        self.assertEqual([p["label_source"] for p in report["points"]],
                         ["observed", "synthetic", "unknown", "synthetic"])

    def test_zero_is_a_valid_reference_and_nonfinite_targets_are_missing(self):
        self.frame["traffic_volume"] = [0, np.nan, np.inf, -1]
        report = self.report()
        self.assertEqual(report["coverage"]["paired_points"], 1)
        self.assertEqual(report["metrics"]["mae"], 1100)
        self.assertEqual(report["points"][0]["reference"], 0)

    def test_missing_hours_are_not_snapped_forward_or_interpolated(self):
        report = self.report(frame=self.frame.drop(index=1), hours=6)
        self.assertEqual(report["coverage"]["requested_points"], 6)
        self.assertEqual(report["coverage"]["dataset_points"], 3)
        self.assertIsNone(report["points"][1]["reference"])
        self.assertEqual(report["points"][1]["label_source"], "missing")
        self.assertIsNone(report["points"][4]["prediction"])
        self.assertEqual(report["coverage"]["paired_points"], 3)

    def test_aware_request_uses_region_time_and_reports_explicit_offset(self):
        report = self.report(start_at=datetime.fromisoformat("2026-06-01T01:00:00+00:00"))
        self.assertEqual(report["start_at"], "2026-06-01T08:00:00+07:00")
        self.assertEqual(report["coverage"]["paired_points"], 4)

    def test_training_rows_are_never_evaluated(self):
        with self.assertRaises(ApplicationError) as error:
            self.report(start_at="2026-06-01 07:00:00")
        self.assertEqual(error.exception.code, "quality_outside_window")

    def test_duplicate_hours_and_nonhour_start_are_rejected(self):
        for frame, start, code in [
            (pd.concat([self.frame, self.frame.iloc[:1]]), None, "quality_duplicate_times"),
            (self.frame, "2026-06-01 08:30:00", "quality_start_invalid"),
        ]:
            with self.subTest(code=code), self.assertRaises(ApplicationError) as error:
                self.report(frame=frame, start_at=start)
            self.assertEqual(error.exception.code, code)

    def test_absent_posttraining_rows_do_not_fall_back_to_training(self):
        self.training.configuration_json["train_end_date"] = "2026-06-02"
        with self.assertRaises(ApplicationError) as error:
            self.report()
        self.assertEqual(error.exception.code, "quality_no_holdout")

    def test_flag_parsing_is_conservative(self):
        for row, expected in [
            ({}, "unknown"), ({"target_is_imputed": False}, "unknown"),
            ({"target_observed": "bad"}, "unknown"),
            ({"target_observed": True, "target_is_imputed": "bad"}, "unknown"),
            ({"target_observed": "TRUE"}, "observed"),
            ({"target_observed": "0"}, "synthetic"),
            ({"target_observed": True, "target_is_imputed": True}, "synthetic"),
        ]:
            with self.subTest(row=row):
                self.assertEqual(quality.label_source(row), expected)

    def test_legacy_prediction_uses_serving_inputs_without_target_or_audit(self):
        model = SimpleNamespace(feature_names_in_=np.array(["humidity", "hour"]))
        model.predict = Mock(side_effect=lambda features: np.full(len(features), 42))
        self.frame["target_observed"] = pd.NA
        self.frame["target_is_imputed"] = pd.NA
        with patch.object(quality.predictions, "_load_model", return_value=model):
            result = quality._predict_points(self.model, self.training, self.dataset, self.frame)
        self.assertEqual(len(result), 4)
        features = model.predict.call_args.args[0]
        self.assertEqual(list(features.columns), ["humidity", "hour"])
        self.assertEqual(features["hour"].tolist(), [8, 9, 10, 11])

    def test_neural_dispatch_does_not_send_target_or_provenance_as_features(self):
        self.training.configuration_json["model_family"] = "neural_sequence"
        self.frame["target_observed"] = True
        with patch.object(quality.predictions, "_predict_neural_sequence_batch", return_value={}) as predict:
            quality._predict_points(self.model, self.training, self.dataset, self.frame)
        snapshots = predict.call_args.args[3]
        self.assertNotIn("traffic_volume", snapshots[0])
        self.assertNotIn("target_observed", snapshots[0])

    def test_legacy_replay_matches_single_request_encoding_in_any_window(self):
        model = SimpleNamespace(feature_names_in_=np.array(["humidity", "hour", "weather_type_Rain"]))
        model.predict = lambda features: features["hour"].to_numpy() + features["weather_type_Rain"].to_numpy() * 100
        self.frame.loc[0, "weather_type"] = "Rain"
        with patch.object(quality.predictions, "_load_model", return_value=model):
            together = quality._predict_points(self.model, self.training, self.dataset, self.frame)
            alone = quality._predict_points(self.model, self.training, self.dataset, self.frame.iloc[:1])
            snapshot = self.frame.iloc[0][quality.REQUIRED_COLUMNS].drop("traffic_volume").to_dict()
            serving = float(model.predict(quality.predictions.prepare_input(snapshot, model))[0])
        first_time = pd.Timestamp(self.frame.iloc[0].date_time).to_pydatetime()
        self.assertEqual(together[first_time], alone[first_time])
        self.assertEqual(alone[first_time], serving)

    def test_tree_replay_predicts_before_current_target_enters_history(self):
        self.training.configuration_json["model_family"] = "tree_autoregressive"
        hourly = pd.concat([self.frame.iloc[:1]] * 172, ignore_index=True)
        hourly["date_time"] = pd.date_range("2026-05-24", periods=172, freq="h")
        hourly["traffic_volume"] = np.arange(172, dtype=float)
        audit = pd.DataFrame({"date_time": hourly.date_time, "target_observed": True})
        def predict(model, when, exogenous, history):
            self.assertLess(history.last_timestamp, when)
            self.assertNotIn("traffic_volume", exogenous)
            return history.build_history_features(when)["traffic_volume_lag_1h"], None
        with patch.object(quality.predictions, "_load_model", return_value=Mock()), \
             patch.object(quality.predictions, "_hourly_context", return_value=(hourly, audit)), \
             patch.object(quality.predictions, "predict_next_hour", side_effect=predict):
            result = quality._predict_points(self.model, self.training, self.dataset, hourly.iloc[168:])
        self.assertEqual(list(result.values()), [167, 168, 169, 170])

    def test_cache_is_copied_and_dataset_or_model_change_invalidates_it(self):
        db = Mock()
        db.get.side_effect = lambda cls, identity: self.region if cls is Region else self.model
        with patch.object(quality.predictions, "_active_training_context", return_value=(self.training, self.dataset)), \
             patch.object(quality.predictions, "_download_dataset_frame", return_value=self.frame) as download, \
             patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)):
            kwargs = {"window": "replay", "mode": "reference"}
            report = quality.get_model_quality(db, self.region.id, **kwargs)
            report["points"][0]["reference"] = 999999
            self.assertEqual(quality.get_model_quality(db, self.region.id, **kwargs)["points"][0]["reference"], 1000)
            self.assertEqual(download.call_count, 1)
            self.dataset.sha256 = "b" * 64
            quality.get_model_quality(db, self.region.id, **kwargs)
            self.model.id = uuid.uuid4()
            quality.get_model_quality(db, self.region.id, **kwargs)
            self.assertEqual(download.call_count, 3)
        db.commit.assert_not_called()

    def test_changed_active_model_and_oversized_window_are_rejected(self):
        db = Mock()
        db.get.side_effect = lambda cls, identity: self.region if cls is Region else self.model
        for kwargs, code in [({"hours": 169}, "quality_request_invalid"),
                             ({"expected_model_id": uuid.uuid4()}, "quality_active_model_changed")]:
            with self.subTest(code=code), self.assertRaises(ApplicationError) as error:
                quality.get_model_quality(db, self.region.id, **kwargs)
            self.assertEqual(error.exception.code, code)

    def test_report_rejects_busy_serving_runtime_and_releases_its_own_lock(self):
        db = Mock()
        db.get.side_effect = lambda cls, identity: self.region if cls is Region else self.model
        runtime = SimpleNamespace(lock=Mock())
        runtime.lock.acquire.return_value = False
        with patch.object(quality.predictions, "_active_training_context", return_value=(self.training, self.dataset)), \
             self.assertRaises(ApplicationError) as error:
            quality.get_model_quality(db, self.region.id, serving_runtime=runtime)
        self.assertEqual(error.exception.code, "quality_busy")
        runtime.lock.release.assert_not_called()
        self.assertFalse(quality._evaluation_lock.locked())

    def recent_report(self, frame=None, hours=24):
        with patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)) as predict:
            report = quality._build_report(
                self.region, self.model, self.training, self.dataset,
                self.frame if frame is None else frame, "reference", None, hours,
                window="recent", now=datetime(2026, 6, 1, 5, 35, tzinfo=UTC),
            )
        return report, predict

    def test_recent_window_uses_clock_in_region_time_not_dataset_start(self):
        report, predict = self.recent_report()
        self.assertEqual(report["start_at"], "2026-06-01T08:00:00+07:00")
        self.assertEqual(report["end_at"], "2026-06-01T11:00:00+07:00")
        self.assertEqual(report["window_end_at"], "2026-06-01T12:00:00+07:00")
        self.assertEqual(report["coverage"]["requested_points"], 4)
        self.assertEqual(report["selected_hours"], 24)
        self.assertEqual(report["coverage"]["paired_points"], 4)
        self.assertEqual(report["metrics"]["mae"], 250)
        self.assertEqual(len(predict.call_args.args[-1]), 4)
        ModelQualityReport.model_validate(report)

    def test_recent_window_excludes_current_hour_and_future_dataset_rows(self):
        future = self.frame.iloc[[0]].copy()
        future["date_time"] = "2026-06-01 12:00:00"
        report, predict = self.recent_report(pd.concat([self.frame, future], ignore_index=True))
        self.assertEqual(report["coverage"]["dataset_points"], 4)
        self.assertLess(predict.call_args.args[-1].date_time.max(), pd.Timestamp("2026-06-01 12:00"))
        self.assertTrue(all(point["forecast_for"] < report["window_end_at"] for point in report["points"]))

    def test_stale_or_future_only_dataset_keeps_recent_window_empty(self):
        for offset in [-48, 48]:
            frame = self.frame.copy()
            frame["date_time"] = pd.to_datetime(frame.date_time) + pd.Timedelta(hours=offset)
            report, _ = self.recent_report(frame)
            self.assertEqual(len(report["points"]), 4)
            self.assertEqual(report["start_at"], "2026-06-01T08:00:00+07:00")
            self.assertEqual(report["coverage"]["dataset_points"], 0)
            self.assertTrue(all(p["reference"] is None and p["prediction"] is None for p in report["points"]))
            self.assertIsNone(report["metrics"]["mae"])

    def test_recent_window_never_scores_rows_used_for_training(self):
        self.training.configuration_json["train_end_date"] = "2026-06-01 11:00:00"
        report, _ = self.recent_report()
        self.assertEqual(report["coverage"]["paired_points"], 1)
        self.assertEqual(report["coverage"]["requested_points"], 1)
        self.assertEqual(report["start_at"], "2026-06-01T11:00:00+07:00")
        self.assertEqual(report["metrics"]["mae"], 200)
        self.assertTrue(all(p["prediction"] is None for p in report["points"][:-1]))

    def test_recent_report_without_holdout_has_empty_bounds_not_an_old_window(self):
        self.training.configuration_json["train_end_date"] = "2026-06-02"
        report, _ = self.recent_report()
        self.assertIsNone(report["available_start_at"])
        self.assertIsNone(report["available_end_at"])
        self.assertEqual(report["coverage"]["paired_points"], 0)
        self.assertEqual(report["coverage"]["requested_points"], 0)
        self.assertIsNone(report["end_at"])
        ModelQualityReport.model_validate(report)

    def test_recent_cache_rolls_window_at_the_next_region_hour(self):
        db = Mock()
        db.get.side_effect = lambda cls, identity: self.region if cls is Region else self.model
        clock = Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 6, 1, 5, 59, tzinfo=UTC)
        with patch.object(quality, "datetime", clock), \
             patch.object(quality.predictions, "_active_training_context", return_value=(self.training, self.dataset)), \
             patch.object(quality.predictions, "_download_dataset_frame", return_value=self.frame) as download, \
             patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)):
            first = quality.get_model_quality(db, self.region.id)
            self.assertEqual(first["mode"], "reference")
            self.assertEqual(first["metrics"]["mae"], 250)  # No provenance flags are needed to compare dataset targets.
            quality.get_model_quality(db, self.region.id)
            self.assertEqual(download.call_count, 1)
            clock.now.return_value = datetime(2026, 6, 1, 6, 1, tzinfo=UTC)
            second = quality.get_model_quality(db, self.region.id)
            self.assertEqual(download.call_count, 2)
            self.assertNotEqual(first["window_end_at"], second["window_end_at"])
        db.commit.assert_not_called()

    def test_recent_window_rejects_manual_range_or_observed_only_policy(self):
        for kwargs in [{"mode": "observed"}, {"hours": 48}, {"hours": 337}, {"start_at": "2026-06-01"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ApplicationError) as error:
                quality.get_model_quality(Mock(), self.region.id, **kwargs)
            self.assertEqual(error.exception.code, "quality_request_invalid")

    def test_recent_uses_existing_csv_targets_without_provenance_flags(self):
        report, _ = self.recent_report()
        self.assertEqual(report["coverage"]["unknown_labels"], 4)
        self.assertEqual(report["coverage"]["paired_points"], 4)
        self.assertEqual([point["reference"] for point in report["points"][-4:]], [1000, 2000, 4000, 3000])
        self.assertEqual(report["metrics"]["mae"], 250)

    def test_recent_dataset_comparison_does_not_filter_filled_targets(self):
        self.frame["target_observed"] = False
        self.frame["target_is_imputed"] = True
        report, _ = self.recent_report()
        self.assertEqual(report["coverage"]["synthetic_labels"], 4)
        self.assertEqual(report["coverage"]["paired_points"], 4)
        self.assertEqual(report["metrics"]["mae"], 250)

    def test_recent_supports_all_three_windows_without_crossing_training(self):
        self.training.configuration_json["train_end_date"] = "2026-05-01 00:00:00"
        for hours in [24, 168, 336]:
            with self.subTest(hours=hours):
                report, _ = self.recent_report(hours=hours)
                self.assertEqual(len(report["points"]), hours)
                self.assertEqual(report["selected_hours"], hours)
                self.assertEqual(report["coverage"]["requested_points"], hours)
                self.assertEqual(report["window_end_at"], "2026-06-01T12:00:00+07:00")
                self.assertEqual(report["metrics"]["mae"], 250)
                self.assertEqual(pd.Timestamp(report["window_end_at"]) - pd.Timestamp(report["start_at"]), pd.Timedelta(hours=hours))

    def test_week_and_fortnight_are_clipped_to_three_days_after_training(self):
        self.training.configuration_json["train_end_date"] = "2026-05-29 12:00:00"
        for hours, expected in [(24, 24), (168, 72), (336, 72)]:
            report, predict = self.recent_report(hours=hours)
            self.assertEqual(report["coverage"]["requested_points"], expected)
            self.assertEqual(len(report["points"]), expected)
            self.assertEqual(report["selected_hours"], hours)
            self.assertTrue((predict.call_args.args[-1].date_time >= pd.Timestamp("2026-05-29 12:00")).all())

    def test_fractional_training_boundary_starts_at_first_eligible_whole_hour(self):
        self.training.configuration_json["train_end_date"] = "2026-06-01 08:30:00"
        report, _ = self.recent_report(hours=168)
        self.assertEqual(report["start_at"], "2026-06-01T09:00:00+07:00")
        self.assertEqual(report["coverage"]["requested_points"], 3)

    def test_cached_report_has_fresh_server_clock_and_hour_aligned_next_refresh(self):
        db = Mock()
        db.get.side_effect = lambda cls, identity: self.region if cls is Region else self.model
        clock = Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 6, 1, 5, 35, tzinfo=UTC)
        with patch.object(quality, "datetime", clock), \
             patch.object(quality.predictions, "_active_training_context", return_value=(self.training, self.dataset)), \
             patch.object(quality.predictions, "_download_dataset_frame", return_value=self.frame) as download, \
             patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)):
            first = quality.get_model_quality(db, self.region.id)
            clock.now.return_value = datetime(2026, 6, 1, 5, 35, 20, tzinfo=UTC)
            cached = quality.get_model_quality(db, self.region.id)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(first["generated_at"], cached["generated_at"])
            self.assertNotEqual(first["server_time"], cached["server_time"])
            self.assertEqual(cached["next_refresh_at"], "2026-06-01T13:00:00+07:00")
            self.assertEqual(cached["selected_hours"], 168)
            quality.get_model_quality(db, self.region.id, hours=24)
            self.assertEqual(download.call_count, 2)


class QualityRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        register_exception_handlers(self.app)
        self.app.include_router(router, prefix="/api/v1/admin")
        self.app.dependency_overrides[get_db] = lambda: Mock()
        self.url = f"/api/v1/admin/regions/{uuid.uuid4()}/model-quality"
        self.client = TestClient(self.app)

    def test_unauthenticated_request_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_regular_user_cannot_read_report(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.USER)
        with patch("backend.app.api.routes.model_quality.get_model_quality") as service:
            self.assertEqual(self.client.get(self.url).status_code, 403)
            service.assert_not_called()

    def test_admin_response_validates_schema_and_query_limits(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.ADMIN)
        region, model, training, dataset, frame = fixture()
        with patch.object(quality, "_predict_points", return_value=fake_predictions(frame)):
            report = quality._build_report(region, model, training, dataset, frame, "reference", None, 4)
        with patch("backend.app.api.routes.model_quality.get_model_quality", return_value=report) as service:
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["metrics"]["mae"], 250)
            self.assertEqual(service.call_args.kwargs["mode"], "reference")
            self.assertEqual(service.call_args.kwargs["window"], "recent")
            self.assertEqual(service.call_args.kwargs["hours"], 168)
            for hours in [24, 168, 336]:
                self.assertEqual(self.client.get(self.url + f"?hours={hours}").status_code, 200)
                self.assertEqual(service.call_args.kwargs["hours"], hours)
            self.assertEqual(self.client.get(self.url + "?hours=337").status_code, 422)
            self.assertEqual(self.client.get(self.url + "?mode=accuracy").status_code, 422)
            body = response.json()
            self.assertAlmostEqual(body["metrics"]["wape_percent"], 10)
            self.assertEqual(body["metrics"]["baseline"]["method"], "same_hour_last_week")
            self.assertEqual([point["percent_error"] for point in body["points"]][:2], [10, 10])
            self.assertEqual(body["points"][0]["low_traffic"], False)


def with_last_week(frame, values):
    """Prepend rows exactly 168 wall-clock hours earlier (inside the training period)."""
    past = frame.iloc[:len(values)].copy()
    past["date_time"] = (pd.to_datetime(past["date_time"]) - pd.Timedelta(hours=168)).dt.strftime("%Y-%m-%d %H:%M:%S")
    past["traffic_volume"] = values
    return pd.concat([past, frame], ignore_index=True)


class ErrorShareAndBaselineTests(unittest.TestCase):
    """Actual [1000, 2000, 4000, 3000], predicted [1100, 1800, 3500, 3200], |e| [100, 200, 500, 200]."""

    def setUp(self):
        self.region, self.model, self.training, self.dataset, self.frame = fixture()
        quality._reports.clear()

    def report(self, frame, mode="reference"):
        with patch.object(quality, "_predict_points", return_value=fake_predictions(self.frame)):
            return quality._build_report(self.region, self.model, self.training, self.dataset,
                                         frame, mode, None, 4)

    def test_wape_is_total_error_over_total_actual_traffic(self):
        metrics = self.report(self.frame)["metrics"]
        self.assertAlmostEqual(metrics["wape_percent"], 1000 / 10000 * 100)
        self.assertAlmostEqual(metrics["actual_mean"], 2500)
        self.assertAlmostEqual(metrics["low_traffic_below"], 625)
        self.assertEqual(metrics["mae"], 250)  # old field is unchanged

    def test_each_hour_has_error_as_percent_of_actual(self):
        points = self.report(self.frame)["points"]
        for point, expected in zip(points, [10, 10, 12.5, 100 * 200 / 3000]):
            self.assertAlmostEqual(point["percent_error"], expected)
        self.assertEqual([point["low_traffic"] for point in points], [False] * 4)

    def test_low_traffic_hour_is_below_a_quarter_of_the_window_mean(self):
        frame = self.frame.copy()
        frame["traffic_volume"] = [100, 2000, 4000, 3000]  # mean 2275, threshold 568.75
        points = self.report(frame)["points"]
        self.assertEqual([point["low_traffic"] for point in points], [True, False, False, False])
        self.assertAlmostEqual(points[0]["percent_error"], 1000)  # |1100 - 100| / 100

    def test_zero_actual_has_no_percent_and_zero_total_has_no_wape(self):
        frame = self.frame.copy()
        frame["traffic_volume"] = [0, np.nan, np.inf, -1]
        report = self.report(frame)
        self.assertIsNone(report["points"][0]["percent_error"])
        self.assertIsNone(report["metrics"]["wape_percent"])
        self.assertIsNone(report["points"][1]["low_traffic"])  # no actual value
        ModelQualityReport.model_validate(report)

    def test_same_hour_last_week_baseline_uses_identical_hours(self):
        baseline = self.report(with_last_week(self.frame, [1200, 1500, 3000, 3000]))["metrics"]["baseline"]
        self.assertEqual(baseline["method"], "same_hour_last_week")
        self.assertEqual(baseline["lag_hours"], 168)
        self.assertEqual(baseline["paired_points"], 4)
        self.assertAlmostEqual(baseline["mae"], (200 + 500 + 1000 + 0) / 4)
        self.assertAlmostEqual(baseline["model_mae"], 250)
        self.assertAlmostEqual(baseline["relative_change_percent"], (250 - 425) / 425 * 100)

    def test_baseline_compares_model_only_where_last_week_exists(self):
        frame = with_last_week(self.frame, [1200.0, 1500.0, np.nan, -5.0])  # last-week values for hours 3 and 4 are unusable
        baseline = self.report(frame)["metrics"]["baseline"]
        self.assertEqual(baseline["paired_points"], 2)
        self.assertAlmostEqual(baseline["mae"], 350)
        self.assertAlmostEqual(baseline["model_mae"], 150)
        self.assertAlmostEqual(baseline["relative_change_percent"], (150 - 350) / 350 * 100)

    def test_worse_than_last_week_is_a_positive_change(self):
        baseline = self.report(with_last_week(self.frame, [1000, 2000, 4100, 3000]))["metrics"]["baseline"]
        self.assertAlmostEqual(baseline["mae"], 25)
        self.assertAlmostEqual(baseline["relative_change_percent"], (250 - 25) / 25 * 100)

    def test_without_last_week_or_perfect_baseline_change_is_unknown(self):
        report = self.report(self.frame)
        baseline = report["metrics"]["baseline"]
        self.assertEqual(baseline["paired_points"], 0)
        self.assertIsNone(baseline["mae"])
        self.assertIsNone(baseline["relative_change_percent"])
        perfect = self.report(with_last_week(self.frame, [1000, 2000, 4000, 3000]))["metrics"]["baseline"]
        self.assertEqual(perfect["mae"], 0)
        self.assertIsNone(perfect["relative_change_percent"])
        ModelQualityReport.model_validate(report)

    def test_observed_mode_baseline_uses_only_observed_pairs(self):
        frame = with_last_week(self.frame, [1200, 1500, 3000, 3000])
        frame["target_observed"] = [True] * 4 + [True, False, False, False]
        baseline = self.report(frame, mode="observed")["metrics"]["baseline"]
        self.assertEqual(baseline["paired_points"], 1)
        self.assertAlmostEqual(baseline["mae"], 200)
        self.assertAlmostEqual(baseline["model_mae"], 100)


if __name__ == "__main__":
    unittest.main()
