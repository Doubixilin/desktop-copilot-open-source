"""Publication guard for tracked files; findings show locations, never secret values."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BAD_PARTS = {".scratch", ".zcode", ".video_agent", "private", "local-data", "fonts", "bin", "obj", "__pycache__", "node_modules"}
BAD_SUFFIX = {".log", ".sqlite3", ".db", ".jsonl", ".zstd", ".pem", ".key", ".pfx", ".ttf", ".otf", ".exe", ".dll"}
BAD_NAMES = {"api-token", "copilot.json", "copilot_agents.json", "quota.json", "dida.json", ".env"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "api_secret": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "aws_access_key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "personal_home": re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+(?!(?:example|test|Public|Default)(?:[/\\]|\b))[A-Za-z0-9_-]+", re.I),
    "personal_checkout": re.compile(r"[A-Za-z]:[/\\]+(?:GIThub|local_works)[/\\]", re.I),
}


def main():
    try:
        data = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT)
    except subprocess.CalledProcessError:
        print("Initialize and stage the public snapshot before checking.")
        return 2
    paths = [p for p in data.decode("utf-8").split("\0") if p]
    if not paths:
        print("No tracked/staged files; nothing verified.")
        return 2
    findings = []
    for name in paths:
        p = Path(name)
        if BAD_PARTS.intersection(p.parts) or p.suffix.lower() in BAD_SUFFIX or p.name in BAD_NAMES:
            findings.append({"file":name,"kind":"excluded_path"})
            continue
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            if p.parent.as_posix() != "docs/images":
                findings.append({"file":name,"kind":"unreviewed_image_location"})
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except UnicodeError:
            findings.append({"file":name,"kind":"unexpected_binary"})
            continue
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({"file":name,"line":text.count("\n",0,match.start())+1,"kind":kind})
        if p.suffix == ".md":
            for match in re.finditer(r"!?\[[^\]\n]*\]\(([^)\n]+)\)",text):
                target = match.group(1).split("#")[0]
                if target and not re.match(r"[a-z]+://", target) and not (ROOT / p.parent / target).is_file():
                    findings.append({"file":name,"kind":"missing_local_link","target":target})
    print(json.dumps({"files":len(paths),"findings":findings,"note":"Pattern and filename check only; image review and content review remain necessary."},ensure_ascii=False,indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
