import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.shap.rollout_retrain_selection import RetrainRollout
from scripts.shap.rollout_neural_shap import ROLES
from scripts.shap.rollout_neural_context_fix import ContextFix, ROOT
from scripts.shap.rollout_neural_shap import digest
from scripts.shap.rollout_neural_metrics_fix import MetricsFix


class RetrainRolloutTests(unittest.TestCase):
    def test_metrics_upgrade_requires_full_pipeline_and_all_five_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = MetricsFix.__new__(MetricsFix)
            operator.directory = Path(tmp)
            rows = [{"model": name, "full_pipeline_completed": True, "selection_mode": "reduced",
                     "final_report_ready": True, "reload_predict": True,
                     "cv_metric_names": ["MAE", "RMSE", "MAPE", "WAPE", "R2"]} for name in ("LSTM", "GRU")]
            path = Path(tmp) / "cloud-neural-fold-proof.json"
            with patch.object(ContextFix, "check", return_value={}):
                path.write_text(json.dumps({"results": rows}))
                self.assertTrue(operator.check()["neural_metrics_fix_ready_to_apply"])
                rows[0]["cv_metric_names"].remove("R2")
                path.write_text(json.dumps({"results": rows}))
                with self.assertRaises(RuntimeError):
                    operator.check()

    def test_context_upgrade_requires_gpu_source_identity_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = ContextFix.__new__(ContextFix)
            operator.directory = Path(tmp)
            required = ("src/time_series_cross_validation.py", "src/feature_selection.py", "src/models/recurrent.py")
            proof = {"passed": True, "remote_stopped": True, "private_staging_cleaned": True,
                     "fit_called": True, "synthetic_only": True,
                     "source_sha256": {name: digest(ROOT / name) for name in required},
                     "results": [{"model": name, "passed": True, "history_context_preserved": True,
                                  "training_targets_in_window": True} for name in ("LSTM", "GRU")]}
            path = Path(tmp) / "cloud-neural-fold-proof.json"
            with patch.object(RetrainRollout, "check", return_value={}):
                path.write_text(json.dumps(proof))
                self.assertTrue(operator.check()["neural_context_fix_ready_to_apply"])
                proof["private_staging_cleaned"] = False
                path.write_text(json.dumps(proof))
                with self.assertRaises(RuntimeError):
                    operator.check()
                proof["private_staging_cleaned"] = True
                proof["source_sha256"][required[0]] = "wrong-source"
                path.write_text(json.dumps(proof))
                with self.assertRaises(RuntimeError):
                    operator.check()

    def test_rollback_refuses_registered_reduced_neural_models(self):
        operator = RetrainRollout.__new__(RetrainRollout)
        operator.cluster = SimpleNamespace(remote=lambda *args: {"reduced_neural_models": 1})
        with self.assertRaisesRegex(RuntimeError, "rollback is blocked"):
            operator.rollback()

    def test_check_requires_both_real_model_compatibility_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = RetrainRollout.__new__(RetrainRollout)
            operator.directory, operator.review = Path(tmp), {"bundle_id": "synthetic"}
            operator.validate = lambda: {"server_validated_not_applied": True}
            for role in ROLES:
                (Path(tmp) / (role + ".source-proof.json")).write_text(json.dumps({
                    "source_startup_verified": True, "bundle_id": "synthetic"}))
            proof = {"passed": True, "fit_called": False, "lifespan_started": False,
                     "bundle_id": "synthetic", "results": []}
            path = Path(tmp) / "selection-compatibility.json"
            path.write_text(json.dumps(proof))
            with self.assertRaises(RuntimeError):
                operator.check()
            proof["results"] = [{"model": name, "mask_roundtrip": True, "shap_ranking": True, "reload_predict": True}
                                for name in ("LSTM", "GRU")]
            path.write_text(json.dumps(proof))
            self.assertTrue(operator.check()["retrain_selection_ready_to_apply"])
            proof["fit_called"] = True
            path.write_text(json.dumps(proof))
            with self.assertRaises(RuntimeError):
                operator.check()


if __name__ == "__main__":
    unittest.main()
