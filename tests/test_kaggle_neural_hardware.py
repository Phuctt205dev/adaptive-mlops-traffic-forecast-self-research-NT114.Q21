"""LSTM/GRU can ask Kaggle for CPU instead of GPU. Tree work is always CPU.

Nothing here fits a model or calls Kaggle: the runner, notebook metadata and
configuration are exercised with fakes.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from pydantic import ValidationError

from backend.app.schemas.model_version import TrainingRunCreate
from backend.app.services import drift_monitoring
from scripts.cloud_training import execute_branch as runner
from src import region_training_variants
from src.cloud_training.kaggle_execution import KaggleExecution, initial_ledger
from src.cloud_training.kaggle_transport import ProviderError
from src.cloud_training.package import configuration_digest, kaggle_accelerator
from tests.test_kaggle_execution import FakeTransport


def request(**overrides):
    return TrainingRunCreate(**{"train_start_date": "2025-01-01", "train_end_date": "2025-02-01", **overrides})


class ConfigurationTests(unittest.TestCase):
    def test_cpu_is_the_default_for_every_new_request(self):
        self.assertEqual(request().model_dump(mode="json")["neural_accelerator"], "cpu")

    def test_gpu_is_still_accepted_when_it_is_chosen(self):
        self.assertEqual(request(neural_accelerator="gpu").neural_accelerator, "gpu")

    def test_cpu_is_accepted_and_anything_else_is_refused(self):
        self.assertEqual(request(neural_accelerator="cpu").neural_accelerator, "cpu")
        for value in ("tpu", "", "GPU ", None, 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                request(neural_accelerator=value)

    def test_a_run_stored_before_the_setting_existed_still_compares_equal_on_replay(self):
        stored = request().model_dump(mode="json")
        del stored["neural_accelerator"]
        again = TrainingRunCreate.model_validate(stored).model_dump(mode="json")
        incoming = TrainingRunCreate.model_validate(request().model_dump(mode="json")).model_dump(mode="json")
        self.assertEqual(again, incoming)
        self.assertEqual(again["neural_accelerator"], "cpu")

    def test_automatic_retrain_keeps_an_explicit_choice_and_otherwise_uses_cpu(self):
        import pandas as pd
        for previous, expected in (({}, "cpu"), ({"neural_accelerator": "cpu"}, "cpu"), ({"neural_accelerator": "gpu"}, "gpu")):
            with self.subTest(previous=previous):
                run = SimpleNamespace(configuration_json={"selected_models": ["lstm", "gru"], **previous})
                config = drift_monitoring._training_configuration(run, pd.Timestamp("2026-06-01"))
                self.assertEqual(config["neural_accelerator"], expected)


class AcceleratorChoiceTests(unittest.TestCase):
    def test_tree_is_always_cpu_and_neural_follows_the_setting(self):
        self.assertEqual(kaggle_accelerator({"neural_accelerator": "gpu"}, "tree"), "cpu")
        self.assertEqual(kaggle_accelerator({"neural_accelerator": "cpu"}, "tree"), "cpu")
        for branch in ("lstm", "gru"):
            self.assertEqual(kaggle_accelerator({}, branch), "gpu")
            self.assertEqual(kaggle_accelerator({"neural_accelerator": "gpu"}, branch), "gpu")
            self.assertEqual(kaggle_accelerator({"neural_accelerator": "cpu"}, branch), "cpu")

    def test_a_configuration_stored_before_the_setting_existed_keeps_the_gpu_it_ran_on(self):
        # The default for NEW requests is cpu, but an attempt already built from a
        # configuration without the field must not change hardware under its feet.
        for branch in ("lstm", "gru"):
            self.assertEqual(kaggle_accelerator({}, branch), "gpu")

    def test_an_unknown_value_never_silently_becomes_gpu_or_cpu(self):
        with self.assertRaises(ValueError):
            kaggle_accelerator({"neural_accelerator": "tpu"}, "lstm")


class NotebookHardwareTests(unittest.TestCase):
    def provider(self, branch, accelerator="unset"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "input").mkdir()
        context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                   "branch": branch, "configuration_sha256": "a" * 64}
        extra = {} if accelerator == "unset" else {"accelerator": accelerator}
        ledger = initial_ledger({"context": context, "files": {}}, "probeuser", **extra)
        self.transport = FakeTransport()
        return KaggleExecution(self.transport, root, ledger, lambda value: None,
                               clock=lambda: 1000, sleep=lambda seconds: None, background_progress=False)

    def test_notebook_asks_for_a_gpu_only_when_the_branch_needs_one(self):
        for branch, accelerator, gpu in (("tree", "unset", False), ("lstm", "unset", True), ("gru", "unset", True),
                                         ("lstm", "gpu", True), ("gru", "cpu", False), ("lstm", "cpu", False),
                                         ("tree", "cpu", False)):
            with self.subTest(branch=branch, accelerator=accelerator):
                provider = self.provider(branch, accelerator)
                provider.submit()
                self.assertIs(self.transport.metadata["enable_gpu"], gpu)

    def test_an_attempt_started_before_this_setting_existed_keeps_its_gpu(self):
        provider = self.provider("lstm")
        legacy = copy.deepcopy(provider.ledger)
        legacy.pop("accelerator", None)
        recovered = KaggleExecution(self.transport, provider.directory, legacy, lambda value: None,
                                    clock=lambda: 1000, sleep=lambda seconds: None, background_progress=False)
        self.assertIs(recovered.metadata()["enable_gpu"], True)

    def test_the_remote_notebook_must_match_the_requested_hardware(self):
        provider = self.provider("lstm", "cpu")
        provider.submit()
        self.transport.metadata["enable_gpu"] = True
        with self.assertRaises(ProviderError):
            provider.control()


class RunnerHardwareTests(unittest.TestCase):
    def execute(self, branch, accelerator, devices, cpus=8):
        configuration = {"selected_models": [branch], "training_backend": "kaggle", "shap": {"enabled": False}}
        if accelerator:
            configuration["neural_accelerator"] = accelerator
        context = {"run_id": str(uuid.uuid4()), "region_id": str(uuid.uuid4()), "dataset_id": str(uuid.uuid4()),
                   "branch": branch, "attempt_id": str(uuid.uuid4()), "configuration_sha256": configuration_digest(configuration)}
        seen = {}
        def train(**kwargs):
            seen.update({name: os.environ.get(name) for name in ("TF_INTRA_OP_THREADS", "TF_INTER_OP_THREADS", "OMP_NUM_THREADS")})
            return {"status": "completed", "candidates": []}
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "synthetic.csv"
            data.write_bytes(b"a,b\n1,2\n")
            job = {"context": context, "configuration": configuration, "dataset_sha256": hashlib.sha256(data.read_bytes()).hexdigest()}
            fake_tf = SimpleNamespace(config=SimpleNamespace(list_physical_devices=lambda kind: devices))
            names = ("TF_INTRA_OP_THREADS", "TF_INTER_OP_THREADS", "OMP_NUM_THREADS")
            with patch.dict(os.environ, {name: "1" for name in names}), patch.dict(sys.modules, {"tensorflow": fake_tf}), \
                    patch.object(runner, "prepare_runtime"), patch.object(runner, "report"), patch.object(runner, "write_bundle"), \
                    patch.object(runner.os, "cpu_count", return_value=cpus), \
                    patch.object(region_training_variants, "train_region_recurrent_candidate", side_effect=train):
                runner.run(job, data, Path(directory) / "output")
        return seen

    def test_a_gpu_request_still_refuses_a_machine_without_a_gpu(self):
        for accelerator in (None, "gpu"):
            with self.subTest(accelerator=accelerator), self.assertRaises(RuntimeError):
                self.execute("lstm", accelerator, [])

    def test_a_cpu_request_does_not_need_a_gpu_and_uses_the_cores_it_was_given(self):
        for branch in ("lstm", "gru"):
            with self.subTest(branch=branch):
                seen = self.execute(branch, "cpu", [], cpus=4)
                self.assertEqual(seen, {"TF_INTRA_OP_THREADS": "4", "TF_INTER_OP_THREADS": "2", "OMP_NUM_THREADS": "4"})

    def test_cpu_threads_are_capped_and_never_below_one(self):
        self.assertEqual(self.execute("lstm", "cpu", [], cpus=64)["TF_INTRA_OP_THREADS"], "4")
        self.assertEqual(self.execute("lstm", "cpu", [], cpus=1)["TF_INTRA_OP_THREADS"], "1")
        self.assertEqual(self.execute("lstm", "cpu", [], cpus=None)["TF_INTRA_OP_THREADS"], "1")

    def test_a_gpu_run_keeps_the_single_thread_default(self):
        seen = self.execute("lstm", "gpu", ["synthetic GPU"], cpus=8)
        self.assertEqual(seen["TF_INTRA_OP_THREADS"], "1")


if __name__ == "__main__":
    unittest.main()
