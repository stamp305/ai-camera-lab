# backend/dlc_monitor.py
"""DLC folder change monitoring."""
from __future__ import annotations

import time

from config import DLC_MASTER_PATH, load_dlc_state, save_dlc_state
from dlc_manager import get_master_manifest


def monitor_dlc_folder_changes() -> None:
    """Background thread that periodically checks the dlc_master folder for changes."""
    POLL_INTERVAL = 15  # Check every 15 seconds
    print(f"[DLC MONITOR] Starting DLC folder monitoring (checking every {POLL_INTERVAL} seconds).")
    
    # Load the state from the file when the thread starts
    last_known_manifest = load_dlc_state()

    while True:
        try:
            # Get the current, live state of the dlc_master folder
            current_manifest = get_master_manifest(DLC_MASTER_PATH)
            
            # Compare the live state with the last known state
            if current_manifest != last_known_manifest:
                print("\n[DLC MONITOR] Change detected in dlc_master directory!")
                print("[DLC MONITOR] Change detected; clients will see updated folders via /dlc-folders API.")
                
                # Update the state for the next check
                save_dlc_state(current_manifest)
                last_known_manifest = current_manifest
                print("[DLC MONITOR] New DLC state has been saved.")

        except Exception as e:
            print(f"[DLC MONITOR] ERROR: An exception occurred during check: {e}")
        
        # Wait for the next poll
        time.sleep(POLL_INTERVAL)
