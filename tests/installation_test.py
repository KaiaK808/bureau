"""Exercise the installer against real temporary Git repositories, without network."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/bureau_install.py"
spec = importlib.util.spec_from_file_location("installer", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
# Test repositories must not inherit the operator's signing, hooks or identity settings, and no
# detached git maintenance may still write into one while its temp dir is deleted (git_maintenance.py).
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **git_maintenance.OFF_ENV,
           "GIT_AUTHOR_NAME": "Bureau Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Bureau Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bureau install ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "target repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def run_install(self, *args, status=0, program=INSTALLER, env=None):
        proc = subprocess.run([sys.executable, str(program), *args, "--repo", str(self.repo)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, status, proc.stdout + proc.stderr)
        return proc

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def snapshot(self):
        return {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(self.repo).parts}

    def test_install_matrix_and_repeat(self):
        for target in ("claude", "codex", "both"):
            with self.subTest(target=target):
                for p in self.repo.iterdir():
                    if p.name != ".git":
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                self.run_install("assets", "--target", target)
                self.assertEqual(self.snapshot(), {})
                self.run_install("assets", "--target", target, "--apply")
                files = self.snapshot()
                self.assertEqual((self.repo / "AGENTS.md").exists(), target != "claude")
                self.assertEqual((self.repo / "CLAUDE.md").exists(), target != "codex")
                commands = list((self.repo / ".agents/skills").glob("*/SKILL.md"))
                self.assertEqual(len(commands), 6 if target != "claude" else 0)
                for cmd in commands:
                    content = cmd.read_text()
                    self.assertIn("name: " + cmd.parent.name, content)
                    self.assertIn("description:", content)
                    self.assertNotIn("$ARGUMENTS", content)
                    self.assertNotIn("Skill tool", content)
                self.run_install("assets", "--apply")
                self.assertEqual(files, self.snapshot())

    def test_local_edits_stop_entire_batch_and_explicit_overwrite(self):
        self.run_install("assets", "--target", "codex", "--apply")
        rel = ".agents/skills/linear-implement/SKILL.md"
        self.write(rel, "custom instructions")
        before = self.snapshot()
        self.run_install("assets", "--scope", "interfaces", "--scope", "scripts", "--apply", status=3)
        self.assertEqual(self.snapshot(), before)
        self.run_install("assets", "--scope", "interfaces", "--scope", "scripts", "--apply", "--overwrite", rel)
        self.assertIn("description:", (self.repo / rel).read_text())
        self.assertTrue(os.access(self.repo / "scripts/queue-loop.sh", os.X_OK))

    def test_instruction_boundaries(self):
        self.write("AGENTS.md", "# My project\nKeep this.\n")
        self.run_install("assets", "--target", "codex", "--apply")
        body = (self.repo / "AGENTS.md").read_text()
        self.write("AGENTS.md", body + "\nUser appendix\n")
        self.run_install("assets", "--apply")
        self.assertEqual((self.repo / "AGENTS.md").read_text(), body + "\nUser appendix\n")
        self.write("AGENTS.md", (body + "\nUser appendix\n").replace("## Bureau workflow", "## My altered workflow"))
        before = self.snapshot()
        self.run_install("assets", "--apply", status=3)
        self.assertEqual(self.snapshot(), before)
        self.run_install("assets", "--apply", "--overwrite", "AGENTS.md")
        self.assertTrue((self.repo / "AGENTS.md").read_text().endswith("User appendix\n"))

    def test_legacy_instructions_are_not_guessed(self):
        self.write("CLAUDE.md", "# User\n<!-- bureau-init managed -->\nOld guidance\nUser guidance\n")
        before = self.snapshot()
        self.run_install("assets", "--apply", status=3)
        self.assertEqual(before, self.snapshot())

    def test_scoped_resync_preserves_config_and_other_interfaces(self):
        self.write(".bureau.json", '{"custom":true}')
        self.run_install("assets", "--target", "both", "--apply")
        self.write(".agents/skills/linear-implement/SKILL.md", "custom")
        self.run_install("assets", "--scope", "scripts", "--apply")
        self.assertEqual((self.repo / ".bureau.json").read_text(), '{"custom":true}')
        self.assertEqual((self.repo / ".agents/skills/linear-implement/SKILL.md").read_text(), "custom")
        self.assertFalse((self.repo / ".github").exists())
        self.run_install("assets", "--scope", "ci", "--apply")
        self.assertEqual((self.repo / ".github/workflows/ci.yml").read_bytes(),
                         (ROOT / "templates/.github/workflows/ci.yml").read_bytes())

    def test_updated_source_and_symlinked_install_path(self):
        source = self.root / "skill source"
        shutil.copytree(ROOT / "templates", source / "templates")
        (source / "scripts").mkdir()
        shutil.copy(INSTALLER, source / "scripts/bureau_install.py")
        link = self.root / "skill link"
        link.symlink_to(source, target_is_directory=True)
        program = link / "scripts/bureau_install.py"
        self.run_install("assets", "--apply", program=program)
        template = source / "templates/commands/linear-implement.md"
        template.write_text(template.read_text() + "\nNew upstream content\n")
        self.run_install("assets", "--apply", program=program)
        self.assertTrue((self.repo / ".claude/commands/linear-implement.md").read_text().endswith("New upstream content\n"))

    def test_rejects_symlink_destinations_unknown_overwrite_and_manifest(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / ".agents").symlink_to(outside, target_is_directory=True)
        self.run_install("assets", "--target", "codex", "--apply", status=1)
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / ".agents").unlink()
        self.run_install("assets", "--overwrite", "../elsewhere", status=1)
        self.write(".bureau-install.json", "[]")
        self.run_install("assets", status=1)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()

    def source_copy(self, parent):
        """This source's templates and installer, placed where a skill checkout would be."""
        source = parent / "skill source"
        shutil.copytree(ROOT / "templates", source / "templates", ignore=shutil.ignore_patterns("__pycache__"))
        (source / "scripts").mkdir()
        shutil.copy(INSTALLER, source / "scripts/bureau_install.py")
        return source

    def tagged_source(self, tag="v9.9.9"):
        source = self.source_copy(self.root)
        shutil.copy(ROOT / ".gitignore", source / ".gitignore")
        self.git(source, "init", "-q")
        self.git(source, "add", "-A")
        self.git(source, "commit", "-qm", "release")
        self.git(source, "tag", "-a", tag, "-m", tag)
        link = self.root / "skill link"
        link.symlink_to(source, target_is_directory=True)
        return source, link / "scripts/bureau_install.py"

    def manifest(self):
        return json.loads((self.repo / ".bureau-install.json").read_text())

    def test_the_tagged_source_starts_no_git_maintenance(self):
        trace = self.root / "trace2.json"
        with patch.dict(GIT_ENV, {"GIT_TRACE2_EVENT": str(trace)}):
            self.tagged_source()
        self.assertTrue(any("commit" in argv for argv in git_maintenance.commands(trace)))  # the commit was traced
        self.assertEqual(git_maintenance.maintenance_started(trace), [])

    def test_the_trace_reader_finds_maintenance_and_gc_children(self):
        # The helper the no-maintenance tests rely on, on a trace whose answer is known.
        trace = self.root / "synthetic-trace2.json"
        events = [{"event": "start", "argv": ["git", "-C", "repo", "commit", "-qm", "x"]},
                  {"event": "child_start", "argv": ["git", "maintenance", "run", "--auto", "--quiet", "--detach"]},
                  {"event": "child_start", "argv": ["git", "gc", "--auto"]},
                  {"event": "child_start", "argv": ["git", "repack", "-d", "-l"]}]
        trace.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(git_maintenance.maintenance_started(trace), [events[1]["argv"], events[2]["argv"]])
        self.assertEqual(git_maintenance.commands(trace), [events[0]["argv"]])

    def test_apply_records_tagged_untagged_and_dirty_source_per_scope(self):
        source, program = self.tagged_source()
        tagged = self.git(source, "rev-parse", "HEAD")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        recorded = {"git": True, "tag": "v9.9.9", "describe": "v9.9.9", "commit": tagged, "dirty": False}
        self.assertEqual(self.manifest()["sources"], {"scripts": recorded})
        # A later partial apply from an untagged commit relabels only the scope it wrote.
        ci = source / "templates/.github/workflows/ci.yml"
        ci.write_text(ci.read_text() + "# after the release\n")
        self.git(source, "commit", "-qam", "after the release")
        untagged = self.git(source, "rev-parse", "HEAD")
        self.run_install("assets", "--scope", "ci", "--apply", program=program)
        sources = self.manifest()["sources"]
        self.assertEqual(sources["scripts"], recorded)
        self.assertEqual({k: sources["ci"][k] for k in ("git", "tag", "commit", "dirty")},
                         {"git": True, "tag": None, "commit": untagged, "dirty": False})
        self.assertTrue(sources["ci"]["describe"].startswith("v9.9.9-1-g"), sources["ci"])
        self.assertTrue(untagged.startswith(sources["ci"]["describe"].rsplit("-g", 1)[1]), sources["ci"])
        # Modified tracked and new untracked template files are both installed, so both make the source dirty.
        script = source / "templates/scripts/queue-loop.sh"
        script.write_text(script.read_text() + "# local\n")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        self.assertEqual(self.manifest()["sources"]["scripts"], {**recorded, "tag": None, "describe": sources["ci"]["describe"],
                                                               "commit": untagged, "dirty": True})
        self.git(source, "checkout", "--", "templates/scripts/queue-loop.sh")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        self.assertFalse(self.manifest()["sources"]["scripts"]["dirty"])
        (source / "templates/scripts/local-extra.sh").write_text("echo local\n")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        self.assertTrue(self.manifest()["sources"]["scripts"]["dirty"])
        self.assertTrue((self.repo / "scripts/local-extra.sh").is_file())
        # A source whose status cannot be read is not reported clean.
        (source / "templates/scripts/local-extra.sh").unlink()
        (source / ".git/index").write_bytes(b"not an index")
        self.assertNotEqual(subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True, env=GIT_ENV).returncode, 0)
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        self.assertEqual(self.manifest()["sources"]["scripts"]["commit"], untagged)
        self.assertTrue(self.manifest()["sources"]["scripts"]["dirty"])

    def test_preview_and_conflicted_apply_leave_the_recorded_source_untouched(self):
        source, program = self.tagged_source()
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        before = (self.repo / ".bureau-install.json").read_bytes()
        script = source / "templates/scripts/queue-loop.sh"
        script.write_text(script.read_text() + "# next release\n")
        self.git(source, "commit", "-qam", "next release")
        self.git(source, "tag", "v9.9.10")  # lightweight: still the exact tag
        preview = self.run_install("assets", "--scope", "scripts", program=program)
        self.assertEqual(json.loads(preview.stdout)["source"]["tag"], "v9.9.10")
        self.assertEqual((self.repo / ".bureau-install.json").read_bytes(), before)
        self.write("scripts/queue-loop.sh", "local edit\n")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program, status=3)
        self.assertEqual((self.repo / ".bureau-install.json").read_bytes(), before)
        self.assertEqual(self.manifest()["sources"]["scripts"]["tag"], "v9.9.9")

    def test_ignored_template_files_are_skipped_and_leave_the_source_clean(self):
        source, program = self.tagged_source()
        for name in (".env", ".DS_Store"):
            (source / "templates/scripts" / name).write_text("local\n")
        self.assertEqual(self.git(source, "status", "--porcelain"), "")  # invisible to status
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        # Ignored files are not installed, so they are not inputs and the recorded source stays clean.
        self.assertEqual({k: self.manifest()["sources"]["scripts"][k] for k in ("tag", "dirty")}, {"tag": "v9.9.9", "dirty": False})
        self.assertFalse((self.repo / "scripts/.env").exists() or (self.repo / "scripts/.DS_Store").exists())
        for name in (".env", ".DS_Store"):
            (source / "templates/scripts" / name).unlink()
        # An ignored file the installer does not copy (a directory entry) leaves the source clean.
        (source / "templates/scripts/__pycache__").mkdir(exist_ok=True)
        (source / "templates/scripts/__pycache__/x.pyc").write_bytes(b"\0")
        self.run_install("assets", "--scope", "scripts", "--apply", program=program)
        self.assertFalse(self.manifest()["sources"]["scripts"]["dirty"])

    def test_source_git_calls_ignore_inherited_repository_redirects(self):
        source, program = self.tagged_source()
        host = self.root / "host repo"
        self.git(self.root, "init", "-q", str(host))
        (host / "f").write_text("x")
        self.git(host, "add", "-A")
        self.git(host, "commit", "-qm", "host")
        env = {**os.environ, "GIT_DIR": str(host / ".git"), "GIT_WORK_TREE": str(host)}
        self.run_install("assets", "--scope", "scripts", "--apply", program=program, env=env)
        self.assertEqual(self.manifest()["sources"]["scripts"]["commit"], self.git(source, "rev-parse", "HEAD"))
        self.assertFalse(self.manifest()["sources"]["scripts"]["dirty"])

    def test_preview_does_not_write_the_source_index(self):
        source, program = self.tagged_source()
        index = source / ".git/index"
        script = source / "templates/scripts/queue-loop.sh"
        os.utime(script, (script.stat().st_atime, script.stat().st_mtime + 5))  # stale stat data, same bytes
        before = index.read_bytes()
        preview = self.run_install("assets", "--scope", "scripts", program=program)
        self.assertFalse(json.loads(preview.stdout)["source"]["dirty"])
        self.assertEqual(index.read_bytes(), before)

    def test_source_that_is_not_its_own_checkout_is_recorded_as_such(self):
        loose = self.source_copy(self.root / "loose")
        self.run_install("assets", "--apply", program=loose / "scripts/bureau_install.py")
        not_git = {"git": False, "note": "not a git checkout"}
        self.assertEqual(self.manifest()["sources"], {"interfaces/claude": not_git})
        # Vendored inside another repository: that repository's commit says nothing about the template.
        host = self.root / "host repo"
        self.git(self.root, "init", "-q", str(host))
        nested = self.source_copy(host / "vendor")
        self.git(host, "add", "-A")
        self.git(host, "commit", "-qm", "vendor bureau")
        self.run_install("assets", "--scope", "scripts", "--apply", program=nested / "scripts/bureau_install.py")
        self.assertEqual(self.manifest()["sources"], {"interfaces/claude": not_git, "scripts": not_git})
        self.assertNotIn(self.git(host, "rev-parse", "HEAD"), (self.repo / ".bureau-install.json").read_text())

    def test_manifest_without_sources_is_accepted_and_malformed_sources_are_not(self):
        self.run_install("assets", "--apply")
        legacy = {k: v for k, v in self.manifest().items() if k not in ("sources", "sources_files_sha256")}
        (self.repo / ".bureau-install.json").write_text(json.dumps(legacy))
        self.run_install("assets", "--scope", "scripts", "--apply")
        self.assertEqual(set(self.manifest()["sources"]), {"scripts"})
        self.assertEqual(self.manifest()["files"], {**legacy["files"], **self.manifest()["files"]})
        for sources in ([], {"scripts": "v9.9.9"}):
            with self.subTest(sources=sources):
                self.write(".bureau-install.json", json.dumps({**legacy, "sources": sources}))
                self.run_install("assets", status=1)

    def test_app_prerequisites_do_not_require_provider_cli_or_tmux(self):
        args = ["bureau-install", "check", "--target", "both", "--repo", str(self.repo)]
        with patch.object(sys, "argv", args), patch.object(installer.shutil, "which", side_effect=lambda x: None if x in ("claude", "codex", "tmux") else x):
            self.assertEqual(installer.main(), 0)
        with patch.object(sys, "argv", args + ["--mode", "background"]), patch.object(installer.shutil, "which", side_effect=lambda x: None if x == "codex" else x):
            self.assertEqual(installer.main(), 1)

    def test_specify_init_uses_pinned_noninteractive_command(self):
        env = self.fake_specify()
        for target, active in (("claude", "claude"), ("codex", "codex"),
                               ("both", "claude"), ("both", "codex")):
            with self.subTest(target=target, active=active):
                before = self.snapshot()
                plan = json.loads(self.run_install("speckit", "--target", target,
                                                   "--active-integration", active, env=env).stdout)
                self.assertEqual(plan["version"], "1.2.2")
                first = ("codex" if active == "claude" else "claude") if target == "both" else active
                expected = [["specify", "init", "--here", "--integration", first, "--force",
                             "--non-interactive", "--ignore-agent-tools"]]
                if target == "both":
                    expected.append(["specify", "integration", "install", active])
                expected.append(["specify", "integration", "use", active])
                self.assertEqual(plan["commands"], expected)
                self.assertEqual(before, self.snapshot())

    def fake_specify(self):
        binary = self.root / "bin"
        binary.mkdir()
        executable = binary / "specify"
        executable.write_text("#!" + sys.executable + '''
import hashlib, json, os, pathlib, sys
if os.environ.get("TEST_SPECIFY_CALLS"):
    with open(os.environ["TEST_SPECIFY_CALLS"], "a") as out:
        out.write(json.dumps({"args":sys.argv[1:], "cwd":str(pathlib.Path.cwd())}) + "\\n")
if sys.argv[1] == "version":
    print("CLI Version " + os.environ.get("TEST_SPECIFY_VERSION", "1.2.2"))
    sys.exit(0)
# Resolve both sides: on macOS the temporary directory is reached through the /var symlink.
in_repo = pathlib.Path.cwd().resolve() == pathlib.Path(os.environ["TEST_SPECIFY_REPO"]).resolve()
if sys.argv[1:3] == ["integration", "upgrade"]:
    # Model the real upgrade's shared-manifest behavior: advance its version, retain legacy
    # hashes (and modified helpers), and mark observed pre-existing user bytes as recovered.
    for host, paths in (("speckit", [".specify/scripts/bash/common.sh", ".specify/templates/plan-template.md"]),
                        ("claude", [".claude/skills/speckit-plan/SKILL.md"]),
                        ("codex", [".agents/skills/speckit-plan/SKILL.md"])):
        manifest = pathlib.Path(".specify/integrations", host + ".manifest.json")
        record = json.loads(manifest.read_text()) if manifest.exists() else {"files":{}}
        recovered = set(record.get("recovered_files", []))
        for relative in paths:
            path = pathlib.Path(relative)
            if path.is_file() and relative not in record["files"]:
                record["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                recovered.add(relative)
        record.update(version="1.2.2", recovered_files=sorted(recovered))
        manifest.parent.mkdir(parents=True, exist_ok=True); manifest.write_text(json.dumps(record))
    sys.exit(0)
if sys.argv[1] == "extension":
    if os.environ.get("TEST_EXTENSION_FAIL"): sys.exit(7)
    assert sys.argv[2:4] == ["add", "--dev"]
    assert pathlib.Path(sys.argv[4], "extension.yml").is_file()
    names = ["speckit.git.feature", "speckit.git.commit"]
    for name in names:
        if name.endswith("commit") and os.environ.get("TEST_EXTENSION_INCOMPLETE"): continue
        skill = pathlib.Path(".agents/skills", name.replace(".", "-"), "SKILL.md")
        skill.parent.mkdir(parents=True, exist_ok=True); skill.write_text("native " + name)
    p = pathlib.Path(".specify/extensions"); p.mkdir(parents=True)
    (p / ".registry").write_text(json.dumps({"extensions":{"git":{"registered_commands":{"codex":names}}}}))
    sys.exit(0)
p = pathlib.Path(".specify"); p.mkdir(exist_ok=True)
state_path = p / "integration.json"
if sys.argv[1] == "init":
    if not in_repo and os.environ.get("TEST_HELPER_RENDER_FAIL"): sys.exit(7)
    assert "--non-interactive" in sys.argv and "--no-git" not in sys.argv
    target = sys.argv[sys.argv.index("--integration") + 1]
    action = "init"
    # Real 1.2.2 init replaces registration, even if another host was installed.
    state = {"version":"1.2.2", "installed_integrations":[target],
             "integration_settings":{target:{"script":"sh"}},
             "integration":target, "default_integration":target}
    (p / "init-options.json").write_text(json.dumps({"integration":target, "speckit_version":"1.2.2"}))
    c = p / "memory/constitution.md"; c.parent.mkdir(exist_ok=True); c.write_text("generated")
    files = {}
    setup = 'source "$SCRIPT_DIR/common.sh"\\n'
    if os.environ.get("TEST_PINNED_EXTRA_SOURCE"): setup += 'source "$SCRIPT_DIR/retired.sh"\\n'
    for relative, content in ((".specify/scripts/bash/common.sh", "# 1.2.2 helper\\n"),
                              (".specify/scripts/bash/setup-tasks.sh", setup),
                              (".specify/.gitignore", "feature.json\\nextensions/*/local-config.yml\\nintegrations/packages/\\nintegrations/packages.json\\n"),
                              (".specify/templates/plan-template.md", "generated template")):
        if not in_repo and os.environ.get("TEST_HELPER_RENDER_EMPTY") and relative.startswith(".specify/scripts/"): continue
        path = pathlib.Path(relative); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifests = p / "integrations"; manifests.mkdir(exist_ok=True)
    (manifests / "speckit.manifest.json").write_text(json.dumps({"version":"1.2.2", "files":files}))
elif sys.argv[1] == "integration":
    action, target = sys.argv[2:]
    assert action in ("install", "use")
    state = json.loads(state_path.read_text())
    if action == "install":
        if target not in state["installed_integrations"]: state["installed_integrations"].append(target)
        state["integration_settings"][target] = {"script":"sh"}
    else:
        assert target in state["installed_integrations"]
        state["integration"] = state["default_integration"] = target
else:
    raise AssertionError(sys.argv)
state_path.write_text(json.dumps(state))
if action != "use":
    host = ".claude" if target == "claude" else ".agents"
    s = pathlib.Path(host, "skills/speckit-plan/SKILL.md"); s.parent.mkdir(parents=True, exist_ok=True); s.write_text("generated")
    pathlib.Path("CLAUDE.md" if target == "claude" else "AGENTS.md").write_text("generated")
with open("calls", "a") as out: out.write(action + " " + target + "\\n")
if in_repo and os.environ.get("TEST_FAIL_TARGET") == target: sys.exit(7)
''')
        executable.chmod(0o755)
        return {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "TEST_SPECIFY_REPO": str(self.repo),
                "TEST_SPECIFY_CALLS": str(self.root / "specify-calls.jsonl")}

    def fake_upgrade(self, env):
        subprocess.run([str(self.root / "bin/specify"), "integration", "upgrade", "claude"],
                       cwd=self.repo, env=env, check=True, capture_output=True)
        Path(env["TEST_SPECIFY_CALLS"]).unlink()

    def assert_no_specify_repo_writes(self, env):
        calls = Path(env["TEST_SPECIFY_CALLS"])
        records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        self.assertEqual([r for r in records if r["cwd"] == str(self.repo) and r["args"] != ["version"]], [])

    def assert_integrations(self, installed, active):
        state = json.loads((self.repo / ".specify/integration.json").read_text())
        self.assertEqual(set(state["installed_integrations"]), set(installed))
        self.assertEqual(set(state["integration_settings"]), set(installed))
        self.assertEqual(state["default_integration"], active)
        self.assertEqual(state["integration"], active)

    def test_specify_dual_registration_and_default(self):
        env = self.fake_specify()
        for active in ("claude", "codex"):
            with self.subTest(active=active):
                self.run_install("speckit", "--target", "both", "--active-integration", active, "--apply", env=env)
                self.assert_integrations(("claude", "codex"), active)

    def test_specify_customized_gitignore_survives_success_and_failure(self):
        env = self.fake_specify()
        self.run_install("speckit", "--target", "claude", "--apply", env=env)
        relative = ".specify/.gitignore"
        path = self.repo / relative
        generated = path.read_bytes()
        record = json.loads((self.repo / ".specify/integrations/speckit.manifest.json").read_text())
        self.assertEqual(record["files"][relative], installer.digest(generated))
        customized = generated + b"\n# Private drafts\r\nprivate-drafts/\r\n"
        for failed in (False, True):
            with self.subTest(failed=failed):
                path.write_bytes(customized)
                self.run_install("speckit", "--apply", status=int(failed),
                                 env={**env, **({"TEST_FAIL_TARGET": "claude"} if failed else {})})
                self.assertEqual(path.read_bytes(), customized)

    def legacy_specify_helpers(self):
        self.write(".specify/integration.json", '{"integration":"claude","version":"0.7.5"}')
        self.write(".specify/init-options.json", '{"integration":"claude","speckit_version":"0.7.5"}')
        helper = ".specify/scripts/bash/common.sh"
        template = ".specify/templates/plan-template.md"
        self.write(helper, "# 0.7.5 helper\n")
        self.write(template, "legacy template\n")
        self.write(".specify/integrations/speckit.manifest.json", json.dumps({
            "version":"0.7.5", "files":{relative: installer.digest((self.repo / relative).read_bytes())
                                        for relative in (helper, template)}}))
        return helper, template

    def test_legacy_customized_common_is_refused_before_specify(self):
        env = self.fake_specify()
        helper, _ = self.legacy_specify_helpers()
        self.write(helper, (self.repo / helper).read_text() + "# local customization\n")
        before = self.snapshot()
        for flags in ((), ("--apply",), ("--apply", "--overwrite", helper)):
            with self.subTest(flags=flags):
                proc = self.run_install("speckit", *flags, env=env, status=1)
                self.assertIn(helper, proc.stderr)
                self.assertIn("rebase the customization onto the 1.2.2 helpers or remove it", proc.stderr)
                self.assertEqual(before, self.snapshot())
                self.assert_no_specify_repo_writes(env)

    def test_legacy_templates_stay_preserved_with_unmodified_helpers(self):
        env = self.fake_specify()
        helper, template = self.legacy_specify_helpers()
        self.write(template, "custom legacy template\n")
        spec_template = '.specify/templates/spec-template.md'
        self.write(spec_template, '# Custom specification layout\nKeep this template verbatim.\n')
        self.run_install("speckit", "--apply", env=env)
        self.assertEqual((self.repo / helper).read_text(), "# 1.2.2 helper\n")
        self.assertEqual((self.repo / template).read_text(), "custom legacy template\n")
        self.assertEqual((self.repo / spec_template).read_text(), '# Custom specification layout\nKeep this template verbatim.\n')
        self.assert_integrations(("claude",), "claude")

    def test_current_customized_helpers_are_refused(self):
        env = self.fake_specify()
        self.run_install("speckit", "--apply", env=env)
        helper = ".specify/scripts/bash/common.sh"
        self.write(helper, (self.repo / helper).read_text() + "# reviewed 1.2.2 customization\n")
        before = self.snapshot()
        Path(env["TEST_SPECIFY_CALLS"]).unlink()
        proc = self.run_install("speckit", "--apply", env=env, status=1)
        self.assertIn(helper, proc.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assert_no_specify_repo_writes(env)

    def test_advanced_manifest_still_refuses_customized_legacy_helper(self):
        env = self.fake_specify()
        helper, _ = self.legacy_specify_helpers()
        manifest = self.repo / ".specify/integrations/speckit.manifest.json"
        legacy_hash = json.loads(manifest.read_text())["files"][helper]
        self.write(helper, (self.repo / helper).read_text() + "# user customization\n")
        second = ".specify/scripts/bash/setup-tasks.sh"
        self.write(second, "# another customized helper\n")
        self.fake_upgrade(env)
        record = json.loads(manifest.read_text())
        self.assertEqual(record["version"], "1.2.2")
        self.assertEqual(record["files"][helper], legacy_hash)
        self.assertNotEqual(legacy_hash, installer.digest((self.repo / helper).read_bytes()))
        before = self.snapshot()
        for flags in ((), ("--apply",), ("--apply", "--overwrite", helper)):
            with self.subTest(flags=flags):
                proc = self.run_install("speckit", *flags, env=env, status=1)
                self.assertIn(helper, proc.stderr)
                self.assertIn(second, proc.stderr)
                self.assertIn("rebase the customization onto the 1.2.2 helpers or remove it", proc.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assert_no_specify_repo_writes(env)

    def test_recovered_templates_and_skills_survive_force_init(self):
        env = self.fake_specify()
        paths = (".specify/templates/plan-template.md", ".claude/skills/speckit-plan/SKILL.md",
                 ".agents/skills/speckit-plan/SKILL.md")
        for relative in paths:
            self.write(relative, "custom recovered bytes: " + relative + "\n")
        self.fake_upgrade(env)
        before = self.snapshot()
        for host, relative in zip(("speckit", "claude", "codex"), paths):
            record = json.loads((self.repo / (".specify/integrations/" + host + ".manifest.json")).read_text())
            self.assertIn(relative, record["recovered_files"])
            self.assertEqual(record["files"][relative], installer.digest(before[relative]))
        self.run_install("speckit", "--target", "both", "--active-integration", "claude", "--apply", env=env)
        for relative in paths:
            self.assertEqual((self.repo / relative).read_bytes(), before[relative])
        # The next resync must preserve them too, after init has recorded generated hashes.
        self.run_install("speckit", "--apply", env=env)
        for relative in paths:
            self.assertEqual((self.repo / relative).read_bytes(), before[relative])

    def test_recovered_helper_matching_recorded_hash_is_refused(self):
        env = self.fake_specify()
        helper = ".specify/scripts/bash/common.sh"
        for content in ("# recovered user helper\n", "# 1.2.2 helper\n"):
            with self.subTest(content=content):
                self.write(helper, content)
                manifest = self.repo / ".specify/integrations/speckit.manifest.json"
                manifest.unlink(missing_ok=True)
                self.fake_upgrade(env)
                record = json.loads(manifest.read_text())
                self.assertIn(helper, record["recovered_files"])
                self.assertEqual(record["files"][helper], installer.digest((self.repo / helper).read_bytes()))
                before = self.snapshot()
                for flags in ((), ("--apply",)):
                    with self.subTest(flags=flags):
                        proc = self.run_install("speckit", *flags, env=env, status=1)
                        self.assertIn(helper, proc.stderr)
                        self.assertEqual(self.snapshot(), before)
                        self.assert_no_specify_repo_writes(env)

    def test_pristine_helper_bytes_override_legacy_or_missing_manifest(self):
        env = self.fake_specify()
        helper, _ = self.legacy_specify_helpers()
        self.write(helper, "# 1.2.2 helper\n")
        before = self.snapshot()
        self.run_install("speckit", env=env)
        self.assertEqual(self.snapshot(), before)
        (self.repo / ".specify/integrations/speckit.manifest.json").unlink()
        self.run_install("speckit", "--apply", env=env)
        self.assertEqual((self.repo / helper).read_text(), "# 1.2.2 helper\n")

    def test_unreferenced_extra_helpers_are_previewed_and_preserved(self):
        env = self.fake_specify()
        self.legacy_specify_helpers()
        extra = ".specify/scripts/bash/retired.sh"
        self.write(extra, "# local standalone helper\n")
        before = self.snapshot()
        plan = json.loads(self.run_install("speckit", env=env).stdout)
        self.assertEqual(plan["extra_helpers_preserved"], [extra])
        self.assertEqual(self.snapshot(), before)
        self.run_install("speckit", "--apply", env=env)
        self.assertEqual((self.repo / extra).read_bytes(), before[extra])

    def test_extra_helper_sourced_by_pinned_helper_is_refused(self):
        env = {**self.fake_specify(), "TEST_PINNED_EXTRA_SOURCE": "1"}
        extra = ".specify/scripts/bash/retired.sh"
        self.write(extra, "# cannot safely mix with new callers\n")
        self.fake_upgrade(env)
        before = self.snapshot()
        for flags in ((), ("--apply",)):
            with self.subTest(flags=flags):
                proc = self.run_install("speckit", *flags, env=env, status=1)
                self.assertIn(extra, proc.stderr)
                self.assertIn(".specify/scripts/bash/setup-tasks.sh", proc.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assert_no_specify_repo_writes(env)

    def test_pristine_helper_render_failure_writes_nothing(self):
        env = self.fake_specify()
        self.write(".specify/memory/constitution.md", "keep user constitution\n")
        before = self.snapshot()
        for flag in ("TEST_HELPER_RENDER_FAIL", "TEST_HELPER_RENDER_EMPTY"):
            with self.subTest(flag=flag):
                self.run_install("speckit", "--apply", env={**env, flag: "1"}, status=1)
                self.assertEqual(self.snapshot(), before)
                self.assert_no_specify_repo_writes(env)

    def test_specify_dual_active_and_preserve_constitution(self):
        env = self.fake_specify()
        self.write(".specify/memory/constitution.md", "My constitution\n")
        self.write(".claude/skills/speckit-plan/SKILL.md", "Custom skill")
        self.write("CLAUDE.md", "User instructions")
        self.run_install("speckit", "--target", "both", status=1)
        self.run_install("speckit", "--target", "both", "--active-integration", "codex", "--apply", env=env)
        self.assertEqual((self.repo / "calls").read_text(), "init claude\ninstall codex\nuse codex\n")
        self.assert_integrations(("claude", "codex"), "codex")
        self.assertEqual((self.repo / ".claude/skills/speckit-plan/SKILL.md").read_text(), "Custom skill")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), "User instructions")
        self.assertEqual((self.repo / ".specify/memory/constitution.md").read_text(), "My constitution\n")
        self.run_install("assets", "--target", "both", "--apply")
        self.run_install("speckit", "--apply", env=env)
        self.assert_integrations(("claude", "codex"), "codex")

    def test_legacy_specify_active_host_survives_adding_second_target(self):
        env = self.fake_specify()
        for active, added in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(active=active):
                self.write(".specify/integration.json", json.dumps({"integration": active}))
                self.assertFalse((self.repo / ".bureau-install.json").exists())
                before = self.snapshot()
                plan = json.loads(self.run_install("speckit", "--target", added, env=env).stdout)
                self.assertEqual(plan["active_integration"], active)
                self.assertEqual(plan["commands"][0][4], added)
                self.assertEqual(plan["commands"][1:], [["specify", "integration", "install", active],
                                                       ["specify", "integration", "use", active]])
                self.assertEqual(before, self.snapshot())
                self.run_install("speckit", "--target", added, "--apply", env=env)
                self.assert_integrations(("claude", "codex"), active)
                self.run_install("speckit", "--target", added, "--active-integration", added, "--apply", env=env)
                self.assert_integrations(("claude", "codex"), added)

    def legacy_git_extension(self):
        self.write(".specify/integration.json", '{"integration":"claude"}')
        self.write(".specify/extensions/git/extension.yml", "id: git\n")
        self.write(".specify/extensions/git/git-config.yml", "custom hook settings\n")
        self.write(".specify/extensions.yml", "custom hook policy\n")
        registry={"schema_version":"1.0", "custom":"keep", "extensions":{"git":{
            "version":"1.0.0", "enabled":True, "installed_at":"original", "custom":42,
            "registered_commands":{"claude":["speckit.git.feature"]}}}}
        self.write(".specify/extensions/.registry", json.dumps(registry))
        return registry

    def test_legacy_git_extension_registers_codex_without_reinstalling_user_hooks(self):
        env=self.fake_specify(); original=self.legacy_git_extension()
        self.write(".agents/skills/speckit-git-feature/SKILL.md", "custom native feature")
        self.write(".claude/skills/speckit-git-feature/SKILL.md", "custom Claude feature")
        before=self.snapshot()
        plan=json.loads(self.run_install("speckit", "--target", "codex", env=env).stdout)
        self.assertEqual(plan["extension_registration"][0]["target"], "codex")
        self.assertEqual(before,self.snapshot())
        self.run_install("speckit", "--target", "codex", "--apply", env=env)
        expected=json.loads(json.dumps(original))
        expected["extensions"]["git"]["registered_commands"]["codex"]=["speckit.git.feature","speckit.git.commit"]
        registry=self.repo/".specify/extensions/.registry"
        self.assertEqual(json.loads(registry.read_text()),expected)
        for path in (".specify/extensions.yml", ".specify/extensions/git/git-config.yml",
                     ".agents/skills/speckit-git-feature/SKILL.md", ".claude/skills/speckit-git-feature/SKILL.md"):
            self.assertEqual((self.repo/path).read_bytes(),before[path])
        skill=self.repo/".agents/skills/speckit-git-commit/SKILL.md"
        self.assertEqual(skill.read_text(),"native speckit.git.commit")
        self.assertEqual(json.loads((self.repo/".specify/integration.json").read_text())["integration"],"claude")
        saved=registry.read_bytes()
        self.run_install("speckit", "--target", "codex", "--apply", env=env)
        self.assertEqual(registry.read_bytes(),saved)
        skill.unlink()
        self.run_install("speckit", "--target", "codex", "--apply", env=env)
        self.assertEqual(skill.read_text(),"native speckit.git.commit")
        self.assertEqual(registry.read_bytes(),saved)

    def test_extension_render_failure_and_invalid_destinations_write_nothing(self):
        env=self.fake_specify();self.legacy_git_extension()
        for flag in ("TEST_EXTENSION_FAIL","TEST_EXTENSION_INCOMPLETE"):
            before=self.snapshot()
            self.run_install("speckit", "--target", "codex", "--apply", env={**env,flag:"1"},status=1)
            self.assertEqual(self.snapshot(),before)
        target=self.repo/".agents/skills/speckit-git-commit/SKILL.md"
        target.mkdir(parents=True)
        before=self.snapshot()
        self.run_install("speckit", "--target", "codex", "--apply", env=env,status=1)
        self.assertEqual(self.snapshot(),before)
        target.rmdir(); target.symlink_to(self.root/"outside")
        self.run_install("speckit", "--target", "codex", "--apply", env=env,status=1)
        self.assertFalse((self.root/"outside").exists())
        target.unlink(); target.parent.rmdir(); target.parent.write_text("existing user file")
        before=self.snapshot()
        self.run_install("speckit", "--target", "codex", "--apply", env=env,status=1)
        self.assertEqual(self.snapshot(),before)

    def test_unknown_active_specify_host_requires_explicit_switch(self):
        self.write(".specify/integration.json", '{"integration":"other-host"}')
        before = self.snapshot()
        self.run_install("speckit", "--target", "codex", status=1)
        self.assertEqual(before, self.snapshot())

    def test_specify_failure_restores_active_metadata_and_pin_is_enforced(self):
        env = self.fake_specify()
        metadata = '{"integration":"claude","custom":true}'
        self.write(".specify/integration.json", metadata)
        self.write(".specify/init-options.json", metadata)
        self.write(".specify/memory/constitution.md", "Keep me")
        self.run_install("speckit", "--target", "both", "--active-integration", "codex", "--apply", env={**env, "TEST_FAIL_TARGET": "codex"}, status=1)
        self.assertEqual((self.repo / ".specify/integration.json").read_text(), metadata)
        self.assertEqual((self.repo / ".specify/init-options.json").read_text(), metadata)
        self.assertEqual((self.repo / ".specify/memory/constitution.md").read_text(), "Keep me")
        before = self.snapshot()
        for version in ("0.7.5", "9.9.9"):
            with self.subTest(version=version):
                self.run_install("speckit", "--apply", env={**env, "TEST_SPECIFY_VERSION": version}, status=1)
                self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
