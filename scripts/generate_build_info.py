"""Generate the package identity used by the independently managed update website."""

import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def write_build_info(repo_root):
    repo_root = Path(repo_root)
    heading = (repo_root / "RELEASE.md").read_text(encoding="utf-8-sig").splitlines()[0].strip()
    if not re.fullmatch(r"# [1-9][0-9]{3}\.(1[0-2]|[1-9])\.[1-9][0-9]*", heading):
        raise ValueError("Invalid RELEASE.md calendar version")
    try:
        sha = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        commit = sha.stdout.strip() if sha.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        commit = ""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if commit != os.environ.get("GITHUB_SHA") or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Build must match the pinned workflow commit")
        identity = f"windy:{commit}:{os.environ['GITHUB_RUN_ID']}.{os.environ['GITHUB_RUN_ATTEMPT']}"
    else:
        identity = f"windy:local:{uuid.uuid4().hex}"
    data = {"schemaVersion": 1, "version": heading[2:], "applicationBuildId": identity,
            "commit": commit, "target": "windows-x64", "updaterProtocol": 1}
    destination = repo_root / "build" / "build-info.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    print(write_build_info(Path(__file__).resolve().parents[1]))
