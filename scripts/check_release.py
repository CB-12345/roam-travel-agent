"""Check the actual Git index for accidental private files and broken public docs.

Does not publish, commit, contact GitHub, or grant a source-code license.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".idea",
    "data",
    "memory",
    "tool-output",
}
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"
)


def private_path(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1].lower()
    return bool(
        set(parts) & FORBIDDEN_PARTS
        or (name.startswith(".env") and name != ".env.example")
        or name.endswith(
            (".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".pyc", ".bak")
        )
    )


def local_credentials() -> list[bytes]:
    # Only compare actual configured credentials; never print their values.
    path = ROOT / ".env"
    if not path.exists():
        return []
    from dotenv import dotenv_values

    return [
        value.encode()
        for key, value in dotenv_values(path).items()
        if value
        and (key.endswith("API_KEY") or key.endswith("TOKEN"))
        and len(value) >= 12
    ]


def check_index() -> dict:
    actual_root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True
        ).strip()
    ).resolve()
    if actual_root != ROOT:
        raise ValueError("项目尚未整理为独立 Git 仓库。")
    files = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode("utf-8")
        .strip("\0")
        .split("\0")
    )
    files = [path for path in files if path]
    if not files:
        raise ValueError("Git 暂存区为空，请先暂存准备发布的项目文件。")
    indexed = {}
    issues = []
    known = local_credentials()
    for path in files:
        if private_path(path):
            issues.append({"file": path, "issue": "private_or_generated_file"})
        content = subprocess.check_output(["git", "show", ":" + path], cwd=ROOT)
        indexed[path] = content
        if SECRET.search(content) or any(value in content for value in known):
            issues.append({"file": path, "issue": "possible_credential_value"})
        if len(content) > 10 * 1024 * 1024:
            issues.append({"file": path, "issue": "unexpected_large_file"})
    for required in [
        "README.md",
        "NOTICE.md",
        ".env.example",
        ".gitignore",
        ".github/workflows/ci.yml",
    ]:
        if required not in indexed:
            issues.append({"file": required, "issue": "missing_release_file"})
    for name, content in indexed.items():
        if not name.endswith(".md"):
            continue
        for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", content.decode("utf-8-sig")):
            if "://" in link or link.startswith(("#", "mailto:")):
                continue
            link = link.split("#")[0]
            target = (ROOT / name).parent / link
            try:
                relative = target.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                issues.append({"file": name, "issue": "local_link_outside_project"})
                continue
            if relative not in indexed:
                issues.append(
                    {"file": name, "issue": "link_not_in_release", "target": relative}
                )
    return {
        "files": len(files),
        "bytes": sum(map(len, indexed.values())),
        "passed": not issues,
        "issues": issues,
        "license_status": "Direct upstream redistribution terms remain unspecified; see NOTICE.md.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = check_index()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc))
        return 1
    if args.output:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
