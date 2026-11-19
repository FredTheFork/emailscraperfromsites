from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "PublicMail_Colab.ipynb"


def client_module():
    spec = importlib.util.spec_from_file_location("colab_client", ROOT / "notebooks" / "colab_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notebook_is_clean_valid_python_and_safe_to_run_all():
    notebook = json.loads(NOTEBOOK.read_text())
    assert notebook["nbformat"] == 4
    identifiers = set()
    for cell in notebook["cells"]:
        assert cell["id"] not in identifiers
        identifiers.add(cell["id"])
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            assert cell["execution_count"] is None
            compile("".join(cell["source"]), cell["id"], "exec")
    cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}
    assert "USE_GOOGLE_DRIVE = True" in cells["settings"]
    assert 'RUN_MODE = "pilot"' in cells["run"]
    assert "REQUEUE_GAPS = False" in cells["retry"]
    assert "drive.mount" in cells["restore"]
    assert "--drive-mount" not in cells["install"]  # The mount is verified in its own step.
    assert "git reset" not in cells["install"] and "git clean" not in cells["install"]
    assert "files.upload" in cells["input"] and "files.download" in cells["download"]


def test_kernel_client_imports_standard_library_only():
    source = (ROOT / "notebooks" / "colab_client.py").read_text()
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= sys.stdlib_module_names | {"__future__"}


def test_client_forwards_paths_as_arguments_not_shell_strings(tmp_path):
    module = client_module()
    client = module.ColabClient(
        Path(sys.executable), tmp_path / "local space", drive_dir=tmp_path / "private drive"
    )
    command = client.command("import", "/tmp/untrusted; filename.csv")
    assert command[-1] == "/tmp/untrusted; filename.csv"
    assert command[command.index("--work-dir") + 1].endswith("local space")
    assert command[command.index("--drive-dir") + 1].endswith("private drive")


def test_notebook_client_can_prepare_import_and_export_without_kernel_imports(tmp_path):
    client = client_module().ColabClient(Path(sys.executable), tmp_path / "local")
    assert client.json("prepare")["stats"]["total"] == 0
    source = tmp_path / "input.csv"
    source.write_text("domain\nacmeglazing.co.uk\n")
    assert client.json("import", source)["inserted"] == 1
    assert client.json("status")["stats"]["total"] == 1
    assert Path(client.json("export")["export_zip"]).is_file()


def test_bootstrap_does_not_install_into_the_notebook_kernel(tmp_path, monkeypatch):
    module = client_module()
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    python, environment = module.install_runtime(ROOT, tmp_path, browser=True)
    assert "publicmail-venv" in str(python)
    assert any("--without-pip" in args for args, _ in calls)
    assert any("--python" in args for args, _ in calls)
    for args, _ in calls:
        if "pip" in args and args[0] == sys.executable:
            assert "--python" in args  # host pip always targets the isolated venv
    assert any("playwright" in args and "--with-deps" in args for args, _ in calls)
    assert environment["PLAYWRIGHT_BROWSERS_PATH"].startswith(str(tmp_path))


@pytest.mark.skipif(
    os.environ.get("PUBLICMAIL_TEST_COLAB_INSTALL") != "1", reason="Opt-in isolated pip installation"
)
def test_real_colab_private_environment_installation(tmp_path):
    import importlib.metadata

    before = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    module = client_module()
    python, environment = module.install_runtime(ROOT, tmp_path, browser=False)
    assert python.is_file()
    client = module.ColabClient(python, tmp_path / "local", environment=environment)
    assert client.json("doctor")["browser_launch_checked"] is False
    assert client.json("prepare")["stats"]["total"] == 0
    source = tmp_path / "input.csv"
    source.write_text("domain\nacmeglazing.co.uk\n")
    assert client.json("import", source)["queue_total"] == 1
    assert Path(client.json("export")["export_zip"]).is_file()
    prefix = subprocess.check_output([str(python), "-c", "import sys; print(sys.prefix)"], text=True).strip()
    assert prefix == str(tmp_path / "publicmail-venv")
    after = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    assert before == after  # Kernel/global application packages were not upgraded.


@pytest.mark.skipif(os.environ.get("PUBLICMAIL_TEST_NOTEBOOK") != "1", reason="Opt-in Jupyter execution")
def test_entire_notebook_executes_with_fixture_websites_and_reruns(tmp_path):
    nbformat = pytest.importorskip("nbformat")
    nbclient = pytest.importorskip("nbclient")
    from jupyter_client import KernelManager

    # The test replaces only cloud authorization/upload/download, installation
    # (tested separately), and target-site networking. Real CLI, subprocess client,
    # engine, parser, SQLite, progress, snapshots, CSV/JSONL/ZIP paths still run.
    fixture_worker = tmp_path / "fixture_worker.py"
    fixture_worker.write_text(
        textwrap.dedent("""
        import asyncio
        import json
        import sys
        from publicmail.colab import parser, Workspace, run_session
        from publicmail.engine import Engine
        from conftest import FakeFetcher, response
        args = parser().parse_args(sys.argv[1:])
        workspace = Workspace(args.work_dir, args.drive_dir, drive_mount=args.drive_mount)
        def factory(store):
            def client(settings):
                return FakeFetcher(settings, {
                    'https://acmeglazing.co.uk/': response('https://acmeglazing.co.uk/', 'office@acmeglazing.co.uk'),
                    'https://secondglazing.co.uk/': response('https://secondglazing.co.uk/', 'fixture@gmail.com'),
                })
            return Engine(store, client_factory=client)
        settings = {k: getattr(args, k) for k in ('concurrency','delay','max_pages','max_depth','browser')}
        result = asyncio.run(run_session(workspace, max_sites=args.max_sites or None,
            duration=args.hours*3600, settings=settings, engine_factory=factory))
        print(json.dumps(result))
    """)
    )
    root = tmp_path / "content"
    root.mkdir()
    (root / "publicmail-code").symlink_to(ROOT, target_is_directory=True)
    setup = textwrap.dedent(f"""
        import importlib.util, json, os, subprocess, sys, types
        from pathlib import Path
        CONTENT_ROOT = Path({str(root)!r})
        USE_GOOGLE_DRIVE = False
        ENABLE_BROWSER = False
        EXPECTED_DOMAINS = 2
        fixture_path = {str(fixture_worker)!r}
        spec = importlib.util.spec_from_file_location('colab_client', {str(ROOT / "notebooks" / "colab_client.py")!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.install_runtime = lambda *a, **kw: (Path(sys.executable), dict(os.environ))
        original_command = module.ColabClient.command
        def command(self, *args):
            real = original_command(self, *args)
            if args[0] == 'run':
                return [sys.executable, fixture_path, *real[3:]]
            return real
        module.ColabClient.command = command
        sys.modules['colab_client'] = module
        colab = types.ModuleType('google.colab')
        files = types.ModuleType('google.colab.files')
        def upload(target_dir):
            data = b'domain,business_name\\nacmeglazing.co.uk,First fixture\\nsecondglazing.co.uk,Second fixture\\n'
            Path(target_dir, 'fixture.csv').write_bytes(data)
            return {{'fixture.csv': data}}
        def download(path):
            assert Path(path).is_file()
            print('Fixture download verified:', path)
        files.upload, files.download = upload, download
        colab.files = files
        sys.modules['google.colab'] = colab
        sys.modules['google.colab.files'] = files
    """)
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    nbformat.validate(notebook)
    position = next(i for i, c in enumerate(notebook.cells) if c.get("id") == "settings") + 1
    notebook.cells.insert(position, nbformat.v4.new_code_cell(setup))
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")]))
    for attempt in range(2):
        manager = KernelManager(kernel_name="python3")
        manager.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        manager.start_kernel(cwd=str(ROOT), env=environment)
        try:
            executed = nbclient.NotebookClient(
                notebook, km=manager, timeout=120, allow_errors=False
            ).execute()
            assert all(
                output.get("output_type") != "error"
                for c in executed.cells
                if c.cell_type == "code"
                for output in c.outputs
            )
        finally:
            manager.shutdown_kernel(now=True)
        work = root / "publicmail-workspaces" / "publicmail-33144"
        status = json.loads((work / "last-run.json").read_text())
        assert status["stats"]["total"] == 2
        assert status["stats"]["unique_emails"] == 2
        assert status["stats"]["terminal"] == 2
        with zipfile.ZipFile(status["export_zip"]) as archive:
            assert b"office@acmeglazing.co.uk" in archive.read("emails.csv")
            assert b"fixture@gmail.com" in archive.read("unique_emails.csv")
        if attempt == 0:
            identifier = json.loads((work / "local-backups" / "collection.json").read_text())["collection_id"]
        else:
            assert (
                json.loads((work / "local-backups" / "collection.json").read_text())["collection_id"]
                == identifier
            )


def test_private_environment_drops_kernel_python_path(tmp_path, monkeypatch):
    module = client_module()
    monkeypatch.setenv("PYTHONPATH", "/kernel-packages-must-not-shadow-application")
    monkeypatch.setenv("PYTHONHOME", "/kernel-python-home")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    _, environment = module.install_runtime(ROOT, tmp_path, browser=False)
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment


def test_bootstrap_uses_feature_ref_before_merge_without_resetting_any_checkout(tmp_path, monkeypatch):
    module = client_module()
    module.install_runtime = lambda repository, root, browser: (Path(sys.executable), {})
    monkeypatch.setitem(sys.modules, "colab_client", module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "clone":
            (Path(args[-1]) / ".git").mkdir(parents=True)
        if "checkout" in args:
            directory = tmp_path / "publicmail-code" / "notebooks"
            directory.mkdir()
            (directory / "colab_client.py").touch()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "test-source-revision\n")
    cells = {c["id"]: "".join(c["source"]) for c in json.loads(NOTEBOOK.read_text())["cells"]}
    namespace = {
        "CONTENT_ROOT": tmp_path,
        "PROJECT_NAME": "fixture",
        "CODE_REF": "main",
        "ENABLE_BROWSER": False,
    }
    exec(compile(cells["install"], "notebook-install", "exec"), namespace)
    assert namespace["CODE_REVISION"] == "test-source-revision"
    assert any("fetch" in call and namespace["COLAB_FEATURE_REF"] in call for call in calls)
    assert not any("reset" in call or "clean" in call for call in calls)


def test_interrupt_is_forwarded_to_worker_and_waits_for_cleanup(tmp_path, monkeypatch):
    import signal

    module = client_module()
    seen = []

    class Output:
        def __iter__(self):
            raise KeyboardInterrupt

        def close(self):
            seen.append("closed")

    class Process:
        returncode = None
        stdout = Output()

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            seen.append(value)

        def communicate(self, timeout):
            self.returncode = 0
            return ("Final backup/export completed\n", None)

    def start(*args, **kwargs):
        assert kwargs["start_new_session"]
        return Process()

    monkeypatch.setattr(module.subprocess, "Popen", start)
    client = module.ColabClient(Path(sys.executable), tmp_path)
    client.run("run")
    assert signal.SIGINT in seen
    assert "closed" in seen


def test_active_previous_process_prevents_a_duplicate_notebook_command(tmp_path):
    module = client_module()
    client = module.ColabClient(Path(sys.executable), tmp_path)

    class Active:
        def poll(self):
            return None

    client._active = Active()
    with pytest.raises(RuntimeError, match="still stopping"):
        client.json("prepare")
