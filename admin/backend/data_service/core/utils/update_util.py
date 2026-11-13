import socket
import json
import struct
from typing import Any, Dict, List

from port_config import SOCKET_PORTS
from data_service.core.logging_config import get_logger

SERVER_HOST = '127.0.0.1'
logger = get_logger(__name__)



# --------------------------------------------------------------------------------------
# Helper functions for sending and receiving messages
# --------------------------------------------------------------------------------------
def _send_msg(sock: socket.socket, msg_dict: Dict[str, Any]) -> None:
    """Send a JSON message prefixed with its length (big-endian 4 bytes)."""
    msg_json = json.dumps(msg_dict).encode('utf-8')
    msg_len = struct.pack('>I', len(msg_json))
    sock.sendall(msg_len + msg_json)

def _recv_msg(sock: socket.socket):
    """Receive a length‑prefixed JSON message and return decoded dict (or None)."""
    raw_msglen = _recvall(sock, 4)
    if not raw_msglen:
        return None
    msglen = struct.unpack('>I', raw_msglen)[0]
    raw_msg = _recvall(sock, msglen)
    if not raw_msg:
        return None
    return json.loads(raw_msg.decode('utf-8'))

def _recvall(sock: socket.socket, n: int):
    """Receive exactly n bytes or return None if connection closes early."""
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

def _send_api_command(command_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Connect to the update server, send a command, and log the response."""
    port = int(SOCKET_PORTS['update_control'])
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            logger.info("Connecting to update API at %s:%s", SERVER_HOST, port)
            s.connect((SERVER_HOST, port))

            logger.info("Sending command: %s", command_payload)
            _send_msg(s, command_payload)

            logger.debug("Waiting for response ...")
            response = _recv_msg(s)

            if response:
                status = response.get('status', 'N/A').upper()
                message = response.get('message', 'No message received.')
                logger.info("Server response | status=%s | message=%s", status, message)
                return response
            else:
                logger.warning("No response received from update server")
                return {"status": "error", "message": "No response received from update server"}

    except ConnectionRefusedError:
        logger.error("Connection refused. Is the server running on port %s?", port)
        return {"status": "error", "message": "Connection refused. Server may be down."}
    except Exception as e:
        logger.exception("Unexpected error while sending update command")
        return {"status": "error", "message": str(e)}


# --------------------------------------------------------------------------------------
# Core functions for sending update requests
# --------------------------------------------------------------------------------------
def send_update_request(activation_code: str) -> None:
    """Send an update request for a specific client."""
    command_payload = {"command": "update_client", "client_id": activation_code}
    _send_api_command(command_payload)

def send_update_all_request() -> None:
    """Broadcast update request for all clients."""
    command_payload = {"command": "broadcast_updates"}
    _send_api_command(command_payload)

def send_update_client_by_service_id(activation_code: str, dlc_folder_list: List[str]) -> Dict[str, Any]:
    command_payload = {
        "command": "update_client_by_id_service",
        "client_id": activation_code,
        "list_folder_dlc": dlc_folder_list
    }
    return _send_api_command(command_payload)