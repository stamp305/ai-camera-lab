"""Encrypt a model for DLC master, under the name of the service that uses it.

    python backend/tools/pack_model.py SOURCE SERVICE [INDEX]

    python backend/tools/pack_model.py ~/models/ViT-FaceMask-Finetuned face_service 1
        -> backend/dlc_master/face_service/models/face.bin
    python backend/tools/pack_model.py ~/models/yolov8n-face.pt face_service 2
        -> backend/dlc_master/face_service/models/face_2.bin

SOURCE is the original model file or folder (a CoreML .mlpackage, a Hugging Face
model directory). INDEX picks the slot the service's code reads (see the table
in dlc_master/configs/model_vault.py's callers); 1 is <service>.bin, 2 is
<service>_2.bin, ... The key is derived from the admin's DLC key (config.py
ENCRYPTION_KEY), the same one every branch already has, so one .bin serves all
branches. Keep the originals off the server and out of git: only the .bin goes
into dlc_master (the .gitignore keeps *.bin there out of git as well).
"""
import importlib.util
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLC_MASTER = os.path.join(BACKEND, "dlc_master")


def main():
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        return 2
    source, service = sys.argv[1], sys.argv[2]
    index = int(sys.argv[3]) if len(sys.argv) == 4 else 1
    if not os.path.exists(source):
        print(f"no such file or folder: {source}")
        return 1
    sys.path.insert(0, BACKEND)
    os.environ.setdefault("STATIC_LEGACY_KEY", _admin_key())
    spec = importlib.util.spec_from_file_location("model_vault", os.path.join(DLC_MASTER, "configs", "model_vault.py"))
    vault = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vault)

    stem = service[:-len("_service")] if service.endswith("_service") else service
    dest = os.path.join(DLC_MASTER, service, "models", f"{stem}.bin" if index == 1 else f"{stem}_{index}.bin")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    vault.pack(source.rstrip(os.sep), dest)
    with vault.materialize(dest) as opened:
        ok = os.path.exists(opened)
    print(f"{dest}  ({os.path.getsize(dest) / 1e6:.1f} MB, opens: {ok})")
    return 0 if ok else 1


def _admin_key() -> str:
    spec = importlib.util.spec_from_file_location("admin_config", os.path.join(BACKEND, "config.py"))
    try:
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        key = cfg.ENCRYPTION_KEY
        return key.decode() if isinstance(key, bytes) else key
    except Exception as exc:
        raise SystemExit(f"cannot read ENCRYPTION_KEY from backend/config.py ({exc}); set STATIC_LEGACY_KEY")


if __name__ == "__main__":
    sys.exit(main())
