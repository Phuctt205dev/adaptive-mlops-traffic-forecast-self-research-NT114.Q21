"""Exercise bootstrap Plan with two kubectl matches; all Kubernetes calls are fake."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class BootstrapClientSelectionTests(unittest.TestCase):
    def test_multiple_kubectl_matches_use_one_client_and_only_server_dry_run(self):
        shell=shutil.which('pwsh') or shutil.which('powershell.exe')
        if not shell:self.skipTest('PowerShell is required for this script regression')
        script=Path(__file__).resolve().parents[1]/'scripts/cicd/bootstrap_local_training.ps1'
        code=r'''
$ErrorActionPreference='Stop'
$global:clientCalls=0
$global:dryRunCalls=0
function global:Get-Command {
    [CmdletBinding()]
    param([Parameter(Position=0)][string]$Name,[string]$CommandType)
    [pscustomobject]@{Source='Invoke-TestKubectlOne'}
    [pscustomobject]@{Source='Invoke-TestKubectlTwo'}
}
function global:Invoke-TestKubectlOne {
    $Rest = $args
    $global:clientCalls++
    $global:LASTEXITCODE=0
    if ($Rest -contains 'get') { '11111111-1111-4111-8111-111111111111'; return }
    if (($Rest -contains 'apply') -and ($Rest -contains '--dry-run=server')) {
        $global:dryRunCalls++
        'serviceaccount/traffic-training-dispatcher'
        'serviceaccount/traffic-local-training'
        'role.rbac.authorization.k8s.io/traffic-training-dispatcher'
        'rolebinding.rbac.authorization.k8s.io/traffic-training-dispatcher'
        'clusterrole.rbac.authorization.k8s.io/traffic-training-capacity-reader'
        'clusterrolebinding.rbac.authorization.k8s.io/traffic-training-capacity-reader'
        return
    }
    throw 'Unexpected Kubernetes mutation'
}
function global:Invoke-TestKubectlTwo { throw 'Second client must never be invoked' }
& $env:TRAFFIC_TEST_BOOTSTRAP -Action Plan -Kubeconfig $env:TRAFFIC_TEST_CONFIG `
  -Context test-only -NamespaceUid '11111111-1111-4111-8111-111111111111'
if ($global:clientCalls -ne 2 -or $global:dryRunCalls -ne 1) { throw 'Plan call boundary differs' }
'PLAN_SINGLE_CLIENT_OK'
'''
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'dummy-config';config.write_text('no credentials')
            result=subprocess.run([shell,'-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-Command',code],
                env={**os.environ,'TRAFFIC_TEST_BOOTSTRAP':str(script),'TRAFFIC_TEST_CONFIG':str(config)},
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('PLAN_SINGLE_CLIENT_OK',result.stdout)


if __name__=='__main__':unittest.main()
