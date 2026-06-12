from __future__ import annotations

from omnicode_adapters.cli.commands import serve_cmd


def test_cloud_index_mode_accepts_sync_but_blocks_patch_apply(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OMNICODE_MODE", raising=False)
    monkeypatch.delenv("OMNICODE_READ_ONLY", raising=False)
    monkeypatch.delenv("OMNICODE_ALLOW_APPLY_PATCH", raising=False)
    monkeypatch.delenv("OMNICODE_EMBEDDING_TORCH_THREADS", raising=False)

    serve_cmd._apply_mode_preset("cloud-index")

    assert "cloud-index" in serve_cmd._MODE_PRESETS
    assert serve_cmd.os.environ["OMNICODE_MODE"] == "hybrid"
    assert serve_cmd.os.environ["OMNICODE_READ_ONLY"] == "false"
    assert serve_cmd.os.environ["OMNICODE_ALLOW_APPLY_PATCH"] == "false"
    assert serve_cmd.os.environ["OMNICODE_EMBEDDING_TORCH_THREADS"] == "2"
    assert serve_cmd._MODE_PRESETS["cloud"]["OMNICODE_READ_ONLY"] == "true"
    assert serve_cmd._MODE_PRESETS["cloud-index"]["OMNICODE_READ_ONLY"] == "false"
