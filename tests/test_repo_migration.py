import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repo_migration as rm  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(*args, cwd=None):
    import os

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    work = tmp_path / "work"
    source = tmp_path / "source.git"
    destination = tmp_path / "destination.git"
    git("init", "-b", "main", str(work))
    (work / "a.txt").write_text("a")
    git("add", ".", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("branch", "feature", cwd=work)
    git("tag", "v1", cwd=work)
    git("init", "--bare", str(source))
    git("init", "--bare", str(destination))
    git("push", str(source), "main", "feature", "v1", cwd=work)
    return source, destination


def make(tmp_path, repos=None, name="m"):
    source, destination = repos if repos else (
        "https://bitbucket.org/ws/src.git",
        "https://github.com/org/dst.git",
    )
    return rm.Migration(
        bitbucket_workspace="ws",
        bitbucket_repo="src",
        github_org="org",
        github_repo="dst",
        owner="owner",
        working_directory=tmp_path / name,
        source_url=str(source),
        destination_url=str(destination),
    )


def test_safe_text_redacts():
    text = "https://user:pw@host/x Authorization: Bearer abc ghp_ABC123"
    safe = rm.to_safe_text(text)
    assert "pw" not in safe and "abc" not in safe and "ghp_ABC123" not in safe


def test_parse_and_compare_refs():
    sha = "a" * 40
    refs = rm.parse_ref_lines([f"{sha}\trefs/heads/main"])
    assert refs == {"refs/heads/main": sha}
    assert rm.compare_refs(refs, dict(refs)) == []
    diffs = rm.compare_refs(
        {"refs/heads/a": sha, "refs/heads/b": sha}, {"refs/heads/b": "b" * 40, "refs/heads/c": sha}
    )
    assert "Missing destination ref: refs/heads/a" in diffs
    assert "Unexpected destination ref: refs/heads/c" in diffs
    assert any(d.startswith("Hash mismatch for refs/heads/b") for d in diffs)
    with pytest.raises(rm.MigrationError):
        rm.parse_ref_lines(["garbage"])


def test_compare_sets():
    assert rm.compare_sets([], [], "LFS object") == []
    diffs = rm.compare_sets(["a", "b"], ["b", "c"], "LFS object")
    assert diffs == ["Missing destination LFS object: a", "Unexpected destination LFS object: c"]


@pytest.mark.parametrize("name", ["../x", "a/b", "user:pw@x", ".hidden", "repo.git"])
def test_rejects_bad_names(name):
    with pytest.raises(rm.MigrationError):
        rm.assert_safe_name(name, "f")


def test_accepts_good_names():
    rm.assert_safe_name("my-repo_1.0", "f")


def test_state_lifecycle(tmp_path):
    m = make(tmp_path)
    assert m.get_state() is None
    m.assert_can_start()
    m.write_state("NotStarted")
    m.write_state("CloneCompleted")
    m.assert_can_start()
    state = m.get_state()
    assert [h["status"] for h in state["history"]] == ["NotStarted", "CloneCompleted"]
    m.write_state("PushStarted")
    with pytest.raises(rm.MigrationError, match="post-push"):
        m.assert_can_start()


def test_state_identity_mismatch(tmp_path):
    m = make(tmp_path)
    m.write_state("NotStarted")
    other = make(tmp_path)
    other.owner = "someone-else"
    with pytest.raises(rm.MigrationError, match="different source"):
        other.assert_can_start()


def test_reconciliation_detects_differences(tmp_path, repos):
    source, destination = repos
    git(f"--git-dir={source}", "push", "--mirror", str(destination))
    m = make(tmp_path, repos)
    assert m.reconciliation_result()["exactMatch"]

    sha = git(f"--git-dir={source}", "rev-parse", "refs/heads/main")
    git(f"--git-dir={destination}", "update-ref", "refs/heads/destination-only", sha)
    result = m.reconciliation_result()
    assert not result["exactMatch"]
    assert result["unexpectedDestinationRefCount"] == 1
    assert "Unexpected destination ref: refs/heads/destination-only" in result["differences"]

    git(f"--git-dir={destination}", "update-ref", "-d", "refs/heads/destination-only")
    git(f"--git-dir={destination}", "update-ref", "-d", "refs/heads/feature")
    result = m.reconciliation_result()
    assert result["missingDestinationRefCount"] == 1
    assert "Missing destination ref: refs/heads/feature" in result["differences"]


def test_non_lfs_mirror_migration(tmp_path, repos):
    source, destination = repos
    m = make(tmp_path, repos)
    source_refs = rm.parse_ref_lines(m.remote_ref_lines(str(source), True))
    result = m.migrate_core(source_refs)
    assert result["sourceRefCount"] == 3
    assert result["destinationRefCount"] == 3
    assert m.get_state()["status"] == "Verified"
    destination_refs = rm.parse_ref_lines(m.remote_ref_lines(str(destination), True))
    assert rm.compare_refs(source_refs, destination_refs) == []


def test_approved_retry_restores_missing_refs(tmp_path, repos):
    source, destination = repos
    git(f"--git-dir={source}", "push", "--mirror", str(destination))
    git(f"--git-dir={destination}", "update-ref", "-d", "refs/heads/feature")
    m = make(tmp_path, repos)
    m.write_state("PushStarted")
    reconciliation = m.reconciliation_result()
    refs = m.comparable_refs(str(source), True)
    result = m.migrate_core(
        refs,
        approved_retry=True,
        expected_source=reconciliation["sourceRefs"],
        expected_destination=reconciliation["destinationRefs"],
        approver="approver",
        reference="CHANGE-123",
    )
    assert result["sourceRefCount"] == 3
    state = m.get_state()
    assert state["status"] == "Verified"
    statuses = [h["status"] for h in state["history"]]
    assert "CloneCompleted" not in statuses
    events = [h.get("details", {}).get("event") for h in state["history"]]
    assert "RecoveryCloneCompleted" in events
    assert m.reconciliation_result()["exactMatch"]


def test_sign_off_after_verification(tmp_path):
    m = make(tmp_path)
    m.write_state("Verified")
    verification_run = m.get_state()["verificationRunId"]
    m.run_id = "later-run"
    m.write_state("Verified", {"event": "Reconciled"})
    record = m.sign_off("repository-owner", "CHANGE-456", "ok")
    assert record["status"] == "Approved"
    assert record["technicalRunId"] == verification_run
    assert m.get_state()["status"] == "SignedOff"
    assert m.sign_off_path.exists()
    with pytest.raises(rm.MigrationError, match="already signed off"):
        m.sign_off("repository-owner", "CHANGE-456", None)


def test_sign_off_rejected_when_verification_failed(tmp_path):
    m = make(tmp_path)
    m.write_state("VerificationFailed")
    with pytest.raises(rm.MigrationError, match="requires migration status 'Verified'"):
        m.sign_off("repository-owner", "CHANGE-456", None)


def test_approval_metadata_required():
    with pytest.raises(rm.MigrationError, match="--approver"):
        rm.Migration.assert_approval("Retry", " ", "x")
    with pytest.raises(rm.MigrationError, match="--approval-reference"):
        rm.Migration.assert_approval("Retry", "a", None)


def test_tls_check_rejects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "true")
    with pytest.raises(rm.MigrationError, match="GIT_SSL_NO_VERIFY"):
        make(tmp_path).assert_tls()
