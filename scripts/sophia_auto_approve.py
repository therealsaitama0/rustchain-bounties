#!/usr/bin/env python3
"""Sophia conservative auto-approve for merged-PR RTC awards.

This is the SAFE auto-award path. It does NOT instantly award arbitrary
amounts. It auto-approves only the conservative, low-risk tier, dispatches
the transfer through the pending ledger (which has a built-in 24-hour void
window), and tags the maintainer so anything wrong can be reversed before
it confirms.

Two-stage safety model:
  1. Approve  -- this script auto-approves ONLY low-risk PRs (docs/test/small,
                 non-bot, no consensus-path files). Everything else is left
                 for a human `Payment: N RTC` directive (handled by
                 auto-pay.py).
  2. Reverse  -- the transfer lands as `pending` with a 24h confirm window.
                 The maintainer can void it before it confirms.

Anything bigger than the conservative tier, or touching node/consensus code,
is deliberately NOT auto-approved here.

Environment (set by the workflow):
  GITHUB_TOKEN     -- GitHub API token
  PR_NUMBER        -- pull request number
  REPO             -- "owner/repo"
  PR_AUTHOR        -- PR author login
  RTC_VPS_HOST     -- RustChain VPS host
  RTC_ADMIN_KEY    -- admin key for /wallet/transfer
"""

import os
import sys

import requests

GITHUB_API = "https://api.github.com"
VPS_PORT = 8099
FROM_WALLET = "founder_community"

# Conservative auto-approve tier. Only this amount is ever auto-awarded.
# Larger awards require a human `Payment: N RTC` directive (auto-pay.py).
AUTO_TIER_RTC = 10  # Low tier

# Size ceiling for auto-approval. Above this, a human must decide the tier.
AUTO_MAX_CHANGED_LINES = 60

# Path prefixes that must NEVER be auto-approved -- consensus / money / auth
# code always needs a human in the loop regardless of diff size.
SENSITIVE_PREFIXES = (
    "node/",
    "rips/",
    "scripts/auto-pay",
    "scripts/sophia_auto_approve",
    ".github/workflows/",
)

# Idempotency markers. If either appears on the PR, this script no-ops.
AUTO_MARKER = "Sophia-AutoApprove-Done"
HUMAN_MARKER = "RTC-AutoPay-Confirmed"  # auto-pay.py's marker


def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        print(f"::error::Missing required environment variable: {name}")
        sys.exit(1)
    return val


def gh_headers() -> dict:
    return {
        "Authorization": f"token {env('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(path: str) -> list:
    """GET a paginated GitHub collection."""
    out = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}{path}",
            headers=gh_headers(),
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def post_comment(repo: str, pr_number: str, body: str) -> None:
    requests.post(
        f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
        headers=gh_headers(),
        json={"body": body},
        timeout=30,
    ).raise_for_status()


def transfer_rtc(host: str, admin_key: str, to_wallet: str, amount: float, memo: str) -> dict:
    resp = requests.post(
        f"https://{host}/wallet/transfer",
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
        json={
            "from_miner": FROM_WALLET,
            "to_miner": to_wallet,
            "amount_rtc": amount,
            "memo": memo,
        },
        timeout=30,
        verify=False,  # self-signed cert on the VPS
    )
    resp.raise_for_status()
    return resp.json()


def is_bot(author: str) -> bool:
    a = author.lower()
    return a.endswith("[bot]") or a in {"dependabot", "github-actions"}


def main() -> None:
    repo = env("REPO")
    pr_number = env("PR_NUMBER")
    pr_author = env("PR_AUTHOR")
    vps_host = env("RTC_VPS_HOST")
    admin_key = env("RTC_ADMIN_KEY")

    print(f"Sophia auto-approve check: PR #{pr_number} in {repo} (author: {pr_author})")

    # --- Skip bots --------------------------------------------------------
    if is_bot(pr_author):
        print(f"Author {pr_author} is a bot. No award.")
        return

    # --- Idempotency: skip if already handled by either path --------------
    comments = gh_get(f"/repos/{repo}/issues/{pr_number}/comments")
    for c in comments:
        body = c.get("body") or ""
        if AUTO_MARKER in body or HUMAN_MARKER in body:
            print("PR already has an award marker. Skipping.")
            return

    # --- Conservative eligibility check -----------------------------------
    files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files")
    total_changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    paths = [f.get("filename", "") for f in files]

    sensitive = [p for p in paths if p.startswith(SENSITIVE_PREFIXES)]
    if sensitive:
        print(f"PR touches sensitive paths {sensitive[:3]} -- not auto-approving. "
              "A human Payment: directive is required.")
        post_comment(
            repo, pr_number,
            "Sophia auto-approve: **skipped** — this PR touches consensus / "
            "money / workflow code. It needs a maintainer `Payment: N RTC` "
            "directive rather than an automatic award.",
        )
        return

    if total_changed > AUTO_MAX_CHANGED_LINES:
        print(f"PR changes {total_changed} lines (> {AUTO_MAX_CHANGED_LINES}) -- "
              "too large to auto-approve.")
        post_comment(
            repo, pr_number,
            f"Sophia auto-approve: **skipped** — this PR changes "
            f"{total_changed} lines, above the {AUTO_MAX_CHANGED_LINES}-line "
            "conservative auto-tier ceiling. It needs a maintainer "
            "`Payment: N RTC` directive.",
        )
        return

    # --- Eligible: dispatch the conservative tier as a pending transfer ---
    memo = f"PR #{pr_number} in {repo} — Sophia auto-approve (Low tier)"
    print(f"Eligible. Auto-approving {AUTO_TIER_RTC} RTC to {pr_author} "
          f"({total_changed} lines changed).")

    try:
        result = transfer_rtc(vps_host, admin_key, pr_author, AUTO_TIER_RTC, memo)
    except requests.exceptions.RequestException as e:
        print(f"::error::Transfer call failed: {e}")
        sys.exit(1)

    if not result.get("ok", False):
        err = result.get("error", "unknown")
        print(f"::error::Transfer rejected: {err}")
        post_comment(
            repo, pr_number,
            f"Sophia auto-approve: transfer of {AUTO_TIER_RTC} RTC was "
            f"rejected (`{err}`). A maintainer should process this manually.",
        )
        sys.exit(1)

    pending_id = result.get("pending_id", "n/a")
    confirms_in = result.get("confirms_in_hours", 24)

    post_comment(
        repo, pr_number,
        f"## Sophia auto-approve — {AUTO_TIER_RTC} RTC (Low tier)\n\n"
        f"This PR met the conservative auto-award criteria: non-bot author, "
        f"{total_changed} lines changed (≤ {AUTO_MAX_CHANGED_LINES}), no "
        f"consensus / money / workflow files.\n\n"
        f"| Field | Value |\n"
        f"|-------|-------|\n"
        f"| Amount | **{AUTO_TIER_RTC} RTC** |\n"
        f"| Recipient | `{pr_author}` |\n"
        f"| pending_id | `{pending_id}` |\n"
        f"| Status | **pending** — confirms in ~{confirms_in}h |\n\n"
        f"**This is reversible.** The transfer is in the pending ledger with "
        f"a ~{confirms_in}-hour void window. @Scottcjn — if this award is "
        f"wrong, void `pending_id {pending_id}` before it confirms.\n\n"
        f"Larger or sensitive PRs are not auto-approved; they require a "
        f"maintainer `Payment: N RTC` directive.\n\n"
        f"<!-- {AUTO_MARKER} pending_id={pending_id} -->",
    )
    print(f"Auto-approved: {AUTO_TIER_RTC} RTC to {pr_author} (pending_id={pending_id})")


if __name__ == "__main__":
    main()
