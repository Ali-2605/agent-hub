"""Delete files older than N days in a folder (example of a destructive task)."""
import argparse
import time
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--dir", required=True)
p.add_argument("--days", type=int, default=7)
a = p.parse_args()

cutoff = time.time() - a.days * 86400
removed = 0
for f in Path(a.dir).expanduser().rglob("*"):
    if f.is_file() and f.stat().st_mtime < cutoff:
        f.unlink()
        removed += 1
print(f"removed {removed} file(s) older than {a.days} days from {a.dir}")
