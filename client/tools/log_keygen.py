"""Make the key pair that seals the client's logs (log_seal.py).

    python tools/log_keygen.py OUT_DIR

Writes OUT_DIR/log_seal_private.pem (mode 600) and prints the public key to
paste into log_seal.SERVER_PUBLIC_KEY. The private key never goes into the app
or into git: it belongs on the server that opens the logs (tools/log_open.py
for now). Lose it and every log sealed to its public key is unreadable; a new
pair only applies to logs written after a build with the new public key.

Not shipped: tools/ is excluded from builds.
"""
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out_dir = sys.argv[1]
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = os.path.join(out_dir, "log_seal_private.pem")
    if os.path.exists(path):
        print(f"{path} already exists; not overwriting it.")
        return 1
    key = X25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(f"private key: {path}")
    print(f'SERVER_PUBLIC_KEY = "{base64.b64encode(public).decode()}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
