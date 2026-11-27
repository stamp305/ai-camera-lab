"""Siren setup on this branch (Tuya siren): pair, find on the LAN, choose sound/volume, test ring.

Runs inside the client as a loopback-only page (127.0.0.1:8767); the Alitech portal drives it
through the admin -> `siren_call` -> live_agent. The siren's local key stays in
~/Library/Application Support/AICamera/siren/monitor_siren.json (0700 / 0600) and is never
sent to the page, the admin or a log. siren.py is the same file monitor_service rings with.
"""
