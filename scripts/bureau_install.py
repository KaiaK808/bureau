#!/usr/bin/env python3
"""Install Bureau assets. Standard library only; preview unless --apply is set."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SPECKIT_VERSION = "0.7.5"
MANIFEST = ".bureau-install.json"
BEGIN = "<!-- bureau-init:begin -->"
END = "<!-- bureau-init:end -->"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def destination(repo, relative):
    path = repo / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid destination: " + relative)
    for ancestor in (path, *path.parents):
        if ancestor == repo:
            break
        if ancestor.is_symlink():
            raise ValueError("refusing symlink destination: " + relative)
    return path


def read_manifest(repo):
    path = destination(repo, MANIFEST)
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or (data and (data.get("version") != 1 or not isinstance(data.get("files"), dict))):
        raise ValueError("unsupported or malformed installation manifest")
    if not isinstance(data.get("targets", []), list) or any(
        t not in ("claude", "codex") for t in data.get("targets", [])
    ):
        raise ValueError("invalid installation targets")
    return data


def targets_for(args, manifest):
    targets = (["claude", "codex"] if args.target == "both" else [args.target]) if args.target else manifest.get("targets", ["claude"])
    return targets or ["claude"]


def atomic_write(path, data, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=".bureau-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(name, mode | (0o111 if executable else 0))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def render_command(source, target):
    text = source.read_text()
    if target == "claude":
        return text.encode()
    # Preserve a single canonical procedure; adapt only host invocation syntax.
    text = text.replace("---\n", f"---\nname: {source.stem}\n", 1)
    text = text.replace("```text\n$ARGUMENTS\n```", "Use the issue identifier and options from the user's invocation or current request.")
    text = text.replace("`$ARGUMENTS`", "the user's supplied arguments")
    text = re.sub(r"Invoke `/(speckit-[\w-]+)` via the Skill tool", r"Read `.agents/skills/\1/SKILL.md` and follow its instructions", text, flags=re.IGNORECASE)
    text = re.sub(r"Invoke `/(linear-[\w-]+)` via the Skill tool", r"Read `.agents/skills/\1/SKILL.md` and follow its instructions", text, flags=re.IGNORECASE)
    text = text.replace("mcp__linear-server__list_comments", "the available Linear list-comments tool")
    text = re.sub(r"`/([a-z][\w-]*)", r"`$\1", text)
    return text.encode()


def instruction_block(target):
    body = (ROOT / "templates/instructions/workflow.md").read_text()
    body = body.replace("{{SKILLS_DIR}}", ".agents/skills" if target == "codex" else ".claude/skills")
    body = body.replace("{{INVOKE}}", "$" if target == "codex" else "/")
    return (BEGIN + "\n" + body.rstrip() + "\n" + END + "\n").encode()


def merge_instructions(old, block):
    text = old.decode()
    if BEGIN not in text and END not in text:
        if "<!-- bureau-init managed -->" in text:
            raise ValueError("legacy Bureau section: review and delimit it with bureau-init:begin/end before resync")
        return old + (b"\n\n" if old and not old.endswith(b"\n\n") else b"") + block, None
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise ValueError("malformed Bureau instruction markers")
    start, end = text.index(BEGIN), text.index(END) + len(END)
    if text[end:end + 1] == "\n":
        end += 1
    return (text[:start] + block.decode() + text[end:]).encode(), text[start:end].encode()


def assets(repo, args, manifest, targets):
    candidates = []
    scopes = set(args.scope or ["interfaces"])
    if "interfaces" in scopes:
        for target in targets:
            for source in sorted((ROOT / "templates/commands").glob("*.md")):
                relative = f".agents/skills/{source.stem}/SKILL.md" if target == "codex" else f".claude/commands/{source.name}"
                candidates.append((relative, render_command(source, target), False, False))
            candidates.append(("AGENTS.md" if target == "codex" else "CLAUDE.md", instruction_block(target), False, True))
            if target == "codex":
                for source in sorted((ROOT / "templates/skills").rglob("*")):
                    if source.is_file():
                        relative = ".agents/skills/" + str(source.relative_to(ROOT / "templates/skills"))
                        candidates.append((relative, source.read_bytes(), False, False))
    if "scripts" in scopes:
        for source in sorted((ROOT / "templates/scripts").iterdir()):
            if source.is_file():
                candidates.append((f"scripts/{source.name}", source.read_bytes(), source.suffix in (".sh", ".py"), False))
    if "workflows" in scopes and "claude" in targets:
        for source in sorted((ROOT / "templates/workflows").glob("*.js")):
            workflow = source.read_text()
            if "/* BUREAU_SCHEDULER_CORE */" in workflow:
                core = (ROOT / "templates/scripts/bureau-schedule.mjs").read_text().replace("export function", "function")
                workflow = workflow.replace("/* BUREAU_SCHEDULER_CORE */", core)
            candidates.append((f".claude/workflows/{source.name}", workflow.encode(), False, False))
    if "ci" in scopes:
        candidates.append((".github/workflows/ci.yml", (ROOT / "templates/.github/workflows/ci.yml").read_bytes(), False, False))

    unknown = set(args.overwrite) - {item[0] for item in candidates}
    if unknown:
        raise ValueError("--overwrite is outside the selected scope: " + ", ".join(sorted(unknown)))
    records = dict(manifest.get("files", {}))
    plan, writes = [], []
    for relative, incoming, executable, managed in candidates:
        path = destination(repo, relative)
        old = path.read_bytes() if path.exists() else b""
        previous = records.get(relative)
        try:
            new, region = merge_instructions(old, incoming) if managed else (incoming, old)
        except ValueError as exc:
            plan.append({"path": relative, "action": "conflict", "reason": str(exc)})
            continue
        current_hash = digest(region) if region is not None else None
        installed_hash = digest(incoming)
        if path.exists() and new == old:
            action = "unchanged"
        elif not path.exists() or (managed and region is None):
            action = "install"
        elif relative in args.overwrite or previous == current_hash:
            action = "update"
        else:
            action = "conflict"
        plan.append({"path": relative, "action": action})
        if action != "conflict":
            records[relative] = installed_hash
            writes.append((path, new, executable))

    print(json.dumps({"targets": targets, "files": plan}, indent=2))
    # An apply containing conflicts writes nothing, including the manifest.
    if any(item["action"] == "conflict" for item in plan):
        return 3
    if not args.apply:
        return 0
    # Validate all destinations, including installer bookkeeping, before writes.
    manifest_path = destination(repo, MANIFEST)
    ignore_path = destination(repo, ".gitignore")
    ignore = ignore_path.read_bytes() if ignore_path.exists() else b""
    if MANIFEST not in ignore.decode().splitlines():
        ignore = ignore.rstrip(b"\n") + b"\n" + MANIFEST.encode() + b"\n"
    for path, data, executable in writes:
        atomic_write(path, data, executable)
    updated = dict(manifest)
    updated.update(version=1, files=records)
    if "interfaces" in scopes:
        updated["targets"] = sorted(set(manifest.get("targets", [])) | set(targets))
    atomic_write(ignore_path, ignore)
    atomic_write(manifest_path, (json.dumps(updated, indent=2) + "\n").encode())
    return 0


def speckit_customizations(repo):
    """Snapshot local drift: specify --force overwrites native host skills."""
    hashes = {}
    directory = destination(repo, ".specify/integrations")
    for path in directory.glob("*.manifest.json"):
        destination(repo, str(path.relative_to(repo)))
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
            raise ValueError("malformed Spec Kit manifest: " + str(path))
        hashes.update(record["files"])
    candidates = [destination(repo, "CLAUDE.md"), destination(repo, "AGENTS.md")]
    for relative in (".specify/templates", ".specify/scripts", ".specify/memory"):
        candidates.extend(destination(repo, relative).rglob("*"))
    for relative in (".claude/skills", ".agents/skills"):
        for skill in destination(repo, relative).glob("speckit-*"):
            candidates.extend(skill.rglob("*"))
    keep = {}
    for path in candidates:
        relative = str(path.relative_to(repo))
        destination(repo, relative)
        if path.is_file():
            data = path.read_bytes()
            if relative in ("CLAUDE.md", "AGENTS.md", ".specify/memory/constitution.md") or digest(data) != hashes.get(relative):
                keep[path] = data
    return keep


def git_extension_plan(repo, installed):
    """Inspect only; extension rendering happens outside the adopting tree."""
    extension = destination(repo, ".specify/extensions/git")
    if "codex" not in installed or not extension.exists():
        return None
    if not destination(repo, ".specify/extensions/git/extension.yml").is_file():
        raise ValueError("existing Git extension is missing extension.yml")
    registry = destination(repo, ".specify/extensions/.registry")
    value = json.loads(registry.read_text())
    extensions = value.get("extensions") if isinstance(value, dict) else None
    entry = extensions.get("git") if isinstance(extensions, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("registered_commands", {}), dict):
        raise ValueError("existing Git extension registry is malformed")
    current = entry.get("registered_commands", {}).get("codex", [])
    if not isinstance(current, list) or any(not isinstance(name, str) for name in current):
        raise ValueError("existing Codex extension registration is malformed")
    return {"extension": "git", "target": "codex", "policy": "install missing skills; preserve existing files",
            "command": ["specify", "extension", "add", "--dev", str(extension)]}


def render_git_extension(repo, plan):
    """Use the pinned CLI's native renderer without reinstalling user hooks."""
    if plan is None:
        return [], []
    with tempfile.TemporaryDirectory(prefix="bureau-speckit-extension-") as temporary:
        stage = Path(temporary)
        (stage / ".specify").mkdir()
        (stage / ".agents/skills").mkdir(parents=True)
        subprocess.run(plan["command"], cwd=stage, check=True)
        registry = json.loads((stage / ".specify/extensions/.registry").read_text())
        try:
            names = registry["extensions"]["git"]["registered_commands"]["codex"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Spec Kit did not register native Codex Git commands") from exc
        if not isinstance(names, list) or not names or any(
                not isinstance(name, str) or not re.fullmatch(r"speckit\.git\.[a-z][a-z0-9_-]*", name) for name in names) or len(set(names)) != len(names):
            raise ValueError("Spec Kit returned invalid Codex Git command names")
        writes = []
        for name in names:
            relative = ".agents/skills/" + name.replace(".", "-") + "/SKILL.md"
            source = destination(stage, relative)
            target = destination(repo, relative)
            for ancestor in target.parents:
                if ancestor == repo:
                    break
                if ancestor.exists() and not ancestor.is_dir():
                    raise ValueError("native extension skill parent is not a directory: " + relative)
            if not source.is_file() or (target.exists() and not target.is_file()):
                raise ValueError("invalid native extension skill: " + relative)
            # Even a partial older registration can contain project customizations.
            if not target.exists():
                writes.append((target, source.read_bytes()))
        return names, writes


def publish_git_extension(repo, names, writes):
    if not names:
        return
    path = destination(repo, ".specify/extensions/.registry")
    registry = json.loads(path.read_text())
    entry = registry["extensions"]["git"]
    commands = entry.setdefault("registered_commands", {})
    combined = list(dict.fromkeys(commands.get("codex", []) + names))
    # Validate all paths before writing; publish registration only after files exist.
    for target, _ in writes:
        destination(repo, str(target.relative_to(repo)))
        for ancestor in target.parents:
            if ancestor == repo:
                break
            if ancestor.exists() and not ancestor.is_dir():
                raise ValueError("native extension skill parent is not a directory: " + str(target))
        if target.exists() and not target.is_file():
            raise ValueError("invalid native extension skill: " + str(target))
    for target, data in writes:
        if not target.exists():
            atomic_write(target, data)
    if commands.get("codex") != combined:
        commands["codex"] = combined
        atomic_write(path, (json.dumps(registry, indent=2) + "\n").encode())


def speckit(repo, args, manifest, targets):
    active_file = destination(repo, ".specify/integration.json")
    old_active = json.loads(active_file.read_text()).get("integration") if active_file.exists() else None
    # Spec Kit installations can predate Bureau's asset manifest.
    discovered = {old_active} if old_active in ("claude", "codex") else set()
    for host, directory in (("claude", ".claude/skills"), ("codex", ".agents/skills")):
        if any(destination(repo, directory).glob("speckit-*/SKILL.md")):
            discovered.add(host)
    installed = sorted(set(manifest.get("targets", [])) | set(targets) | discovered)
    if old_active and old_active not in installed and not args.active_integration:
        raise ValueError("existing active Spec Kit integration is unsupported; choose --active-integration explicitly")
    active = args.active_integration or (old_active if old_active in installed else None)
    if active is None and len(installed) == 1:
        active = installed[0]
    if active not in installed:
        raise ValueError("choose --active-integration claude|codex for a new dual installation")
    order = [t for t in installed if t != active] + [active]
    commands = [["specify", "init", "--here", "--integration", t, "--force", "--no-git", "--ignore-agent-tools"] for t in order]
    preserved = speckit_customizations(repo)
    extension = git_extension_plan(repo, installed)
    print(json.dumps({"version": SPECKIT_VERSION, "active_integration": active, "commands": commands,
                      "extension_registration": [extension] if extension else [],
                      "preserved": [str(p.relative_to(repo)) for p in preserved]}, indent=2))
    if not args.apply:
        return 0
    version = subprocess.run(["specify", "version"], capture_output=True, text=True, check=True)
    if not re.search(r"CLI Version\s+" + re.escape(SPECKIT_VERSION) + r"\b", version.stdout):
        raise ValueError(f"specify-cli {SPECKIT_VERSION} required; install the pinned version before retrying")
    # Spec Kit writes whole subtrees. Refuse redirected trees before executing it.
    for relative in (".specify", ".agents", ".claude"):
        directory = destination(repo, relative)
        if directory.exists() and any(p.is_symlink() for p in directory.rglob("*")):
            raise ValueError("Spec Kit target contains symlinks: " + relative)
    for relative in ("AGENTS.md", "CLAUDE.md"):
        destination(repo, relative)
    extension_names, extension_writes = render_git_extension(repo, extension)
    metadata = [active_file, destination(repo, ".specify/init-options.json")]
    snapshots = {p: p.read_bytes() if p.exists() else None for p in metadata}
    succeeded = False
    try:
        for command in commands:
            subprocess.run(command, cwd=repo, check=True)
        if json.loads(active_file.read_text()).get("integration") != active:
            raise ValueError("Spec Kit did not select the requested active integration")
        publish_git_extension(repo, extension_names, extension_writes)
        succeeded = True
    finally:
        for path, data in preserved.items():
            atomic_write(path, data)
        if not succeeded:
            for path, data in snapshots.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data)
            print("Spec Kit failed; existing constitution and active-integration metadata restored. Inspect any partially installed assets before retrying.", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["assets", "speckit", "check", "doctor", "migrate"])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--target", choices=["claude", "codex", "both"])
    parser.add_argument("--scope", action="append", choices=["interfaces", "scripts", "workflows", "ci"])
    parser.add_argument("--active-integration", choices=["claude", "codex"])
    parser.add_argument("--mode", choices=["interactive", "background"], default="interactive")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--overwrite", action="append", default=[], metavar="RELATIVE_PATH")
    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve(strict=True)
        top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() != repo:
            raise ValueError("--repo must be the repository or worktree root")
        if args.action in ("doctor", "migrate"):
            command = [sys.executable, str(ROOT / "templates/scripts/bureau-doctor.py"), "--repo", str(repo),
                       "--mode", "app" if args.mode == "interactive" else "background"]
            if args.action == "migrate": command += ["--migrate"] + (["--apply"] if args.apply else [])
            return subprocess.call(command)
        manifest = read_manifest(repo)
        targets = targets_for(args, manifest)
        if args.action == "check":
            required = ["git", "python3", "jq", "curl"]
            if args.mode == "background":
                required += targets + ["tmux"]
            missing = [tool for tool in required if not shutil.which(tool)]
            print(json.dumps({"targets": targets, "mode": args.mode, "missing": missing}))
            return 1 if missing else 0
        if args.action == "speckit":
            return speckit(repo, args, manifest, targets)
        return assets(repo, args, manifest, targets)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("bureau-install: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
