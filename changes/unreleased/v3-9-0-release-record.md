section: Documentation

- Record the published v3.9.0 tag and merge commit, the GitHub Release, the release review rounds, both gate suites, release CI results passing on the first attempt with no reruns and the tested-tree comparison; refresh install and upgrade markers and the static manual. Documentation only; update the source checkout for this record, with no adopting-repository resync or compatibility change. Validated with `tests/test_docs.sh`, `tests/test_no_internal_refs.sh` and `python3 scripts/changelog.py check`; release and CI facts are maintainer-supplied.
