# backend/managed_services_manager.py
"""Git-based managed services version manager (Bitbucket source).

Versions come from commits of the Bitbucket client repository
(``ai-camera-client``). The admin pulls the commit list, picks which
commits to keep, and each chosen commit is extracted into an isolated
cache folder (``client_code/versions/<version_id>/``) using ``git archive``
so branches can each run an independent version without file conflicts.

A small JSON registry (``client_code/versions/versions.json``) remembers which
commits were stored and under which (possibly custom) version id/label.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import (
    BITBUCKET_DEFAULT_BRANCH,
    BUILD_JOBS_DIR,
    BITBUCKET_MANAGED_SERVICES_SUBDIR,
    BITBUCKET_REPO_URL,
    BUILDER_SIGNING_PUBKEY,
    CYTHON_PYTHON,
    MANAGED_SERVICES_REPO_PATH,
    MANAGED_SERVICES_VERSIONS_CACHE,
)

import client_build  # shared Cython routines (also used by the Mac builder)
import manifest_diff

logger = logging.getLogger(__name__)

_MAX_FIRST_PULL_COMMITS = 500
_REGISTRY_FILE = "versions.json"
_FETCH_STATE_FILE = "fetch_state.json"

# Auto-version numbering: only ids like V.M.m[.b] count towards the running
# max; odd leftovers (e.g. V.a, V.ฟ) are ignored when picking the next number.
_VERSION_RE = re.compile(r"^V\.(\d+)\.(\d+)(?:\.(\d+))?$")
_BUMP_TYPES = ("major", "minor", "bug")

# Mock admin: how a stored version reaches a branch. "so" is the real admin's
# only way (the build farm compiles it, a signed artifact lands in the cache);
# "source" serves the commit's plain .py as it is, with no builder involved.
DELIVERY_MODES = ("so", "source")

# What a "source" tree leaves out — the same list the build worker applies to
# every payload it compiles (worker.py _excluded), so both modes ship the same
# set of files and differ only in .py versus .so.
_SOURCE_EXCLUDE_DIRS = {"python_env", "bin", "db_support", "node_modules", ".git", "__pycache__"}
_SOURCE_EXCLUDE_PREFIXES = ("frontend/.next/", "tools/", "tests/", "no_sleep.app/", "run_ai_camera_client.app/")
_SOURCE_EXCLUDE_FILES = {
    "build_cython.py", "build_phase4.py", "__compile_cython.py",
    "build_mac_app.sh", "build_dmg.sh", "build_pkg.sh",
    "setup_secure_env.py", "generate_secure_env.py",
    "_secrets.example.py", "AICamera.spec", "CLAUDE.md", ".gitignore",
    "tests_remote_tunnel_e2e.py", "TEST_FINDINGS_CHECKLIST.md",
    ".env", ".DS_Store", "client_port_config.py", "custom_tracking.yaml",
}


def _source_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in _SOURCE_EXCLUDE_DIRS for p in parts):
        return True
    if rel.startswith(_SOURCE_EXCLUDE_PREFIXES):
        return True
    return parts[-1] in _SOURCE_EXCLUDE_FILES or parts[-1].startswith("._")

# Top-level packages the client loads as runtime services. Their source is
# Cython-compiled to .so when a version is stored so the payload never
# carries readable Python and branch machines keep running compiled code
# after every update.
_PAYLOAD_SERVICE_ROOTS = {
    "data_service",
    "camera_service",
    "in_pass_service",
    "cus_emp_service",
    "age_gender_service",
    "rate_service",
    "charge_service",
    "hotzone_service",
    "face_service",
    "torchreid",
}


class ManagedServicesManager:
    """Manage Git-tagged/stored managed services versions and their extracted caches."""

    def __init__(self, repo_path: str = MANAGED_SERVICES_REPO_PATH, cache_dir: str = MANAGED_SERVICES_VERSIONS_CACHE):
        self.repo_path = os.path.abspath(repo_path)
        self.cache_dir = os.path.abspath(cache_dir)
        self._extract_lock = threading.Lock()
        self._resolved: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Repo access
    # ------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        """Run a git command inside the managed services repo and return stdout text."""
        cmd = ["git", "-C", self.repo_path, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _git_net(self, *args: str, timeout: int = 180) -> str:
        """Run a git network command non-interactively (no credential prompts)."""
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
        cmd = ["git", "-C", self.repo_path, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _require_repo(self) -> None:
        """Verify the managed services repo exists and is a valid git repository."""
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            raise RuntimeError(
                f"Managed services git repo not found at '{self.repo_path}'. "
                f"Create it or set MANAGED_SERVICES_REPO_PATH in config.py."
            )
        self._git("rev-parse", "--git-dir")  # raises if not a repo

    def _ensure_remote(self) -> None:
        """Init the local repo (if missing) and point origin at Bitbucket."""
        os.makedirs(self.repo_path, exist_ok=True)
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            self._git("init")
        remotes = self._git("remote")
        if "origin" in remotes:
            current = self._git("remote", "get-url", "origin")
            if current != BITBUCKET_REPO_URL:
                self._git("remote", "set-url", "origin", BITBUCKET_REPO_URL)
        else:
            self._git("remote", "add", "origin", BITBUCKET_REPO_URL)

    def ensure_remote(self) -> None:
        """Public wrapper used at server startup (no network performed)."""
        self._ensure_remote()

    def _ref_exists(self, ref: str) -> bool:
        try:
            return bool(self._git("rev-parse", "--verify", "--quiet", ref))
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Version registry (stored commits with optional custom labels)
    # ------------------------------------------------------------------
    def _registry_path(self) -> str:
        return os.path.join(self.cache_dir, _REGISTRY_FILE)

    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self._registry_path(), "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_registry(self, registry: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(self._registry_path(), "w") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)

    def get_registry(self) -> Dict[str, Dict[str, Any]]:
        """Return the stored-version registry."""
        return self._load_registry()

    def _fetch_state_path(self) -> str:
        return os.path.join(self.cache_dir, _FETCH_STATE_FILE)

    def _load_fetch_state(self) -> Dict[str, Dict[str, Any]]:
        """Return the per-branch 'last fetched commit' state."""
        try:
            with open(self._fetch_state_path(), "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_fetch_state(self, state: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(self._fetch_state_path(), "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Bitbucket / remote operations
    # ------------------------------------------------------------------
    def fetch_origin(self, branch: Optional[str] = None, timeout: int = 180) -> None:
        """Fetch remote branch heads into refs/remotes/origin/* (no checkout)."""
        self._ensure_remote()
        if branch:
            spec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        else:
            spec = "+refs/heads/*:refs/remotes/origin/*"
        self._git_net("fetch", "--prune", "origin", spec, timeout=timeout)

    def list_remote_branches(self) -> List[str]:
        """List remote branch names (e.g. ['main', 'develop'])."""
        self._ensure_remote()
        out = self._git_net("ls-remote", "--heads", "origin", timeout=60)
        branches = sorted(
            {
                line.split("refs/heads/")[1]
                for line in out.splitlines()
                if "refs/heads/" in line
            }
        )
        return branches or [BITBUCKET_DEFAULT_BRANCH]

    def list_commits(self, branch: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """List commits from a Bitbucket branch.

        If ``limit`` is given, always returns the newest ``limit`` commits
        (e.g. 1 = latest only, 5/10 = that many commits back). If ``limit``
        is None the original behavior applies: only commits new since the
        last pull are returned (the first pull returns up to
        ``_MAX_FIRST_PULL_COMMITS``). Each commit is marked ``stored=True``
        when a version was already created from it.
        """
        self._require_repo()
        self._ensure_remote()
        branch = branch or BITBUCKET_DEFAULT_BRANCH

        state = self._load_fetch_state()
        prev = (state.get(branch) or {}).get("last_fetched")
        last_fetch_time = (state.get(branch) or {}).get("fetched_at")
        import time as _time
        skip_fetch = False
        if last_fetch_time:
            try:
                from datetime import datetime as _dt
                elapsed = _time.time() - _dt.fromisoformat(last_fetch_time).timestamp()
                if elapsed < 60:
                    skip_fetch = True
            except Exception:
                pass
        if not skip_fetch:
            self.fetch_origin(branch)
        head = self._git("rev-parse", f"refs/remotes/origin/{branch}")

        fmt = "%H%x00%an%x00%ae%x00%aI%x00%s"
        lines: List[str] = []
        if limit is not None:
            # Explicit limit: always show the newest N commits.
            out = self._git("log", "-n", str(limit), f"--pretty=format:{fmt}", head)
            lines = out.splitlines()
        elif prev:
            if prev != head:
                # Only commits added since the last pull (oldest first, then reversed)
                out = self._git("log", "--reverse", f"--pretty=format:{fmt}", f"{prev}..{head}")
                lines = [ln for ln in reversed(out.splitlines())]
        else:
            # First pull: everything reachable from the branch head (newest first)
            out = self._git("log", "-n", str(_MAX_FIRST_PULL_COMMITS), f"--pretty=format:{fmt}", head)
            lines = out.splitlines()

        state[branch] = {"last_fetched": head, "fetched_at": datetime.now().isoformat()}
        self._save_fetch_state(state)

        stored_commits = {
            rec.get("commit") for rec in self._load_registry().values()
        }
        commits: List[Dict[str, Any]] = []
        for line in lines:
            parts = line.split("\x00", 4)
            if len(parts) < 5:
                continue
            hash_, author, email, iso, subject = parts
            commits.append({
                "hash": hash_,
                "short": hash_[:10],
                "id": hash_[:10],
                "author": author,
                "email": email,
                "date": iso,
                "message": subject,
                "stored": hash_ in stored_commits,
            })
        return commits

    def _commit_info(self, ref: str) -> Dict[str, str]:
        out = self._git("log", "-1", "--pretty=format:%an%x00%ae%x00%aI%x00%s", ref)
        parts = out.split("\x00", 3)
        return {
            "author": parts[0],
            "email": parts[1] if len(parts) > 1 else "",
            "date": parts[2] if len(parts) > 2 else "",
            "message": parts[3] if len(parts) > 3 else "",
        }

    # ------------------------------------------------------------------
    # Auto version numbering (V.major.minor.bug)
    # ------------------------------------------------------------------
    def _parse_version(self, vid: str) -> Optional[Tuple[int, int, int]]:
        """Parse a version id into (major, minor, bug); None if not numeric."""
        m = _VERSION_RE.match(vid or "")
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))

    def latest_stored_version(self) -> Tuple[int, int, int]:
        """Return the highest stored numeric version as (major, minor, bug)."""
        best = (0, 0, 0)
        for vid in self._load_registry():
            parsed = self._parse_version(vid)
            if parsed and parsed > best:
                best = parsed
        return best

    def generate_version(
        self,
        bump: str,
        base: Optional[Tuple[int, int, int]] = None,
    ) -> str:
        """Return the next version label for a bump type.

        major -> V.(M+1).0.0 · minor -> V.M.(m+1).0 · bug -> V.M.m.(b+1).
        ``base`` defaults to the highest stored version, so numbers always
        run forward and never drop below what was already recorded.
        """
        if bump not in _BUMP_TYPES:
            raise ValueError(f"bump ต้องเป็นหนึ่งใน {', '.join(_BUMP_TYPES)} เท่านั้น")
        major, minor, bug = base if base is not None else self.latest_stored_version()
        if bump == "major":
            return f"V.{major + 1}.0.0"
        if bump == "minor":
            return f"V.{major}.{minor + 1}.0"
        return f"V.{major}.{minor}.{bug + 1}"

    def commit_date(self, commit: str) -> str:
        """Return a commit's ISO date (resolved to a full sha)."""
        ref = self._ensure_commit(commit)
        return self._commit_info(ref).get("date", "")

    def _ensure_commit(self, commit: str) -> str:
        """Resolve a commit to a full sha, fetching origin when missing."""
        try:
            return self._git("rev-parse", f"{commit}^{{commit}}")
        except RuntimeError:
            pass
        self.fetch_origin()
        try:
            return self._git("rev-parse", f"{commit}^{{commit}}")
        except RuntimeError:
            raise ValueError(f"Commit '{commit}' not found after fetching origin.")

    # ------------------------------------------------------------------
    # Version listing (dropdown)
    # ------------------------------------------------------------------
    def list_versions(self) -> List[Dict[str, Any]]:
        """Return available versions: stored (Bitbucket) first, then tags.

        Only versions the admin actually stored (plus git tags) appear here;
        un-selected commits are never auto-included. Each entry is
        {id, type, ref, label} where `id` is what the admin assigns to a
        branch and `ref` is the commit sha used by git archive.
        """
        self._ensure_remote()
        self._require_repo()
        versions: List[Dict[str, Any]] = []

        # 1. Stored versions (commits the admin chose to keep)
        # `built` is the build receipt install_artifact() leaves behind. Being
        # in the registry only means the admin picked the commit; without a
        # receipt there is no compiled tree, and pushing it to a branch is
        # refused later on. The list said nothing about that, so a version
        # stored while no builder was connected looked finished.
        try:
            import build_farm_gateway as build_gateway
            is_built = build_gateway.is_version_built
        except Exception:  # gateway not importable — don't take the list down
            is_built = lambda _vid: False  # noqa: E731

        for vid, rec in self._load_registry().items():
            ref = rec.get("commit", "")
            versions.append({
                "id": vid,
                "type": "stored",
                "ref": ref,
                "label": rec.get("label") or vid,
                "commit": ref,
                "author": rec.get("author"),
                "date": rec.get("date"),
                "message": rec.get("message"),
                # A source version needs no build: its plain tree is what ships.
                "mode": rec.get("mode") or "so",
                "built": (rec.get("mode") == "source") or bool(is_built(vid)),
            })

        # 2. Tags with their commit dates (newest first)
        try:
            tag_lines = self._git(
                "for-each-ref", "--sort=-creatordate",
                "--format=%(refname:short) %(objectname) %(creatordate:iso8601)",
                "refs/tags",
            ).splitlines()
            for line in tag_lines:
                name, sha, date = line.split(" ", 2)
                versions.append({
                    "id": name,
                    "type": "tag",
                    "ref": sha,
                    "label": f"{name} ({sha[:10]})",
                })
        except RuntimeError:
            pass
        return versions

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def _resolve_ref(self, version_id: str) -> str:
        """Resolve a version id (tag name or sha prefix) to a full commit sha."""
        if version_id in self._resolved:
            return self._resolved[version_id]

        for candidate in (f"refs/tags/{version_id}^{{commit}}", f"{version_id}^{{commit}}"):
            try:
                full = self._git("rev-parse", candidate)
                if full:
                    self._resolved[version_id] = full
                    return full
            except RuntimeError:
                continue

        raise ValueError(f"Version '{version_id}' not found in managed services repo '{self.repo_path}'.")

    # ------------------------------------------------------------------
    # Cache extraction (git archive)
    # ------------------------------------------------------------------
    def _extract_to(self, version_id: str, ref: str, dest: Optional[str] = None) -> str:
        """Extract the tree of ``ref`` into client_code/versions/<version_id>/.

        When ``BITBUCKET_MANAGED_SERVICES_SUBDIR`` is set and exists in the commit, only
        that sub-tree is archived (with the prefix stripped) so the cache
        folder holds just the managed services files.

        ``dest`` overrides the destination. Rebuilds pass a staging folder so
        the live version cache — which is what branches are served from — is
        not replaced by plaintext source while the builder is still working.
        """
        target = dest or os.path.join(self.cache_dir, version_id)
        subdir = (BITBUCKET_MANAGED_SERVICES_SUBDIR or "").strip("/")

        with self._extract_lock:
            if os.path.isdir(target):
                shutil.rmtree(target)
            os.makedirs(target, exist_ok=True)

            args = ["archive", "--format=tar", ref]
            prefix = None
            if subdir:
                tree = self._git("ls-tree", ref, subdir)
                if tree:
                    args.append(subdir)
                    prefix = subdir + "/"

            logger.info(f"[MANAGED SERVICES] Extracting version '{version_id}' ({ref[:10]}) from {self.repo_path} ...")
            proc = subprocess.run(
                ["git", "-C", self.repo_path, *args],
                capture_output=True,
                timeout=180,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git archive failed for '{version_id}': {proc.stderr.decode('utf-8', 'ignore').strip()}"
                )

            tar_stream = io.BytesIO(proc.stdout)
            with tarfile.open(fileobj=tar_stream, mode="r:") as tf:
                for member in tf.getmembers():
                    name = member.name
                    # Safety: never allow path traversal out of the target dir
                    if name.startswith("/") or ".." in name.split("/"):
                        raise RuntimeError(f"Unsafe path '{name}' inside git archive.")
                    if prefix:
                        if name == prefix.rstrip("/"):
                            continue  # skip the directory entry itself
                        if name.startswith(prefix):
                            member.name = name[len(prefix):]
                        else:
                            continue  # outside the managed services sub-folder
                    tf.extract(member, target)

            logger.info(f"[MANAGED SERVICES] Version '{version_id}' cached at {target}")
            return target

    def get_version_path(self, version_id: str) -> str:
        """Return the cache folder for a version, extracting it on first use."""
        self._require_repo()
        target = os.path.join(self.cache_dir, version_id)

        if os.path.isdir(target) and os.listdir(target):
            return target

        ref = self._resolve_ref(version_id)
        return self._extract_to(version_id, ref)

    # ------------------------------------------------------------------
    # Version payload hardening (overlay + Cython compile)
    # ------------------------------------------------------------------
    @staticmethod
    def _sync_builder_pubkey(target: str) -> None:
        """Stamp the current builder public key into the payload's verifier.

        ``signed_update.py`` runs on the branch machine and embeds the trust
        anchor as a literal, so it silently goes stale whenever the builder's
        signing key is rotated. Rewriting it here means a rotation reaches
        every new payload instead of waiting to be noticed.
        """
        verifier = os.path.join(target, "data_service", "configs", "signed_update.py")
        if not os.path.isfile(verifier):
            return
        try:
            with open(BUILDER_SIGNING_PUBKEY) as f:
                pem = f.read().strip()
            if not pem.startswith("-----BEGIN PUBLIC KEY-----"):
                logger.warning(f"[MANAGED SERVICES] '{BUILDER_SIGNING_PUBKEY}' is not a PEM public key — leaving verifier untouched.")
                return
            with open(verifier) as f:
                src = f.read()
            patched, n = re.subn(
                r'MAC_BUILDER_PUBKEY_PEM = b"""-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----"""',
                'MAC_BUILDER_PUBKEY_PEM = b"""' + pem + '"""',
                src,
                flags=re.DOTALL,
            )
            if not n:
                logger.warning("[MANAGED SERVICES] Could not find MAC_BUILDER_PUBKEY_PEM in signed_update.py.")
                return
            if patched != src:
                with open(verifier, "w") as f:
                    f.write(patched)
                logger.info("[MANAGED SERVICES] Refreshed the builder public key in signed_update.py.")
        except OSError as e:
            logger.warning(f"[MANAGED SERVICES] Could not sync the builder public key: {e}")

    def compile_version_payload(self, root: str) -> int:
        """Cython-compile service-package .py files in a version cache to .so.

        After this pass the cache holds only compiled extensions for the
        client's runtime services — no readable Python. Compiled extensions
        are produced with the interpreter in ``CYTHON_PYTHON`` (CPython 3.12,
        macOS arm64) so they are ABI-compatible with the branch clients.

        ``data_service`` has no ``__init__.py`` (namespace package), so each
        module is registered as an explicit ``Extension`` with its dotted
        name to guarantee the .so lands next to its source.

        The compile logic itself lives in ``client_build.py`` so the same
        routines run on the Mac builder (the Linux controller never compiles
        macOS .so — it hands trees to the Mac worker instead).
        """
        return client_build.compile_version_payload(root, CYTHON_PYTHON)

    def convert_version_to_so(self, version_id: str) -> int:
        """Rebuild a stored version cache as compiled .so (re-extract + overlay + compile).

        Used to migrate caches that were stored before the compile step
        existed (e.g. as encrypted/Fernet payloads).
        """
        registry = self._load_registry()
        if version_id not in registry:
            raise FileNotFoundError(f"Version '{version_id}' not in registry")
        ref = registry[version_id]["commit"]
        self._extract_to(version_id, ref)
        target = os.path.join(self.cache_dir, version_id)
        self._sync_builder_pubkey(target)
        return self.compile_version_payload(target)

    def convert_all_stored_to_so(self) -> Dict[str, int]:
        """Rebuild every stored version cache as compiled .so."""
        registry = self._load_registry()
        result: Dict[str, int] = {}
        for vid in list(registry):
            try:
                result[vid] = self.convert_version_to_so(vid)
            except (FileNotFoundError, OSError, RuntimeError) as e:
                logger.warning(f"[MANAGED SERVICES] convert {vid} to .so failed: {e}")
        return result

    def get_version_manifest(self, version_id: str) -> Dict[str, str]:
        """Return {relative_path: sha256} for all files in a cached version."""
        root = self.get_version_path(version_id)
        manifest: Dict[str, str] = {}
        base_len = len(root.rstrip(os.sep)) + 1
        for dirpath, _dirs, filenames in os.walk(root):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = full[base_len:].replace(os.sep, "/")
                manifest[rel] = _sha256_file(full)
        return manifest

    def compare_versions(self, old_id: str, new_id: str) -> Dict[str, Any]:
        """Diff two stored versions into added / updated / removed sets (P2-2).

        The admin uses this to preview what an update from ``old_id`` to
        ``new_id`` will ship, overwrite, and delete before releasing it.
        """
        old = self.get_version_manifest(old_id)
        new = self.get_version_manifest(new_id)
        result = manifest_diff.diff_manifests(old, new)
        result["old"] = old_id
        result["new"] = new_id
        return result

    # ------------------------------------------------------------------
    # Store / delete versions
    # ------------------------------------------------------------------
    def store_version(
        self,
        commit: str,
        version_id: Optional[str] = None,
        label: Optional[str] = None,
        bump: Optional[str] = None,
        mode: str = "so",
    ) -> Dict[str, Any]:
        """Store a Bitbucket commit as a local version (extract + register).

        ``mode`` (mock admin): "so" orders a build as the real admin does;
        "source" extracts the commit's plain tree into the cache instead.

        ``version_id`` defaults to the short commit hash; the admin may
        supply a custom id/label (e.g. 'V.1.3'). Alternatively pass
        ``bump`` ('major'/'minor'/'bug') to auto-generate the next number
        from the highest stored version.
        """
        self._require_repo()
        if mode not in DELIVERY_MODES:
            raise ValueError(f"mode must be one of {', '.join(DELIVERY_MODES)}")
        ref = self._ensure_commit(commit)
        if bump:
            vid = self.generate_version(bump)
        else:
            vid = (version_id or ref[:10]).strip()
            if not vid:
                raise ValueError("Version id must not be empty.")
            if not vid.startswith("V."):
                raise ValueError("Version id ต้องขึ้นต้นด้วย 'V.' (เช่น V.1.7)")

        # No extract here any more. The version cache holds compiled trees only,
        # written by install_artifact() once the builder's signature verifies —
        # putting plaintext source in it first meant any branch that checked in
        # during the build was served uncompiled code, and forever if the build
        # never finished. Stamping the builder's public key moved to the builder
        # too, where it is derived from the key actually doing the signing.
        # Whether a builder took this matters to the caller: with none
        # connected the version is stored but nothing compiles, and saying
        # "building automatically" then would be a plain lie.
        notified = False
        if mode == "source":
            self._extract_source(vid, ref)
        else:
            try:
                notified = self.publish_to_farm(vid, ref) != "no-builder"
            except Exception as e:
                logger.error(f"[MANAGED SERVICES] Failed to publish to farm: {e}")


        info = self._commit_info(ref)
        registry = self._load_registry()
        registry[vid] = {
            "commit": ref,
            "label": (label or vid),
            "author": info.get("author"),
            "email": info.get("email"),
            "date": info.get("date"),
            "message": info.get("message"),
            "stored_at": datetime.now().isoformat(),
            "mode": mode,
        }
        self._save_registry(registry)
        return {
            "id": vid,
            "type": "stored",
            "ref": ref,
            "label": registry[vid]["label"],
            "stored": True,
            "mode": mode,
            "notified": notified,
            **info,
        }

    def get_mode(self, version_id: str) -> str:
        """How a stored version ships: "so" (compiled, the default) or "source"."""
        return (self._load_registry().get(version_id) or {}).get("mode") or "so"

    def _extract_source(self, version_id: str, ref: str) -> str:
        """Put the commit's plain tree in the cache, minus what no payload carries.

        Replacing the folder also drops any build receipt left from an earlier
        .so build, so nothing can mistake this tree for a compiled one.
        """
        target = self._extract_to(version_id, ref)
        removed = 0
        for dirpath, dirnames, filenames in os.walk(target, topdown=True):
            rel_dir = os.path.relpath(dirpath, target)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            for d in list(dirnames):
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if d in _SOURCE_EXCLUDE_DIRS or (rel + "/").startswith(_SOURCE_EXCLUDE_PREFIXES):
                    shutil.rmtree(os.path.join(dirpath, d))
                    dirnames.remove(d)
                    removed += 1
            for f in filenames:
                rel = f"{rel_dir}/{f}" if rel_dir else f
                if _source_excluded(rel):
                    os.remove(os.path.join(dirpath, f))
                    removed += 1
        logger.info(f"[MANAGED SERVICES] '{version_id}' stored as source ({removed} build-only path(s) left out)")
        return target

    def set_mode(self, version_id: str, mode: str) -> Dict[str, Any]:
        """Switch a stored version between shipping as source and as .so.

        To source: the plain tree replaces whatever is cached, at once.
        To .so: a build is ordered; until its signed artifact lands the version
        counts as unbuilt, so the plain tree still in the cache is never served
        as if it were compiled.
        """
        if mode not in DELIVERY_MODES:
            raise ValueError(f"mode must be one of {', '.join(DELIVERY_MODES)}")
        registry = self._load_registry()
        rec = registry.get(version_id)
        if rec is None:
            raise ValueError(f"Version '{version_id}' is not stored.")
        ref = rec.get("commit") or self._resolve_ref(version_id)
        rec["mode"] = mode
        self._save_registry(registry)

        import build_farm_gateway as build_gateway
        if mode == "source":
            # Old build jobs would keep showing this version as built/building.
            build_gateway.cancel_jobs_for_version(version_id)
            build_gateway.forget_jobs_for_version(version_id)
            self._extract_source(version_id, ref)
            return {"version": version_id, "mode": mode, "notified": False, "builders": 0}
        notified = self.publish_to_farm(version_id, ref) != "no-builder"
        return {"version": version_id, "mode": mode, "notified": notified,
                "builders": build_gateway.connected_builder_count()}

    def publish_to_farm(self, version_id: str, commit: Optional[str] = None) -> str:
        """Order a build of ``version_id`` from the remote Mac builder.

        This side never materialises source any more. It resolves the version
        to a commit sha and names it over Socket.IO; the builder archives that
        commit out of its own clone, stamps its own public key into the
        payload's verifier, compiles to .so, signs and stages the result. The
        only thing that ever lands here is the compiled artifact, installed by
        install_artifact() after its signature verifies.

        ``commit`` defaults to resolving ``version_id`` as a tag/sha.
        """
        self._require_repo()
        ref = self._ensure_commit(commit) if commit else self._resolve_ref(version_id)

        import build_farm_gateway as build_gateway
        reached = build_gateway.notify_build_start(version_id, commit=ref)
        if not reached:
            # Not an exception: the version is still stored and can be rebuilt
            # once a builder connects. Callers surface this via `notified`.
            logger.warning(
                f"[MANAGED SERVICES] '{version_id}' ({ref[:10]}) ordered but no builder is connected."
            )
            return "no-builder"
        logger.info(f"[MANAGED SERVICES] Ordered build of '{version_id}' ({ref[:10]}) "
                    f"from {reached} builder(s)")
        return ref

    def delete_version(self, version_id: str) -> None:
        """Remove a stored version (registry entry + cache folder + jobs)."""
        try:
            import build_farm_gateway as build_gateway

            build_gateway.cancel_jobs_for_version(version_id)
            build_gateway.forget_jobs_for_version(version_id)
        except Exception as e:
            logger.warning(f"[MANAGED SERVICES] Could not clear jobs for '{version_id}': {e}")

        registry = self._load_registry()
        if version_id in registry:
            del registry[version_id]
            self._save_registry(registry)

        target = os.path.join(self.cache_dir, version_id)
        if os.path.isdir(target):
            shutil.rmtree(target)
        self._resolved.pop(version_id, None)

    def rename_version(self, old_id: str, new_id: str) -> Dict[str, Any]:
        """Rename a stored version (registry key + cache folder).

        The admin can retitle a version (e.g. '401a74e9c9' -> 'v1.3') so the
        assign dropdown shows a friendly name instead of the short commit hash.
        """
        new_id = (new_id or "").strip()
        if not new_id:
            raise ValueError("New version id must not be empty.")
        if not new_id.startswith("V."):
            raise ValueError("Version id ต้องขึ้นต้นด้วย 'V.' (เช่น V.1.7)")
        if any(ch in new_id for ch in "/\\") or new_id in (".", ".."):
            raise ValueError("Invalid version id.")
        registry = self._load_registry()
        if old_id not in registry:
            raise ValueError(f"Version '{old_id}' not found.")
        if new_id in registry and new_id != old_id:
            raise ValueError(f"Version '{new_id}' already exists.")

        rec = registry.pop(old_id)
        rec["label"] = new_id
        registry[new_id] = rec
        self._save_registry(registry)

        old_path = os.path.join(self.cache_dir, old_id)
        new_path = os.path.join(self.cache_dir, new_id)
        if os.path.isdir(old_path) and old_path != new_path:
            if os.path.isdir(new_path):
                shutil.rmtree(new_path)
            shutil.move(old_path, new_path)
        self._resolved.pop(old_id, None)
        self._resolved.pop(new_id, None)

        return {
            "id": new_id,
            "type": "stored",
            "ref": rec.get("commit", ""),
            "label": rec.get("label") or new_id,
        }

    def ensure_default_latest(self, default_version_id: Optional[str] = None) -> None:
        """Pre-extract the first available version so the first push is fast."""
        try:
            versions = self.list_versions()
        except RuntimeError:
            return
        if not versions:
            return
        target = default_version_id or versions[0]["id"]
        try:
            self.get_version_path(target)
        except (RuntimeError, ValueError):
            pass


def _sha256_file(filepath: str) -> str:
    """Compute the sha256 hex digest of a file."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()
