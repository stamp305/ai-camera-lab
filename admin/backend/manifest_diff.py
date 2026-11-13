"""Diff two file manifests into add / update / delete sets (P2-2).

A manifest is ``{relative_path: sha256_hex}``. Comparing old -> new tells the
update pipeline exactly which files must be shipped, rewritten, or removed.
Used by the admin side (``managed_services_manager.compare_versions``) and by the
client updater to build an apply plan (backup / rollback support).
"""
from __future__ import annotations

from typing import Dict, List


def diff_manifests(old: Dict[str, str], new: Dict[str, str]) -> Dict[str, List[str]]:
    """Classify files when moving from ``old`` manifest to ``new`` manifest.

    Returns:
        added:   files present in new but absent in old  (ship new)
        updated: files present in both with different hashes (overwrite)
        removed: files present in old but absent in new  (delete on client)
        unchanged: files identical in both (leave untouched)
    """
    old_files = set(old.keys())
    new_files = set(new.keys())

    added = sorted(new_files - old_files)
    removed = sorted(old_files - new_files)
    shared = new_files & old_files
    updated = sorted(f for f in shared if old[f] != new[f])
    unchanged = sorted(f for f in shared if old[f] == new[f])

    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "stats": {
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            "unchanged": len(unchanged),
            "total": len(new_files),
        },
    }
