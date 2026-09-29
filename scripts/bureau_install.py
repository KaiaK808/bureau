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
# Inherited from a hook or wrapper, these would point every git call at some other repository.
GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
BEGIN = "<!-- bureau-init:begin -->"
END = "<!-- bureau-init:end -->"
# Older installers wrote `<!-- bureau-init managed -->`, variants such as
# `<!-- bureau-init managed: regenerate via ... -->`, and the closer `<!-- end bureau-init managed -->`.
LEGACY_MARKER = re.compile(r"<!--\s*(?:end\s+)?bureau-init\s+managed\b[^\n]*", re.IGNORECASE)


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
    sources = data.get("sources", {})
    if not isinstance(sources, dict) or any(not isinstance(v, dict) for v in sources.values()):
        raise ValueError("malformed installation sources")
    return data


def git_env():
    return {k: v for k, v in os.environ.items() if k not in GIT_REDIRECTS}


def source_revision(inputs):
    """Describe the template revision this installer runs from (ROOT, symlinks resolved).

    `inputs` are the source files this batch reads. The source is dirty when the checkout reports
    changes or when any input is not byte-identical to its blob at HEAD, which also catches
    gitignored files that the installer copies; anything unreadable counts as dirty."""
    def git(*args):
        # --no-optional-locks: a preview must not refresh the source checkout's index.
        proc = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), *args], capture_output=True, env=git_env())
        return proc.stdout if proc.returncode == 0 else None
    def text(*args):
        out = git(*args)
        return out.decode().strip() if out is not None else None
    top = text("rev-parse", "--show-toplevel")
    # A copy that sits inside some other repository must not borrow that repository's commit.
    if top is None or Path(top).resolve() != ROOT:
        return {"git": False, "note": "not a git checkout"}
    commit = text("rev-parse", "--verify", "HEAD")
    if commit is None:
        return {"git": False, "note": "git checkout without a commit"}
    status = git("status", "--porcelain", "--untracked-files=normal")
    tree = git("ls-tree", "-r", "-z", "HEAD")
    blobs = {}
    for entry in (tree or b"").split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        if path:
            blobs[path] = meta.split()[2].decode()
    algorithm = hashlib.sha256 if len(commit) == 64 else hashlib.sha1
    def at_head(path):
        data = path.read_bytes()
        return blobs.get(os.fsencode(path.relative_to(ROOT))) == algorithm(b"blob %d\0" % len(data) + data).hexdigest()
    return {"git": True, "tag": text("describe", "--tags", "--exact-match", "HEAD"),
            "describe": text("describe", "--tags", "--always", "HEAD"), "commit": commit,
            # An unreadable tree leaves `blobs` empty, so every input then counts as changed.
            "dirty": status != b"" or not all(at_head(path) for path in sorted(inputs))}


def files_digest(files):
    """Binds the source record to the file hashes it describes; the doctor recomputes it."""
    return digest(json.dumps(files, sort_keys=True).encode())


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


def legacy_section(text, block=None):
    """The first legacy Bureau marker outside the delimited block (`block` is its (start, end)
    span, or None): a file that still carries an older generated section must not get a
    second Bureau block next to it. Returns a description for the refusal, or None."""
    for match in LEGACY_MARKER.finditer(text):
        if block and block[0] <= match.start() < block[1]:
            continue
        marker = match.group(0).strip()
        return "line %d (%s)" % (text.count("\n", 0, match.start()) + 1, marker if len(marker) <= 80 else marker[:77] + "...")
    return None


def merge_instructions(old, block):
    text = old.decode()
    if BEGIN not in text and END not in text:
        legacy = legacy_section(text)
        if legacy:
            raise ValueError("legacy Bureau section at " + legacy + ": review it and delimit it with "
                             + BEGIN + " and " + END + " in place of its old markers before resync")
        return old + (b"\n\n" if old and not old.endswith(b"\n\n") else b"") + block, None
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise ValueError("malformed Bureau instruction markers")
    start, end = text.index(BEGIN), text.index(END) + len(END)
    legacy = legacy_section(text, (start, end))
    if legacy:
        raise ValueError("legacy Bureau section at " + legacy + " outside the bureau-init:begin/end block: "
                         "remove the old section, or move what is still needed into the block, before resync")
    if text[end:end + 1] == "\n":
        end += 1
    return (text[:start] + block.decode() + text[end:]).encode(), text[start:end].encode()


def source_ignores(paths):
    """The subset of `paths` (template files under ROOT) the source ignores; those are never
    installed. A source that is its own git checkout answers with git's ignore rules, where a
    tracked file never counts as ignored. Otherwise, or when that checkout cannot answer (an
    unreadable index), ROOT's .gitignore files are read through a scratch repository, and every
    dotfile counts as ignored as well, since nothing records which files the release carries."""
    if not paths:
        return set()
    names = b"".join(os.fsencode(p.relative_to(ROOT)) + b"\0" for p in paths)
    def answer(proc):
        return {ROOT / os.fsdecode(name) for name in proc.stdout.split(b"\0") if name}
    top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], capture_output=True, env=git_env())
    if top.returncode == 0 and Path(os.fsdecode(top.stdout.strip())).resolve() == ROOT:
        proc = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), "check-ignore", "-z", "--stdin"],
                              input=names, capture_output=True, env=git_env())
        if proc.returncode in (0, 1):
            return answer(proc)
    with tempfile.TemporaryDirectory(prefix="bureau-install-ignore-") as scratch:
        subprocess.run(["git", "init", "-q", scratch], capture_output=True, check=True, env=git_env())
        proc = subprocess.run(["git", "--git-dir=" + os.path.join(scratch, ".git"), "--work-tree=" + str(ROOT), "-C", str(ROOT),
                               "check-ignore", "--no-index", "-z", "--stdin"], input=names, capture_output=True, env=git_env())
    if proc.returncode not in (0, 1):
        raise ValueError("cannot read the template source's ignore rules: " + proc.stderr.decode(errors="replace").strip())
    return answer(proc) | {p for p in paths if any(part.startswith(".") for part in p.relative_to(ROOT).parts)}


def assets(repo, args, manifest, targets):
    candidates = []
    inputs = {Path(__file__).resolve(), ROOT / "templates/instructions/workflow.md"}
    scopes = set(args.scope or ["interfaces"])
    skipped = set()
    def template_files(directory, pattern):
        """Files of a template directory that the source does not ignore; the rest are skipped."""
        found = sorted(p for p in (ROOT / directory).glob(pattern) if p.is_file())
        ignored = source_ignores(found)
        skipped.update(str(p.relative_to(ROOT)) for p in ignored)
        return [p for p in found if p not in ignored]
    if "interfaces" in scopes:
        for target in targets:
            for source in template_files("templates/commands", "*.md"):
                inputs.add(source)
                relative = f".agents/skills/{source.stem}/SKILL.md" if target == "codex" else f".claude/commands/{source.name}"
                candidates.append((relative, render_command(source, target), False, False, "interfaces/" + target))
            candidates.append(("AGENTS.md" if target == "codex" else "CLAUDE.md", instruction_block(target), False, True, "interfaces/" + target))
            if target == "codex":
                for source in template_files("templates/skills", "**/*"):
                    inputs.add(source)
                    relative = ".agents/skills/" + str(source.relative_to(ROOT / "templates/skills"))
                    candidates.append((relative, source.read_bytes(), False, False, "interfaces/" + target))
    if "scripts" in scopes:
        for source in template_files("templates/scripts", "*"):
            inputs.add(source)
            candidates.append((f"scripts/{source.name}", source.read_bytes(), source.suffix in (".sh", ".py"), False, "scripts"))
    if "workflows" in scopes and "claude" in targets:
        for source in template_files("templates/workflows", "*.js"):
            inputs.update((source, ROOT / "templates/scripts/bureau-schedule.mjs"))
            workflow = source.read_text()
            if "/* BUREAU_SCHEDULER_CORE */" in workflow:
                core = (ROOT / "templates/scripts/bureau-schedule.mjs").read_text().replace("export function", "function")
                workflow = workflow.replace("/* BUREAU_SCHEDULER_CORE */", core)
            candidates.append((f".claude/workflows/{source.name}", workflow.encode(), False, False, "workflows"))
    if "ci" in scopes:
        inputs.add(ROOT / "templates/.github/workflows/ci.yml")
        candidates.append((".github/workflows/ci.yml", (ROOT / "templates/.github/workflows/ci.yml").read_bytes(), False, False, "ci"))

    unknown = set(args.overwrite) - {item[0] for item in candidates}
    if unknown:
        raise ValueError("--overwrite is outside the selected scope: " + ", ".join(sorted(unknown)))
    records = dict(manifest.get("files", {}))
    plan, writes = [], []
    for relative, incoming, executable, managed, _ in candidates:
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

    source = source_revision(inputs)
    print(json.dumps({"targets": targets, "source": source, "files": plan, "skipped": sorted(skipped)}, indent=2))
    if skipped:
        print("bureau-install: skipped template files the source ignores (never installed): " + ", ".join(sorted(skipped)), file=sys.stderr)
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
    # Keyed by scope (and host for interfaces): a later partial apply must not relabel files it did not write.
    # A record whose digest no longer matches was carried along by a writer that changed hashes without
    # recording its source (an older installer after a rollback); none of it is trustworthy any more.
    carried = manifest.get("sources", {})
    if manifest.get("sources_files_sha256") != files_digest(manifest.get("files", {})):
        carried = {}
    sources = dict(carried)
    for key in {item[4] for item in candidates}:
        sources[key] = source
    updated.update(sources=dict(sorted(sources.items())), sources_files_sha256=files_digest(records))
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
        top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True, env=git_env()).stdout.strip()
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
