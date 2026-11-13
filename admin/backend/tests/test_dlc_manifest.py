"""DLC manifest: test files never ship to Branches.

Run (from ai-camera-admin/backend):  python -m pytest tests/test_dlc_manifest.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from dlc_manager import get_master_manifest, is_manifest_junk


def _write(root, relative, text="x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_manifest_lists_service_files_but_not_tests(tmp_path):
    _write(tmp_path, "heatmap_service/main.py")
    _write(tmp_path, "conftest.py")
    _write(tmp_path, "events/conftest.py")
    _write(tmp_path, "events/tests/test_person_lookup.py")
    _write(tmp_path, "tests/test_top.py")

    assert list(get_master_manifest(str(tmp_path))) == ["heatmap_service/main.py"]


def test_junk_check_agrees_with_walk_pruning():
    assert is_manifest_junk("events/tests/test_person_lookup.py")
    assert is_manifest_junk("tests/helpers.py")
    assert is_manifest_junk("events/conftest.py")
    assert not is_manifest_junk("events/person_lookup.py")
    assert not is_manifest_junk("heatmap_service/tests_util.py")
