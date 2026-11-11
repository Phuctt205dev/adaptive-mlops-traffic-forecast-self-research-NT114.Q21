"""Display labels must not disconnect workflow triggers or machine gates."""
from pathlib import Path
import json
import re
from types import SimpleNamespace
import unittest

from scripts.cicd import pipeline

ROOT=Path(__file__).resolve().parents[1]
WORKFLOWS=ROOT/'.github/workflows'


class WorkflowNavigationTests(unittest.TestCase):
    def test_frontend_acceptance_checks_the_uploaded_artifact_and_keeps_hidden_output(self):
        source=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8')
        def job(name):
            return re.split(r'\n  [a-z][a-z0-9-]*:\n', source.split('\n  '+name+':\n')[1], maxsplit=1)[0]
        build=job('frontend-build')
        producer_steps=re.split(r'\n      - ',build)[1:]
        upload=next(s for s in producer_steps if 'actions/upload-artifact@' in s
                    and 'name: frontend-build-${{ github.sha }}' in s)
        self.assertIn('include-hidden-files: true',upload)
        self.assertIn('frontend/dist/',upload)
        self.assertIn('frontend/frontend-build.json',upload)
        self.assertNotIn('actions/download-artifact@',build)
        self.assertNotIn('pipeline.py frontend-verify',build)
        self.assertLess(build.index('- run: npm ci'),build.index('- run: npm test'))
        self.assertLess(build.index('- run: npm test'),build.index('- run: npm run build'))
        package=json.loads((ROOT/'frontend/package.json').read_text(encoding='utf-8'))
        self.assertEqual(package['scripts']['test'],'node --test')
        consumer=job('acceptance')
        self.assertRegex(consumer,r'(?m)^    needs: \[[^\n]*frontend-build')
        steps=re.split(r'\n      - ',consumer)[1:]
        config=next(i for i,s in enumerate(steps) if 'pipeline.py frontend-config' in s)
        download=next(i for i,s in enumerate(steps) if 'actions/download-artifact@' in s
                      and 'name: frontend-build-${{ github.sha }}' in s)
        verify=next(i for i,s in enumerate(steps) if 'pipeline.py frontend-verify' in s)
        accept=next(i for i,s in enumerate(steps) if 'pipeline.py accept' in s)
        self.assertIn('path: frontend-checked',steps[download])
        self.assertLess(config,download);self.assertLess(download,verify);self.assertLess(verify,accept)
        for index in (config,download,verify):
            condition=re.search(r'^        if: (.+)$',steps[index],re.M)
            self.assertIsNotNone(condition, 'Frontend consumer steps need explicit scope conditions')
            expression=condition.group(1).replace('needs.frontend-build','frontend')
            for result in ('success','failure','skipped','cancelled'):
                with self.subTest(step=index,producer_result=result):
                    allowed=eval(expression,{'__builtins__':{}},{'frontend':SimpleNamespace(result=result)})
                    self.assertEqual(allowed,result=='success')

    def condition(self, job, event, ref, *, scope=None, accepted=True):
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
        outputs=SimpleNamespace(**{k:str(scope[k]).lower() for k in ('backend','tests','frontend','current','full')}) if scope else \
            SimpleNamespace(backend='true',tests='true',frontend='true',current='true',full='true')
        needs=SimpleNamespace(scope=SimpleNamespace(outputs=outputs),acceptance=SimpleNamespace(result='success' if accepted else 'failure'))
        variables=SimpleNamespace(RKE2_CD_ENABLED='true')
        return bool(eval(expression,{'__builtins__':{}},{'github':github,'needs':needs,'vars':variables,
                         'backend_delivery':SimpleNamespace(outputs=SimpleNamespace(outcome='success')),
                         'always':lambda:True,'cancelled':lambda:False,'format':lambda text,value:text.format(value)}))

    def test_daily_manifest_graph_skips_clusters_and_requires_acceptance_to_deploy(self):
        daily=pipeline.classify(['deployment/rke2/roles/tree.json'],baseline='b'*40,commit='a'*40)
        expected={'image':True,'frontend-build':False,'current-runtime':False,'legacy-migration':False,'backend-delivery':True}
        for job,allowed in expected.items():
            with self.subTest(job=job):
                self.assertEqual(self.condition(job,'push','refs/heads/master',scope=daily),allowed)
        self.assertFalse(self.condition('backend-delivery','push','refs/heads/master',scope=daily,accepted=False))
        for event in ('workflow_dispatch','pull_request','schedule'):
            self.assertFalse(self.condition('backend-delivery',event,'refs/heads/master',scope=daily))
        controller=pipeline.classify(['scripts/cicd/roles_deploy.py'],baseline='b'*40,commit='a'*40)
        self.assertTrue(self.condition('current-runtime','push','refs/heads/master',scope=controller))
        full=pipeline.classify([],baseline='b'*40,commit='a'*40,force=True)
        self.assertTrue(self.condition('legacy-migration','workflow_dispatch','refs/heads/ci/check',scope=full))

    def test_manual_input_defaults_to_daily_and_is_wired_into_scope(self):
        source=(WORKFLOWS/'backend-ci.yml').read_text(encoding='utf-8')
        dispatch=source.split('\n  workflow_dispatch:\n')[1].split('\n  schedule:')[0]
        self.assertIn('options: [daily, full]',dispatch)
        self.assertIn('default: daily',dispatch)
        self.assertIn("VALIDATION_MODE: ${{ inputs.validation || 'daily' }}",source)

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
