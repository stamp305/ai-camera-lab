# Security work for the company client (kept here for now)

Files written for the Alitech portal's AI Camera > Security section, taken out of the company
client repo on 2026-11-27 so they are not lost. **Only these files are here, not the company
repo or its history.**

| File | What it is |
|---|---|
| `live_agent.py` | Live / playback video for the admin over the branch's own Socket.IO link (MPEG-TS chunks, `live_chunk`), or WHIP; NVR login never leaves the machine; waits 25 s for a dropped link; 720p full picture, sub 560k; reports the shop's IT/MONO areas |
| `branch_features.py` | tells the admin yes/no what the branch is ready for (live, alerts, siren, siren paired) |
| `lan_probe.py` | the portal's "same network?" check (one-use ticket, store code, its origin only) |
| `siren_setup/` | Tuya siren setup page on 127.0.0.1:8767 (QR pairing, find on the LAN, sound/volume, test ring), run inside the client; `siren.py` is the copy of the admin's `monitor_service/siren.py` |
| `wiring.patch` | the edits to `client_logic.py`, `build_cython.py` and `requirements.txt`, against company client commit `ec3b75c` (`git apply`) |
| `tests/` | `test_live_agent.py`, `test_siren_setup.py` (run from the client's `tests/` folder) |

Put back into the client: copy the files next to `client_logic.py` (`siren_setup/` as a folder),
`git apply wiring.patch`, `pip install qrcode==8.2 tuya-device-sharing-sdk==0.2.15 paho-mqtt==2.2.0`.
The client also needs ffmpeg with the Mac's H.264 encoder (Homebrew's; the one bundled in the
app misses a library). The admin side (`live_service.py`, MediaMTX config) and Alitech live in
their own company repos.

Settings read here: `AICAM_LIVE_MAX_BACKLOG_S` (default 4), `AICAM_LIVE_MAX_STREAMS`,
`AICAM_LIVE_MAX_MAIN`, `SIREN_STORE`, `TUYA_USER_CODE` (bench only; the admin sends it with each
QR request in production). The siren's key file stays under
`~/Library/Application Support/AICamera/siren/` (0700 / 0600), never in `log/`.
