"""Fetch the pinned public test tool through GitHub's release asset API.

The release-page download endpoint can fail before redirecting to storage.
The asset ID and SHA-256 are fixed; redirects never forward the GitHub token.
"""
import hashlib
import os
from pathlib import Path
import sys
import time
from urllib.request import Request,build_opener

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.ci_coverage import SafeRedirect

ASSET='https://api.github.com/repos/kubernetes-sigs/kind/releases/assets/531396433'
SHA256='aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d'
LIMIT=16*1024*1024


def install(path,*,opener=None,sleep=time.sleep):
    opener=opener or build_opener(SafeRedirect())
    for attempt in range(3):
        try:
            headers={'Accept':'application/octet-stream','Cache-Control':'no-cache'}
            request=Request(ASSET,headers=headers)
            with opener.open(request,timeout=60) as response:data=response.read(LIMIT+1)
            if len(data)>LIMIT or hashlib.sha256(data).hexdigest()!=SHA256:
                raise ValueError('Pinned kind binary checksum differs')
            target=Path(path);temporary=target.with_suffix('.download')
            temporary.write_bytes(data);temporary.chmod(0o755);temporary.replace(target)
            print('Pinned kind v0.33.0 verified')
            return
        except ValueError:raise
        except Exception as error:
            if attempt==2:raise RuntimeError('Pinned kind download failed; '+type(error).__name__+'; details omitted') from None
            sleep(5*(attempt+1))


if __name__=='__main__':install(Path(os.environ['RUNNER_TEMP'])/'kind')
