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

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/bureau_install.py"
spec = importlib.util.spec_from_file_location("installer", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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

    def test_app_prerequisites_do_not_require_provider_cli_or_tmux(self):
        args = ["bureau-install", "check", "--target", "both", "--repo", str(self.repo)]
        with patch.object(sys, "argv", args), patch.object(installer.shutil, "which", side_effect=lambda x: None if x in ("claude", "codex", "tmux") else x):
            self.assertEqual(installer.main(), 0)
        with patch.object(sys, "argv", args + ["--mode", "background"]), patch.object(installer.shutil, "which", side_effect=lambda x: None if x == "codex" else x):
            self.assertEqual(installer.main(), 1)

    def fake_specify(self):
        binary = self.root / "bin"
        binary.mkdir()
        executable = binary / "specify"
        executable.write_text("#!" + sys.executable + '''
import json, os, pathlib, sys
if sys.argv[1] == "version":
    print("CLI Version " + os.environ.get("TEST_SPECIFY_VERSION", "0.7.5"))
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
target = sys.argv[sys.argv.index("--integration") + 1]
p = pathlib.Path(".specify"); p.mkdir(exist_ok=True)
(p / "integration.json").write_text(json.dumps({"integration":target}))
(p / "init-options.json").write_text(json.dumps({"integration":target}))
c = p / "memory/constitution.md"; c.parent.mkdir(exist_ok=True); c.write_text("generated")
s = pathlib.Path(".claude/skills/speckit-plan/SKILL.md"); s.parent.mkdir(parents=True, exist_ok=True); s.write_text("generated")
pathlib.Path("CLAUDE.md").write_text("generated")
with open("calls", "a") as out: out.write(target + "\\n")
if os.environ.get("TEST_FAIL_TARGET") == target: sys.exit(7)
''')
        executable.chmod(0o755)
        return {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}

    def test_specify_dual_active_and_preserve_constitution(self):
        env = self.fake_specify()
        self.write(".specify/memory/constitution.md", "My constitution\n")
        self.write(".claude/skills/speckit-plan/SKILL.md", "Custom skill")
        self.write("CLAUDE.md", "User instructions")
        self.run_install("speckit", "--target", "both", status=1)
        self.run_install("speckit", "--target", "both", "--active-integration", "codex", "--apply", env=env)
        self.assertEqual((self.repo / "calls").read_text(), "claude\ncodex\n")
        self.assertEqual((self.repo / ".claude/skills/speckit-plan/SKILL.md").read_text(), "Custom skill")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), "User instructions")
        self.assertEqual((self.repo / ".specify/memory/constitution.md").read_text(), "My constitution\n")
        self.run_install("assets", "--target", "both", "--apply")
        self.run_install("speckit", "--apply", env=env)
        self.assertEqual(json.loads((self.repo / ".specify/integration.json").read_text())["integration"], "codex")

    def test_legacy_specify_active_host_survives_adding_second_target(self):
        env = self.fake_specify()
        for active, added in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(active=active):
                self.write(".specify/integration.json", json.dumps({"integration": active}))
                self.assertFalse((self.repo / ".bureau-install.json").exists())
                before = self.snapshot()
                plan = json.loads(self.run_install("speckit", "--target", added).stdout)
                self.assertEqual(plan["active_integration"], active)
                self.assertEqual([cmd[cmd.index("--integration") + 1] for cmd in plan["commands"]], [added, active])
                self.assertEqual(before, self.snapshot())
                self.run_install("speckit", "--target", added, "--apply", env=env)
                self.assertEqual(json.loads((self.repo / ".specify/integration.json").read_text())["integration"], active)
                self.run_install("speckit", "--target", added, "--active-integration", added, "--apply", env=env)
                self.assertEqual(json.loads((self.repo / ".specify/integration.json").read_text())["integration"], added)

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
        plan=json.loads(self.run_install("speckit", "--target", "codex").stdout)
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
        self.run_install("speckit", "--apply", env={**env, "TEST_SPECIFY_VERSION": "9.9.9"}, status=1)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
