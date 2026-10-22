"""Check tracked and unignored files without printing potentially sensitive values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {"lib", ".venv", "venv", "__pycache__", "导出文件", "exports", "data", "logs"}
FORBIDDEN_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv", ".json", ".log", ".pem", ".key", ".p12", ".pfx"}
PATTERNS = {
    "Google API credential": r"AIza[0-9A-Za-z_-]{35}",
    "GitHub credential": r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "AWS access identifier": r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "Private key material": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "Bearer credential": r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{20,}",
    "Credential in URL": r"https?://[^\s/:@]+:[^\s/@]+@",
    "Personal local path": r"(?i)(?:[a-z]:[\\/](?:Users|竞品)[\\/]|/(?:Users|home)/[^/\s]+/)",
    "Email address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
}


def main():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    )
    names = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    if not names:
        raise SystemExit("No publication files found.")
    findings = []
    for name in names:
        path = ROOT / name
        lowered = name.lower()
        if (FORBIDDEN_DIRS.intersection(path.relative_to(ROOT).parts)
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or ".sqlite" in lowered or ".db" in lowered
                or path.name == ".env" or path.name.startswith(".env.")
                or any(word in lowered for word in ("cookie", "credential"))):
            findings.append(f"{name}: prohibited runtime/data/secret file")
        if not path.is_file() or path.is_symlink():
            findings.append(f"{name}: missing file or symlink requires review")
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            findings.append(f"{name}: binary file requires manual review")
            continue
        for label, pattern in PATTERNS.items():
            for match in re.finditer(pattern, content):
                line = content[:match.start()].count("\n") + 1
                findings.append(f"{name}:{line}: {label}")
    if findings:
        print("Publication check failed (values suppressed):")
        print("\n".join(findings))
        return 1
    print(f"Publication check passed: {len(names)} text files; no prohibited data files or common credential patterns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
