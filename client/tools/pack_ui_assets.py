"""Build step: pack the web UI's templates/ and static/ into frontend_web/assets.bin.

    python tools/pack_ui_assets.py          # before the bundle is assembled
    python tools/pack_ui_assets.py --clean  # afterwards

The key is derived from the DLC key in _secrets.py (frontend_web/assets_vault.py).
--clean removes assets.bin from the working copy again: left there, a source
checkout would serve the packed copy instead of the pages being edited.

Not shipped: tools/ is excluded from builds.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "frontend_web")
sys.path.insert(0, ROOT)


def _vault():
    spec = importlib.util.spec_from_file_location("assets_vault_src", os.path.join(UI, "assets_vault.py"))
    vault = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vault)
    return vault


def main():
    vault = _vault()
    try:
        os.remove(vault.BUNDLE)
    except OSError:
        pass
    if "--clean" in sys.argv:
        return 0
    key = vault.key()
    vault.pack(UI, vault.BUNDLE, key)
    with open(vault.BUNDLE, "rb") as f:
        files = vault.unpack(f.read(), key)
    pages = sum(1 for n in files if n.startswith("templates/"))
    print(f"[UI] packed {len(files)} files ({pages} pages) into frontend_web/assets.bin")
    return 0 if pages else 1


if __name__ == "__main__":
    sys.exit(main())
