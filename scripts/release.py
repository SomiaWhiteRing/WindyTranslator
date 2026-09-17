#!/usr/bin/env python3
"""Publish the checked-out commit using RELEASE.md and already-built assets."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def run(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        result.check_returncode()
    return result.stdout.strip()


def read_release():
    content = Path("RELEASE.md").read_text(encoding="utf-8-sig")
    heading, separator, notes = content.partition("\n")
    match = re.fullmatch(r"# ([1-9][0-9]{3})\.(1[0-2]|[1-9])\.([1-9][0-9]*)", heading.strip())
    if not match or not separator or not notes.strip():
        raise ValueError("RELEASE.md must start with '# YEAR.MONTH.SEQUENCE' and contain release notes; no leading zeroes.")
    return heading.strip()[2:], notes.strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def version_key(tag):
    value = tag.removeprefix("v")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
        return tuple(int(part) for part in value.split("."))
    return ()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "publish"])
    parser.add_argument("--branch")
    parser.add_argument("--project")
    parser.add_argument("--tag-prefix", default="")
    parser.add_argument("--assets", nargs="+", type=Path)
    args = parser.parse_args()
    version, changelog = read_release()
    print(f"RELEASE.md: {version}", flush=True)
    if args.command == "validate":
        return
    if not args.branch or not args.project or not args.assets:
        parser.error("publish requires --branch, --project and --assets")
    repo = os.environ["GITHUB_REPOSITORY"]
    commit = run("git", "rev-parse", "HEAD")
    if commit != os.environ["GITHUB_SHA"]:
        raise ValueError("Checkout does not match the workflow's pinned commit")
    if os.environ["GITHUB_REF"] != f"refs/heads/{args.branch}":
        raise ValueError("Releases must run on the publishing branch")
    names = [path.name for path in args.assets]
    if len(set(names)) != len(names) or "release-manifest.json" in names:
        raise ValueError("Asset names must be unique and cannot use release-manifest.json")
    for path in args.assets:
        if not path.is_file() or not 0 < path.stat().st_size < 100_000_000:
            raise ValueError(f"Missing, empty or >= 100 MB asset: {path}")

    def gh(*command, timeout=120):
        return run("gh", *command, timeout=timeout)

    def upload(tag, path):
        for attempt in range(1, 4):
            print(f"Uploading {tag}/{path.name} (attempt {attempt}/3)", flush=True)
            try:
                gh("release", "upload", tag, "--repo", repo, "--clobber", str(path), timeout=300)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                if attempt == 3:
                    raise
                print(f"Upload failed: {error}. Retrying this asset in 5 seconds.", flush=True)
                time.sleep(5)

    def paginated(endpoint):
        pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/{endpoint}"))
        return [item for page in pages for item in page]

    def remote_ref(ref):
        output = run("git", "ls-remote", "--refs", "origin", ref)
        return output.split()[0] if output else ""

    def summary(message):
        print(message, flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(message + "\n\n")

    def current():
        if remote_ref(f"refs/heads/{args.branch}") == commit:
            return True
        summary(f"Skipped obsolete build {commit}: {args.branch} has a newer commit.")
        return False

    if not current():
        return
    releases = paginated("releases?per_page=100")
    numbered_tag = args.tag_prefix + version
    latest = not any(
        version_key(release["tag_name"]) > version_key(numbered_tag)
        for release in releases if not release["prerelease"] and not release["draft"]
    )
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    subject = run("git", "log", "-1", "--pretty=%s")
    release_file_hash = digest(Path("RELEASE.md"))

    with tempfile.TemporaryDirectory(prefix="github-release-") as temp:
        for channel, tag in (("nightly", "nightly"), ("release", numbered_tag)):
            if not current():
                return
            staging = Path(temp) / channel
            staging.mkdir()
            assets = []
            for source in args.assets:
                name = source.name if channel == "nightly" else source.name.replace("-nightly", f"-{version}")
                target = staging / name
                shutil.copyfile(source, target)
                assets.append({"name": name, "size": target.stat().st_size, "sha256": digest(target)})
            manifest = {
                "schema_version": 1,
                "project": args.project,
                "repository": repo,
                "version": version,
                "channel": channel,
                "tag": tag,
                "branch": args.branch,
                "commit": commit,
                "workflow_run": run_url,
                "workflow_attempt": int(run_attempt),
                "published_at": published_at,
                "release_file_sha256": release_file_hash,
                "assets": assets,
            }
            manifest_path = staging / "release-manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            notes = staging / "notes.md"
            introduction = (
                f"自动构建：跟随 `{args.branch}` 的最新成功构建。版本文件：`{version}`。"
                if channel == "nightly" else f"{args.project} {version}"
            )
            checksums = "\n".join(f"| `{item['name']}` | {item['size']} | `{item['sha256']}` |" for item in assets)
            notes.write_text(
                f"{introduction}\n\n{changelog}\n\n## 构建来源\n\n"
                f"- 提交：[{commit}]({server}/{repo}/commit/{commit})\n"
                f"- 提交说明：{subject}\n"
                f"- 构建：[运行 {os.environ['GITHUB_RUN_ID']}，第 {run_attempt} 次]({run_url}/attempts/{run_attempt})\n"
                f"- 发布于：{published_at}\n\n"
                "同版本会被后续提交覆盖；请同时记录提交 SHA 和文件 SHA-256。完整构建信息见 `release-manifest.json`。\n\n"
                "| 文件 | 字节数 | SHA-256 |\n| --- | ---: | --- |\n" + checksums + "\n",
                encoding="utf-8",
            )
            existing = next((release for release in releases if release["tag_name"] == tag), None)
            title = f"{args.project} {'Nightly' if channel == 'nightly' else version}"
            ref = f"refs/tags/{tag}"
            expected = remote_ref(ref)
            # Only replace the exact tag state observed by this run.
            run("git", "push", f"--force-with-lease={ref}:{expected}", "origin", f"{commit}:{ref}")
            if existing is None:
                gh("release", "create", tag, "--repo", repo, "--verify-tag", "--draft",
                   "--title", title, "--notes-file", str(notes))
            # Upload the manifest last, after every application asset succeeds.
            for item in assets:
                upload(tag, staging / item["name"])
            upload(tag, manifest_path)
            # The by-tag API only promises published releases; drafts need the list API.
            release = next(item for item in paginated("releases?per_page=100") if item["tag_name"] == tag)
            uploaded = paginated(f"releases/{release['id']}/assets?per_page=100")
            wanted = {item["name"]: item for item in assets}
            wanted[manifest_path.name] = {
                "size": manifest_path.stat().st_size, "sha256": digest(manifest_path),
            }
            for name, item in wanted.items():
                asset = next((candidate for candidate in uploaded if candidate["name"] == name), None)
                if not asset or asset["state"] != "uploaded" or asset["size"] != item["size"]:
                    raise ValueError(f"Uploaded asset is missing or incomplete: {tag}/{name}")
                if asset.get("digest") and asset["digest"] != f"sha256:{item['sha256']}":
                    raise ValueError(f"Uploaded checksum does not match: {tag}/{name}")
            # A release owns exactly these files; remove obsolete names after successful upload.
            for asset in uploaded:
                if asset["name"] not in wanted:
                    gh("release", "delete-asset", tag, asset["name"], "--repo", repo, "--yes")
            gh("release", "edit", tag, "--repo", repo, "--verify-tag", "--target", commit,
               "--title", title, "--notes-file", str(notes), "--draft=false",
               f"--prerelease={'true' if channel == 'nightly' else 'false'}",
               f"--latest={'true' if channel == 'release' and latest else 'false'}")
            summary(f"Published [{title}]({server}/{repo}/releases/tag/{tag}) from `{commit}`.")


if __name__ == "__main__":
    main()
