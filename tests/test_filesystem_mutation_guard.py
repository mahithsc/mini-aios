from aios_core.tools import filesystem


def test_write_rejects_direct_durable_app_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "get_workspace_dir", lambda: tmp_path)
    target = tmp_path / "apps" / "app_cloud123" / "aios.deploy.yaml"

    result = filesystem.write(str(target), "version: 1\n")

    assert "direct modification of durable app source is not allowed" in result
    assert "Use codex_start" in result
    assert not target.exists()


def test_edit_rejects_direct_durable_app_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "get_workspace_dir", lambda: tmp_path)
    target = tmp_path / "apps" / "app_cloud123" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_text("before\n")

    result = filesystem.edit(str(target), "before", "after")

    assert "direct modification of durable app source is not allowed" in result
    assert target.read_text() == "before\n"


def test_write_still_allows_non_app_scratch_files(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "get_workspace_dir", lambda: tmp_path)
    target = tmp_path / "session" / "chat" / "notes.txt"

    result = filesystem.write(str(target), "allowed\n")

    assert result.startswith("ok:")
    assert target.read_text() == "allowed\n"
