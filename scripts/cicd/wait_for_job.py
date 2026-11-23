"""Hold a publication step until a sibling job of the same run attempt has succeeded.

Image builds start while the regression tests run. Pushing to the registry is the one
step that must not happen for a commit whose tests have not passed, so it waits here.
Anything other than a clean success (failure, cancelled, skipped, ambiguous, timeout,
or repeated API errors) stops the job: the gate fails closed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd import transient

MAX_API_ERRORS = 3


class JobNotSuccessful(RuntimeError):
    pass


def get(path):
    request = Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'})

    def once():
        with urlopen(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response exceeds its budget.')
            return json.loads(raw)
    try:
        return transient.retry(once)
    except HTTPError as error:
        raise RuntimeError('GitHub API check failed (HTTP ' + str(error.code) + '); token/body omitted.') from None
    except (URLError, OSError) as error:
        raise RuntimeError('GitHub API check failed (network error ' + type(error).__name__ +
                           ' after 3 attempts); token/body omitted.') from None


def find_job(repository, run_id, attempt, name, fetch):
    jobs = []
    for page in range(1, 6):
        data = fetch('repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100&page=%d' % (repository, run_id, attempt, page))
        jobs.extend(data['jobs'])
        if len(data['jobs']) < 100:
            break
    matches = [j for j in jobs if j.get('name') == name]
    return matches[0] if len(matches) == 1 else None


def wait(repository, run_id, attempt, name, *, fetch=None, sleep=time.sleep, clock=time.monotonic,
         timeout=1200, interval=5):
    fetch = fetch or get  # resolved at call time so tests and callers can substitute the reader
    deadline, errors = clock() + timeout, 0
    while True:
        try:
            job = find_job(repository, run_id, attempt, name, fetch)
            errors = 0
        except RuntimeError:
            errors += 1
            if errors > MAX_API_ERRORS:
                raise JobNotSuccessful("Could not read the '" + name + "' job state; nothing is published.") from None
            job = None
        if job is not None and job.get('status') == 'completed':
            if job.get('conclusion') == 'success':
                return {'job': name, 'conclusion': 'success'}
            raise JobNotSuccessful("The required '" + name + "' job ended as " + str(job.get('conclusion')) +
                                   '; nothing is published.')
        if clock() >= deadline:
            raise JobNotSuccessful("The required '" + name + "' job did not finish within " + str(timeout) +
                                   ' seconds; nothing is published.')
        sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--interval', type=int, default=5)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9 ()-]{0,60}', args.job):
        raise ValueError('Invalid job name')
    env = os.environ
    try:
        result = wait(env['GITHUB_REPOSITORY'], env['GITHUB_RUN_ID'], env['GITHUB_RUN_ATTEMPT'], args.job,
                      timeout=args.timeout, interval=args.interval)
    except JobNotSuccessful as error:
        message = str(error)
        print('::error title=Not published::' + message.replace('%', '%25').replace('\n', '%0A'))
        if env.get('GITHUB_STEP_SUMMARY'):
            with Path(env['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
                stream.write('## Image not published\n\n' + message + '\n')
        sys.exit(1)
    print(json.dumps({'passed': True, **result}))


if __name__ == '__main__':
    main()
