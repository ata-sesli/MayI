"""Resolve a pinned Auto checkpoint into persistent Hugging Face cache."""

import re
from pathlib import Path

MODEL = re.compile(
    r"hf://(ProCreations/auto-200m-2-int8)@(2501a22901e8cc520c746a86f3f9d04f7feaaefb)\Z"
)


def resolve_model(spec, data_dir):
    match = MODEL.fullmatch(spec)
    if not match:
        if spec.startswith("hf://"):
            raise ValueError("Unsupported or unpinned Auto model")
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
