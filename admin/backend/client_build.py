"""Cython compile routines shared by the admin backend and the Mac builder.

The Linux controller never compiles (macOS .so can only be produced on a
Mac). It hands an extracted/overlaid version tree to the Mac builder, which
runs these routines to turn the payload into compiled .so and uploads it
back. Ported from ``managed_services_manager.py`` so the behaviour stays identical.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Top-level packages the client loads as runtime services.
PAYLOAD_SERVICE_ROOTS = {
    "data_service",
    # The client's own build_cython.py has COMPILE_DIRS = data_service +
    # frontend_web; frontend_web is reached via importlib.import_module("main")
    # so a .so loads exactly like the .py did. (frontend_python is NOT here:
    # run_frontend_service() opens main.py by explicit path.)
    "frontend_web",
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

_LEVEL_A_MODULES = (("client.py", "client_core"), ("camera_worker_bootstrap.py", None))


def collect_payload_files(root: str) -> Tuple[List[str], List[str]]:
    files: List[str] = []
    ext_names: List[str] = []
    for pkg in PAYLOAD_SERVICE_ROOTS:
        pkg_dir = os.path.join(root, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for dirpath, _dirs, filenames in os.walk(pkg_dir):
            for fn in filenames:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    files.append(rel)
                    ext_names.append(rel[:-3].replace(os.sep, "."))
    return files, ext_names


def _run_cython(root: str, ext_entries: str, cython_python: str, setup_name: str) -> None:
    setup_path = os.path.join(root, setup_name)
    with open(setup_path, "w") as f:
        f.write(
            "from setuptools import setup, Extension\n"
            "from Cython.Build import cythonize\n"
            f"setup(ext_modules=cythonize([{ext_entries}], compiler_directives={{'language_level': '3', 'binding': True, 'annotation_typing': False}}), script_args=['build_ext', '--inplace'])\n"
        )
    try:
        proc = subprocess.run(
            [cython_python, setup_path, "build_ext", "--inplace"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=900,
        )
    finally:
        if os.path.exists(setup_path):
            os.remove(setup_path)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-25:]
        raise RuntimeError(f"Cython compile failed in '{root}':\n" + "\n".join(tail))


def _clean_service_roots(root: str) -> None:
    for pkg in PAYLOAD_SERVICE_ROOTS:
        pkg_dir = os.path.join(root, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for dirpath, _dirs, filenames in os.walk(pkg_dir):
            for fn in list(filenames):
                full = os.path.join(dirpath, fn)
                if fn.endswith(".py") or fn.endswith(".c"):
                    os.remove(full)
                elif ".cpython-" in fn and fn.endswith(".so"):
                    os.rename(full, os.path.join(dirpath, fn.split(".cpython")[0] + ".so"))
            for d in list(os.listdir(dirpath)):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
    build_dir = os.path.join(root, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def compile_payload_packages(root: str, cython_python: str) -> int:
    """Compile service-package .py files under a version tree to .so.

    Returns the number of modules compiled. Leaves only compiled extensions
    inside the service roots (top-level launchers are untouched).
    """
    files, ext_names = collect_payload_files(root)
    if not files:
        return 0
    ext_entries = ", ".join(
        f"Extension({name!r}, [{path!r}])" for name, path in zip(ext_names, files)
    )
    logger.info("[CLIENT BUILD] Cython-compiling %d service module(s) in %s ...", len(files), root)
    _run_cython(root, ext_entries, cython_python, "_cython_setup.py")
    _clean_service_roots(root)
    logger.info("[CLIENT BUILD] Compiled %d service module(s) to .so in %s", len(files), root)
    return len(files)


def compile_top_level(root: str, cython_python: str) -> int:
    """Compile the top-level controller + camera worker bootstrap (Level A).

    ``client.py`` becomes ``client_core.so`` with a thin plaintext launcher;
    ``camera_worker_bootstrap.py`` is removed entirely once compiled.
    """
    top_entries = []
    for fname, stem in _LEVEL_A_MODULES:
        if os.path.exists(os.path.join(root, fname)):
            top_entries.append((fname, stem or fname[:-3]))
    if not top_entries:
        return 0
    ext_entries = ", ".join(
        f"Extension({stem!r}, [{fname!r}])" for fname, stem in top_entries
    )
    logger.info("[CLIENT BUILD] Level A: compiling %d top-level module(s) ...", len(top_entries))
    _run_cython(root, ext_entries, cython_python, "_cython_top_setup.py")
    for fname, stem in top_entries:
        compiled = os.path.join(root, f"{stem}.cpython-312-darwin.so")
        target_so = os.path.join(root, f"{stem}.so")
        if os.path.exists(compiled):
            os.replace(compiled, target_so)
        elif not os.path.exists(target_so):
            raise RuntimeError(f"Level A: compiled extension not found for '{fname}'")
    for fname, stem in top_entries:
        src = os.path.join(root, fname)
        if stem == "client_core":
            with open(src, "w") as f:
                f.write(
                    "import sys\n"
                    "from client_core import main\n\n"
                    "if __name__ == '__main__':\n"
                    "    sys.exit(main())\n"
                )
        else:
            if os.path.exists(src):
                os.remove(src)
        gen = os.path.join(root, fname[:-3] + ".c")
        if os.path.exists(gen):
            os.remove(gen)
    build_dir = os.path.join(root, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    logger.info("[CLIENT BUILD] Level A: compiled %d top-level module(s) to .so in %s", len(top_entries), root)
    return len(top_entries)


def compile_version_payload(root: str, cython_python: str) -> int:
    """Compile a version tree: service packages then top-level (Level A)."""
    count = compile_payload_packages(root, cython_python)
    compile_top_level(root, cython_python)
    return count
