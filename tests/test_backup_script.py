"""Exercise the Windows backup script with a native fake exporter; never contact MySQL."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from app.config import settings

POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell operational script")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_db.ps1"
DUMP_BYTES = "-- fake export\nCREATE TABLE example (label TEXT);\n-- 中文数据\n".encode("utf-8")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def backup_fixture():
    # All generated files stay under the project test runtime, cleaned by conftest.
    root = Path(settings.SQLITE_PATH).parent / ("backup test " + uuid4().hex)
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / "backup_db.ps1")
    (root / ".env").write_text(
        'DB_DRIVER=mysql\nDB_HOST=not-a-real-host\nDB_PORT=3306\n'
        'DB_NAME=test_db\nDB_WRITE_USER=test_user\nDB_WRITE_PASSWORD="fake-secret"\n', encoding="utf-8")
    backups = root / "backups"
    backups.mkdir()
    for day in range(1, 4):
        (backups / f"backup_2000010{day}_000000.sql").write_bytes(b"existing backup")
    (backups / "manual.sql").write_bytes(b"keep manual backup")
    (backups / "backup_20000101_000000.sql.partial").write_bytes(b"old incomplete dump")
    exporter = root / "fake_dump.py"
    exporter.write_text(
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['DUMP_ARGS_FILE']).write_text(json.dumps(args))\n"
        "assert os.environ['MYSQL_PWD'] == 'fake-secret'\n"
        "target = pathlib.Path(next(a.split('=', 1)[1] for a in args if a.startswith('--result-file=')))\n"
        "mode = os.environ['DUMP_MODE']\n"
        f"data = {DUMP_BYTES!r}\n"
        "if mode != 'missing': target.write_bytes(b'' if mode == 'empty' else data)\n"
        "if mode == 'warning': print('harmless native warning', file=sys.stderr)\n"
        "if mode == 'failure':\n"
        "    print('private fake-secret SQL payload', file=sys.stderr)\n"
        "    sys.exit(7)\n",
        encoding="utf-8")
    proxy = root / "fake_dump.ps1"
    proxy.write_text(
        f"& {ps_quote(sys.executable)} {ps_quote(exporter)} @args\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
    runner = root / "run_test.ps1"
    runner.write_text(
        f"& {ps_quote(scripts / 'backup_db.ps1')} -DumpExecutable {ps_quote(proxy)} @args\n"
        "$invocationOk = $?\n"
        "$backupExit = $LASTEXITCODE\n"
        "if (-not $invocationOk -and -not $backupExit) { $backupExit = 1 }\n"
        f"[IO.File]::WriteAllText({ps_quote(root / 'restored.txt')}, [string]$env:MYSQL_PWD)\n"
        "exit $backupExit\n", encoding="utf-8-sig")

    def run(mode, *args, previous_pwd=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith("DB_") and key != "MYSQL_PWD"}
        env.update(DUMP_ARGS_FILE=str(root / "args.json"), DUMP_MODE=mode)
        if previous_pwd:
            env["MYSQL_PWD"] = "parent-token"
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner), *args],
            cwd=root, env=env, capture_output=True, timeout=30)
        return result

    return root, backups, run


@pytest.mark.parametrize("mode", ["failure", "empty", "missing"])
def test_failed_export_never_publishes_or_prunes(backup_fixture, mode):
    root, backups, run = backup_fixture
    before = {path.name: path.read_bytes() for path in backups.glob("*.sql")}
    result = run(mode, "-Keep", "1")
    assert result.returncode == 1
    assert b"[OK]" not in result.stdout
    assert b"fake-secret" not in result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in backups.glob("*.sql")} == before
    assert (root / "restored.txt").read_text() == "parent-token"


@pytest.mark.parametrize("no_prune", [False, True])
@pytest.mark.parametrize("previous_pwd", [False, True])
def test_success_preserves_bytes_credentials_and_retention(backup_fixture, no_prune, previous_pwd):
    root, backups, run = backup_fixture
    options = ["-Keep", "2"] + (["-NoPrune"] if no_prune else [])
    result = run("success", *options, previous_pwd=previous_pwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"[OK]" in result.stdout
    published = [path for path in backups.glob("backup_*.sql") if path.read_bytes() == DUMP_BYTES]
    assert len(published) == 1
    assert len(list(backups.glob("backup_*.sql"))) == (4 if no_prune else 2)
    assert (backups / "manual.sql").read_bytes() == b"keep manual backup"
    assert (backups / "backup_20000101_000000.sql.partial").exists()
    assert (root / "restored.txt").read_text() == ("parent-token" if previous_pwd else "")
    args = json.loads((root / "args.json").read_text())
    assert "--single-transaction" in args and "--no-tablespaces" in args
    assert "fake-secret" not in json.dumps(args)


@pytest.mark.parametrize("keep", ["0", "-1"])
def test_invalid_retention_does_not_start_export(backup_fixture, keep):
    root, backups, run = backup_fixture
    result = run("success", "-Keep", keep)
    assert result.returncode != 0
    assert not (root / "args.json").exists()
    assert len(list(backups.glob("backup_*.sql"))) == 3


def test_native_warning_with_zero_exit_code_is_not_a_failed_dump(backup_fixture):
    root, backups, run = backup_fixture
    result = run("warning", "-NoPrune")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len([path for path in backups.glob("*.sql") if path.read_bytes() == DUMP_BYTES]) == 1
