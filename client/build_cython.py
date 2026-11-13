#!/usr/bin/env python3
"""Cython build script for AI Camera client.
Compiles all .py files to .so, creates thin launchers for entry points.
Run from the project root.
"""
import os
import shutil
import subprocess
import sys
import tempfile

# Directories to compile recursively
COMPILE_DIRS = ["data_service", "frontend_web"]

# Directories to skip entirely
SKIP_DIRS = {"__pycache__", "python_env", "site-packages", "temp_update",
             "node_modules", ".git", "ViT-FaceMask-Finetuned", "no_sleep.app",
             "run_ai_camera_client.app", "logs", "dlc_local", "managed_services",
             "frontend", "scripts", "tests", "mock_folder", "dead_toplevel",
             ".p4_backup_20260030_dbreset", ".p4_backup_20260807_024404",
             "release", "build", "dist"}

# Top-level .py files to compile (not in COMPILE_DIRS)
TOP_LEVEL_FILES = [
    "bootstrap.py", "client_logic.py", "menubar_app.py", "updater.py",
    "_secrets.py",
    # Pure library modules, never executed directly (only ever `import X`) —
    # clean_source_py() deletes their .py entirely once compiled, no launcher
    # needed since Python's import system prefers the .so automatically.
    "mac_utils.py", "port_manager.py", "get_client_info.py", "port_store.py",
    "device_identity.py", "dlc_manifest.py",
    # Imported by name only: runtime_paths by bootstrap/updater/loading_gui;
    # dlc_import_hook from the bundled site-packages (build_mac_app.sh copies
    # the .so there); loading_gui from the one-line .loading_gui.py launcher.
    "runtime_paths.py", "dlc_import_hook.py", "loading_gui.py",
    # Imported by bootstrap and client_logic to keep camera URLs out of logs,
    # and to seal every log file for the server.
    "log_redact.py", "log_seal.py",
    # Imported by bootstrap when the app is installed over a git-clone branch.
    "legacy_clone.py",
    # The installed version, written by build_mac_app.sh from VERSION right
    # before this runs (version_store reads it first).
    "_build_info.py",
    # client.py is dead code in this app (client_logic.py replaced it as the
    # real entry point, nothing imports it) but still ships as 1600+ lines of
    # readable source — compile it too rather than leave it exposed.
    "client.py",
]

# Files that must stay as .py (not compiled)
# Nothing has to stay readable at runtime any more: the ports that used to
# live in client_port_config.py are rows in the store's database now, so the
# last file that needed to be editable in place is gone.
KEEP_PY = {"redirect_server.py", "setup_secure_env.py", "__compile_cython.py",
           "build_cython.py", "build_mac_app.sh", "build_phase4.py"}

# Entry points that need thin launchers (.py that imports .so)
ENTRY_POINTS = {
    "bootstrap.py": "bootstrap_launcher",
    "client_logic.py": "client_logic",
    "menubar_app.py": "menubar_app",
    "updater.py": "updater",
}


def collect_py_files(root):
    """Collect all .py files recursively, excluding SKIP_DIRS."""
    files = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for e in entries:
            p = os.path.join(d, e)
            if os.path.isdir(p):
                if e not in SKIP_DIRS:
                    stack.append(p)
            elif e.endswith((".py", ".pyx")) and e not in KEEP_PY and not e.startswith("."):
                files.append(p)
    return files


def to_module_name(base, path):
    """Convert absolute path to dotted module name."""
    rel = os.path.relpath(path, base)
    parts = rel.split(os.sep)
    if parts[-1] == "__init__.py" or parts[-1] == "__init__.pyx":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    elif parts[-1].endswith(".pyx"):
        parts[-1] = parts[-1][:-4]
    return ".".join(parts)


def make_setup_script(files, base):
    """Generate a setup.py string for Cython compilation."""
    ext_lines = []
    for p in sorted(files):
        name = to_module_name(base, p)
        ext_lines.append(f'        Extension({name!r}, [r"{p}"],),')
    body = "\n".join(ext_lines)
    directives = '{"language_level": "3", "binding": True, "annotation_typing": False}'
    return (
        "from setuptools import setup, Extension\n"
        "from Cython.Build import cythonize\n"
        "setup(\n"
        '    name="ai_camera_cython",\n'
        "    ext_modules=cythonize(\n"
        "        [\n"
        f"{body}\n"
        "        ],\n"
        f"        compiler_directives={directives},\n"
        "    ),\n"
        ")\n"
    )


def compile_all(base, python_bin):
    """Compile all .py files to .so using Cython."""
    all_files = []
    
    # Collect from COMPILE_DIRS
    for d in COMPILE_DIRS:
        root = os.path.join(base, d)
        if os.path.isdir(root):
            all_files += collect_py_files(root)
    
    # Add top-level files
    for f in TOP_LEVEL_FILES:
        p = os.path.join(base, f)
        if os.path.isfile(p):
            all_files.append(p)
    
    all_files = sorted(set(all_files))
    print(f"[CYTHON] Collecting {len(all_files)} modules under {base}")
    
    if not all_files:
        print("[CYTHON] No files to compile")
        return 0
    
    # Create setup.py in temp dir
    setup_dir = tempfile.mkdtemp(prefix="cython_")
    setup_path = os.path.join(setup_dir, "setup.py")
    with open(setup_path, "w") as f:
        f.write(make_setup_script(all_files, base))
    
    try:
        print("[CYTHON] Running Cython compile...")
        result = subprocess.run(
            [python_bin, setup_path, "build_ext", "--inplace"],
            cwd=base,
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            stderr = result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr
            stdout = result.stdout[-1000:] if len(result.stdout) > 1000 else result.stdout
            raise RuntimeError(
                f"Cython compile failed (code {result.returncode}):\n"
                f"{stderr}\n{stdout}"
            )
        print(f"[CYTHON] Compilation successful ({len(all_files)} modules)")
    finally:
        shutil.rmtree(setup_dir, ignore_errors=True)
    
    return len(all_files)


def clean_source_py(base):
    """Remove .py files that have been compiled to .so, except KEEP_PY and entry points.
    Creates thin launchers for entry points.
    """
    for dirpath, dirnames, filenames in os.walk(base):
        # Skip unwanted dirs
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        
        for fn in filenames:
            if fn in KEEP_PY:
                continue
            if not fn.endswith(".py"):
                continue
            
            full = os.path.join(dirpath, fn)
            # Check if .so exists for this .py
            so_name = fn[:-3] + ".so"
            so_path = os.path.join(dirpath, so_name)
            if os.path.exists(so_path):
                os.remove(full)
                print(f"  Removed: {os.path.relpath(full, base)}")
            elif fn in ENTRY_POINTS:
                # Create thin launcher
                module_name = ENTRY_POINTS[fn]
                launcher = f"""import sys
import {module_name}

if __name__ == '__main__':
    {module_name}.main()
"""
                with open(full, "w") as f:
                    f.write(launcher)
                print(f"  Launcher: {os.path.relpath(full, base)} -> {module_name}.so")
    
    # Clean up build artifacts
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        # Remove .c files generated by Cython
        for fn in filenames:
            if fn.endswith(".c") and os.path.exists(os.path.join(dirpath, fn[:-2] + ".so")):
                os.remove(os.path.join(dirpath, fn))
            if fn.endswith(".pyc") or fn.endswith(".pyo"):
                os.remove(os.path.join(dirpath, fn))
            if ".cpython-" in fn and fn.endswith(".so"):
                # Rename .cpython-312-darwin.so -> .so
                new_name = fn.split(".cpython")[0] + ".so"
                if new_name != fn:
                    old_path = os.path.join(dirpath, fn)
                    new_path = os.path.join(dirpath, new_name)
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    os.rename(old_path, new_path)
                    print(f"  Renamed: {fn} -> {new_name}")
        # Remove __pycache__
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
    
    # Remove build/ directory
    build_dir = os.path.join(base, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def main():
    base = os.path.abspath(os.getcwd())
    python_bin = os.path.join(base, "python_env", "bin", "python3.12")
    if not os.path.exists(python_bin):
        python_bin = sys.executable
    
    # Note: secure_env.pyx is now automatically collected by collect_py_files 
    # since we added .pyx to the allowed extensions.
    
    # Compile all .py -> .so
    count = compile_all(base, python_bin)
    if count == 0:
        print("[CYTHON] Nothing to compile")
        return
    
    # NOTE: cleanup of .py files is done in build_mac_app.sh on the .app bundle
    # Do NOT remove .py files from source tree — keep for development
    
    # Rename .cpython-312-darwin.so to .so for all compiled files
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if ".cpython-" in fn and fn.endswith(".so"):
                new_name = fn.split(".cpython")[0] + ".so"
                if new_name != fn:
                    old_path = os.path.join(dirpath, fn)
                    new_path = os.path.join(dirpath, new_name)
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    os.rename(old_path, new_path)
    
    # Remove build/ directory
    build_dir = os.path.join(base, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    
    print(f"[CYTHON] Done. Compiled {count} modules to .so")


if __name__ == "__main__":
    main()