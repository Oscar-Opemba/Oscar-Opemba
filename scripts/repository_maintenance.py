#!/usr/bin/env python3
"""Automated repository maintenance for the Oscar-Opemba profile repository.

What this does
--------------
Audits the repository for the failure modes that actually affect a profile
repo -- broken README references, a missing GitHub Pages entry point,
workflow-hygiene regressions, accidentally committed credentials, and dead
external links -- then records the result in a small JSON health file.

Commit policy
-------------
The health file is rewritten only when something *meaningful* changed. A
content digest is computed over the audit facts, deliberately excluding the
run timestamp and transient link-check noise, so a scheduled run that finds
nothing new writes nothing and produces no commit. A heartbeat refresh
(default: every 30 days) keeps the file from looking abandoned.

This script has no third-party dependencies. PyYAML is used when importable
for exact workflow parsing, and a conservative regex reader is used otherwise;
the mode is recorded in the output as ``yaml_parser``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # optional, present on GitHub-hosted runners; never required
    import yaml  # type: ignore

    _YAML_PARSER = "pyyaml"
except Exception:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None  # type: ignore
    _YAML_PARSER = "regex-fallback"

SCHEMA_VERSION = 1
WORKFLOW_ID = "repository-maintenance"
DEFAULT_OUTPUT = Path(".github/repository-activity.json")
DEFAULT_PAGES_ENTRY = Path("docs/index.html")

# Directories whose contents are produced by another workflow. A README
# reference into one of these is "not generated yet" (a warning), not a
# genuinely broken link (a failure).
DEFAULT_GENERATED_PATHS = ("profile-3d-contrib",)

# Actions published by GitHub itself; a version tag on these is acceptable.
FIRST_PARTY_ACTION_OWNERS = ("actions", "github")

# Checks whose presence depends on how the run was invoked rather than on the
# state of the repository. Including these in the digest would mean a run with
# --check-links and a run without it disagree, so the two would rewrite the file
# back and forth forever. The durable part of the link verdict -- the set of
# links that are actually broken -- is fed into the digest separately.
DIGEST_EXCLUDED_CHECKS = frozenset({"external_links_reachable"})

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".yml", ".yaml", ".json", ".html", ".htm",
    ".css", ".js", ".mjs", ".ts", ".py", ".sh", ".toml", ".cfg", ".ini",
}

# Credential signatures. The literal prefixes are assembled from fragments so
# that this file cannot match its own patterns -- that keeps the scan honest
# and lets it cover every tracked file, including this one.
_GH = "gh"
CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token-classic", re.compile(_GH + r"[pousr]_[A-Za-z0-9]{36}")),
    ("github-token-fine-grained", re.compile("github" + r"_pat_[A-Za-z0-9_]{22,}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("slack-token", re.compile("xox" + r"[abprs]-[A-Za-z0-9-]{10,}")),
)

# Reference extraction across the mixed Markdown + HTML that a profile
# README uses.
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)")
_HTML_ATTR = re.compile(r"""(?:href|src|srcset)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SKIP_REF_PREFIXES = ("#", "mailto:", "tel:", "data:", "javascript:")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def log(message: str) -> None:
    print(message, file=sys.stderr)


def annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation (no-op noise when run locally)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")


def utc_now(override: str | None) -> datetime:
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_git(root: Path, *args: str) -> str | None:
    """Run a fixed-argument git command. Never uses a shell."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"git {' '.join(args)} unavailable: {exc}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def tracked_files(root: Path) -> list[str]:
    listing = run_git(root, "ls-files")
    if listing is not None:
        return sorted(p for p in listing.splitlines() if p)
    # Fallback for a non-git checkout: walk the tree, skipping .git.
    found: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def detect_default_branch(root: Path) -> str:
    head = run_git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        return head.split("/", 1)[1]
    return run_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "main"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# reference extraction
# --------------------------------------------------------------------------
def extract_references(text: str) -> tuple[list[str], list[str]]:
    """Split every link/image reference into (internal_paths, external_urls)."""
    raw: list[str] = list(_MD_LINK.findall(text))
    for attr in _HTML_ATTR.findall(text):
        # srcset may hold a comma-separated candidate list
        raw.extend(part.strip().split(" ")[0] for part in attr.split(","))

    internal: set[str] = set()
    external: set[str] = set()
    for ref in raw:
        ref = ref.strip().strip('"').strip("'")
        if not ref or ref.lower().startswith(_SKIP_REF_PREFIXES):
            continue
        if ref.startswith("//") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", ref):
            external.add(ref.split("#", 1)[0])
            continue
        path = ref.split("#", 1)[0].split("?", 1)[0]
        # Strip "./" prefixes explicitly. str.lstrip("./") would strip those
        # characters as a set and turn "./.github/x" into "github/x".
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("/"):
            path = path[1:]
        if path:
            internal.add(path)
    return sorted(internal), sorted(external)


# --------------------------------------------------------------------------
# workflow inspection
# --------------------------------------------------------------------------
def _yaml_workflow(text: str) -> dict[str, Any] | None:
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # invalid YAML is a finding, not a crash
        return {"__error__": str(exc).splitlines()[0][:200]}
    if not isinstance(data, dict):
        return {"__error__": "workflow root is not a mapping"}
    return data


def inspect_workflow(path: Path, rel: str) -> dict[str, Any]:
    text = read_text(path)
    info: dict[str, Any] = {
        "path": rel,
        "name": None,
        "yaml_valid": True,
        "triggers": [],
        "schedules": [],
        "declares_permissions": False,
        "has_concurrency_guard": False,
        "external_actions": [],
    }

    data = _yaml_workflow(text)
    if data is not None and "__error__" in data:
        info["yaml_valid"] = False
        info["yaml_error"] = data["__error__"]
        return info

    if data is not None:
        info["name"] = data.get("name")
        # PyYAML resolves a bare `on:` key to the boolean True.
        triggers = data.get("on", data.get(True))
        if isinstance(triggers, dict):
            info["triggers"] = sorted(str(k) for k in triggers)
            schedule = triggers.get("schedule") or []
            if isinstance(schedule, list):
                info["schedules"] = [
                    str(item.get("cron")) for item in schedule if isinstance(item, dict) and item.get("cron")
                ]
        elif isinstance(triggers, list):
            info["triggers"] = sorted(str(t) for t in triggers)
        elif triggers is not None:
            info["triggers"] = [str(triggers)]

        jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        info["declares_permissions"] = "permissions" in data or any(
            isinstance(job, dict) and "permissions" in job for job in jobs.values()
        )
        info["has_concurrency_guard"] = "concurrency" in data or any(
            isinstance(job, dict) and "concurrency" in job for job in jobs.values()
        )
        uses: list[str] = []
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    uses.append(step["uses"].strip())
    else:
        # Regex fallback: enough signal for hygiene checks without PyYAML.
        info["name"] = next(iter(re.findall(r"(?m)^name:\s*(.+)$", text)), None)
        info["triggers"] = sorted(set(re.findall(r"(?m)^\s{2}(\w+):", text)) & {
            "schedule", "push", "pull_request", "workflow_dispatch", "workflow_call", "release", "issues",
        })
        info["schedules"] = re.findall(r"""(?m)cron:\s*["']?([^"'\n]+)""", text)
        info["declares_permissions"] = bool(re.search(r"(?m)^\s*permissions:", text))
        info["has_concurrency_guard"] = bool(re.search(r"(?m)^\s*concurrency:", text))
        uses = [u.strip() for u in re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*(\S+)", text)]

    for ref in sorted(set(uses)):
        owner = ref.split("/", 1)[0]
        version = ref.split("@", 1)[1] if "@" in ref else ""
        info["external_actions"].append(
            {
                "uses": ref,
                "first_party": owner in FIRST_PARTY_ACTION_OWNERS,
                "pinned_to_sha": bool(re.fullmatch(r"[0-9a-f]{40}", version)),
                "pinned_to_version": bool(re.match(r"v?\d", version)),
            }
        )
    if info["name"] is not None:
        info["name"] = str(info["name"]).strip().strip("\"'")
    return info


# --------------------------------------------------------------------------
# link checking
# --------------------------------------------------------------------------
def check_link(url: str, timeout: float) -> tuple[str, str, str]:
    """Return (url, state, detail) where state is ok | broken | unverified.

    Only unambiguous, durable failures are reported as ``broken``; timeouts,
    rate limits and server errors are ``unverified`` so that transient
    flakiness can never churn the committed health file.
    """
    headers = {
        "User-Agent": "repository-maintenance-bot/1.0 (+https://github.com/Oscar-Opemba)",
        "Accept": "*/*",
    }
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return url, "ok", str(response.status)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return url, "broken", f"HTTP {exc.code}"
            if exc.code in (403, 405, 429) and method == "HEAD":
                continue  # some hosts reject HEAD; retry with GET
            return url, "unverified", f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "Name or service not known" in reason or "nodename nor servname" in reason:
                return url, "broken", "DNS resolution failed"
            return url, "unverified", reason[:120]
        except Exception as exc:  # never let a link check abort the run
            return url, "unverified", f"{type(exc).__name__}: {exc}"[:120]
    return url, "unverified", "no conclusive response"


def check_links(urls: list[str], timeout: float, workers: int) -> dict[str, Any]:
    results: dict[str, list[str]] = {"ok": [], "broken": [], "unverified": []}
    details: dict[str, str] = {}
    if not urls:
        return {"performed": True, "checked": 0, "ok": 0, "broken": [], "unverified": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for url, state, detail in pool.map(lambda u: check_link(u, timeout), urls):
            results[state].append(url)
            details[url] = detail
    return {
        "performed": True,
        "checked": len(urls),
        "ok": len(results["ok"]),
        "broken": sorted(results["broken"]),
        "unverified": sorted(results["unverified"]),
        "details": {u: details[u] for u in sorted(results["broken"] + results["unverified"])},
    }


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
def audit(root: Path, args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, Any]]:
    checks: list[dict[str, str]] = []

    def record(check_id: str, status: str, detail: str) -> None:
        checks.append({"id": check_id, "status": status, "detail": detail})

    files = tracked_files(root)
    file_set = set(files)

    # -- README ------------------------------------------------------------
    readme_path = root / "README.md"
    readme_info: dict[str, Any] = {"present": readme_path.is_file()}
    internal_refs: list[str] = []
    external_urls: list[str] = []
    if readme_info["present"]:
        readme_text = read_text(readme_path)
        internal_refs, external_urls = extract_references(readme_text)
        readme_info.update(
            {
                "bytes": readme_path.stat().st_size,
                "internal_references": len(internal_refs),
                "external_links": len(external_urls),
            }
        )
        record("readme_present", "pass", "README.md is present")
    else:
        record("readme_present", "fail", "README.md is missing")

    # -- internal references resolve --------------------------------------
    missing_generated: list[str] = []
    missing_broken: list[str] = []
    for ref in internal_refs:
        if (root / ref).exists() or ref in file_set:
            continue
        top = ref.split("/", 1)[0]
        (missing_generated if top in args.generated_path else missing_broken).append(ref)
    if missing_broken:
        record(
            "readme_internal_references_resolve",
            "fail",
            "README references paths that do not exist: " + ", ".join(missing_broken),
        )
    elif missing_generated:
        record(
            "readme_internal_references_resolve",
            "warn",
            "Awaiting generated output (created by another workflow): " + ", ".join(missing_generated),
        )
    else:
        record("readme_internal_references_resolve", "pass", f"All {len(internal_refs)} internal references resolve")

    # -- GitHub Pages entry -----------------------------------------------
    pages_entry = root / args.pages_entry
    pages_info = {"entry": args.pages_entry, "present": pages_entry.is_file()}
    if pages_info["present"]:
        pages_text = read_text(pages_entry)
        title = re.search(r"<title>(.*?)</title>", pages_text, re.IGNORECASE | re.DOTALL)
        pages_info["bytes"] = pages_entry.stat().st_size
        pages_info["title"] = title.group(1).strip() if title else None
        if title:
            record("pages_entry_present", "pass", f"{args.pages_entry} present with a <title>")
        else:
            record("pages_entry_present", "warn", f"{args.pages_entry} present but has no <title>")
    else:
        record("pages_entry_present", "warn", f"{args.pages_entry} not found (GitHub Pages may be unconfigured)")

    # -- workflows ---------------------------------------------------------
    workflow_dir = root / ".github" / "workflows"
    workflows: list[dict[str, Any]] = []
    if workflow_dir.is_dir():
        for path in sorted(workflow_dir.iterdir()):
            if path.suffix in (".yml", ".yaml") and path.is_file():
                workflows.append(inspect_workflow(path, path.relative_to(root).as_posix()))

    invalid = [w["path"] for w in workflows if not w["yaml_valid"]]
    if invalid:
        record("workflow_yaml_valid", "fail", "Invalid YAML: " + ", ".join(invalid))
    elif yaml is None:
        record("workflow_yaml_valid", "warn", f"PyYAML unavailable; parsed {len(workflows)} workflow(s) with the regex reader")
    else:
        record("workflow_yaml_valid", "pass", f"{len(workflows)} workflow file(s) parsed as valid YAML")

    undeclared = [w["path"] for w in workflows if not w["declares_permissions"]]
    record(
        "workflows_declare_permissions",
        "fail" if undeclared else "pass",
        ("No explicit permissions block (inherits repository default): " + ", ".join(undeclared))
        if undeclared
        else "Every workflow declares an explicit permissions block",
    )

    unpinned = [
        f"{w['path']}: {a['uses']}"
        for w in workflows
        for a in w["external_actions"]
        if not a["first_party"] and not a["pinned_to_sha"]
    ]
    floating = [
        f"{w['path']}: {a['uses']}"
        for w in workflows
        for a in w["external_actions"]
        if not a["pinned_to_sha"] and not a["pinned_to_version"]
    ]
    if floating:
        record("third_party_actions_pinned", "fail", "Action reference has no version or SHA: " + ", ".join(floating))
    elif unpinned:
        record(
            "third_party_actions_pinned",
            "warn",
            "Third-party action pinned to a mutable tag rather than a commit SHA: " + ", ".join(unpinned),
        )
    else:
        record("third_party_actions_pinned", "pass", "All third-party actions are pinned to a commit SHA")

    unguarded = [w["path"] for w in workflows if not w["has_concurrency_guard"] and "schedule" in w["triggers"]]
    record(
        "scheduled_workflows_have_concurrency_guard",
        "warn" if unguarded else "pass",
        ("Scheduled workflow without a concurrency guard (overlapping runs can race on push): " + ", ".join(unguarded))
        if unguarded
        else "Scheduled workflows declare a concurrency guard",
    )

    # -- credential scan ---------------------------------------------------
    findings: list[str] = []
    for rel in files:
        path = root / rel
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            content = read_text(path)
        except OSError:
            continue
        for label, pattern in CREDENTIAL_PATTERNS:
            if pattern.search(content):
                findings.append(f"{rel} ({label})")
    record(
        "no_embedded_credentials",
        "fail" if findings else "pass",
        ("Possible credential material detected: " + ", ".join(sorted(set(findings))))
        if findings
        else f"No credential signatures across {len(files)} tracked file(s)",
    )

    # -- external links ----------------------------------------------------
    link_result: dict[str, Any] = {"performed": False}
    if args.check_links and external_urls:
        log(f"Checking {len(external_urls)} external link(s)...")
        link_result = check_links(external_urls, args.link_timeout, args.link_workers)
        broken = link_result["broken"]
        if broken:
            record("external_links_reachable", "fail", "Unreachable README links: " + ", ".join(broken))
        elif link_result["unverified"]:
            record(
                "external_links_reachable",
                "pass",
                f"{link_result['ok']}/{link_result['checked']} links verified; "
                f"{len(link_result['unverified'])} inconclusive (treated as healthy)",
            )
        else:
            record("external_links_reachable", "pass", f"All {link_result['checked']} external links reachable")

    repository = {
        "default_branch": detect_default_branch(root),
        "tracked_files": len(files),
        "readme": readme_info,
        "pages": pages_info,
        "workflows": workflows,
    }
    return repository, checks, link_result


# --------------------------------------------------------------------------
# document assembly
# --------------------------------------------------------------------------
def overall_status(checks: list[dict[str, str]]) -> str:
    if any(c["status"] == "fail" for c in checks):
        return "attention"
    if any(c["status"] == "warn" for c in checks):
        return "healthy-with-warnings"
    return "healthy"


def compute_digest(repository: dict[str, Any], checks: list[dict[str, str]], link_result: dict[str, Any]) -> str:
    """Hash only the durable facts.

    Excluded on purpose: the run timestamp, per-link diagnostic detail, the
    ``unverified`` bucket, and any check listed in ``DIGEST_EXCLUDED_CHECKS``.
    Including any of those would make an unchanged repository produce a new
    commit on every run.
    """
    payload = {
        "repository": repository,
        "checks": [c for c in checks if c["id"] not in DIGEST_EXCLUDED_CHECKS],
        "broken_links": sorted(link_result.get("broken", [])),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_document(
    now: datetime,
    digest: str,
    status: str,
    repository: dict[str, Any],
    checks: list[dict[str, str]],
    link_result: dict[str, Any],
    last_content_change: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow": WORKFLOW_ID,
        "status": status,
        "last_maintenance_run": iso(now),
        "last_content_change": last_content_change,
        "content_digest": digest,
        "yaml_parser": _YAML_PARSER,
        "summary": {
            "passed": sum(1 for c in checks if c["status"] == "pass"),
            "warnings": sum(1 for c in checks if c["status"] == "warn"),
            "failures": sum(1 for c in checks if c["status"] == "fail"),
        },
        "repository": repository,
        "checks": checks,
        "link_check": link_result,
    }


def load_existing(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"Existing {path} is unreadable ({exc}); it will be regenerated.")
        return None
    return data if isinstance(data, dict) else None


def write_outputs(changed: bool, status: str, reason: str, document: dict[str, Any]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
            handle.write(f"status={status}\n")
            handle.write(f"reason={reason}\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    lines = [
        "## Repository maintenance",
        "",
        f"**Status:** `{status}` &nbsp;·&nbsp; **File updated:** `{'yes' if changed else 'no'}` ({reason})",
        "",
        "| | Check | Detail |",
        "|---|---|---|",
    ]
    for check in document["checks"]:
        detail = check["detail"].replace("|", "\\|")
        lines.append(f"| {icons.get(check['status'], '•')} | `{check['id']}` | {detail} |")
    lines += ["", f"Digest: `{document['content_digest']}`", ""]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"health file, relative to root (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--pages-entry", default=str(DEFAULT_PAGES_ENTRY), help="GitHub Pages entry point to verify")
    parser.add_argument("--generated-path", action="append", default=None, metavar="DIR",
                        help="directory produced by another workflow; missing refs there warn instead of fail (repeatable)")
    parser.add_argument("--heartbeat-days", type=int, default=30,
                        help="refresh the timestamp after this many days even with no changes; 0 disables (default: 30)")
    parser.add_argument("--check-links", action="store_true", help="verify external README links over the network")
    parser.add_argument("--link-timeout", type=float, default=12.0, help="per-link timeout in seconds (default: 12)")
    parser.add_argument("--link-workers", type=int, default=8, help="parallel link checks (default: 8)")
    parser.add_argument("--force", action="store_true", help="rewrite the file even when nothing changed")
    parser.add_argument("--dry-run", action="store_true", help="print the document; write nothing")
    parser.add_argument("--now", default=None, metavar="ISO8601", help="freeze the timestamp (for reproducible testing)")
    parser.add_argument("--strict", action="store_true", help="exit 2 when any check fails")
    args = parser.parse_args(argv)
    if args.generated_path is None:
        args.generated_path = list(DEFAULT_GENERATED_PATHS)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    if not root.is_dir():
        log(f"error: repository root does not exist: {root}")
        return 1

    output_path = (root / args.output).resolve()
    if root not in output_path.parents:
        log(f"error: --output must stay inside the repository root: {output_path}")
        return 1

    now = utc_now(args.now)
    repository, checks, link_result = audit(root, args)
    existing = load_existing(output_path)

    if link_result.get("performed"):
        link_result["last_checked"] = iso(now)
    else:
        # This run did not check links. Inherit the previous verdict instead of
        # implicitly asserting that every link is fine -- otherwise a run
        # without --check-links would erase a known breakage and the next
        # link-checking run would rediscover it, commit after commit.
        previous_link = (existing or {}).get("link_check")
        if isinstance(previous_link, dict) and previous_link.get("performed"):
            link_result = dict(previous_link)
            link_result["stale"] = True
            broken = link_result.get("broken", [])
            checks.append(
                {
                    "id": "external_links_reachable",
                    "status": "fail" if broken else "pass",
                    "detail": (
                        ("Unreachable README links: " + ", ".join(broken))
                        if broken
                        else f"{link_result.get('ok', 0)}/{link_result.get('checked', 0)} links reachable"
                    )
                    + f" (not re-checked this run; verdict from {link_result.get('last_checked', 'an earlier run')})",
                }
            )

    status = overall_status(checks)
    digest = compute_digest(repository, checks, link_result)

    previous_digest = existing.get("content_digest") if existing else None
    previous_change = existing.get("last_content_change") if existing else None

    if existing is None:
        changed, reason = True, "initial run"
    elif previous_digest != digest:
        changed, reason = True, "repository state changed"
    elif args.force:
        changed, reason = True, "forced refresh"
    elif args.heartbeat_days > 0:
        try:
            last_run = datetime.fromisoformat(str(existing.get("last_maintenance_run", "")).replace("Z", "+00:00"))
        except ValueError:
            changed, reason = True, "unparseable previous timestamp"
        else:
            age = now - last_run.astimezone(timezone.utc)
            if age >= timedelta(days=args.heartbeat_days):
                changed, reason = True, f"heartbeat refresh ({age.days}d since last run)"
            else:
                changed, reason = False, f"no change ({age.days}d since last run)"
    else:
        changed, reason = False, "no change"

    last_content_change = iso(now) if (previous_digest != digest or not previous_change) else str(previous_change)
    document = build_document(now, digest, status, repository, checks, link_result, last_content_change)

    for check in checks:
        if check["status"] == "fail":
            annotate("error", f"{check['id']}: {check['detail']}")
        elif check["status"] == "warn":
            annotate("warning", f"{check['id']}: {check['detail']}")

    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    if args.dry_run:
        print(rendered, end="")
        log(f"[dry-run] status={status} would_write={changed} ({reason})")
        return 2 if args.strict and document["summary"]["failures"] else 0

    if changed:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        log(f"Wrote {output_path.relative_to(root)} ({reason}); status={status}")
    else:
        log(f"No update required ({reason}); status={status}")

    write_outputs(changed, status, reason, document)
    log(
        f"Checks: {document['summary']['passed']} passed, "
        f"{document['summary']['warnings']} warning(s), {document['summary']['failures']} failure(s)"
    )
    return 2 if args.strict and document["summary"]["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
