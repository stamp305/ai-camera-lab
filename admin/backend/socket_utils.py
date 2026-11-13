# backend/socket_utils.py
"""Socket communication utilities for message passing."""
from __future__ import annotations

import json
import struct
from typing import Any, Dict, Optional


def send_msg(sock, msg_dict: Dict[str, Any]) -> None:
    """Send a JSON message with a length prefix."""
    msg_json = json.dumps(msg_dict).encode('utf-8')
    msg_len = struct.pack('>I', len(msg_json))
    sock.sendall(msg_len + msg_json)


def recv_msg(sock) -> Optional[Dict[str, Any]]:
    """Receive a JSON message with a length prefix."""
    raw_msglen = _recvall(sock, 4)
    if not raw_msglen:
        return None
    msglen = struct.unpack('>I', raw_msglen)[0]
    raw_msg = _recvall(sock, msglen)
    if not raw_msg:
        return None
    return json.loads(raw_msg.decode('utf-8'))


def _recvall(sock, n: int) -> Optional[bytearray]:
    """Receive exactly n bytes from socket."""
    data = bytearray()
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                return None
            data.extend(packet)
        except ConnectionResetError:
            return None
    return data
