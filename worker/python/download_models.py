"""Build-time only: fetch immutable model revisions; runtime is offline."""
import json
from pathlib import Path
from huggingface_hub import snapshot_download

models = json.loads(Path(__file__).with_name("models.json").read_text())
for name, model in models.items():
    snapshot_download(repo_id=model["repo"], revision=model["revision"],
                      local_dir=f"/app/models/{name}",
                      ignore_patterns=["*.bin", "README.md", ".gitattributes"])
