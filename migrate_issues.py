#!/usr/bin/env python3
"""
Migrate issues (and their comments) from a private source repo to a public
destination repo, e.g. VesperDuck/BGv2 -> VesperDuck/BodyGuardIssuesTraacker.

Since the source is private and the destination is public, this script
supports skipping issues by label (e.g. "internal", "do-not-publish") so
sensitive/internal-only issues never get copied over.

Usage:
    setx GITHUB_TOKEN "ghp_xxx"   (needs 'repo' scope on both source/dest repos)
    python migrate_issues.py --source VesperDuck/BGv2 --dest VesperDuck/BodyGuardIssuesTraacker

Options:
    --skip-label LABEL      Repeatable. Issues with this label are not migrated.
    --state open|closed|all Which issues to pull from source (default: all)
    --dry-run                Print what would be created, without calling the dest API
    --since ISO8601           Only migrate issues updated at/after this timestamp
"""

import argparse
import os
import sys
import time
import urllib.request
import urllib.error
import json

API_ROOT = "https://api.github.com"


def api_request(method, url, token, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    while True:
        try:
            with urllib.request.urlopen(req) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                headers = dict(resp.headers)
                payload = resp.read()
                return resp.status, (json.loads(payload) if payload else None), headers
        except urllib.error.HTTPError as e:
            if e.code == 403 and "rate limit" in e.read().decode("utf-8", "ignore").lower():
                print("Rate limited, sleeping 60s...", file=sys.stderr)
                time.sleep(60)
                continue
            raise


def get_all_pages(url, token):
    items = []
    while url:
        status, data, headers = api_request("GET", url, token)
        items.extend(data)
        url = None
        link = headers.get("Link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.index("<") + 1: part.index(">")]
    return items


def fetch_issues(source_repo, token, state, since):
    url = f"{API_ROOT}/repos/{source_repo}/issues?state={state}&per_page=100&sort=created&direction=asc"
    if since:
        url += f"&since={since}"
    all_issues = get_all_pages(url, token)
    # GitHub's /issues endpoint also returns PRs; filter those out.
    return [i for i in all_issues if "pull_request" not in i]


def fetch_comments(source_repo, issue_number, token):
    url = f"{API_ROOT}/repos/{source_repo}/issues/{issue_number}/comments?per_page=100"
    return get_all_pages(url, token)


def ensure_labels_exist(dest_repo, token, labels, dry_run):
    if not labels:
        return
    status, existing, _ = api_request("GET", f"{API_ROOT}/repos/{dest_repo}/labels?per_page=100", token)
    existing_names = {l["name"] for l in existing}
    for label in labels:
        name = label["name"]
        if name in existing_names:
            continue
        if dry_run:
            print(f"  [dry-run] would create label '{name}'")
            continue
        api_request("POST", f"{API_ROOT}/repos/{dest_repo}/labels", token, {
            "name": name,
            "color": label.get("color", "ededed"),
            "description": label.get("description") or "",
        })
        existing_names.add(name)


def migrate(source_repo, dest_repo, token, skip_labels, state, dry_run, since):
    issues = fetch_issues(source_repo, token, state, since)
    print(f"Fetched {len(issues)} issue(s) from {source_repo} (state={state})")

    skip_labels = set(skip_labels or [])
    migrated, skipped = 0, 0

    for issue in issues:
        labels = issue.get("labels", [])
        label_names = {l["name"] for l in labels}
        if label_names & skip_labels:
            print(f"Skipping #{issue['number']} '{issue['title']}' (labeled: {label_names & skip_labels})")
            skipped += 1
            continue

        ensure_labels_exist(dest_repo, token, labels, dry_run)

        original_url = issue["html_url"]
        author = issue["user"]["login"]
        body_prefix = f"> Migrated from {source_repo}#{issue['number']}, originally opened by @{author}.\n\n"
        new_body = body_prefix + (issue.get("body") or "")

        comments = fetch_comments(source_repo, issue["number"], token)

        print(f"Migrating #{issue['number']} '{issue['title']}' ({len(comments)} comment(s))")

        if dry_run:
            migrated += 1
            continue

        status, created, _ = api_request("POST", f"{API_ROOT}/repos/{dest_repo}/issues", token, {
            "title": issue["title"],
            "body": new_body,
            "labels": sorted(label_names),
        })
        new_number = created["number"]

        for c in comments:
            c_author = c["user"]["login"]
            c_body = f"> Comment by @{c_author} on original issue.\n\n{c.get('body') or ''}"
            api_request("POST", f"{API_ROOT}/repos/{dest_repo}/issues/{new_number}/comments", token, {
                "body": c_body,
            })

        if issue["state"] == "closed":
            api_request("PATCH", f"{API_ROOT}/repos/{dest_repo}/issues/{new_number}", token, {
                "state": "closed",
                "state_reason": issue.get("state_reason") or "completed",
            })

        migrated += 1

    print(f"\nDone. Migrated: {migrated}, Skipped: {skipped}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="VesperDuck/BGv2", help="source owner/repo (private)")
    parser.add_argument("--dest", default="VesperDuck/BodyGuardIssuesTraacker", help="destination owner/repo (public)")
    parser.add_argument("--skip-label", action="append", default=[], help="label name to exclude (repeatable)")
    parser.add_argument("--state", choices=["open", "closed", "all"], default="all")
    parser.add_argument("--since", default=None, help="ISO8601 timestamp; only migrate issues updated since then")
    parser.add_argument("--dry-run", action="store_true", help="print actions without writing to the destination repo")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set the GITHUB_TOKEN environment variable (needs 'repo' scope on both repos).", file=sys.stderr)
        sys.exit(1)

    migrate(args.source, args.dest, token, args.skip_label, args.state, args.dry_run, args.since)


if __name__ == "__main__":
    main()
