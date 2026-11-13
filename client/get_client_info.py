from typing import Optional
import subprocess
import re

def _get_serial_number() -> Optional[str]:
    """Best-effort fetch of host serial number (macOS). Returns None on failure."""
    try:
        output = subprocess.check_output(["system_profiler", "SPHardwareDataType"], text=True)
        match = re.search(r"Serial Number \(system\):\s+(\S+)", output)
        return match.group(1) if match else None
    except Exception:
        return None

def _get_mac_address() -> Optional[str]:
    """The MAC the client presents on activation — register this one on the admin.

    getmac.get_mac_address() used to be here; on Apple silicon it returns an
    internal anpi*/bridge address, not the card the client reports.
    """
    from mac_utils import get_canonical_mac_address
    return get_canonical_mac_address()

if __name__ == "__main__":
    print("Device info:")
    print("\tSerial number: ", _get_serial_number())
    print("\tMAC address: ", _get_mac_address())