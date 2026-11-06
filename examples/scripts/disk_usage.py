"""Show free space and biggest sub-folders. Example of a plain script: no hub code needed."""
import argparse
import os
import shutil
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--path", default="~")
p.add_argument("--top", type=int, default=10)
a = p.parse_args()

root = Path(a.path).expanduser()
total, used, free = shutil.disk_usage(root)
print(f"{root}: {free / 1e9:.1f} GB free of {total / 1e9:.1f} GB")

sizes = []
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink():
        size = 0
        for dirpath, _, files in os.walk(child, onerror=lambda e: None):
            for f in files:
                try:
                    size += os.lstat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
        sizes.append((size, child.name))
for size, name in sorted(sizes, reverse=True)[: a.top]:
    print(f"{size / 1e9:8.2f} GB  {name}")
