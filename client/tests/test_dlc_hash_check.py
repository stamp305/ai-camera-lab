"""tools/dlc_hash_check.py: the read-only check before pulling the Main Client,
and get_client_manifest() still hashing exactly as before it moved to dlc_manifest."""
import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

import dlc_manifest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'tools', 'dlc_hash_check.py')
_spec = importlib.util.spec_from_file_location('dlc_hash_check', TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

KEY = Fernet.generate_key()
CAMERA_MAIN = b'print("camera")\n'
CONFIG = b'{"a": 1}\n'


def sha(b):
    return hashlib.sha256(b).hexdigest()


def write(root, rel, data):
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


@pytest.fixture
def branch(tmp_path, monkeypatch):
    """A managed_services as the .app keeps it (.py encrypted) and a manifest it matches."""
    monkeypatch.setenv('STATIC_LEGACY_KEY', KEY.decode())
    managed = str(tmp_path / 'managed_services')
    write(managed, 'camera_service/main.py', Fernet(KEY).encrypt(CAMERA_MAIN))
    write(managed, 'configs/settings.json', CONFIG)
    files = {
        'camera_service/main.py': sha(CAMERA_MAIN),
        'configs/settings.json': sha(CONFIG),
        'heatmap_service/main.py': sha(b'not on this branch'),  # folder the Branch lacks
        'conftest.py': sha(b'top level, never sent'),
    }
    return managed, files


def run(tmp_path, managed, files):
    manifest = tmp_path / 'm.json'
    manifest.write_text(json.dumps({'commit': 'abc', 'files': files}))
    lines = []
    rc = tool.check(str(manifest), managed, None, out=lines.append)
    return rc, '\n'.join(lines)


def test_encrypted_file_that_matches_passes(tmp_path, branch):
    rc, out = run(tmp_path, *branch)
    assert rc == 0, out
    assert 'PASS: all 2 files match' in out
    assert 'Not checked: 2' in out


def test_encrypted_file_without_the_key_fails_and_says_why(tmp_path, branch, monkeypatch):
    monkeypatch.delenv('STATIC_LEGACY_KEY')
    rc, out = run(tmp_path, *branch)
    assert rc == 1
    assert 'DLC key not found' in out
    assert 'DIFFERENT  camera_service/main.py' in out


def test_missing_file_fails_and_names_it(tmp_path, branch):
    managed, files = branch
    files['camera_service/utils.py'] = sha(b'x')
    files['events/handler.py'] = sha(b'y')  # events is checked even with no folder on the Branch
    rc, out = run(tmp_path, managed, files)
    assert rc == 1
    assert 'MISSING    camera_service/utils.py' in out
    assert 'MISSING    events/handler.py' in out
    assert 'heatmap_service' not in out.split('Not checked')[0]


def test_changed_file_fails_and_names_it(tmp_path, branch):
    managed, files = branch
    write(managed, 'configs/settings.json', b'{"a": 2}\n')
    rc, out = run(tmp_path, managed, files)
    assert rc == 1
    assert 'DIFFERENT  configs/settings.json' in out
    assert 'FAIL: 1 of 2' in out


def test_extra_file_is_listed_and_does_not_fail(tmp_path, branch):
    managed, files = branch
    write(managed, 'camera_service/old_helper.py', b'left by an older save')
    rc, out = run(tmp_path, managed, files)
    assert rc == 0, out
    assert 'extra      camera_service/old_helper.py' in out


def test_pycache_pyc_and_part_files_are_skipped(tmp_path, branch):
    managed, files = branch
    write(managed, 'camera_service/__pycache__/main.cpython-312.pyc', b'bytecode')
    write(managed, 'camera_service/stray.pyc', b'bytecode')
    write(managed, 'camera_service/models/big.bin.dlcpart', b'half')
    write(managed, 'camera_service/models/big.bin.part', b'half')
    rc, out = run(tmp_path, managed, files)
    assert rc == 0, out
    assert 'extra' not in out.lower()


def test_runs_as_a_copied_script_with_defaults_and_writes_nothing(tmp_path, branch):
    """As on a Branch: the two files copied into one folder, run by path, no repo around."""
    managed, files = branch
    home = tmp_path / 'home'
    shutil.copytree(managed, home / 'Library/Application Support/AICamera/managed_services')
    shutil.rmtree(managed)
    kit = tmp_path / 'kit'
    kit.mkdir()
    shutil.copy(TOOL, kit)
    shutil.copy(os.path.join(ROOT, 'dlc_manifest.py'), kit)
    (kit / 'm.json').write_text(json.dumps({'commit': 'abc', 'files': files}))

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob('*'))

    before = snapshot()
    env = dict(os.environ, HOME=str(home), STATIC_LEGACY_KEY=KEY.decode())
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    r = subprocess.run([sys.executable, 'dlc_hash_check.py', 'check', 'm.json', '--client', str(kit)],
                       cwd=kit, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'PASS' in r.stdout and len(r.stdout.splitlines()) <= 6
    assert snapshot() == before


@pytest.mark.parametrize('folder', ['configs', 'events', 'ai_camera_db'])
def test_always_checked_folder_missing_on_branch_fails(tmp_path, monkeypatch, folder):
    """configs, events, ai_camera_db are required even when the Branch has no such folder."""
    monkeypatch.setenv('STATIC_LEGACY_KEY', KEY.decode())
    managed = str(tmp_path / 'managed_services')
    write(managed, 'camera_service/main.py', CAMERA_MAIN)
    rc, out = run(tmp_path, managed, {'camera_service/main.py': sha(CAMERA_MAIN),
                                      f'{folder}/x.py': sha(b'x')})
    assert rc == 1, out
    assert f'MISSING    {folder}/x.py' in out


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads any folder')
def test_unreadable_branch_folder_prints_one_line_and_exits_2(tmp_path, branch):
    managed, files = branch
    os.chmod(managed, 0)
    try:
        rc, out = run(tmp_path, managed, files)
    finally:
        os.chmod(managed, 0o700)
    assert rc == 2
    assert out.count('\n') == 0 and 'PermissionError' in out


def test_any_file_under_pycache_is_skipped(tmp_path, branch):
    managed, files = branch
    write(managed, 'camera_service/__pycache__/notes.txt', b'not bytecode')
    rc, out = run(tmp_path, managed, files)
    assert rc == 0, out
    assert 'extra' not in out.lower()


@pytest.mark.parametrize('content', ['{"files": ["a"]}', '{"files": null}', '[]', '{"files": {}}',
                                     '{"files": {"a/b": 1}}', 'not json'])
def test_malformed_manifest_prints_one_line_and_exits_2(tmp_path, branch, content):
    managed, _ = branch
    manifest = tmp_path / 'm.json'
    manifest.write_text(content)
    lines = []
    assert tool.check(str(manifest), managed, None, out=lines.append) == 2
    assert len(lines) == 1, lines


def kit(tmp_path, files, client_secrets=None):
    """As on a Branch: tool + dlc_manifest + manifest copied into one folder; a client folder."""
    k = tmp_path / 'kit'
    k.mkdir()
    shutil.copy(TOOL, k)
    shutil.copy(os.path.join(ROOT, 'dlc_manifest.py'), k)
    (k / 'm.json').write_text(json.dumps({'commit': 'abc', 'files': files}))
    client = tmp_path / 'client'
    client.mkdir()
    if client_secrets is not None:
        (client / '_secrets.py').write_text(client_secrets)
    # Importing client_logic would show: it is here, beside _secrets, and refuses.
    (client / 'client_logic.py').write_text('raise SystemExit("client_logic imported")\n')
    return k, client


def run_kit(k, client, *, env_key=False):
    env = dict(os.environ)
    env.pop('STATIC_LEGACY_KEY', None)
    if env_key:
        env['STATIC_LEGACY_KEY'] = KEY.decode()
    code = ('import runpy, sys\n'
            'sys.argv = sys.argv[1:]\n'
            'try:\n'
            '    runpy.run_path(sys.argv[0], run_name="__main__")\n'
            'except SystemExit as e:\n'
            '    rc = e.code\n'
            'print("IMPORTED client_logic" if "client_logic" in sys.modules else "no client_logic")\n'
            'sys.exit(rc)\n')
    return subprocess.run([sys.executable, '-c', code, 'dlc_hash_check.py', 'check', 'm.json',
                           '--client', str(client), '--managed', str(k.parent / 'managed_services')],
                          cwd=k, env=env, capture_output=True, text=True)


def test_secrets_loaded_from_the_client_file_not_a_stray_one(tmp_path, branch):
    managed, files = branch
    k, client = kit(tmp_path, files, client_secrets=f'STATIC_LEGACY_KEY = {KEY!r}\n')
    wrong = Fernet.generate_key()
    (k / '_secrets.py').write_text(f'STATIC_LEGACY_KEY = {wrong!r}\n')          # next to the kit
    (tmp_path / '_secrets.py').write_text(f'STATIC_LEGACY_KEY = {wrong!r}\n')   # one folder up
    r = run_kit(k, client)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f'DLC key: {client / "_secrets.py"}' in r.stdout
    assert 'no client_logic' in r.stdout


def test_broken_secrets_prints_one_line_and_exits_2(tmp_path, branch):
    managed, files = branch
    k, client = kit(tmp_path, files, client_secrets='raise RuntimeError("half-built")\n')
    r = run_kit(k, client)
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stderr == ''
    assert r.stdout.splitlines() == [f'Cannot load {client / "_secrets.py"}: RuntimeError: half-built',
                                     'no client_logic']


def test_checker_never_imports_client_logic(tmp_path, branch):
    managed, files = branch
    k, client = kit(tmp_path, files)
    r = run_kit(k, client, env_key=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1] == 'no client_logic'


# ------------------------------------------------------------ manifest (Mac)
def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


def test_manifest_reads_the_commit_not_the_working_tree(tmp_path):
    repo = tmp_path / 'python_server'
    master = repo / 'ai-camera-admin/backend/dlc_master'
    git(tmp_path, 'init', '-q', str(repo))
    write(str(master), 'camera_service/main.py', b'v1')
    write(str(master), 'camera_service/__pycache__/main.cpython-312.pyc', b'bc')
    write(str(master), 'configs/a.json', CONFIG)
    write(str(repo), 'ai-camera-admin/backend/other.py', b'not dlc')
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'one')
    first = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                           capture_output=True, text=True).stdout.strip()
    write(str(master), 'camera_service/main.py', b'v2')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'two')
    write(str(master), 'camera_service/main.py', b'uncommitted edit')

    m = tool.build_manifest(str(repo), first[:7])
    assert m['commit'] == first
    assert m['files'] == {'camera_service/main.py': sha(b'v1'), 'configs/a.json': sha(CONFIG)}
    assert tool.build_manifest(str(repo), 'HEAD')['files']['camera_service/main.py'] == sha(b'v2')
    with pytest.raises(SystemExit):
        tool.build_manifest(str(repo), 'no-such-commit')


# ------------------------------------------- get_client_manifest unchanged
def _old_get_client_manifest(MANAGED_PATH, _PART_SUFFIX, get_dlc_cipher):
    """client_logic.get_client_manifest as it was before dlc_manifest.py (26163bc), verbatim."""
    os.makedirs(MANAGED_PATH, exist_ok=True)
    manifest = {}
    for dirpath, _, filenames in os.walk(MANAGED_PATH):
        for filename in filenames:
            if filename.endswith(_PART_SUFFIX):
                continue  # a large file still arriving, or one that stopped halfway
            full_path = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(full_path, MANAGED_PATH)
            manifest_key = relative_path.replace(os.sep, '/')
            # Hash should represent plaintext content; decrypt if looks like Fernet token
            try:
                with open(full_path, 'rb') as f:
                    head = f.read(5)
                    if head != b'gAAAA':
                        sha = hashlib.sha256(head)
                        for block in iter(lambda: f.read(1024 * 1024), b''):
                            sha.update(block)
                        manifest[manifest_key] = sha.hexdigest()
                        continue
                    data = head + f.read()
                cipher = get_dlc_cipher()
                if data.startswith(b'gAAAA') and cipher is not None:
                    try:
                        plaintext = cipher.decrypt(data)
                        sha = hashlib.sha256(); sha.update(plaintext)
                        manifest[manifest_key] = sha.hexdigest()
                        continue
                    except Exception:
                        pass  # fallback to raw hashing
                sha = hashlib.sha256(); sha.update(data)
                manifest[manifest_key] = sha.hexdigest()
            except Exception:
                manifest[manifest_key] = None
    return manifest


def _current_get_client_manifest():
    """The function from client_logic.py itself, without importing client_logic."""
    with open(os.path.join(ROOT, 'client_logic.py'), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_client_manifest')
    ns = {'os': os, 'hashlib': hashlib, 'dlc_manifest': dlc_manifest}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'client_logic.py', 'exec'), ns)
    return ns


@pytest.mark.parametrize('with_key', [True, False])
def test_get_client_manifest_unchanged(tmp_path, with_key):
    managed = str(tmp_path / 'managed_services')
    write(managed, 'camera_service/main.py', Fernet(KEY).encrypt(CAMERA_MAIN))
    write(managed, 'camera_service/other_key.py', Fernet(Fernet.generate_key()).encrypt(b'x'))
    write(managed, 'camera_service/__pycache__/main.cpython-312.pyc', b'bytecode')
    write(managed, 'camera_service/models/m.bin', os.urandom(3 * 1024 * 1024 + 7))
    write(managed, 'camera_service/models/m2.bin.dlcpart', b'half')
    write(managed, 'configs/settings.json', CONFIG)
    write(managed, 'tiny', b'gA')
    write(managed, 'unreadable.py', b'secret')
    os.chmod(os.path.join(managed, 'unreadable.py'), 0)
    cipher = Fernet(KEY) if with_key else None

    ns = _current_get_client_manifest()
    ns.update(MANAGED_PATH=managed, _PART_SUFFIX='.dlcpart', get_dlc_cipher=lambda: cipher)
    try:
        new = ns['get_client_manifest']()
        old = _old_get_client_manifest(managed, '.dlcpart', lambda: cipher)
    finally:
        os.chmod(os.path.join(managed, 'unreadable.py'), 0o600)
    assert new == old
    assert list(new) == list(old)
    assert (new['camera_service/main.py'] == sha(CAMERA_MAIN)) is with_key
    if os.geteuid() != 0:
        assert new['unreadable.py'] is None
