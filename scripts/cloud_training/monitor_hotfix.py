"""Bounded monitor read retry patch. Operator Apply only; default Check is read-only."""
import argparse
from datetime import datetime, timezone
import hashlib
import json

try:
    from .result_hotfix import ROOT, ResultFix, prepare, read
except ImportError:
    from result_hotfix import ROOT, ResultFix, prepare, read

DIRECTORY = ROOT / "results/cloud-training/cloud-monitor-fix"
PRIOR = ROOT / "results/cloud-training/cloud-result-fix"
ALLOWED = {"project/src/cloud_training/kaggle_execution.py", "project/src/cloud_training/kaggle_transport.py",
           "project/backend/app/services/training_execution.py"}


class MonitorFix(ResultFix):
    def __init__(self, kubectl):
        super().__init__(kubectl, directory=DIRECTORY, baseline_review=PRIOR)

    def additional_proofs(self):
        proof = read(DIRECTORY / "monitor-retry-proof.json")
        resources = read(self.directory / "configmaps.json")["items"]
        code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        manifest = next(json.loads(value) for value in code["data"].values() if value.startswith('{"bundle_id":'))
        expected = manifest["files"]["project/src/cloud_training/kaggle_transport.py"]
        if (proof.get("transport_sha256") != expected or not proof.get("retry_recovered")
                or set(proof.get("operations", [])) != {"status"} or not proof.get("read_only")):
            raise RuntimeError("The exact monitor retry needs a passing read-only worker SDK proof.")

    def check(self):
        result = super().check()
        result["monitor_fix_check_passed"] = result.pop("result_fix_check_passed")
        return result

    def apply(self):
        result = super().apply()
        result["monitor_fix_applied"] = result.pop("result_fix_applied")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["Prepare", "Check", "Apply", "Verify", "Rollback"], default="Check")
    parser.add_argument("--kubectl", required=True)
    args = parser.parse_args()
    try:
        result = (prepare(args.kubectl, directory=DIRECTORY, previous_review=PRIOR, allowed=ALLOWED)
                  if args.action == "Prepare" else getattr(MonitorFix(args.kubectl), args.action.lower())())
        if args.action == "Check":
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            (DIRECTORY / "check-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"monitor_fix_failed": True, "action": args.action, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect sanitized proof/journal; raw details omitted."}))
        raise SystemExit(2)
