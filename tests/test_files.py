"""Tests for moving files between the SAS server and this machine.

The safety property under test: an agent can never choose an arbitrary local
path, in either direction.
"""

import pytest

from sas_mcp import files
from sas_mcp.files import TransferError
from sas_mcp.session import SASSessionManager


class FakeSAS:
    def __init__(self, succeed=True, writes=True):
        self.succeed = succeed
        self.writes = writes
        self.downloads = []
        self.uploads = []
        self.listing = ["report.xlsx", "data.csv"]

    def download(self, localfile, remotefile, overwrite=True, **kw):
        self.downloads.append((localfile, remotefile))
        if self.succeed and self.writes:
            open(localfile, "w").write("xlsx bytes")
        return {"Success": self.succeed, "LOG": "log text"}

    def upload(self, localfile, remotefile, overwrite=True, **kw):
        self.uploads.append((localfile, remotefile))
        return {"Success": self.succeed, "LOG": "log text"}

    def dirlist(self, path):
        return self.listing

    def file_info(self, filepath, results="dict", fileref="_spfinfo", quiet=False):
        return {"Filename": filepath, "Size": "1024"}


def manager(tmp_path, sas=None):
    mgr = SASSessionManager(file_dir=str(tmp_path))
    mgr._sas = sas or FakeSAS()
    return mgr


# --- remote filenames --------------------------------------------------------


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("/home/user/report.xlsx", "report.xlsx"),
        (r"C:\SASOutput\report.xlsx", "report.xlsx"),
        ("/saswork/a/b/summary.csv", "summary.csv"),
        ('  "/home/u/q.xlsx"  ', "q.xlsx"),
    ],
)
def test_remote_basename_handles_either_server_os(remote, expected):
    """The SAS server's OS is independent of this machine's."""
    assert files.remote_basename(remote) == expected


@pytest.mark.parametrize("bad", ["/home/user/", "", "   ", "/a/b/.."])
def test_remote_basename_rejects_pathless_input(bad):
    with pytest.raises(TransferError):
        files.remote_basename(bad)


# --- local filename safety ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.xlsx", "a/b.xlsx", r"a\b.xlsx", "/etc/passwd",
        "..", ".", ".hidden", "with space.xlsx", "semi;colon",
    ],
)
def test_unsafe_local_names_refused(bad):
    with pytest.raises(TransferError):
        files.safe_local_name(bad)


@pytest.mark.parametrize(
    "ok", ["report.xlsx", "a-b_c.csv", "data2024.sas7bdat", "x+y.txt"]
)
def test_reasonable_local_names_accepted(ok):
    assert files.safe_local_name(ok) == ok


def test_resolve_within_refuses_traversal(tmp_path):
    with pytest.raises(TransferError):
        files.resolve_within(tmp_path, "../outside.xlsx")


# --- download ----------------------------------------------------------------


def test_download_lands_in_the_transfer_directory(tmp_path):
    mgr = manager(tmp_path)
    r = files.download(mgr, "/home/u/report.xlsx")
    assert r["status"] == "ok"
    assert r["local_path"] == str(tmp_path / "report.xlsx")
    assert (tmp_path / "report.xlsx").is_file()
    assert r["size_bytes"] > 0


def test_download_cannot_be_aimed_at_an_arbitrary_path(tmp_path):
    """An agent must not be able to overwrite anything the user can write."""
    mgr = manager(tmp_path)
    for evil in ["../../.bashrc", "/etc/passwd", "sub/dir.xlsx"]:
        with pytest.raises(TransferError):
            files.download(mgr, "/home/u/report.xlsx", local_name=evil)


def test_download_refuses_to_clobber_by_default(tmp_path):
    mgr = manager(tmp_path)
    files.download(mgr, "/home/u/report.xlsx")
    with pytest.raises(TransferError, match="already exists"):
        files.download(mgr, "/home/u/report.xlsx")


def test_download_overwrite_when_asked(tmp_path):
    mgr = manager(tmp_path)
    files.download(mgr, "/home/u/report.xlsx")
    r = files.download(mgr, "/home/u/report.xlsx", overwrite=True)
    assert r["status"] == "ok"


def test_download_reports_sas_side_failure(tmp_path):
    mgr = manager(tmp_path, FakeSAS(succeed=False))
    with pytest.raises(TransferError, match="did not succeed"):
        files.download(mgr, "/home/u/missing.xlsx")


def test_download_detects_missing_file_despite_reported_success(tmp_path):
    """Trust the filesystem over the return code."""
    mgr = manager(tmp_path, FakeSAS(succeed=True, writes=False))
    with pytest.raises(TransferError, match="no local file appeared"):
        files.download(mgr, "/home/u/report.xlsx")


def test_download_custom_name(tmp_path):
    mgr = manager(tmp_path)
    r = files.download(mgr, "/home/u/report.xlsx", local_name="renamed.xlsx")
    assert r["local_path"].endswith("renamed.xlsx")


# --- upload ------------------------------------------------------------------


def test_upload_sends_from_the_transfer_directory(tmp_path):
    mgr = manager(tmp_path)
    (tmp_path / "input.csv").write_text("a,b\n1,2\n")
    r = files.upload(mgr, "input.csv", "/home/u/input.csv")
    assert r["status"] == "ok"
    assert mgr._sas.uploads == [(str(tmp_path / "input.csv"), "/home/u/input.csv")]


def test_upload_cannot_read_arbitrary_local_files(tmp_path):
    """Otherwise an agent could send any readable file to a remote server."""
    mgr = manager(tmp_path)
    for evil in ["../secret.txt", "/etc/passwd", r"..\..\creds"]:
        with pytest.raises(TransferError):
            files.upload(mgr, evil, "/home/u/x")


def test_upload_missing_file_explains_the_restriction(tmp_path):
    mgr = manager(tmp_path)
    with pytest.raises(TransferError, match="uploads are restricted"):
        files.upload(mgr, "absent.csv", "/home/u/x")


def test_upload_reports_sas_side_failure(tmp_path):
    mgr = manager(tmp_path, FakeSAS(succeed=False))
    (tmp_path / "input.csv").write_text("x")
    with pytest.raises(TransferError, match="did not succeed"):
        files.upload(mgr, "input.csv", "/home/u/input.csv")


# --- listing -----------------------------------------------------------------


def test_list_remote_returns_entries(tmp_path):
    mgr = manager(tmp_path)
    r = files.list_remote(mgr, "/home/u")
    assert r["entries"] == ["report.xlsx", "data.csv"]
    assert r["count"] == 2


def test_remote_info_returns_attributes(tmp_path):
    mgr = manager(tmp_path)
    r = files.remote_info(mgr, "/home/u/report.xlsx")
    assert r["info"]["Size"] == "1024"


def test_transfer_directory_is_created_on_demand(tmp_path):
    target = tmp_path / "nested" / "transfers"
    mgr = SASSessionManager(file_dir=str(target))
    assert mgr.file_dir.is_dir()


# --- where output lands ------------------------------------------------------


def test_defaults_to_the_working_directory(tmp_path, monkeypatch):
    """A temporary directory is invisible to the user; the working folder
    shows up in the editor's file tree."""
    monkeypatch.chdir(tmp_path)
    mgr = SASSessionManager()
    assert mgr.file_dir == tmp_path / "sas-mcp" / "files"
    assert mgr.log_dir == tmp_path / "sas-mcp" / "logs"


def test_output_directory_is_git_ignored(tmp_path, monkeypatch):
    """It appears inside whatever project the editor has open, and SAS output
    can contain real data."""
    monkeypatch.chdir(tmp_path)
    SASSessionManager().file_dir
    gitignore = tmp_path / "sas-mcp" / ".gitignore"
    assert gitignore.is_file()
    assert "*" in gitignore.read_text()


def test_existing_gitignore_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "sas-mcp"
    base.mkdir()
    (base / ".gitignore").write_text("# mine\n")
    SASSessionManager().file_dir
    assert (base / ".gitignore").read_text() == "# mine\n"


def test_explicit_setting_wins_over_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chosen = tmp_path / "elsewhere"
    mgr = SASSessionManager(file_dir=str(chosen))
    assert mgr.file_dir == chosen


def test_falls_back_to_temp_when_cwd_is_unwritable(monkeypatch, tmp_path):
    """Some clients start the server somewhere it cannot write, such as /."""
    import sas_mcp.session as sess

    def refuse(self, *a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(sess.Path, "mkdir", refuse)
    d = sess.resolve_output_dir(None, "files")
    assert d.is_dir()
    assert "sas-mcp-files-" in d.name


def test_logs_and_files_are_separate_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mgr = SASSessionManager()
    assert mgr.log_dir != mgr.file_dir
    assert mgr.log_dir.parent == mgr.file_dir.parent
