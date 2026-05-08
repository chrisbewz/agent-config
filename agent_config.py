import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

VERSION = "0.2.1"


def normalize_filter_value(value: str) -> str:
    return value.strip().lower()


def get_entry_category(entry: dict) -> str:
    """Return the category for a manifest entry.

    Prefers an explicit ``category`` field in the entry.  Falls back to
    inferring the category from the ``source`` path prefix so that existing
    manifests without the field continue to work unchanged.
    """
    explicit = entry.get("category", "").strip().lower()
    if explicit in {"memory", "skills", "config"}:
        return explicit

    # Legacy path-prefix inference
    source = entry.get("source", "")
    normalized = source.replace("\\", "/")
    if normalized.startswith("memory/"):
        return "memory"
    if normalized.startswith("skills/"):
        return "skills"
    return "config"


def expand_destination(destination: str) -> str:
    # On non-Windows, %USERPROFILE% is not a defined env var — map it to the home dir.
    if os.name != "nt" and "%USERPROFILE%" in destination:
        destination = destination.replace("%USERPROFILE%", str(Path.home()))
    expanded = os.path.expandvars(destination)
    # Also expand ~ for cross-platform home-directory support.
    return str(Path(expanded).expanduser())


def add_content(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(message + "\n")


def _pre_parse_config_dir() -> Path:
    """Extract --config-dir / -C from sys.argv before full argparse.

    Falls back to the ``AGENT_CONFIG_DIR`` environment variable, then cwd.
    Used to compute correct defaults for other arguments before parse_args() runs.
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("--config-dir", "-C") and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
        if arg.startswith("--config-dir="):
            return Path(arg.split("=", 1)[1]).resolve()
    env_dir = os.environ.get("AGENT_CONFIG_DIR", "")
    return Path(env_dir).resolve() if env_dir else Path.cwd().resolve()


def copy_directory(source: Path, destination: Path, exclude: Optional[set] = None) -> int:
    item_count = 0
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if exclude and item.name in exclude:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
            item_count += sum(1 for _ in item.rglob("*"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            item_count += 1
    return item_count


def run_skills_cli_entry(
    entry: dict,
    agents_override: Optional[List[str]],
    dry_run: bool,
    log_path: Path,
    manifest_dir: Path,
) -> bool:
    """Install a skill repository via ``npx skills add``.

    Returns True on success, False on failure.
    Aborts with a clear error if ``npx`` is not found on PATH.
    """
    # Resolve agents: CLI override takes precedence over manifest field
    agents = agents_override if agents_override else entry.get("agents", [])
    if not agents:
        message = f"x skills-cli entry '{entry.get('source')}' has no 'agents' field and no -SkillsAgents override -- skipping"
        add_content(log_path, message)
        print(message)
        return False

    source_path = (manifest_dir / entry["source"]).resolve()
    if not source_path.exists():
        message = f"x Source not found -- skipping: {entry['source']}"
        add_content(log_path, message)
        print(message)
        return False

    # On Windows, npx is a .cmd batch file and cannot be invoked directly by
    # subprocess without shell=True.  shutil.which() respects PATHEXT and
    # returns the full resolved path (e.g. npx.cmd), making the subprocess call
    # work regardless of how the process was started (mise, nvm, system Node…).
    npx_exe = shutil.which("npx")
    if npx_exe is None:
        message = (
            "x 'npx' not found on PATH -- cannot install skills via skills CLI.\n"
            "  Install Node.js (https://nodejs.org) and ensure 'npx' is available, then re-run deploy."
        )
        add_content(log_path, message)
        print(message)
        raise SystemExit(1)

    cmd = [npx_exe, "skills", "add", str(source_path), "--all", "-y"]
    if entry.get("global", True):
        cmd.append("--global")
    for agent in agents:
        cmd += ["-a", agent]

    print(f"[skills-cli] {entry['source']}  ->  agents: {', '.join(agents)}")
    add_content(log_path, f"[skills-cli] {entry['source']}  ->  agents: {', '.join(agents)}")

    if dry_run:
        message = f"~ DRY RUN: would run: {' '.join(cmd)}"
        add_content(log_path, message)
        print(message)
        return True

    try:
        # shell=True is required on Windows when the resolved executable is a
        # .cmd/.bat file; it is a no-op on POSIX so this is safe cross-platform.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=os.name == "nt",
            encoding="utf-8",
            errors="replace",
        )
        if result.stdout:
            print(result.stdout)
            add_content(log_path, result.stdout.strip())
        if result.stderr:
            print(result.stderr)
            add_content(log_path, result.stderr.strip())
        if result.returncode != 0:
            message = f"x skills-cli failed with return code {result.returncode}"
            add_content(log_path, message)
            print(message)
            return False
        message = "v Installed"
        add_content(log_path, message)
        print(message)
        return True
    except FileNotFoundError:
        message = (
            "x 'npx' not found on PATH -- cannot install skills via skills CLI.\n"
            "  Install Node.js (https://nodejs.org) and ensure 'npx' is available, then re-run deploy."
        )
        add_content(log_path, message)
        print(message)
        raise SystemExit(1)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def git_add(paths: List[Path], log_path: Path, cwd: Path) -> None:
    """Stage the given paths with git add. Silently skips if not in a git repo."""
    str_paths = [str(p) for p in paths]
    try:
        result = subprocess.run(
            ["git", "add", "--"] + str_paths,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            message = f"~ Staged {len(str_paths)} path(s) with git add"
        else:
            message = f"~ git add skipped: {result.stderr.strip()}"
        add_content(log_path, message)
        print(message)
    except FileNotFoundError:
        message = "~ git not found -- skipping git add"
        add_content(log_path, message)
        print(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy or pull agent configuration files between the repo and their target destinations."
    )
    parser.add_argument(
        "--config-dir", "-C",
        default=os.environ.get("AGENT_CONFIG_DIR", str(Path.cwd())),
        metavar="DIR",
        help="Root directory of your agent-config repository (default: cwd or $AGENT_CONFIG_DIR).",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"agent-config {VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── deploy (default behaviour, mirrors the original main) ──────────────
    _cfg = _pre_parse_config_dir()
    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Copy files from the repo to their configured destinations (default).",
    )
    deploy_parser.add_argument("--manifest-path", default=str(_cfg / "manifest.json"))
    deploy_parser.add_argument("--log-path", default=str(_cfg / "deploy.log"))
    deploy_parser.add_argument("--only", nargs="*", help="Deploy only the specified categories: memory, skills, config.")
    deploy_parser.add_argument("--memory", action="store_true", help="Deploy only memory entries.")
    deploy_parser.add_argument("--skills", action="store_true", help="Deploy only skills entries.")
    deploy_parser.add_argument("--config", action="store_true", help="Deploy only config entries.")
    deploy_parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without making changes.")
    deploy_parser.add_argument("--skills-agents", nargs="*", help="Override the target agents for skills-cli entries (e.g. github-copilot claude-code).")

    # ── pull ───────────────────────────────────────────────────────────────
    _deployed_manifest = Path(expand_destination("%USERPROFILE%")) / ".agents" / "manifest.json"
    pull_parser = subparsers.add_parser(
        "pull",
        help="Copy memory files from their deployed locations back into the repo and stage them with git add.",
    )
    pull_parser.add_argument("--manifest-path", default=str(_deployed_manifest))
    pull_parser.add_argument("--log-path", default=str(_cfg / "deploy.log"))
    pull_parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without making changes.")

    # ── legacy: no subcommand → behaves as deploy ──────────────────────────
    parser.add_argument("--manifest-path", default=str(_cfg / "manifest.json"))
    parser.add_argument("--log-path", default=str(_cfg / "deploy.log"))
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--skills", action="store_true")
    parser.add_argument("--config", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skills-agents", nargs="*")

    sync_parser = subparsers.add_parser(
        "sync",
        help="Run agents sync to propagate configuration across all registered LLM providers.",
    )
    sync_parser.add_argument("--manifest-path", default=str(_deployed_manifest))
    sync_parser.add_argument("--log-path", default=str(_cfg / "deploy.log"))

    # ── mcp ────────────────────────────────────────────────────────────────
    _default_agents_json = str(_cfg / "agents.json")
    _default_mcp_providers = str(_cfg / "mcp_providers.json")

    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Deploy, sync, or inspect MCP server configurations across providers independently.",
    )
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_subcommand")

    mcp_deploy_p = mcp_sub.add_parser(
        "deploy",
        help="Write MCP server configs to all registered provider config files.",
    )
    mcp_deploy_p.add_argument("--agents-json", default=_default_agents_json)
    mcp_deploy_p.add_argument("--providers-json", default=_default_mcp_providers)
    mcp_deploy_p.add_argument(
        "--providers", nargs="*",
        help="Limit to specific provider IDs (e.g. vscode copilot-cli claude).",
    )
    mcp_deploy_p.add_argument("--dry-run", action="store_true")
    mcp_deploy_p.add_argument(
        "--env-file", default=None,
        help="Path to a .env file whose KEY=VALUE pairs substitute {KEY} placeholders in server env values.",
    )

    mcp_sync_p = mcp_sub.add_parser(
        "sync",
        help="Additive sync: write only servers missing from each provider's on-disk config.",
    )
    mcp_sync_p.add_argument("--agents-json", default=_default_agents_json)
    mcp_sync_p.add_argument("--providers-json", default=_default_mcp_providers)
    mcp_sync_p.add_argument("--providers", nargs="*")
    mcp_sync_p.add_argument("--dry-run", action="store_true")
    mcp_sync_p.add_argument("--env-file", default=None)

    mcp_show_p = mcp_sub.add_parser(
        "show",
        help="Display MCP configuration status per provider (no writes).",
    )
    mcp_show_p.add_argument("--agents-json", default=_default_agents_json)
    mcp_show_p.add_argument("--providers-json", default=_default_mcp_providers)
    mcp_show_p.add_argument("--providers", nargs="*")

    # ── instructions ───────────────────────────────────────────────────────
    _default_instr_providers = str(_cfg / "instruction_providers.json")

    instr_parser = subparsers.add_parser(
        "instructions",
        help="Deploy or inspect per-tool instruction files (AGENTS.md, CLAUDE.md, .cursorrules, …).",
    )
    instr_sub = instr_parser.add_subparsers(dest="instr_subcommand")

    instr_deploy_p = instr_sub.add_parser(
        "deploy",
        help="Copy instruction source files to each provider's expected destination path.",
    )
    instr_deploy_p.add_argument("--providers-json", default=_default_instr_providers)
    instr_deploy_p.add_argument("--providers", nargs="*")
    instr_deploy_p.add_argument("--dry-run", action="store_true")

    instr_show_p = instr_sub.add_parser(
        "show",
        help="Display instruction file status per provider (no writes).",
    )
    instr_show_p.add_argument("--providers-json", default=_default_instr_providers)
    instr_show_p.add_argument("--providers", nargs="*")

    # ── providers ──────────────────────────────────────────────────────────
    prov_parser = subparsers.add_parser(
        "providers",
        help="Manage the MCP provider registry (mcp_providers.json).",
    )
    prov_sub = prov_parser.add_subparsers(dest="providers_subcommand")

    prov_list_p = prov_sub.add_parser("list", help="List all registered MCP providers.")
    prov_list_p.add_argument("--providers-json", default=str(_cfg / "mcp_providers.json"))

    prov_add_p = prov_sub.add_parser("add", help="Add a new MCP provider to the registry.")
    prov_add_p.add_argument("--providers-json", default=str(_cfg / "mcp_providers.json"))
    prov_add_p.add_argument("--id", required=True, dest="provider_id", help="Provider identifier (e.g. my-tool).")
    prov_add_p.add_argument("--label", required=True)
    prov_add_p.add_argument("--config-path", required=True)
    prov_add_p.add_argument("--format", default="vscode", choices=["vscode", "claude", "opencode", "junie"])
    prov_add_p.add_argument("--aliases", nargs="*", default=[])

    prov_remove_p = prov_sub.add_parser("remove", help="Remove a provider from the registry.")
    prov_remove_p.add_argument("--providers-json", default=str(_cfg / "mcp_providers.json"))
    prov_remove_p.add_argument("provider_id", help="Provider ID to remove.")

    prov_set_path_p = prov_sub.add_parser("set-path", help="Update the config path for a provider.")
    prov_set_path_p.add_argument("--providers-json", default=str(_cfg / "mcp_providers.json"))
    prov_set_path_p.add_argument("provider_id", help="Provider ID to update.")
    prov_set_path_p.add_argument("config_path", help="New config file path (supports %%USERPROFILE%% etc.).")

    # ── skills ─────────────────────────────────────────────────────────────
    _default_skills_registry = str(_cfg / "skills_registry.json")

    skills_parser = subparsers.add_parser(
        "skills",
        help="Deploy, sync, or inspect skills across agents.",
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_subcommand")

    skills_deploy_p = skills_sub.add_parser(
        "deploy",
        help="Deploy all registered skills to their target agents.",
    )
    skills_deploy_p.add_argument("--registry-json", default=_default_skills_registry)
    skills_deploy_p.add_argument(
        "--agents", nargs="*",
        help="Limit to skills targeting specific agents (e.g. github-copilot).",
    )
    skills_deploy_p.add_argument("--dry-run", action="store_true")

    skills_show_p = skills_sub.add_parser(
        "show",
        help="Display skill deployment status (no writes).",
    )
    skills_show_p.add_argument("--registry-json", default=_default_skills_registry)
    skills_show_p.add_argument("--agents", nargs="*")

    skills_sync_p = skills_sub.add_parser(
        "sync",
        help="Additive sync: deploy only skills not already installed.",
    )
    skills_sync_p.add_argument("--registry-json", default=_default_skills_registry)
    skills_sync_p.add_argument("--agents", nargs="*")
    skills_sync_p.add_argument("--dry-run", action="store_true")

    # ── init ───────────────────────────────────────────────────────────────
    init_parser = subparsers.add_parser(
        "init",
        help="Scaffold a new agent-config repository with starter template files.",
    )
    init_parser.add_argument(
        "--dir", default=str(Path.cwd()),
        metavar="DIR",
        help="Target directory to initialise (default: current directory).",
    )
    init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing files instead of skipping them.",
    )

    return parser.parse_args()


def resolve_filters(args: argparse.Namespace) -> List[str]:
    filters: List[str] = []
    if args.only:
        for value in args.only:
            filters.extend([normalize_filter_value(part) for part in value.split(",") if part.strip()])
    if args.memory:
        filters.append("memory")
    if args.skills:
        filters.append("skills")
    if args.config:
        filters.append("config")
    return sorted(set(filters))


def validate_manifest(manifest: dict) -> None:
    if "deployments" not in manifest or not manifest["deployments"]:
        raise ValueError("No deployments defined in manifest. Nothing to do.")


def load_manifest(manifest_path: Path) -> dict:
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ── deploy ─────────────────────────────────────────────────────────────────

def _write_deployed_manifest(source_manifest_path: Path, log_path: Path, dry_run: bool) -> None:
    """Write an enriched copy of the manifest to %USERPROFILE%\\.agents\\manifest.json.

    The copy adds a ``repo_root`` field so that ``agent-config pull`` can
    resolve relative source paths without knowing where the repo is cloned.
    """
    deployed_dir = Path(expand_destination("%USERPROFILE%")) / ".agents"
    deployed_path = deployed_dir / "manifest.json"

    try:
        with source_manifest_path.open("r", encoding="utf-8") as f:
            manifest_copy = json.load(f)

        manifest_copy["repo_root"] = str(source_manifest_path.resolve().parent)

        if not dry_run:
            deployed_dir.mkdir(parents=True, exist_ok=True)
            with deployed_path.open("w", encoding="utf-8") as f:
                json.dump(manifest_copy, f, indent=2, ensure_ascii=False)
            message = f"~ Deployed manifest written to {deployed_path}"
        else:
            message = f"~ DRY RUN: would write deployed manifest to {deployed_path}"

        add_content(log_path, message)
        print(message)
    except Exception as exc:
        message = f"~ Warning: could not write deployed manifest -- {exc}"
        add_content(log_path, message)
        print(message)


def run_deploy(args: argparse.Namespace) -> int:
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        manifest = load_manifest(Path(args.manifest_path))
        validate_manifest(manifest)
    except Exception as exc:
        print(f"x {exc}")
        return 1

    filter_categories = resolve_filters(args)
    if filter_categories:
        allowed = {"memory", "skills", "config"}
        invalid = [cat for cat in filter_categories if cat not in allowed]
        if invalid:
            print(f"x Invalid deployment category: {', '.join(invalid)}. Valid categories are memory, skills, config.")
            return 1

        manifest["deployments"] = [entry for entry in manifest["deployments"] if get_entry_category(entry) in filter_categories]
        if not manifest["deployments"]:
            print("~ No deployments match the selected filter. Nothing to do.")
            return 0

    separator = "----------------------------------------"
    header = [
        "",
        separator,
        f"{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}  Agent Config Deploy{' [DRY RUN]' if args.dry_run else ''}",
        separator,
        f"Manifest : {args.manifest_path}",
        f"Log      : {args.log_path}",
        f"Entries  : {len(manifest['deployments'])}",
    ]
    if filter_categories:
        header.append(f"Filters  : {', '.join(filter_categories)}")
    header.append("")
    add_content(log_path, "\n".join(header))
    print("\n".join(header))

    success = 0
    failed = 0

    manifest_dir = Path(args.manifest_path).resolve().parent

    for entry in manifest["deployments"]:
        # skills-cli entries are handled by npx skills add, not by file copy
        if entry.get("installer") == "skills-cli":
            skills_agents_override = getattr(args, "skills_agents", None)
            ok = run_skills_cli_entry(entry, skills_agents_override, args.dry_run, log_path, manifest_dir)
            if ok:
                success += 1
            else:
                failed += 1
            continue

        source_path = manifest_dir / entry["source"]
        destination_path = Path(expand_destination(entry["destination"]))
        entry_type = entry.get("type", "file").lower()

        if not source_path.exists():
            message = f"x Source not found -- skipping: {entry['source']}"
            add_content(log_path, message)
            print(message)
            failed += 1
            continue

        print(f"[{entry_type}] {entry['source']}  ->  {destination_path}")
        add_content(log_path, f"[{entry_type}] {entry['source']}  ->  {destination_path}")

        if args.dry_run:
            message = "~ DRY RUN: would copy"
            add_content(log_path, message)
            print(message)
            continue

        try:
            if entry_type == "directory":
                if not source_path.is_dir():
                    raise ValueError(f"Source is not a directory: {entry['source']}")
                exclude_set = set(entry["exclude"]) if entry.get("exclude") else None
                count = copy_directory(source_path, destination_path, exclude=exclude_set)
                message = f"v Copied ({count} file(s))"
            elif entry_type == "file":
                if not source_path.is_file():
                    raise ValueError(f"Source is not a file: {entry['source']}")
                copy_file(source_path, destination_path)
                message = "v Copied"
            else:
                raise ValueError(f"Unknown type '{entry_type}' -- skipping: {entry['source']}")

            add_content(log_path, message)
            print(message)
            success += 1
        except Exception as exc:
            message = f"x Failed: {exc}"
            add_content(log_path, message)
            print(message)
            failed += 1

    if args.dry_run:
        summary = "Dry run complete. No files were modified."
        add_content(log_path, summary)
        print(summary)
        return 0

    # Always write the deployed manifest so that `agent-config pull` can resolve
    # repo_root even when individual entries failed. Failed entries are already
    # logged individually above.
    _write_deployed_manifest(Path(args.manifest_path), log_path, args.dry_run)

    if failed == 0:
        summary = f"Deploy complete. {success} entrie(s) deployed."
        add_content(log_path, summary)
        print(summary)
        return 0

    summary = f"Deploy finished with errors. Success: {success}  Failed: {failed}"
    add_content(log_path, summary)
    print(summary)
    return 1


# ── pull ───────────────────────────────────────────────────────────────────

def run_pull(args: argparse.Namespace) -> int:
    """Copy memory files from their deployed locations back into the repo,
    then stage the changed files with git add.

    Reads the deployed manifest from ``%USERPROFILE%\\.agents\\manifest.json`` by
    default. That manifest contains a ``repo_root`` field injected by deploy,
    which is used to resolve relative source paths without requiring the agent
    to know where the repo is cloned.
    """
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        manifest = load_manifest(Path(args.manifest_path))
        validate_manifest(manifest)
    except Exception as exc:
        print(f"x {exc}")
        return 1

    repo_root_str = manifest.get("repo_root")
    if not repo_root_str:
        print("x 'repo_root' not found in manifest. Run 'agent-config deploy' first to generate the deployed manifest.")
        return 1

    repo_root = Path(repo_root_str)
    if not repo_root.exists():
        print(f"x repo_root does not exist: {repo_root}")
        return 1

    # Pull operates exclusively on memory entries
    memory_entries = [
        entry for entry in manifest["deployments"]
        if get_entry_category(entry) == "memory"
    ]

    if not memory_entries:
        print("~ No memory entries found in manifest. Nothing to pull.")
        return 0

    separator = "----------------------------------------"
    header = [
        "",
        separator,
        f"{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}  Agent Config Pull{' [DRY RUN]' if args.dry_run else ''}",
        separator,
        f"Manifest : {args.manifest_path}",
        f"Repo     : {repo_root}",
        f"Log      : {args.log_path}",
        f"Entries  : {len(memory_entries)}",
        "",
    ]
    add_content(log_path, "\n".join(header))
    print("\n".join(header))

    success = 0
    failed = 0
    repo_paths: List[Path] = []

    for entry in memory_entries:
        # Inverted: deployed location is the source, repo path is the destination
        deployed_path = Path(expand_destination(entry["destination"]))
        repo_path = repo_root / entry["source"]
        entry_type = entry.get("type", "file").lower()

        if not deployed_path.exists():
            message = f"x Deployed source not found -- skipping: {deployed_path}"
            add_content(log_path, message)
            print(message)
            failed += 1
            continue

        print(f"[{entry_type}] {deployed_path}  ->  {repo_path}")
        add_content(log_path, f"[{entry_type}] {deployed_path}  ->  {repo_path}")

        if args.dry_run:
            message = "~ DRY RUN: would copy"
            add_content(log_path, message)
            print(message)
            continue

        try:
            if entry_type == "directory":
                if not deployed_path.is_dir():
                    raise ValueError(f"Deployed source is not a directory: {deployed_path}")
                count = copy_directory(deployed_path, repo_path)
                message = f"v Copied ({count} file(s))"
            elif entry_type == "file":
                if not deployed_path.is_file():
                    raise ValueError(f"Deployed source is not a file: {deployed_path}")
                copy_file(deployed_path, repo_path)
                message = "v Copied"
            else:
                raise ValueError(f"Unknown type '{entry_type}' -- skipping: {entry['source']}")

            add_content(log_path, message)
            print(message)
            repo_paths.append(repo_path)
            success += 1
        except Exception as exc:
            message = f"x Failed: {exc}"
            add_content(log_path, message)
            print(message)
            failed += 1

    if args.dry_run:
        summary = "Dry run complete. No files were modified."
        add_content(log_path, summary)
        print(summary)
        return 0

    if repo_paths:
        git_add(repo_paths, log_path, repo_root)

    if failed == 0:
        summary = f"Pull complete. {success} entrie(s) pulled. Review staged changes before committing."
        add_content(log_path, summary)
        print(summary)
        return 0

    summary = f"Pull finished with errors. Success: {success}  Failed: {failed}"
    add_content(log_path, summary)
    print(summary)
    return 1

# ── sync ────────────────────────────────────────────────────────────

def run_sync(args: argparse.Namespace) -> int:

    print("calling agents CLI to sync configurations across llm providers")

    # https://github.com/amtiYo/agents
    result = subprocess.run(
        ["powershell.exe", "-c", "& (Get-Command agents -ErrorAction Stop).Source sync --verbose"],
        cwd=Path.home(),
        capture_output=True,
        text=True)

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        print(f"Sync command failed with return code {result.returncode}")
        return result.returncode
    return 0

# ── mcp helpers ────────────────────────────────────────────────────────────

def _load_mcp_sources(agents_json_path: Path, providers_json_path: Path):
    """Return (servers_dict, providers_dict) or raise on error."""
    with agents_json_path.open("r", encoding="utf-8") as f:
        agents = json.load(f)
    servers: dict = agents.get("mcp", {}).get("servers", {})

    with providers_json_path.open("r", encoding="utf-8") as f:
        registry = json.load(f)
    providers: dict = registry.get("providers", {})

    return servers, providers


def _provider_matches(provider_id: str, provider_def: dict, target: str) -> bool:
    """Return True if *target* (from agents.json targets list) maps to this provider."""
    if target == provider_id:
        return True
    aliases = provider_def.get("aliases", [])
    return target in aliases


def _servers_for_provider(servers: dict, provider_id: str, provider_def: dict) -> dict:
    """Filter servers whose targets include this provider (by ID or alias)."""
    result = {}
    for name, srv in servers.items():
        if not srv.get("enabled", True):
            continue
        targets = srv.get("targets", [])
        if any(_provider_matches(provider_id, provider_def, t) for t in targets):
            result[name] = srv
    return result


def _build_vscode_mcp(servers: dict) -> dict:
    """Build VS Code / Copilot VSCode MCP JSON structure."""
    out = {}
    for name, srv in servers.items():
        entry: dict = {"type": "stdio", "command": srv["command"], "args": srv.get("args", [])}
        if srv.get("env"):
            entry["env"] = srv["env"]
        out[name] = entry
    return {"servers": out}


def _build_claude_mcp(servers: dict) -> dict:
    """Build Claude Desktop mcpServers JSON structure (to be merged into existing file)."""
    out = {}
    for name, srv in servers.items():
        entry: dict = {"command": srv["command"], "args": srv.get("args", [])}
        env = srv.get("env", {})
        entry["env"] = env  # Claude always wants env key (may be empty)
        out[name] = entry
    return {"mcpServers": out}


def _build_opencode_mcp(servers: dict) -> dict:
    """Build OpenCode MCP JSON structure."""
    out = {}
    for name, srv in servers.items():
        cmd_list = [srv["command"]] + srv.get("args", [])
        entry: dict = {"type": "local", "enabled": True, "command": cmd_list}
        if srv.get("env"):
            entry["environment"] = srv["env"]
        out[name] = entry
    return {"mcp": out}


def _build_junie_mcp(servers: dict) -> dict:
    """Build JetBrains Junie MCP JSON structure."""
    out = {}
    for name, srv in servers.items():
        entry: dict = {"command": srv["command"], "args": srv.get("args", [])}
        if srv.get("env"):
            entry["env"] = srv["env"]
        out[name] = entry
    return {"mcpServers": out}


_FORMAT_BUILDERS = {
    "vscode": _build_vscode_mcp,
    "claude": _build_claude_mcp,
    "opencode": _build_opencode_mcp,
    "junie": _build_junie_mcp,
}


def _merge_mcp_into_file(config_path: Path, new_block: dict, dry_run: bool) -> str:
    """Merge new_block into an existing JSON config file, preserving all other keys.

    Returns a short status string for logging.
    """
    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    merged = {**existing, **new_block}
    if dry_run:
        return "DRY RUN: would write"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return "written"


def _read_mcp_section(config_path: Path, fmt: str) -> dict:
    """Read the current MCP servers section from a provider config file on disk."""
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    if fmt in ("vscode",):
        return data.get("servers", {})
    if fmt in ("claude", "junie"):
        return data.get("mcpServers", {})
    if fmt == "opencode":
        return data.get("mcp", {})
    return {}


def _resolve_providers(all_providers: dict, filter_list) -> dict:
    """Return the subset of providers matching the filter list (or all if filter is empty)."""
    if not filter_list:
        return all_providers
    normalized = [p.strip().lower() for p in filter_list]
    return {k: v for k, v in all_providers.items() if k.lower() in normalized}


# ── mcp deploy ─────────────────────────────────────────────────────────────

def run_mcp_deploy(args: argparse.Namespace) -> int:
    try:
        servers, providers = _load_mcp_sources(Path(args.agents_json), Path(args.providers_json))
    except Exception as exc:
        print(f"x Failed to load config: {exc}")
        return 1

    # Apply env-file substitution if provided
    env_file = getattr(args, "env_file", None)
    if env_file:
        try:
            env_vars = _load_env_file(env_file)
            servers = _interpolate_servers(servers, env_vars)
            print(f"~ Loaded env vars from {env_file}: {', '.join(env_vars.keys())}")
        except Exception as exc:
            print(f"x Failed to load env file: {exc}")
            return 1

    target_providers = _resolve_providers(providers, args.providers)
    if not target_providers:
        print("~ No matching providers found. Nothing to do.")
        return 0

    dry_run: bool = args.dry_run
    label = " [DRY RUN]" if dry_run else ""
    print(f"\n{'─' * 48}")
    print(f"MCP Deploy{label}")
    print(f"{'─' * 48}")

    success = 0
    failed = 0

    for provider_id, provider_def in target_providers.items():
        label_str = provider_def.get("label", provider_id)
        config_path = Path(expand_destination(provider_def["config_path"]))
        fmt = provider_def.get("format", "vscode")
        builder = _FORMAT_BUILDERS.get(fmt)

        if builder is None:
            print(f"  [{provider_id}] x Unknown format '{fmt}' -- skipping")
            failed += 1
            continue

        matched = _servers_for_provider(servers, provider_id, provider_def)
        print(f"\n  [{provider_id}] {label_str}")
        print(f"  Config : {config_path}")
        print(f"  Servers: {', '.join(matched.keys()) if matched else '(none targeted)'}")

        if not matched:
            print("  ~ Skipped (no servers target this provider)")
            continue

        try:
            new_block = builder(matched)
            status = _merge_mcp_into_file(config_path, new_block, dry_run)
            print(f"  v {status}")
            success += 1
        except Exception as exc:
            print(f"  x Failed: {exc}")
            failed += 1

    print(f"\n{'─' * 48}")
    if failed == 0:
        print(f"MCP deploy complete. {success} provider(s) updated.")
        return 0
    print(f"MCP deploy finished with errors. Success: {success}  Failed: {failed}")
    return 1


# ── mcp sync ───────────────────────────────────────────────────────────────

def run_mcp_sync(args: argparse.Namespace) -> int:
    """Additive sync: write only servers missing from each provider's on-disk config."""
    try:
        servers, providers = _load_mcp_sources(Path(args.agents_json), Path(args.providers_json))
    except Exception as exc:
        print(f"x Failed to load config: {exc}")
        return 1

    env_file = getattr(args, "env_file", None)
    if env_file:
        try:
            env_vars = _load_env_file(env_file)
            servers = _interpolate_servers(servers, env_vars)
            print(f"~ Loaded env vars from {env_file}: {', '.join(env_vars.keys())}")
        except Exception as exc:
            print(f"x Failed to load env file: {exc}")
            return 1

    target_providers = _resolve_providers(providers, args.providers)
    if not target_providers:
        print("~ No matching providers found. Nothing to do.")
        return 0

    dry_run: bool = args.dry_run
    label = " [DRY RUN]" if dry_run else ""
    print(f"\n{'─' * 48}")
    print(f"MCP Sync{label}")
    print(f"{'─' * 48}")

    total_added = 0

    for provider_id, provider_def in target_providers.items():
        label_str = provider_def.get("label", provider_id)
        config_path = Path(expand_destination(provider_def["config_path"]))
        fmt = provider_def.get("format", "vscode")
        builder = _FORMAT_BUILDERS.get(fmt)

        if builder is None:
            print(f"\n  [{provider_id}] x Unknown format '{fmt}' -- skipping")
            continue

        expected = _servers_for_provider(servers, provider_id, provider_def)
        on_disk = _read_mcp_section(config_path, fmt)
        missing = {k: v for k, v in expected.items() if k not in on_disk}

        print(f"\n  [{provider_id}] {label_str}")
        print(f"  Config : {config_path}")

        if not missing:
            already = len(expected)
            print(f"  ~ All {already} server(s) already present. Nothing to add.")
            continue

        print(f"  Adding : {', '.join(missing.keys())}")

        if dry_run:
            print(f"  ~ DRY RUN: would add {len(missing)} server(s)")
            total_added += len(missing)
            continue

        try:
            new_block = builder({**on_disk, **missing})
            _merge_mcp_into_file(config_path, new_block, dry_run=False)
            print(f"  v Added {len(missing)} server(s)")
            total_added += len(missing)
        except Exception as exc:
            print(f"  x Failed: {exc}")

    print(f"\n{'─' * 48}")
    print(f"MCP sync complete. {total_added} server(s) added across all providers.")
    return 0


# ── mcp show ───────────────────────────────────────────────────────────────

def run_mcp_show(args: argparse.Namespace) -> int:
    """Display MCP configuration status per provider (read-only)."""
    try:
        servers, providers = _load_mcp_sources(Path(args.agents_json), Path(args.providers_json))
    except Exception as exc:
        print(f"x Failed to load config: {exc}")
        return 1

    target_providers = _resolve_providers(providers, args.providers)

    print(f"\n{'─' * 56}")
    print("MCP Configuration Status")
    print(f"{'─' * 56}")

    for provider_id, provider_def in target_providers.items():
        label_str = provider_def.get("label", provider_id)
        config_path = Path(expand_destination(provider_def["config_path"]))
        fmt = provider_def.get("format", "vscode")

        exists = config_path.exists()
        status_icon = "✓" if exists else "✗"
        print(f"\n  {status_icon} [{provider_id}] {label_str}")
        print(f"    Path   : {config_path}  {'(exists)' if exists else '(missing)'}")

        expected = _servers_for_provider(servers, provider_id, provider_def)
        if not expected:
            print("    Servers: (none targeted)")
            continue

        on_disk = _read_mcp_section(config_path, fmt) if exists else {}
        for name in expected:
            icon = "✓" if name in on_disk else "✗"
            print(f"    {icon}  {name}")

    print(f"\n{'─' * 56}")
    return 0


# ── env interpolation ──────────────────────────────────────────────────────

def _load_env_file(env_file_path: str) -> dict:
    """Parse a .env file and return a {KEY: VALUE} dict.

    Handles:
    - ``KEY=VALUE`` and ``KEY="VALUE"`` forms
    - Lines starting with ``#`` (comments)
    - Blank lines
    """
    env_vars: dict = {}
    path = Path(env_file_path)
    if not path.exists():
        raise FileNotFoundError(f".env file not found: {env_file_path}")
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env_vars[key] = value
    return env_vars


def _interpolate_servers(servers: dict, env_vars: dict) -> dict:
    """Return a copy of servers with ``{KEY}`` placeholders in env values substituted."""
    import copy
    result = copy.deepcopy(servers)
    for srv in result.values():
        srv_env = srv.get("env")
        if not isinstance(srv_env, dict):
            continue
        for k, v in srv_env.items():
            if isinstance(v, str):
                for var_name, var_value in env_vars.items():
                    v = v.replace(f"{{{var_name}}}", var_value)
                srv_env[k] = v
    return result


# ── instructions ───────────────────────────────────────────────────────────

def _load_instruction_providers(providers_json_path: Path) -> dict:
    with providers_json_path.open("r", encoding="utf-8") as f:
        return json.load(f).get("providers", {})


def run_instructions_deploy(args: argparse.Namespace) -> int:
    try:
        providers = _load_instruction_providers(Path(args.providers_json))
    except Exception as exc:
        print(f"x Failed to load instruction_providers.json: {exc}")
        return 1

    target = _resolve_providers(providers, args.providers)
    if not target:
        print("~ No matching providers found. Nothing to do.")
        return 0

    dry_run: bool = args.dry_run
    tag = " [DRY RUN]" if dry_run else ""
    print(f"\n{'─' * 48}")
    print(f"Instructions Deploy{tag}")
    print(f"{'─' * 48}")

    success = 0
    failed = 0

    for provider_id, pdef in target.items():
        label_str = pdef.get("label", provider_id)
        source_path = Path(args.config_dir) / pdef["source"]
        dest_path = Path(expand_destination(pdef["destination"]))

        print(f"\n  [{provider_id}] {label_str}")
        print(f"  Source : {source_path}")
        print(f"  Dest   : {dest_path}")

        if not source_path.exists():
            print(f"  x Source file not found -- skipping")
            failed += 1
            continue

        if dry_run:
            print("  ~ DRY RUN: would copy")
            success += 1
            continue

        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_path)
            print("  v Copied")
            success += 1
        except Exception as exc:
            print(f"  x Failed: {exc}")
            failed += 1

    print(f"\n{'─' * 48}")
    if failed == 0:
        print(f"Instructions deploy complete. {success} provider(s) updated.")
        return 0
    print(f"Instructions deploy finished with errors. Success: {success}  Failed: {failed}")
    return 1


def run_instructions_show(args: argparse.Namespace) -> int:
    try:
        providers = _load_instruction_providers(Path(args.providers_json))
    except Exception as exc:
        print(f"x Failed to load instruction_providers.json: {exc}")
        return 1

    target = _resolve_providers(providers, args.providers)

    print(f"\n{'─' * 56}")
    print("Instruction Files Status")
    print(f"{'─' * 56}")

    for provider_id, pdef in target.items():
        label_str = pdef.get("label", provider_id)
        source_path = Path(args.config_dir) / pdef["source"]
        dest_path = Path(expand_destination(pdef["destination"]))
        dest_exists = dest_path.exists()

        if dest_exists:
            try:
                src_text = source_path.read_text(encoding="utf-8") if source_path.exists() else None
                dst_text = dest_path.read_text(encoding="utf-8")
                if src_text is None:
                    file_status = "source missing"
                elif src_text == dst_text:
                    file_status = "up-to-date"
                else:
                    file_status = "outdated"
            except Exception:
                file_status = "unreadable"
        else:
            file_status = "missing"

        icon = "✓" if file_status == "up-to-date" else ("~" if file_status == "outdated" else "✗")
        print(f"\n  {icon} [{provider_id}] {label_str}  ({file_status})")
        print(f"    Source : {source_path}")
        print(f"    Dest   : {dest_path}")

    print(f"\n{'─' * 56}")
    return 0


# ── providers management ───────────────────────────────────────────────────

def _load_providers_registry(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_providers_registry(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"v Saved {path}")


def run_providers(args: argparse.Namespace) -> int:
    sub = getattr(args, "providers_subcommand", None)
    providers_path = Path(args.providers_json)

    if sub == "list":
        try:
            registry = _load_providers_registry(providers_path)
        except Exception as exc:
            print(f"x Failed to load {providers_path}: {exc}")
            return 1

        providers = registry.get("providers", {})
        if not providers:
            print("(no providers registered)")
            return 0

        col_id = max(len(k) for k in providers) + 2
        print(f"\n{'─' * 70}")
        print(f"  {'ID':<{col_id}}  {'FORMAT':<10}  {'LABEL'}")
        print(f"{'─' * 70}")
        for pid, pdef in providers.items():
            fmt = pdef.get("format", "vscode")
            lbl = pdef.get("label", "")
            path_str = expand_destination(pdef.get("config_path", ""))
            print(f"  {pid:<{col_id}}  {fmt:<10}  {lbl}")
            print(f"  {'':>{col_id}}  {'':10}  {path_str}")
        print(f"{'─' * 70}\n")
        return 0

    if sub == "add":
        try:
            registry = _load_providers_registry(providers_path)
        except Exception:
            registry = {"providers": {}}

        pid = args.provider_id
        if pid in registry.get("providers", {}):
            print(f"x Provider '{pid}' already exists. Use set-path to update it.")
            return 1

        registry.setdefault("providers", {})[pid] = {
            "label": args.label,
            "config_path": args.config_path,
            "format": args.format,
            "aliases": args.aliases or [],
        }
        _save_providers_registry(providers_path, registry)
        print(f"+ Added provider '{pid}'")
        return 0

    if sub == "remove":
        try:
            registry = _load_providers_registry(providers_path)
        except Exception as exc:
            print(f"x Failed to load {providers_path}: {exc}")
            return 1

        pid = args.provider_id
        if pid not in registry.get("providers", {}):
            print(f"x Provider '{pid}' not found.")
            return 1

        del registry["providers"][pid]
        _save_providers_registry(providers_path, registry)
        print(f"- Removed provider '{pid}'")
        return 0

    if sub == "set-path":
        try:
            registry = _load_providers_registry(providers_path)
        except Exception as exc:
            print(f"x Failed to load {providers_path}: {exc}")
            return 1

        pid = args.provider_id
        if pid not in registry.get("providers", {}):
            print(f"x Provider '{pid}' not found. Use 'providers add' to create it.")
            return 1

        old_path = registry["providers"][pid].get("config_path", "")
        registry["providers"][pid]["config_path"] = args.config_path
        _save_providers_registry(providers_path, registry)
        print(f"~ Updated '{pid}' config_path: {old_path}  →  {args.config_path}")
        return 0

    print("Usage: agent-config providers <list|add|remove|set-path> [options]")
    return 1


# ── skills ─────────────────────────────────────────────────────────────────

def _load_skills_registry(registry_path: Path) -> dict:
    with registry_path.open("r", encoding="utf-8") as f:
        return json.load(f).get("skills", {})


def _parse_skill_md_frontmatter(skill_source: Path) -> dict:
    """Read name and description from a SKILL.md YAML frontmatter block."""
    skill_md = skill_source / "SKILL.md"
    if not skill_md.exists():
        return {}
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    front = text[3:end]
    result = {}
    for line in front.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def _skills_deploy_dir() -> Path:
    """Return the global skills deployment directory (~/.agents/skills/)."""
    return Path(expand_destination("%USERPROFILE%")) / ".agents" / "skills"


def _filter_skills_by_agents(skills: dict, agents_filter) -> dict:
    """Return skills whose agents list overlaps with the given filter (or all if empty)."""
    if not agents_filter:
        return skills
    normalized = {a.strip().lower() for a in agents_filter}
    return {
        k: v for k, v in skills.items()
        if any(a.lower() in normalized for a in v.get("agents", []))
    }


def _skills_cli_sub_skills(source_path: Path, skills_subdir: str = "") -> List[str]:
    """Return names of sub-skill directories inside a skills-cli source.

    If *skills_subdir* is given (e.g. "skills"), look inside that subdirectory
    instead of directly in *source_path* (handles repos like agent-skills that
    nest their individual skills under a sub-folder).
    """
    root = source_path / skills_subdir if skills_subdir else source_path
    if not root.is_dir():
        return []
    return [d.name for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]


# ── skills show ────────────────────────────────────────────────────────────

def run_skills_show(args: argparse.Namespace) -> int:
    try:
        skills = _load_skills_registry(Path(args.registry_json))
    except Exception as exc:
        print(f"x Failed to load skills_registry.json: {exc}")
        return 1

    target = _filter_skills_by_agents(skills, args.agents)
    deploy_dir = _skills_deploy_dir()

    print(f"\n{'─' * 60}")
    print("Skills Status")
    print(f"{'─' * 60}")

    for skill_id, sdef in target.items():
        source_path = Path(args.config_dir) / sdef["source"]
        skill_type = sdef.get("type", "local")
        agents_str = ", ".join(sdef.get("agents", []))
        front = _parse_skill_md_frontmatter(source_path)
        description = front.get("description", sdef.get("label", ""))

        if skill_type == "local":
            deployed = (deploy_dir / skill_id).exists()
            icon = "✓" if deployed else "✗"
            status = "deployed" if deployed else "missing"
            print(f"\n  {icon} [{skill_id}]  ({status})")
            if description:
                print(f"    {description}")
            print(f"    Source : {source_path}")
            print(f"    Dest   : {deploy_dir / skill_id}")
            print(f"    Agents : {agents_str}")

        elif skill_type == "skills-cli":
            skills_subdir = sdef.get("skills_subdir", "")
            sub_skills = _skills_cli_sub_skills(source_path, skills_subdir)
            deployed_count = sum(1 for s in sub_skills if (deploy_dir / s).exists())
            total = len(sub_skills)
            all_ok = deployed_count == total and total > 0
            icon = "✓" if all_ok else ("~" if deployed_count > 0 else "✗")
            status = f"{deployed_count}/{total} sub-skills deployed"
            print(f"\n  {icon} [{skill_id}]  ({status})")
            if description:
                print(f"    {description}")
            print(f"    Source : {source_path}")
            print(f"    Agents : {agents_str}")
            for s in sub_skills:
                sub_icon = "✓" if (deploy_dir / s).exists() else "✗"
                print(f"    {sub_icon}  {s}")

    print(f"\n{'─' * 60}")
    return 0


# ── skills deploy ──────────────────────────────────────────────────────────

def run_skills_deploy(args: argparse.Namespace) -> int:
    try:
        skills = _load_skills_registry(Path(args.registry_json))
    except Exception as exc:
        print(f"x Failed to load skills_registry.json: {exc}")
        return 1

    target = _filter_skills_by_agents(skills, args.agents)
    deploy_dir = _skills_deploy_dir()
    dry_run: bool = args.dry_run
    tag = " [DRY RUN]" if dry_run else ""

    print(f"\n{'─' * 48}")
    print(f"Skills Deploy{tag}")
    print(f"{'─' * 48}")

    # Resolve log path from manifest default (reuse existing deploy.log)
    log_path = Path(args.config_dir) / "deploy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0

    for skill_id, sdef in target.items():
        source_path = Path(args.config_dir) / sdef["source"]
        skill_type = sdef.get("type", "local")

        print(f"\n  [{skill_id}] {sdef.get('label', skill_id)}")

        if not source_path.exists():
            print(f"  x Source not found: {source_path} -- skipping")
            failed += 1
            continue

        if skill_type == "local":
            dest = deploy_dir / skill_id
            print(f"  {source_path}  →  {dest}")
            if dry_run:
                print("  ~ DRY RUN: would copy")
                success += 1
                continue
            try:
                shutil.copytree(source_path, dest, dirs_exist_ok=True)
                print("  v Copied")
                success += 1
            except Exception as exc:
                print(f"  x Failed: {exc}")
                failed += 1

        elif skill_type == "skills-cli":
            # Delegate to existing skills-cli logic
            entry = {
                "source": sdef["source"],
                "agents": sdef.get("agents", []),
                "global": sdef.get("global", True),
            }
            agents_override = args.agents if args.agents else None
            ok = run_skills_cli_entry(entry, agents_override, dry_run, log_path, Path(args.config_dir))
            if ok:
                success += 1
            else:
                failed += 1

    print(f"\n{'─' * 48}")
    if failed == 0:
        print(f"Skills deploy complete. {success} skill(s) deployed.")
        return 0
    print(f"Skills deploy finished with errors. Success: {success}  Failed: {failed}")
    return 1


# ── skills sync ────────────────────────────────────────────────────────────

def run_skills_sync(args: argparse.Namespace) -> int:
    """Additive sync: deploy only skills not already installed."""
    try:
        skills = _load_skills_registry(Path(args.registry_json))
    except Exception as exc:
        print(f"x Failed to load skills_registry.json: {exc}")
        return 1

    target = _filter_skills_by_agents(skills, args.agents)
    deploy_dir = _skills_deploy_dir()
    dry_run: bool = args.dry_run
    tag = " [DRY RUN]" if dry_run else ""
    log_path = Path(args.config_dir) / "deploy.log"

    print(f"\n{'─' * 48}")
    print(f"Skills Sync{tag}")
    print(f"{'─' * 48}")

    added = 0

    for skill_id, sdef in target.items():
        source_path = Path(args.config_dir) / sdef["source"]
        skill_type = sdef.get("type", "local")

        if skill_type == "local":
            dest = deploy_dir / skill_id
            if dest.exists():
                print(f"\n  ~ [{skill_id}] already deployed — skipping")
                continue
            print(f"\n  [{skill_id}] missing — deploying")
            if dry_run:
                print("  ~ DRY RUN: would copy")
                added += 1
                continue
            try:
                shutil.copytree(source_path, dest, dirs_exist_ok=True)
                print("  v Copied")
                added += 1
            except Exception as exc:
                print(f"  x Failed: {exc}")

        elif skill_type == "skills-cli":
            skills_subdir = sdef.get("skills_subdir", "")
            sub_skills = _skills_cli_sub_skills(source_path, skills_subdir)
            missing = [s for s in sub_skills if not (deploy_dir / s).exists()]
            if not missing:
                print(f"\n  ~ [{skill_id}] all sub-skills present — skipping")
                continue
            print(f"\n  [{skill_id}] {len(missing)} sub-skill(s) missing — deploying")
            entry = {
                "source": sdef["source"],
                "agents": sdef.get("agents", []),
                "global": sdef.get("global", True),
            }
            agents_override = args.agents if args.agents else None
            ok = run_skills_cli_entry(entry, agents_override, dry_run, log_path, Path(args.config_dir))
            if ok:
                added += len(missing)

    print(f"\n{'─' * 48}")
    print(f"Skills sync complete. {added} skill(s) added.")
    return 0


# ── init ───────────────────────────────────────────────────────────────────

_INIT_TEMPLATES: dict = {
    "manifest.json": json.dumps(
        {
            "schema_version": "1.3.0",
            "deployments": [
                {
                    "type": "file",
                    "category": "config",
                    "source": "agents.json",
                    "destination": "%USERPROFILE%\\.agents\\agents.json",
                },
                {
                    "type": "file",
                    "category": "config",
                    "source": "manifest.json",
                    "destination": "%USERPROFILE%\\.agents\\manifest.json",
                },
                {
                    "type": "file",
                    "category": "config",
                    "source": "mcp_providers.json",
                    "destination": "%USERPROFILE%\\.agents\\mcp_providers.json",
                },
                {
                    "type": "file",
                    "category": "config",
                    "source": "instruction_providers.json",
                    "destination": "%USERPROFILE%\\.agents\\instruction_providers.json",
                },
                {
                    "type": "file",
                    "category": "config",
                    "source": "skills_registry.json",
                    "destination": "%USERPROFILE%\\.agents\\skills_registry.json",
                },
            ],
        },
        indent=2,
    ),
    "agents.json": json.dumps(
        {
            "integrations": {"enabled": []},
            "mcp": {"servers": {}},
            "workspace": {"vscode": {"hideGenerated": False, "hiddenPaths": []}},
        },
        indent=2,
    ),
    "mcp_providers.json": json.dumps(
        {
            "providers": {
                "vscode": {
                    "label": "VS Code / GitHub Copilot (VS Code)",
                    "config_path": "%USERPROFILE%\\.vscode\\mcp.json",
                    "format": "vscode",
                    "aliases": ["copilot_vscode"],
                },
                "copilot-cli": {
                    "label": "GitHub Copilot CLI",
                    "config_path": "%USERPROFILE%\\.copilot\\mcp.json",
                    "format": "vscode",
                    "aliases": [],
                },
                "claude": {
                    "label": "Claude Desktop",
                    "config_path": "%APPDATA%\\Claude\\claude_desktop_config.json",
                    "format": "claude",
                    "aliases": ["claude_desktop"],
                },
                "opencode": {
                    "label": "OpenCode",
                    "config_path": "%USERPROFILE%\\opencode.json",
                    "format": "opencode",
                    "aliases": [],
                },
                "cursor": {
                    "label": "Cursor",
                    "config_path": "%USERPROFILE%\\.cursor\\mcp.json",
                    "format": "vscode",
                    "aliases": ["cursor_vscode"],
                },
                "windsurf": {
                    "label": "Windsurf",
                    "config_path": "%USERPROFILE%\\.codeium\\windsurf\\mcp_config.json",
                    "format": "vscode",
                    "aliases": [],
                },
                "junie": {
                    "label": "JetBrains Junie",
                    "config_path": "%USERPROFILE%\\.junie\\mcp.json",
                    "format": "junie",
                    "aliases": [],
                },
            }
        },
        indent=2,
    ),
    "instruction_providers.json": json.dumps(
        {
            "providers": {
                "agents-md": {
                    "label": "Global AGENTS.md (all agents)",
                    "source": "instructions/AGENTS.md",
                    "destination": "%USERPROFILE%\\AGENTS.md",
                },
                "claude-md": {
                    "label": "CLAUDE.md (Claude Code)",
                    "source": "instructions/CLAUDE.md",
                    "destination": "%USERPROFILE%\\CLAUDE.md",
                },
            }
        },
        indent=2,
    ),
    "skills_registry.json": json.dumps({"skills": {}}, indent=2),
    "justfile": """\
set shell := ["powershell.exe", "-c"]

# Deploy all manifest entries to their configured destinations
deploy:
    uv run agent_config.py deploy

# Preview deploy without making changes
deploy-dry:
    uv run agent_config.py deploy --dry-run

# Deploy MCP configs to all registered providers
mcp-deploy:
    uv run agent_config.py mcp deploy

# Preview MCP deploy without changes
mcp-deploy-dry:
    uv run agent_config.py mcp deploy --dry-run

# Additive MCP sync (add only missing servers)
mcp-sync:
    uv run agent_config.py mcp sync

# Show MCP configuration status across providers
mcp-show:
    uv run agent_config.py mcp show

# Deploy instruction files (AGENTS.md etc.) to all providers
instructions-deploy:
    uv run agent_config.py instructions deploy

# Show instruction file status
instructions-show:
    uv run agent_config.py instructions show

# Deploy all registered skills
skills-deploy:
    uv run agent_config.py skills deploy

# Preview skills deploy
skills-deploy-dry:
    uv run agent_config.py skills deploy --dry-run

# Show skills deployment status
skills-show:
    uv run agent_config.py skills show

# Pull memory files back from deployed locations into the repo
pull:
    uv run agent_config.py pull

# Full cycle: deploy then agents sync
sync:
    uv run agent_config.py sync

# Reinstall CLI from source after editing agent_config.py
reinstall:
    uv tool install . --reinstall
""",
    "instructions/AGENTS.md": """\
# Agent Instructions

You are an AI coding assistant. Follow these guidelines when working in this repository.

## General

- Write clean, readable, well-structured code.
- Follow the existing code style and conventions in the project.
- Run available tests before considering a task complete.
- Ask for clarification when requirements are ambiguous.

## Repository

- Read README.md for project overview before making changes.
- Check for a CLAUDE.md for Claude Code-specific guidance.
- Prefer small, focused commits over large sweeping changes.
""",
}


def run_init(args: argparse.Namespace) -> int:
    """Scaffold a new agent-config directory with starter template files."""
    target_dir = Path(args.dir).resolve()
    force: bool = args.force

    print(f"\n{'─' * 56}")
    print(f"agent-config init")
    print(f"{'─' * 56}")
    print(f"  Target : {target_dir}")
    if force:
        print("  Mode   : overwrite (--force)")
    print()

    created = 0
    skipped = 0

    for rel_path, content in _INIT_TEMPLATES.items():
        dest = target_dir / rel_path
        if dest.exists() and not force:
            print(f"  ~ {rel_path}  (already exists — skipping, use --force to overwrite)")
            skipped += 1
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            action = "overwritten" if dest.exists() and force else "created"
            print(f"  v {rel_path}  ({action})")
            created += 1
        except Exception as exc:
            print(f"  x {rel_path}  FAILED: {exc}")

    print(f"\n{'─' * 56}")
    print(f"Init complete.  {created} file(s) created,  {skipped} skipped.")
    if created > 0:
        print()
        print("  Next steps:")
        print(f"    1. cd {target_dir}")
        print("    2. Edit mcp_providers.json to set your provider config paths.")
        print("    3. Edit agents.json to add your MCP server definitions.")
        print("    4. Run:  agent-config deploy")
    return 0


def main() -> int:
    # Ensure stdout/stderr use UTF-8 on all platforms (box-drawing chars, arrows, etc.)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()

    # Resolve and attach config_dir so every run function can use it uniformly.
    config_dir = Path(getattr(args, "config_dir", str(Path.cwd()))).resolve()
    args.config_dir = str(config_dir)

    if args.command == "init":
        return run_init(args)

    if args.command == "pull":
        return run_pull(args)

    if args.command == "sync":
        return run_sync(args)

    if args.command == "mcp":
        sub = getattr(args, "mcp_subcommand", None)
        if sub == "deploy":
            return run_mcp_deploy(args)
        if sub == "sync":
            return run_mcp_sync(args)
        if sub == "show":
            return run_mcp_show(args)
        print("Usage: agent-config mcp <deploy|sync|show> [options]")
        return 1

    if args.command == "instructions":
        sub = getattr(args, "instr_subcommand", None)
        if sub == "deploy":
            return run_instructions_deploy(args)
        if sub == "show":
            return run_instructions_show(args)
        print("Usage: agent-config instructions <deploy|show> [options]")
        return 1

    if args.command == "providers":
        return run_providers(args)

    if args.command == "skills":
        sub = getattr(args, "skills_subcommand", None)
        if sub == "deploy":
            return run_skills_deploy(args)
        if sub == "show":
            return run_skills_show(args)
        if sub == "sync":
            return run_skills_sync(args)
        print("Usage: agent-config skills <deploy|show|sync> [options]")
        return 1

    # "deploy" subcommand or no subcommand (legacy invocation)
    return run_deploy(args)


if __name__ == "__main__":
    raise SystemExit(main())