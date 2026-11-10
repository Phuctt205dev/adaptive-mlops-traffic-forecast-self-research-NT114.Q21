"""Display labels must not disconnect workflow triggers or machine gates."""
from pathlib import Path
import re
import unittest

from scripts.cicd import pipeline

ROOT=Path(__file__).resolve().parents[1]
WORKFLOWS=ROOT/'.github/workflows'


class WorkflowNavigationTests(unittest.TestCase):
    def test_main_run_calls_backend_after_acceptance_without_a_second_automatic_listener(self):
        ci=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8')
        cd=(WORKFLOWS/'backend-deploy.yml').read_text(encoding='utf-8')
        self.assertNotIn('workflow_run:',cd)
        self.assertIn('workflow_call:',cd)
        caller=ci.split('\n  backend-delivery:\n')[1].split('\n  frontend-publication:\n')[0]
        self.assertIn('needs: [scope, acceptance]',caller)
        self.assertIn("needs.acceptance.result == 'success'",caller)
        self.assertIn("github.event_name == 'push'",caller)
        self.assertIn('uses: ./.github/workflows/backend-deploy.yml',caller)
        self.assertIn('current_run: true',caller)

    def test_required_job_names_still_match_the_coverage_gate(self):
        source=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8').split('\njobs:\n',1)[1]
        jobs=re.findall(r'^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:|\Z)',source,re.M|re.S)
        actual=set()
        for identifier,body in jobs:
            label=re.search(r'^    name: (.+)$',body,re.M)
            name=label.group(1).strip('"\'') if label else identifier
            if identifier=='image':
                roles=re.search(r'^        role: \[(.+)\]$',body,re.M).group(1).split(',')
                actual.update(name+' ('+role.strip()+')' for role in roles)
            else:actual.add(name)
        scope=pipeline.classify([],baseline=None,commit='a'*40,frontend=True)
        expected=pipeline.required_jobs(scope)|{'backend-delivery','frontend-publication','summary'}
        self.assertEqual(actual,expected)

    def test_all_file_based_workflow_dispatch_and_lookup_targets_exist(self):
        references=set()
        for path in (ROOT/'scripts/cicd').iterdir():
            if path.suffix in {'.py','.ps1'}:
                references.update(re.findall(r'actions/workflows/([a-zA-Z0-9_-]+\.yml)',path.read_text(encoding='utf-8')))
        self.assertTrue(references,'The real controller must contain workflow API references')
        for name in references:
            with self.subTest(workflow=name):self.assertTrue((WORKFLOWS/name).is_file())


if __name__=='__main__':unittest.main()
