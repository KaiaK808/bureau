"""Keep git's automatic maintenance out of the repositories the Python tests create and delete.

git commit, merge, fetch, rebase and am, and receive-pack on the side a push goes to, all start
`git maintenance run --auto --detach`. From git 2.54 on, its default strategy repacks a repository
once objects/17/ holds two loose objects (a copy of templates/scripts already puts one there), and
that detached repack can still write into .git while a test deletes its temp dir, which then fails
with "Directory not empty: '.git'" (ubuntu CI run 36972857574).
"""
import json
from pathlib import Path

# For git calls that run with the test's own environment.
OFF_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false"}
# git arguments that write the same switch into a repository's config: needed where git runs without
# the test's environment, as receive-pack does behind a local push (git drops GIT_CONFIG_* for it).
OFF_CONFIG = ("config", "maintenance.auto", "false")


def _events(trace):
    return [json.loads(line) for line in Path(trace).read_text().splitlines()]


def commands(trace):
    """The argv of every git process that wrote to the GIT_TRACE2_EVENT file `trace`."""
    return [event["argv"] for event in _events(trace) if event["event"] == "start"]


def maintenance_started(trace):
    """The argv of every `git maintenance` or `git gc` that those processes started."""
    return [event["argv"] for event in _events(trace)
            if event["event"] == "child_start" and event["argv"][1:2] in (["maintenance"], ["gc"])]
