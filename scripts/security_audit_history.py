"""Read-only audit of every Git blob reachable from repository branches/tags.

Outputs paths and counts only, never raw matching credentials or surrounding text.
Run with a full clone/fetch. An unavailable blob makes the audit incomplete.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


PATTERNS = {
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "google_api_key": re.compile(rb"AIza[0-9A-Za-z_-]{25,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "token_in_url": re.compile(rb"(?:[?&](?:key|api_key|client_secret|access_token|refresh_token)=|x-access-token:)(?!\*{3})[A-Za-z0-9_-]{16,}", re.I),
    "secret_assignment": re.compile(rb"\b(?:api_key|client_secret|access_token|refresh_token|password|passwd|token)\b\s*[:=]\s*[\"']([A-Za-z0-9_-]{24,})[\"']", re.I),
}
PERSONAL_EMAIL = re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PRIVATE_IP = re.compile(rb"\b(?:192\.168|10\.\d+|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b")
SENSITIVE_FILENAME = re.compile(
    r"(^|/)(?:\.env(?:\..*)?|id_rsa|id_ed25519|[^/]+\.(?:pem|p12|pfx|key|sqlite|db))$", re.I
)


def main() -> None:
    rows = git("rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    paths: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        sha, _, path = row.partition(" ")
        if path:
            paths[sha].add(path)
    counts: Counter[str] = Counter()
    hits: dict[str, list[dict[str, object]]] = defaultdict(list)
    incomplete = []
    blobs = 0
    bytes_scanned = 0

    for sha, names in paths.items():
        try:
            obj_type = git("cat-file", "-t", sha).decode().strip()
            if obj_type != "blob":
                continue
            raw = git("cat-file", "-p", sha)
        except (subprocess.CalledProcessError, OSError):
            incomplete.append(sha[:12])
            continue
        blobs += 1
        bytes_scanned += len(raw)
        file_name = sorted(names)[0]
        for name in names:
            if SENSITIVE_FILENAME.search(name):
                counts["sensitive_filename"] += 1
                if len(hits["sensitive_filename"]) < 15:
                    hits["sensitive_filename"].append({"path": name, "blob": sha[:12]})
        for kind, pattern in PATTERNS.items():
            matches = list(pattern.finditer(raw))
            if kind == "secret_assignment":
                matches = [
                    match for match in matches
                    if not any(
                        example in match.group(1).lower()
                        for example in (b"test", b"example", b"placeholder", b"dummy")
                    )
                ]
            if matches:
                counts[kind] += len(matches)
                if len(hits[kind]) < 15:
                    hits[kind].append({"path": file_name, "blob": sha[:12], "count": len(matches)})
        emails = {
            match.group().lower()
            for match in PERSONAL_EMAIL.finditer(raw)
            if b"noreply.github.com" not in match.group().lower()
            and b"example." not in match.group().lower()
            and b"localhost" not in match.group().lower()
        }
        if emails:
            counts["non_noreply_email"] += len(emails)
            if len(hits["non_noreply_email"]) < 15:
                hits["non_noreply_email"].append({"path": file_name, "blob": sha[:12], "count": len(emails)})
        internal_ips = set(PRIVATE_IP.findall(raw))
        if internal_ips:
            counts["private_ip"] += len(internal_ips)
            if len(hits["private_ip"]) < 15:
                hits["private_ip"].append({"path": file_name, "blob": sha[:12], "count": len(internal_ips)})

    report = {
        "reachable_git_objects": len(rows),
        "unique_blob_paths": len(paths),
        "scanned_blobs": blobs,
        "bytes_scanned": bytes_scanned,
        "incomplete_object_count": len(incomplete),
        "incomplete_object_sha_prefixes": incomplete[:15],
        "finding_counts": counts,
        "finding_paths_only": hits,
        "raw_values_printed": False,
    }
    print("SECURITY_HISTORY_AUDIT " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    critical = set(PATTERNS) | {"sensitive_filename"}
    if incomplete or any(counts[kind] for kind in critical):
        raise SystemExit("History needs human review before public visibility")
    print("SECURITY_HISTORY_AUDIT_PASS: no matching credential patterns or sensitive filenames")


if __name__ == "__main__":
    main()
