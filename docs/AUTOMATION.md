# Repository Maintenance Automation

This repository runs one scheduled audit of itself and records the result in a
small JSON health file. It exists to catch the ways a profile repository quietly
rots — a README image that points at a file nobody generated, a Pages entry point
that got renamed, a workflow that silently inherits write permissions it doesn't
need, a link that 404s six months after you added it.

It is deliberately **not** a contribution-graph generator. See
[What it deliberately does not do](#what-it-deliberately-does-not-do).

---

## 1. What the automation does

Each run performs the following checks and writes the outcome to
[`.github/repository-activity.json`](../.github/repository-activity.json):

| Check | Fails when | Warns when |
|---|---|---|
| `readme_present` | `README.md` is missing | — |
| `readme_internal_references_resolve` | A README link/image points at a path that does not exist | The missing path lives under a known generated directory (`profile-3d-contrib/`), i.e. it just hasn't been produced yet |
| `pages_entry_present` | — | `docs/index.html` is missing, or has no `<title>` |
| `workflow_yaml_valid` | A workflow file is not valid YAML | PyYAML is unavailable, so the fallback regex reader was used |
| `workflows_declare_permissions` | A workflow has no explicit `permissions:` block and silently inherits the repository default | — |
| `third_party_actions_pinned` | An action reference has no version *or* SHA at all | A third-party action is pinned to a mutable tag rather than a commit SHA |
| `scheduled_workflows_have_concurrency_guard` | — | A scheduled workflow has no `concurrency:` group, so overlapping runs can race when pushing |
| `no_embedded_credentials` | A tracked text file matches a GitHub / AWS / Slack token or private-key signature | — |
| `external_links_reachable` | A README link returns 404/410 or fails DNS | — |

The overall `status` field is `healthy`, `healthy-with-warnings`, or `attention`.
Failures and warnings also appear as annotations and as a table in the workflow
run summary, so you can read the result without opening the JSON.

### The commit policy (why this doesn't spam your history)

The script computes a SHA-256 **content digest** over the durable audit facts and
compares it against the digest stored in the existing file. It rewrites the file —
and therefore produces a commit — only when:

1. the digest changed (something about the repository genuinely changed), **or**
2. the file has not been refreshed in 30 days (a heartbeat), **or**
3. you passed `--force` / ran the workflow with `force_refresh: true`.

Deliberately excluded from the digest, because including them would generate
commits that carry no information:

- `last_maintenance_run` — the timestamp itself.
- The `unverified` link bucket. A timeout, a 403, a rate limit, or a 5xx from a
  shared badge host is *not* evidence of a broken link, so it never enters the
  digest. Only an unambiguous 404/410/DNS failure does.
- The `external_links_reachable` check entry, whose mere presence depends on
  whether the run was invoked with link checking on. Its durable part — the set
  of links actually found broken — is fed into the digest separately.
- When a run does not check links, it **inherits** the previous link verdict
  rather than implicitly asserting all links are fine. Without this, a
  push-triggered run would erase a known breakage and the next weekly run would
  rediscover it, forever, one commit each way.

A scheduled run over an unchanged repository therefore writes nothing, stages
nothing, and creates no commit.

---

## 2. Where the automation lives

```
.github/workflows/repository-maintenance.yml   the workflow
scripts/repository_maintenance.py              the audit logic (stdlib only)
.github/repository-activity.json               generated output — do not hand-edit
docs/AUTOMATION.md                             this document
```

The script has **no third-party dependencies**. PyYAML is used for exact workflow
parsing when it is importable (it is preinstalled on GitHub-hosted runners) and a
conservative regex reader is used otherwise; the mode is recorded in the output as
`yaml_parser`.

---

## 3. How the schedule works

```yaml
schedule:
  - cron: "17 5 * * 1"   # Mondays, 05:17 UTC
```

Weekly, on purpose. Repository hygiene does not change hour to hour, and a
conservative interval keeps the commit history meaningful.

The odd minute is intentional: GitHub queues an enormous number of jobs at `:00`,
and scheduled workflows are dropped or delayed under that load.

Two other triggers exist:

- **`workflow_dispatch`** — manual runs (see below).
- **`push` to `main`**, restricted to `README.md`, `docs/**`, the script, and
  `.github/workflows/**`. This keeps the audit accurate the moment the audited
  content changes. It cannot loop: pushes made with `GITHUB_TOKEN` do not trigger
  workflow runs.

> **Note on scheduled workflows:** GitHub disables `schedule` triggers in public
> repositories after **60 days without any repository activity**, and emails the
> owner. Any commit or a manual run re-enables it.

---

## 4. How to trigger it manually

**Via the UI:** Actions → **Repository Maintenance** → **Run workflow**. Two inputs:

- `check_links` (default `true`) — verify external README links over the network.
- `force_refresh` (default `false`) — rewrite the health file even if nothing changed.

**Via the CLI:**

```bash
gh workflow run repository-maintenance.yml
gh workflow run repository-maintenance.yml -f check_links=false
gh workflow run repository-maintenance.yml -f force_refresh=true

gh run watch                                   # follow the active run
gh run list --workflow=repository-maintenance.yml --limit 5
```

---

## 5. Required permissions

The workflow declares `permissions: contents: read` at the top level and elevates
to `contents: write` only on the single job that pushes. Nothing else is granted.

It uses the automatic `GITHUB_TOKEN`. **No Personal Access Token, no repository
secret, and no other credential is required or referenced.**

One repository setting must allow the push:

> Settings → Actions → General → Workflow permissions → **Read and write permissions**

(The existing `profile-3d-contrib` workflow already requires this.)

---

## 6. How to disable it

Pick whichever level of permanence you want:

| Approach | Effect |
|---|---|
| Actions → *Repository Maintenance* → `···` → **Disable workflow** | Stops all triggers; keeps the files. Reversible in one click. **Recommended.** |
| Delete `.github/workflows/repository-maintenance.yml` | Removes it entirely. The script stays usable locally. |
| Comment out the `schedule:` block | Keeps manual runs, stops automatic ones. |
| Settings → Actions → General → **Disable Actions** | Disables *every* workflow in the repository, including the 3D contribution graph. |

Nothing outside this repository is affected, and no state needs cleaning up.

---

## 7. How to change the interval

Edit the cron expression in `.github/workflows/repository-maintenance.yml`
(times are **UTC**, and GitHub does not observe daylight saving):

```yaml
- cron: "17 5 * * 1"     # weekly, Mondays        (current)
- cron: "17 5 1 * *"     # monthly, the 1st
- cron: "17 5 1 */3 *"   # quarterly
- cron: "17 5 * * 1,4"   # twice weekly, Mon + Thu
```

The heartbeat interval is separate — it is how long the file may go unrefreshed
before the timestamp is bumped even with no findings. Change `--heartbeat-days`
in the *Run repository maintenance* step:

```bash
--heartbeat-days 30    # default
--heartbeat-days 0     # disable the heartbeat: commit only on real changes
```

Running more often than the heartbeat window costs nothing — extra runs simply
find nothing and exit without writing.

---

## 8. Local usage

No installation, no virtualenv, no dependencies:

```bash
# Preview the audit without writing anything
python3 scripts/repository_maintenance.py --dry-run

# Include external link checking
python3 scripts/repository_maintenance.py --dry-run --check-links

# Write the file exactly as CI would
python3 scripts/repository_maintenance.py --check-links

# Use as a pre-commit / CI gate: exit code 2 if any check fails
python3 scripts/repository_maintenance.py --dry-run --strict --check-links

# Reproducible output (frozen clock) — useful for diffing runs
python3 scripts/repository_maintenance.py --dry-run --now 2026-01-01T00:00:00Z

python3 scripts/repository_maintenance.py --help
```

Exit codes: `0` success · `1` internal error · `2` `--strict` and a check failed.

---

## What it deliberately does not do

This automation performs real maintenance and produces contribution activity only
as a side effect of that maintenance actually happening. It does not, and will not:

- fabricate historical or backdated commits
- manipulate `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` or any author metadata
- spoof identities or invent contributors
- create empty commits, or commit unchanged content
- commit on every scheduled run regardless of whether anything changed
- generate high-frequency or bulk commits to inflate the contribution graph
- misrepresent development activity to employers, recruiters, or anyone else

Every commit it makes corresponds to a real, inspectable change in the audit
result, and the diff shows exactly what changed.

### How GitHub counts the resulting activity

Commits are authored by `github-actions[bot]`, not by you.

**GitHub does not attribute bot commits to a human contribution graph.** The
`github-actions[bot]` identity is not linked to your user account, so these
commits do **not** produce green squares on your profile. What they do produce is
an accurate, timestamped maintenance record in the repository history and in the
Actions run log.

If your contribution graph fills in, it is because you pushed code — which is the
only thing that should fill it in.
