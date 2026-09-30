"""Resolve a pinned Julia checkpoint into persistent Hugging Face cache."""

import re
from pathlib import Path

MODEL = re.compile(r"hf://(SupersonicLabs/Julia-1)@([0-9a-f]{40})\Z")


def resolve_model(spec, data_dir):
    match = MODEL.fullmatch(spec)
    if not match:
        if spec.startswith("hf://"):
            raise ValueError("Unsupported or unpinned Julia model")
        return spec
    from huggingface_hub import snapshot_download

    options = {
        "repo_id": match[1],
        "revision": match[2],
        "cache_dir": str(Path(data_dir) / "models"),
    }
    try:
        return snapshot_download(**options, local_files_only=True)
    except OSError:
        return snapshot_download(**options, local_files_only=False)
