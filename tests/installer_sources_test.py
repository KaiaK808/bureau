"""Installer changes in v3.1, run against real temporary Git repositories: legacy managed-block
markers in every form are refused instead of getting a second block, template files the source
ignores are never installed, and the managed block describes the three merge routes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/bureau_install.py"
spec = importlib.util.spec_from_file_location("installer", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Bureau Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Bureau Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
# The forms older installers and hand edits left behind, one per line of a CLAUDE.md.
LEGACY = ("<!-- bureau-init managed -->",
          "<!-- bureau-init managed: regenerate via `/bureau-init --update` -->",
          "<!-- end bureau-init managed -->",
          "<!--bureau-init managed-->",
          "<!-- Bureau-Init Managed (do not edit) -->")


class Target(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bureau installer ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "target repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=GIT_ENV)

    def install(self, *args, status=0, program=INSTALLER):
        proc = subprocess.run([sys.executable, str(program), *args, "--repo", str(self.repo)],
                              capture_output=True, text=True, env=GIT_ENV)
        self.assertEqual(proc.returncode, status, proc.stdout + proc.stderr)
        return proc

    def snapshot(self):
        return {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(self.repo).parts}

    def plan(self, proc):
        return {item["path"]: item for item in json.loads(proc.stdout)["files"]}


class LegacyMarkerTests(Target):
    def test_every_legacy_form_is_refused_and_nothing_is_written(self):
        for target, name in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
            for marker in LEGACY:
                with self.subTest(target=target, marker=marker):
                    for p in self.repo.iterdir():
                        if p.name != ".git":
                            shutil.rmtree(p) if p.is_dir() else p.unlink()
                    (self.repo / name).write_text("# Project\nKeep this.\n\n" + marker + "\nOld generated guidance\n")
                    before = self.snapshot()
                    preview = self.install("assets", "--target", target, status=3)
                    item = self.plan(preview)[name]
                    self.assertEqual(item["action"], "conflict")
                    self.assertTrue(item["reason"].startswith("legacy Bureau section at line 4 ("), item["reason"])
                    self.assertIn("bureau-init:begin", item["reason"])
                    self.install("assets", "--target", target, "--apply", status=3)
                    self.install("assets", "--target", target, "--apply", "--overwrite", name, status=3)
                    self.assertEqual(self.snapshot(), before)

    def test_a_legacy_section_next_to_the_delimited_block_is_refused(self):
        self.install("assets", "--target", "claude", "--apply")
        block = (self.repo / "CLAUDE.md").read_text()
        for text in (block + "\n<!-- bureau-init managed: regenerate via `/bureau-init --update` -->\nOld\n<!-- end bureau-init managed -->\n",
                     "<!-- bureau-init managed -->\nOld\n\n" + block):
            with self.subTest(text=text[:40]):
                (self.repo / "CLAUDE.md").write_text(text)
                before = self.snapshot()
                item = self.plan(self.install("assets", status=3))["CLAUDE.md"]
                self.assertIn("outside the bureau-init:begin/end block", item["reason"])
                self.install("assets", "--apply", "--overwrite", "CLAUDE.md", status=3)
                self.assertEqual(self.snapshot(), before)

    def test_a_legacy_section_delimited_by_hand_is_replaced(self):
        legacy = ("# Project\n\n" + installer.BEGIN + "\n<!-- bureau-init managed: regenerate via `/bureau-init --update` -->\n"
                  "Old guidance\n<!-- end bureau-init managed -->\n" + installer.END + "\n\nUser appendix\n")
        (self.repo / "CLAUDE.md").write_text(legacy)
        # The region does not match any recorded install, so adopting it needs an explicit overwrite.
        self.assertEqual(self.plan(self.install("assets", status=3))["CLAUDE.md"]["action"], "conflict")
        self.install("assets", "--apply", "--overwrite", "CLAUDE.md")
        text = (self.repo / "CLAUDE.md").read_text()
        self.assertNotIn("bureau-init managed", text)
        self.assertTrue(text.startswith("# Project\n\n" + installer.BEGIN + "\n## Bureau workflow"), text[:80])
        self.assertTrue(text.endswith("User appendix\n"))

    def test_prose_about_bureau_init_is_not_a_marker(self):
        text = "# Project\nRun `/bureau-init --update` for settings; bureau-init managed files live in scripts/.\n<!-- managed by hand -->\n"
        (self.repo / "CLAUDE.md").write_text(text)
        self.assertEqual(self.plan(self.install("assets"))["CLAUDE.md"]["action"], "install")
        self.install("assets", "--apply")
        self.assertTrue((self.repo / "CLAUDE.md").read_text().startswith(text))


class IgnoredTemplateFileTests(Target):
    # Files a maintainer's checkout accumulates: ignored by the repository's .gitignore or
    # by the patterns added in source(); none of them may reach an installation.
    IGNORED = ("templates/scripts/.DS_Store", "templates/scripts/.env", "templates/scripts/.env.local",
               "templates/scripts/debug.log", "templates/skills/bureau/.DS_Store",
               "templates/skills/bureau/__pycache__/helper.cpython-312.pyc",
               "templates/commands/draft-notes.md", "templates/workflows/scratch.js")

    def source(self, git=True, ignore=True):
        source = self.root / "skill source"
        shutil.copytree(ROOT / "templates", source / "templates", ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        (source / "scripts").mkdir()
        shutil.copy(INSTALLER, source / "scripts/bureau_install.py")
        if ignore:
            (source / ".gitignore").write_text((ROOT / ".gitignore").read_text()
                                                + "templates/commands/draft-*.md\ntemplates/workflows/scratch.js\n")
        if git:
            for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "release"], ["tag", "v9.9.9"]):
                subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, env=GIT_ENV)
        for relative in self.IGNORED:
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_text("local only\n")
        return source, source / "scripts/bureau_install.py"

    ALL = ("assets", "--target", "both", "--scope", "interfaces", "--scope", "scripts", "--scope", "workflows")

    def installed(self):
        return set(self.snapshot())

    def test_ignored_files_are_skipped_named_and_leave_the_source_clean(self):
        source, program = self.source()
        self.assertEqual(subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True,
                                        text=True, env=GIT_ENV).stdout, "")
        preview = self.install(*self.ALL, program=program)
        result = json.loads(preview.stdout)
        planned = {item["path"] for item in result["files"]}
        for relative in self.IGNORED:
            name = relative.split("/")[-1]
            self.assertFalse(any(path.endswith("/" + name) or path == name for path in planned), (relative, planned))
        self.assertFalse(result["source"]["dirty"], result["source"])
        self.assertEqual(result["skipped"], sorted(self.IGNORED))
        self.assertIn("skipped template files the source ignores", preview.stderr)
        self.assertIn("templates/scripts/.env", preview.stderr)
        self.install(*self.ALL, "--apply", program=program)
        installed = self.installed()
        # Every tracked template is still installed.
        for script in (ROOT / "templates/scripts").iterdir():
            if script.is_file() and script.name not in (".DS_Store",):
                self.assertIn("scripts/" + script.name, installed)
        self.assertIn(".agents/skills/bureau/SKILL.md", installed)
        self.assertIn(".claude/commands/linear-implement.md", installed)
        self.assertIn(".claude/workflows/conflict-aware-schedule.js", installed)
        for path in installed:
            self.assertFalse(Path(path).name in (".DS_Store", ".env", ".env.local", "debug.log", "draft-notes.md", "scratch.js")
                             or path.endswith(".pyc"), path)
        manifest = json.loads((self.repo / ".bureau-install.json").read_text())
        self.assertEqual({s["tag"] for s in manifest["sources"].values()}, {"v9.9.9"})
        self.assertFalse(any(s["dirty"] for s in manifest["sources"].values()), manifest["sources"])

    def test_a_tracked_file_that_matches_an_ignore_pattern_is_installed(self):
        source, program = self.source()
        keep = source / "templates/scripts/fixture.log"
        keep.write_text("tracked on purpose\n")
        subprocess.run(["git", "-C", str(source), "add", "-f", str(keep)], check=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "force-add"], check=True, env=GIT_ENV)
        result = json.loads(self.install("assets", "--scope", "scripts", "--apply", program=program).stdout)
        self.assertNotIn("templates/scripts/fixture.log", result["skipped"])
        self.assertEqual((self.repo / "scripts/fixture.log").read_text(), "tracked on purpose\n")
        self.assertIn("templates/scripts/debug.log", result["skipped"])

    def test_an_untracked_file_that_is_not_ignored_is_still_installed_and_marks_the_source_dirty(self):
        source, program = self.source()
        (source / "templates/scripts/local-extra.sh").write_text("echo local\n")
        result = json.loads(self.install("assets", "--scope", "scripts", "--apply", program=program).stdout)
        self.assertTrue((self.repo / "scripts/local-extra.sh").is_file())
        self.assertTrue(result["source"]["dirty"])

    def test_a_source_whose_index_cannot_be_read_still_applies_its_ignore_rules(self):
        source, program = self.source()
        (source / ".git/index").write_bytes(b"not an index")
        result = json.loads(self.install("assets", "--scope", "scripts", "--apply", program=program).stdout)
        self.assertIn("templates/scripts/.env", result["skipped"]); self.assertIn("templates/scripts/debug.log", result["skipped"])
        self.assertFalse((self.repo / "scripts/.env").exists() or (self.repo / "scripts/debug.log").exists())

    def test_ignore_rules_that_cannot_be_read_stop_the_install_before_any_write(self):
        # A git whose check-ignore fails: neither the checkout nor the scratch repository can say
        # what the source ignores, so nothing may be installed on a guess.
        source, program = self.source()
        real = shutil.which("git")
        fake = self.root / "fake bin"
        fake.mkdir()
        (fake / "git").write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = check-ignore ] && { echo "fatal: simulated" >&2; exit 128; }; done\n'
                                  'exec "' + real + '" "$@"\n')
        (fake / "git").chmod(0o755)
        before = self.snapshot()
        proc = subprocess.run([sys.executable, str(program), "assets", "--scope", "scripts", "--apply", "--repo", str(self.repo)],
                              capture_output=True, text=True, env={**GIT_ENV, "PATH": str(fake) + os.pathsep + GIT_ENV["PATH"]})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("cannot read the template source's ignore rules", proc.stderr)
        self.assertIn("simulated", proc.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.repo / ".bureau-install.json").exists())

    def test_a_source_outside_git_skips_dotfiles_and_its_ignore_patterns(self):
        source, program = self.source(git=False)
        result = json.loads(self.install(*self.ALL, "--apply", program=program).stdout)
        self.assertEqual(result["source"], {"git": False, "note": "not a git checkout"})
        self.assertEqual(result["skipped"], sorted(self.IGNORED))
        shutil.rmtree(source)
        # Without any .gitignore only the dotfiles are known to be local.
        source, program = self.source(git=False, ignore=False)
        result = json.loads(self.install("assets", "--scope", "scripts", program=program).stdout)
        self.assertEqual(result["skipped"], ["templates/scripts/.DS_Store", "templates/scripts/.env", "templates/scripts/.env.local"])
        self.assertIn("scripts/debug.log", {item["path"] for item in result["files"]})


class ManagedBlockTests(Target):
    def test_the_block_names_the_three_merge_routes_for_both_hosts(self):
        for target in ("claude", "codex"):
            with self.subTest(target=target):
                block = installer.instruction_block(target).decode()
                self.assertNotIn("can merge inline when the separate merge agent is disabled", block)
                for text in ("`agents.merge_mode`", "with `auto` and the merge agent on (`agents.merge` and a Merge state), the review moves the ticket to Merge",
                             "with `auto` and the merge agent off, the review itself merges inline through the same gate",
                             "with `manual`, the review parks the ticket in Merge and a human merges",
                             "Never take a merge route for a review-only request", "A request for a spec or review does not authorize implementation or merging"):
                    self.assertIn(text, block)
        self.install("assets", "--target", "both", "--apply")
        for name in ("CLAUDE.md", "AGENTS.md"):
            self.assertIn("a human merges", (self.repo / name).read_text())


if __name__ == "__main__":
    unittest.main()
