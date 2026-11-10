"""Display labels must not disconnect workflow triggers or machine gates."""
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from scripts.cicd import pipeline

ROOT=Path(__file__).resolve().parents[1]
WORKFLOWS=ROOT/'.github/workflows'


class WorkflowNavigationTests(unittest.TestCase):
    def test_frontend_acceptance_checks_the_uploaded_artifact_and_keeps_hidden_output(self):
        source=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8')
        build=source.split('\n  frontend-build:\n')[1].split('\n  backend-delivery:\n')[0]
        upload=build.split('name: frontend-build-${{ github.sha }}',1)[1].split('\n      - name:',1)[0]
        self.assertIn('include-hidden-files: true',upload)
        self.assertIn('frontend/dist/',upload)
        self.assertIn('frontend/frontend-build.json',upload)
        download=build.index('name: Download the uploaded frontend for verification')
        self.assertIn('actions/download-artifact@',build[download:])
        self.assertIn('path: frontend-checked',build[download:])
        self.assertLess(download,build.index('pipeline.py frontend-verify'))

    def condition(self, job, event, ref):
        """Evaluate the actual simple boolean conditions for a healthy graph.

        These conditions use the shared Python/Actions subset (strings,
        attributes, comparisons and booleans); dependencies are successful.
        """
        source=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8')
        # Match to the next job, preserving indented job attributes/steps.
        body=re.split(r'\n  [a-z][a-z0-9-]*:\n',source.split('\n  '+job+':\n')[1],maxsplit=1)[0]
        expression=re.search(r'^    if: (.+)$',body,re.M).group(1)
        if expression=='>-':
            expression=' '.join(re.search(r'^    if: >-\n((?:      .+\n)+)',body,re.M).group(1).split())
        expression=expression.replace('&&',' and ').replace('||',' or ')
        expression=re.sub(r'!(?!=)',' not ',expression).strip()
        expression=expression.replace('needs.backend-delivery','backend_delivery')
        github=SimpleNamespace(event_name=event,ref=ref,
                 event=SimpleNamespace(repository=SimpleNamespace(default_branch='master')))
        outputs=SimpleNamespace(backend='true',tests='true',frontend='true',current='true',full='true')
        needs=SimpleNamespace(scope=SimpleNamespace(outputs=outputs),acceptance=SimpleNamespace(result='success'))
        variables=SimpleNamespace(RKE2_CD_ENABLED='true')
        return bool(eval(expression,{'__builtins__':{}},{'github':github,'needs':needs,'vars':variables,
                         'backend_delivery':SimpleNamespace(outputs=SimpleNamespace(outcome='success')),
                         'always':lambda:True,'cancelled':lambda:False,'format':lambda text,value:text.format(value)}))

    def test_manual_branch_full_check_reaches_images_but_cannot_deploy(self):
        for event,ref,images in [('workflow_dispatch','refs/heads/ci/check',True),
                                ('workflow_dispatch','refs/heads/master',True),
                                ('schedule','refs/heads/master',True),
                                ('push','refs/heads/master',True),
                                ('push','refs/heads/ci/check',False),
                                ('pull_request','refs/pull/1/merge',False)]:
            with self.subTest(event=event,ref=ref):
                self.assertEqual(self.condition('image',event,ref),images)
                self.assertEqual(self.condition('backend-delivery',event,ref),event=='push' and ref=='refs/heads/master')
                self.assertEqual(self.condition('frontend-publication',event,ref),event=='push' and ref=='refs/heads/master')
        self.assertTrue(self.condition('frontend-build','workflow_dispatch','refs/heads/ci/check'))
        self.assertTrue(self.condition('current-runtime','workflow_dispatch','refs/heads/ci/check'))
        self.assertTrue(self.condition('legacy-migration','workflow_dispatch','refs/heads/ci/check'))
        self.assertFalse(self.condition('frontend-publication','workflow_dispatch','refs/heads/ci/check'))

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
