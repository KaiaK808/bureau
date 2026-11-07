"""Keep git's automatic maintenance out of the repositories the Python tests create and delete.

git commit, merge, fetch, rebase and am, and receive-pack on the side a push goes to, all start
`git maintenance run --auto --detach`. From git 2.54 on, its default strategy repacks a repository
once objects/17/ holds two loose objects (a copy of templates/scripts already puts one there), and
that detached repack can still write into .git while a test deletes its temp dir, which then fails
with "Directory not empty: '.git'" (ubuntu CI run 36972857574).
"""
import json
from pathlib import Path
import subprocess

# For git calls that run with the test's own environment.
OFF_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false"}
# git arguments that write the same switch into a repository's config: needed where git runs without
# the test's environment, as receive-pack does behind a local push (git drops GIT_CONFIG_* for it).
OFF_CONFIG = ("config", "maintenance.auto", "false")
CLONE_CONFIG = ("-c", "=".join(OFF_CONFIG[1:]))


def disable(repo):
    """Persist OFF_CONFIG for Git children that do not inherit OFF_ENV."""
    subprocess.run(['git', '-C', str(repo), *OFF_CONFIG], check=True, timeout=5)


def require_disabled(root):
    """Audit working, separate and bare fixture repositories, including nested origins."""
    configs = [path for path in Path(root).rglob('config')
               if (path.parent/'HEAD').is_file() and (path.parent/'objects').is_dir()]
    if not configs:
        raise AssertionError(f'no fixture repositories found in {root}')
    for config in configs:
        result = subprocess.run(['git', '--git-dir', str(config.parent), 'config', '--local',
                                 '--get', OFF_CONFIG[1]], text=True, capture_output=True, timeout=5)
        if result.returncode or result.stdout.strip() != OFF_CONFIG[2]:
            raise AssertionError(f'automatic maintenance enabled in fixture {config.parent}')


def _events(trace):
    return [json.loads(line) for line in Path(trace).read_text().splitlines()]


def commands(trace):
    """The argv of every git process that wrote to the GIT_TRACE2_EVENT file `trace`."""
    return [event["argv"] for event in _events(trace) if event["event"] == "start"]


def maintenance_started(trace):
    """The argv of every `git maintenance` or `git gc` that those processes started."""
    return [event["argv"] for event in _events(trace)
            if event["event"] == "child_start" and event["argv"][1:2] in (["maintenance"], ["gc"])]
