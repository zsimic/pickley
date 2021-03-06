import os

import pytest
import runez
from mock import patch

from pickley import PackageSpec
from pickley.package import PackageContents, PythonVenv


PIP_SHOW_OUTPUT = """
Name: ansible
Version: 1.0.0
Location: .
Files:
  ../bin/ansible
  ../bin/ansible_completer
  ansible.dist-info/metadata.json
  foo/__pycache__/bar.py
  foo/bar.py
  foo/bar.pyc
"""


def test_edge_cases(temp_cfg):
    pspec = PackageSpec(temp_cfg, "foo")
    with runez.CaptureOutput(dryrun=True) as logged:
        pspec.cfg._bundled_virtualenv_path = None
        pspec.python = temp_cfg.available_pythons.invoker
        venv = PythonVenv(pspec, "myvenv")
        assert str(venv) == "myvenv"
        assert not pspec.is_healthily_installed()
        if runez.PY2:
            assert "virtualenv.pyz myvenv" in logged

        else:
            assert "-mvenv myvenv" in logged.pop()

        tox = PackageSpec(temp_cfg, "tox")
        PythonVenv(tox, "myvenv")
        assert "virtualenv.pyz" in logged.pop()

    with runez.CaptureOutput() as logged:
        runez.touch("dummy.whl")
        runez.ensure_folder(".", clean=True)
        assert "Cleaned 1 file from" in logged.pop()
        assert not os.path.exists("dummy.whl")


def simulated_run(*args, **_):
    if "ansible-base" in args:
        return runez.program.RunResult(PIP_SHOW_OUTPUT, code=0)

    if "no-location" in args:
        return runez.program.RunResult("Files:\n  no-location.dist-info/metadata.json", code=0)

    return runez.program.RunResult("", code=1)


def test_entry_points(temp_cfg):
    venv = PythonVenv(folder="", cfg=temp_cfg)
    with runez.CaptureOutput(dryrun=True):
        pspec = PackageSpec(temp_cfg, "mgit")
        contents = PackageContents(venv, pspec)
        assert str(contents) == "mgit [None]"
        assert str(contents.bin) == "bin [1 files]"
        assert contents.entry_points == {"mgit": "dryrun"}

    runez.write("ansible.dist-info/metadata.json", '{"extensions": {"python.commands": {"wrap_console": ["ansible"]}}}')
    with patch("runez.run", side_effect=simulated_run):
        pspec = PackageSpec(temp_cfg, "ansible")  # Used to trigger ansible edge case
        contents = PackageContents(venv, pspec)
        assert str(contents) == "ansible [.]"
        assert str(contents.bin) == "bin [0 files]"
        assert str(contents.completers) == "bin [1 files]"
        assert str(contents.dist_info) == "ansible.dist-info [1 files]"
        assert contents.entry_points == ["ansible"]
        assert str(contents.files) == " [1 files]"
        assert contents.files.files.get("foo/bar.py")
        assert contents.info == {"Name": "ansible", "Version": "1.0.0", "Location": "."}
        assert contents.location == "."
        assert contents.pspec is pspec
        assert contents.venv is venv

        contents = PackageContents(venv, PackageSpec(temp_cfg, "no-location"))
        assert contents.files is None
        assert contents.entry_points is None

        contents = PackageContents(venv, PackageSpec(temp_cfg, "no-such-package"))
        assert contents.files is None
        assert contents.entry_points is None


def test_pip_fail(temp_cfg, logged):
    pspec = PackageSpec(temp_cfg, "bogus")
    venv = PythonVenv(pspec, folder="")
    assert str(venv) == ""
    with patch("pickley.package.PythonVenv._run_pip", return_value=runez.program.RunResult("", "some\nerror", code=1)):
        with pytest.raises(SystemExit):
            venv.pip_install("foo")

        assert "some\nerror" == logged.stdout.pop()

    r = runez.program.RunResult("", "foo\nNo matching distribution for ...\nYou should consider upgrading pip", code=1)
    with patch("pickley.package.PythonVenv._run_pip", return_value=r):
        with pytest.raises(SystemExit):
            venv.pip_install("foo")

        assert "No matching distribution for ..." in logged
        assert "You should consider" not in logged
