"""Grab still frames from a branch NVR's recordings, for the lab's Alert & Log pictures.

Runs ON the branch machine (it needs the NVR login, which never leaves that machine):

    DB_NAME=ai_camera python capture_frames.py <jobs.json> <out_dir>

jobs.json: [{"name": "p3d", "camera": 2, "channel": 3, "at": "YYYY-MM-DD HH:MM:SS", "blur": false}, ...]
Writes <out_dir>/<name>.jpg (960 wide, 0600) for each job that has video; prints a JSON
summary {name: "ok" | error} on the last line. The pictures show real people: they are for
the lab only, are kept out of git, and should be deleted when no longer needed.
"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

sys.path.insert(0, os.path.expanduser("~/ai-camera-lab-client"))
import live_agent as la  # noqa: E402


def grab(job: dict, out_dir: str) -> tuple[str, str]:
    name = job["name"]
    if not re.match(r"^[a-z0-9_]{1,32}$", name):
        return name, "bad_name"
    at = datetime.strptime(job["at"], "%Y-%m-%d %H:%M:%S")
    list_path = None
    try:
        list_path = la._private_input(la._rtsp_url(int(job["camera"]), int(job["channel"]), 0, "playback", at, at + timedelta(seconds=6)))
        vf = "scale=960:-2" + (",boxblur=12:2" if job.get("blur") else "")
        out = os.path.join(out_dir, f"{name}.jpg")
        r = subprocess.run([la._tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", *la._input_args(list_path),
                            "-frames:v", "1", "-vf", vf, "-q:v", "4", out], capture_output=True, text=True, timeout=40)
        if r.returncode != 0 or not os.path.exists(out):
            return name, "no_video"
        os.chmod(out, 0o600)
        return name, "ok"
    except la.LiveError as exc:
        return name, exc.code
    except Exception as exc:
        return name, type(exc).__name__
    finally:
        la._remove_input(list_path)


def main() -> None:
    jobs = json.load(open(sys.argv[1]))
    out_dir = sys.argv[2]
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:      # gentle on the NVR
        results = dict(pool.map(lambda j: grab(j, out_dir), jobs))
    print(json.dumps(results))


if __name__ == "__main__":
    main()
