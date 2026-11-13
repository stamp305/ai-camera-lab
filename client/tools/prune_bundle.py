"""Build step: keep only what the repo accounts for in the assembled bundle.

    python tools/prune_bundle.py <bundle app dir>           # delete the rest
    python tools/prune_bundle.py <bundle app dir> --check   # report only, exit 1 if any

build_mac_app.sh copies the working copy and then excludes what it knows about,
so anything else lying in the build Mac's checkout went into the app: a .venv,
an img/ folder of test pictures and scripts, and the .so of every module ever
compiled there, long after its source was deleted (data_service/api/hotzone.so
and two dozen more shipped that way). This turns the copy around: a file stays
only if git tracks it, if it is the compiled form of a tracked .py/.pyx, or if
it is one of the few files the build itself makes.

python_env, bin and db_support are copied from outside git on purpose and are
left alone. Not shipped: tools/ is excluded from builds.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Copied in from outside git by build_mac_app.sh itself.
OUTSIDE_GIT = {"python_env", "bin", "db_support"}

# Made by the build, never tracked: the version, the secrets, the sealed web UI
# (tools/pack_ui_assets.py) and secure_env, generated from _secrets.py.
BUILD_MADE = {
    "_build_info.so",
    "_secrets.so",
    "frontend_web/assets.bin",
    "data_service/configs/secure_env.cpython-312-darwin.so",
}


def tracked_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True, check=True).stdout
    return {p for p in out.decode("utf-8", "surrogateescape").split("\0") if p}


def strays(app_dir):
    """Every file under app_dir the repo does not account for, as relative paths."""
    tracked = tracked_files()
    found = []
    for dirpath, dirnames, filenames in os.walk(app_dir):
        rel_dir = os.path.relpath(dirpath, app_dir)
        rel_dir = "" if rel_dir == "." else rel_dir
        if not rel_dir:
            dirnames[:] = [d for d in dirnames if d not in OUTSIDE_GIT]
        for name in filenames:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if rel in tracked or rel in BUILD_MADE:
                continue
            stem, ext = os.path.splitext(rel)
            if ext == ".so" and (stem + ".py" in tracked or stem + ".pyx" in tracked):
                continue
            found.append(rel)
    return sorted(found)


def remove_empty_dirs(app_dir):
    for dirpath, dirnames, filenames in os.walk(app_dir, topdown=False):
        rel = os.path.relpath(dirpath, app_dir)
        if rel == "." or rel.split(os.sep)[0] in OUTSIDE_GIT:
            continue
        if not os.listdir(dirpath):
            os.rmdir(dirpath)


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2])
        return 2
    app_dir = sys.argv[1]
    check = "--check" in sys.argv[2:]
    found = strays(app_dir)
    if check:
        for rel in found[:20]:
            print(f"      {rel}")
        return 1 if found else 0
    by_top = {}
    for rel in found:
        os.remove(os.path.join(app_dir, rel))
        top = rel.split(os.sep)[0]
        by_top[top] = by_top.get(top, 0) + 1
    remove_empty_dirs(app_dir)
    if found:
        summary = ", ".join(f"{top} {n}" for top, n in sorted(by_top.items(), key=lambda kv: -kv[1]))
        print(f"  Removed {len(found)} file(s) git does not account for: {summary}")
    else:
        print("  Nothing outside git in the bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
