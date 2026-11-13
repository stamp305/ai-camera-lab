#!/usr/bin/env python3
"""Check a Branch's delivered Services against dlc_master before pulling the Main Client.

Read-only on both sides. Two steps:

1. On the Mac: list every file of ai-camera-admin/backend/dlc_master at the
   merged commit with its sha256, read through git (git show + git archive),
   not from the working tree:

     cd /path/to/ai-camera-client
     python3 tools/dlc_hash_check.py manifest ~/Advice/in-progress/server/python_server <commit> > ~/Desktop/dlc_master_<commit>.json

2. Copy three files to the Branch over AnyDesk, into one folder (~/Desktop/dlc_check/):
   tools/dlc_hash_check.py, dlc_manifest.py (repo root) and the .json.
   After saving that Branch's Services, open Terminal there and run:

   Branch with the .app (/Applications/AICamera.app):
     cd ~/Desktop/dlc_check && /Applications/AICamera.app/Contents/Resources/app/python_env/bin/python3.12 dlc_hash_check.py check dlc_master_<commit>.json

   Branch installed by git clone (/Applications/advice/ai-camera-client):
     cd ~/Desktop/dlc_check && /opt/miniconda3/envs/ai-camera/bin/python dlc_hash_check.py check dlc_master_<commit>.json

   Exit 0 and "PASS": pull the Main Client. Exit 1 and "FAIL": do not pull;
   save the Services again and run the check again. Exit 2: the check could
   not run (one line says why).

Both kinds of Branch keep the Services in
~/Library/Application Support/AICamera/managed_services (--managed to change).
The .app keeps each .py encrypted; the DLC key is read from STATIC_LEGACY_KEY in
the environment, else from the client's own _secrets file, loaded by its path
(--client: the .app's Contents/Resources/app, or the git clone; the one that
exists is found). The output names the file used.

Rules: __pycache__, .pyc and .part/.dlcpart are skipped on both sides. Every
dlc_master file in a folder the Branch has, plus configs, events and
ai_camera_db, must exist and match. Files the Branch has that dlc_master does
not are listed and do not fail the check (saving Services never deletes them).
dlc_master files at its top level, outside any folder, are never sent and are
not checked. Model files kept out of git are not in the manifest either: they
show as extra, unchecked.
"""
import sys

sys.dont_write_bytecode = True  # writes nothing on the Branch, not even a __pycache__

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
# dlc_manifest.py sits next to this file on a Branch, one folder up in the repo.
sys.path.append(os.path.dirname(HERE))
import dlc_manifest  # noqa: E402

DLC_MASTER = 'ai-camera-admin/backend/dlc_master'
ALWAYS_CHECKED = ('configs', 'events', 'ai_camera_db')
MANAGED = os.path.expanduser('~/Library/Application Support/AICamera/managed_services')
CLIENT_DIRS = ('/Applications/AICamera.app/Contents/Resources/app',
               '/Applications/advice/ai-camera-client')


def skipped(key):
    # .dlcpart is the client's own name for a file still arriving.
    return ('__pycache__' in key.split('/')
            or key.endswith(('.pyc', '.part', '.dlcpart')))


# ---------------------------------------------------------------- Mac side
def build_manifest(repo, commit):
    """{'commit', 'path', 'files': {path: sha256}} of dlc_master at commit."""
    full = subprocess.run(['git', '-C', repo, 'show', '-s', '--format=%H', commit + '^{commit}'],
                          capture_output=True, text=True)
    if full.returncode != 0:
        raise SystemExit(f'not a commit in {repo}: {commit}\n{full.stderr.strip()}')
    sha = full.stdout.strip()
    files = {}
    proc = subprocess.Popen(['git', '-C', repo, 'archive', '--format=tar', sha, DLC_MASTER],
                            stdout=subprocess.PIPE)
    try:
        tar = tarfile.open(fileobj=proc.stdout, mode='r|')
    except tarfile.ReadError:
        tar = ()  # nothing came out: git says why on stderr, and the exit code below fails
    with proc:
        for member in tar:
            if not member.isfile():
                continue  # dlc_master has no symlinks; a directory has no content
            rel = member.name[len(DLC_MASTER) + 1:]
            if skipped(rel):
                continue
            blob = tar.extractfile(member)
            sha256 = hashlib.sha256()
            for block in iter(lambda: blob.read(1024 * 1024), b''):
                sha256.update(block)
            files[rel] = sha256.hexdigest()
    if proc.wait() != 0:
        raise SystemExit(f'git archive failed for {DLC_MASTER} at {sha}')
    if not files:
        raise SystemExit(f'no files under {DLC_MASTER} at {sha}')
    return {'commit': sha, 'path': DLC_MASTER, 'files': files}


# ------------------------------------------------------------- Branch side
class CannotRun(Exception):
    """The check could not run: one line for the operator, exit 2."""


def secrets_file(client_dir):
    """The client's own _secrets: compiled in the .app, _secrets.py in a git clone."""
    for suffix in importlib.machinery.EXTENSION_SUFFIXES + ['.py']:
        path = os.path.join(client_dir, '_secrets' + suffix)
        if os.path.isfile(path):
            return path
    return None


def load_cipher(client_dir):
    """(Fernet or None, the key's source or why there is none).

    _secrets is loaded from its exact file, never through sys.path, so no other
    _secrets.py (next to the tool, on the Desktop) can stand in for it.
    """
    key, source = os.environ.get('STATIC_LEGACY_KEY', ''), 'STATIC_LEGACY_KEY'
    path = secrets_file(client_dir) if not key and client_dir else None
    if path:
        try:
            spec = importlib.util.spec_from_file_location('_secrets', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            raise CannotRun(f'Cannot load {path}: {type(e).__name__}: {e}')
        key, source = getattr(module, 'STATIC_LEGACY_KEY', '') or '', path
    if isinstance(key, str):
        key = key.strip().encode()
    if not key:
        return None, 'DLC key not found' + (f' in {path}' if path else '')
    try:
        from cryptography.fernet import Fernet
        return Fernet(key), source
    except Exception as e:
        return None, f'DLC key from {source} not usable ({type(e).__name__})'


def compare(manifest_files, branch_files, branch_folders):
    """(missing, different, extra, not_checked) — the first two fail the check."""
    folders = set(branch_folders) | set(ALWAYS_CHECKED)
    required = {k: v for k, v in manifest_files.items()
                if '/' in k and k.split('/', 1)[0] in folders}
    missing = sorted(k for k in required if k not in branch_files)
    different = sorted(k for k in required if k in branch_files and branch_files[k] != required[k])
    extra = sorted(k for k in branch_files if k not in manifest_files)
    return missing, different, extra, len(manifest_files) - len(required)


def read_manifest(manifest_path):
    try:
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception as e:
        raise CannotRun(f'Cannot read {manifest_path}: {type(e).__name__}: {e}')
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if (not isinstance(files, dict) or not files
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
        raise CannotRun(f'Not a dlc_master manifest: {manifest_path}')
    return manifest


def check(manifest_path, managed, client_dir, out=print):
    """0 pass, 1 fail; anything that stops the check prints one line and gives 2."""
    try:
        return _check(manifest_path, managed, client_dir, out)
    except CannotRun as e:
        out(str(e))
    except Exception as e:
        out(f'Check could not run: {type(e).__name__}: {e}')
    return 2


def _check(manifest_path, managed, client_dir, out):
    manifest = read_manifest(manifest_path)
    files = manifest['files']
    if not os.path.isdir(managed):
        raise CannotRun(f'Not found: {managed}')
    cipher, key_note = load_cipher(client_dir)
    branch_files = dlc_manifest.tree_sha256(managed, lambda: cipher, skip=skipped)
    folders = sorted(d for d in os.listdir(managed) if os.path.isdir(os.path.join(managed, d)))
    missing, different, extra, not_checked = compare(files, branch_files, folders)

    out(f'dlc_master {manifest.get("commit", "?")[:10]}  vs  {managed}')
    out(f'Branch folders: {" ".join(folders) or "(none)"}')
    if cipher is None:
        out(f'Note: {key_note}; encrypted files show as DIFFERENT.')
    else:
        out(f'DLC key: {key_note}')
    for k in missing:
        out(f'  MISSING    {k}')
    for k in different:
        out(f'  DIFFERENT  {k}' + ('  (cannot read)' if branch_files[k] is None else ''))
    if extra:
        out(f'Extra on Branch, not in dlc_master (does not fail): {len(extra)}')
        for k in extra:
            out(f'  extra      {k}')
    checked = len(files) - not_checked
    out(f'Not checked: {not_checked} dlc_master files in folders this Branch does not have.')
    bad = len(missing) + len(different)
    if bad:
        out(f'FAIL: {bad} of {checked} files missing or different. Do not pull. Save Services again, then check again.')
        return 1
    out(f'PASS: all {checked} files match. OK to pull the Main Client.')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('manifest', help='Mac: print the dlc_master manifest of a commit as JSON')
    m.add_argument('python_server', help='path of a python_server checkout')
    m.add_argument('commit', help='the merged commit (any name git knows)')
    c = sub.add_parser('check', help='Branch: compare managed_services with a manifest')
    c.add_argument('manifest', help='the .json made by "manifest"')
    c.add_argument('--managed', default=MANAGED, help=f'default: {MANAGED}')
    c.add_argument('--client', default=next((d for d in CLIENT_DIRS if os.path.isdir(d)), None),
                   help='folder holding the client\'s _secrets file (default: the first of '
                        + ', '.join(CLIENT_DIRS) + ' that exists)')
    a = p.parse_args(argv)
    if a.cmd == 'manifest':
        json.dump(build_manifest(a.python_server, a.commit), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
        return 0
    return check(a.manifest, a.managed, a.client)


if __name__ == '__main__':
    sys.exit(main())
