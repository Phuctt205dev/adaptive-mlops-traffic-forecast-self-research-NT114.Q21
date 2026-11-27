"""Recovery after the guard lease expired during an automatic role rollback (run 124, 27/11/2026).

The engine restored every patched resource but lost its lease before it could record the end of the
rollback, so the receipt still says phase=rollback / guard_held / recovery_required. Ownership may be
released only when the live cluster proves the exact BEFORE state and an idle, recorded dispatch state.
"""
import copy
import json
import tempfile
from pathlib import Path
import unittest

from scripts.cicd import cd_state, recover_lease_lost as recovery
from tests.test_cd_control import MemoryAPI, transaction

NAMESPACE_UID = "ns-uid"
COMMIT = "3" * 40


def deployment(name, replicas, image="old", uid=None):
    return {"metadata": {"name": name, "uid": uid or "uid-" + name},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": "c", "image": image}]}}}}


def pod(app, ready=True, terminating=False):
    value = {"metadata": {"name": app + "-pod", "labels": {"app": app}},
             "status": {"phase": "Running", "containerStatuses": [{"ready": ready}]}}
    if terminating:
        value["metadata"]["deletionTimestamp"] = "now"
    return value


class FakeCluster:
    def __init__(self):
        self.namespace_uid = NAMESPACE_UID
        self.deployments = {name: deployment(name, replicas) for name, replicas in [
            ("api", 0), ("api-next", 2), ("train-tree", 0), ("tree-next", 1), ("train-neural", 0), ("neural-next", 1),
            ("airflow-scheduler", 1), ("airflow-webserver", 1)]}
        selectors = {"traffic-api": "api-next", "traffic-api-training": "api-next", "api-peers": "api-next",
                     "api-preview": "api-next", "traffic-training-tree": "tree-next", "traffic-training-worker": "neural-next"}
        self.services = {name: {"metadata": {"name": name}, "spec": {"selector": {"app": app}}} for name, app in selectors.items()}
        self.pods = [pod("api-next"), pod("api-next"), pod("tree-next"), pod("neural-next"),
                     pod("airflow-scheduler"), pod("airflow-webserver")]
        self.job = {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
        self.guard_pod_phase = "Failed"
        self.idle = {"heads": ["h"], "active": 0, "running": 0, "paused": False, "drift_paused": False}
        self.writes = []

    def call(self, *arguments, **kwargs):
        verb, kind = arguments[0], arguments[1]
        if verb != "get":
            self.writes.append(arguments)
            raise AssertionError("recovery proof must be read-only")
        if kind == "namespace":
            return {"metadata": {"uid": NAMESPACE_UID}}
        if kind == "pods":
            guard = {"metadata": {"name": "guard-pod", "labels": {"job-name": "guard-job"}}, "status": {"phase": self.guard_pod_phase}}
            return {"items": copy.deepcopy(self.pods) + [guard]}
        if kind == "job":
            return copy.deepcopy(self.job)
        if kind == "service":
            return copy.deepcopy(self.services[arguments[2]])
        if kind == "deployment":
            return copy.deepcopy(self.deployments[arguments[2]])
        raise AssertionError(arguments)

    def deployment(self, name):
        return copy.deepcopy(self.deployments[name])

    def remote(self, value, source):
        assert source == recovery.IDLE
        return dict(self.idle)


def receipt(**changes):
    value = {"schema": "traffic-role-delivery/v2", "commit": COMMIT, "release_id": "r" * 64, "phase": "rollback",
             "completed": False, "rolled_back": False, "guard_held": True, "recovery_required": True,
             "patched": ["api"], "active_before": "api-next", "dag_was_paused": False, "drift_was_paused": False,
             "guard_job": "guard-job", "guard_pod": "guard-pod"}
    value.update(changes)
    return value


def inverses(cluster):
    return [{"kind": "deployment", "name": "api", "uid": "uid-api", "before": copy.deepcopy(cluster.deployments["api"]["spec"]),
             "after": "a" * 64}]


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self.active = {**transaction(124, "auto"), "commit": COMMIT, "recovery_required": True}

    def prove(self, journal=None, plans=None):
        return recovery.prove(self.cluster, journal or receipt(), plans if plans is not None else inverses(self.cluster), self.active)

    def test_exact_before_state_and_idle_dispatch_prove_recovery_without_writes(self):
        proof = self.prove()
        self.assertTrue(proof["recovery_verified"])
        self.assertEqual(proof["deployments_ready"], 5)
        self.assertEqual(proof["restored"], [{"kind": "deployment", "name": "api", "uid_matches": True, "spec_equals_before": True}])
        self.assertEqual(proof["routing"]["traffic-api"], "api-next")
        self.assertEqual(self.cluster.writes, [])

    def test_patched_resource_not_back_to_before_is_refused(self):
        self.cluster.deployments["api"]["spec"]["template"]["spec"]["containers"][0]["image"] = "new"
        plans = inverses(FakeCluster())
        with self.assertRaisesRegex(ValueError, "not restored"):
            self.prove(plans=plans)

    def test_replaced_resource_uid_is_refused(self):
        plans = inverses(self.cluster)
        plans[0]["uid"] = "another"
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.prove(plans=plans)

    def test_journal_must_cover_every_patched_resource(self):
        with self.assertRaisesRegex(ValueError, "patched"):
            self.prove(journal=receipt(patched=["api", "train-tree"]))

    def test_only_an_unfinished_rollback_with_lost_lease_is_accepted(self):
        for changes in ({"phase": "stage_api"}, {"completed": True}, {"rolled_back": True}, {"guard_held": False},
                        {"recovery_required": False}, {"schema": "traffic-delivery/v1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prove(journal=receipt(**changes))

    def test_receipt_of_another_transaction_is_refused(self):
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.prove(journal=receipt(commit="4" * 40))

    def test_guard_still_running_is_refused(self):
        self.cluster.job = {"status": {"conditions": []}}
        with self.assertRaisesRegex(ValueError, "guard"):
            self.prove()
        self.cluster.job = {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
        self.cluster.guard_pod_phase = "Running"
        with self.assertRaisesRegex(ValueError, "guard"):
            self.prove()

    def test_traffic_must_route_to_the_previous_ready_cohort(self):
        self.cluster.services["traffic-api"]["spec"]["selector"]["app"] = "api"
        with self.assertRaisesRegex(ValueError, "route"):
            self.prove()

    def test_serving_cohort_not_fully_ready_is_refused(self):
        self.cluster.pods[1] = pod("api-next", ready=False)
        with self.assertRaisesRegex(ValueError, "Ready"):
            self.prove()

    def test_candidate_slot_still_running_is_refused(self):
        self.cluster.deployments["api"]["spec"]["replicas"] = 2
        plans = inverses(self.cluster)
        with self.assertRaisesRegex(ValueError, "candidate"):
            self.prove(plans=plans)

    def test_dispatch_must_match_recorded_state_and_be_idle(self):
        for changes in ({"paused": True}, {"drift_paused": True}, {"active": 1}, {"running": 1}):
            with self.subTest(changes=changes):
                self.cluster.idle = {"heads": ["h"], "active": 0, "running": 0, "paused": False, "drift_paused": False, **changes}
                with self.assertRaisesRegex(ValueError, "dispatch"):
                    self.prove()

    def test_wrong_namespace_is_refused(self):
        self.cluster.namespace_uid = "other"
        with self.assertRaisesRegex(ValueError, "namespace"):
            self.prove()


class ReleaseTests(unittest.TestCase):
    def test_release_clears_ownership_but_keeps_auto_blocked(self):
        active = {**transaction(124, "auto"), "commit": COMMIT, "recovery_required": True}
        last = transaction(122, "manual")
        api = MemoryAPI()
        store = cd_state.Store(api)
        store.update(lambda state: ({**state, "mode": "auto", "blocked": True, "active": active,
                                     "failure": copy.deepcopy(active), "last_success": last}, None), message="seed")
        cluster = FakeCluster()
        proof = recovery.prove(cluster, receipt(), inverses(cluster), active)
        recovery.release(store, active, proof, expected_success=last)
        state, _ = store.load()
        self.assertIsNone(state["active"])
        self.assertTrue(state["blocked"])
        self.assertFalse(state["failure"]["recovery_required"])
        self.assertEqual(state["last_success"], last)

    def test_journal_files_must_belong_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "delivery-a.json").write_text(json.dumps(receipt()), encoding="utf-8")
            (root / "private").mkdir()
            (root / "private" / "delivery-b.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same transaction"):
                recovery.load(root / "delivery-a.json", root / "private" / "delivery-b.json")


if __name__ == "__main__":
    unittest.main()
