"""Source fingerprint membership races and narrow runtime exclusions."""
import subprocess

import pytest

from jev_factorio import provenance


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text("ignored-output\n")
    (tmp_path / "source.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    return tmp_path, git


@pytest.mark.parametrize("created", ["new.py", "ignored-output", "checkpoint.json.temporary"])
def test_rechecks_filtered_untracked_membership(repo, monkeypatch, created):
    root, _ = repo
    prefixes = (root / "checkpoint.json.",)
    initial = provenance.source_revision(root, exclude_untracked_prefixes=prefixes)
    assert initial is not None
    real_run = provenance.subprocess.run
    listings = 0

    def racing_run(args, **kwargs):
        nonlocal listings
        result = real_run(args, **kwargs)
        if args == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
            listings += 1
            if listings == 1:
                (root / created).write_text("created during fingerprint\n")
        return result

    monkeypatch.setattr(provenance.subprocess, "run", racing_run)
    result = provenance.source_revision(root, exclude_untracked_prefixes=prefixes)
    assert listings == 2
    assert result == (None if created == "new.py" else initial)


def test_prefix_excludes_only_matching_untracked_siblings(repo):
    root, git = repo
    checkpoint = root / "checkpoint.json"
    options = {"exclude_untracked": (checkpoint,),
               "exclude_untracked_prefixes": (checkpoint.with_name(checkpoint.name + "."),)}
    initial = provenance.source_revision(root, **options)
    assert initial is not None
    checkpoint.write_text("runtime state\n")
    temporary = root / "checkpoint.json.temporary"
    temporary.write_text("atomic runtime state\n")
    assert provenance.source_revision(root, **options) == initial
    unrelated = root / "checkpoint.json-other"
    unrelated.write_text("source\n")
    assert provenance.source_revision(root, **options) != initial
    unrelated.unlink()
    nested = root / "nested"
    nested.mkdir()
    nested_sibling = nested / temporary.name
    nested_sibling.write_text("different directory\n")
    assert provenance.source_revision(root, **options) != initial
    nested_sibling.unlink()
    git("add", temporary.name)
    tracked = provenance.source_revision(root, **options)
    assert tracked is not None and tracked != initial
    temporary.write_text("changed tracked content\n")
    assert provenance.source_revision(root, **options) != tracked
