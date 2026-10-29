"""
restore_published_snapshot.py - copy the last published live data into the working tree.

The refresh workflow deploys generated files to GitHub Pages and does not commit them, so the
previous snapshot only exists on the published site. Run this before the pipelines: a pipeline
that then fails leaves the last published file in place (shown as stale by the dashboard)
instead of an older committed file.

Restores:
  - every layer in data/pipeline/live/manifest.json, and the manifest itself
  - every risk-response in data/pipeline/risk/index.json that is newer than the local copy
  - every AI briefing in data/pipeline/briefing/index.json, and that index (it carries the
    input fingerprints briefing_pipeline.py uses to skip unchanged regions)

Best effort: a missing site or file is reported and skipped; the exit code is always 0.
Standard library only.

  python data/restore_published_snapshot.py --base-url https://<owner>.github.io/<repo>
"""

import argparse
import json
from pathlib import Path

if __package__:
    from . import wildfire_pipeline as wildfire
else:
    import wildfire_pipeline as wildfire

DATA_DIR = Path(__file__).resolve().parent


def fetch(base_url, rel_path):
    return wildfire.get_json(f"{base_url}/data/{rel_path}", timeout=30, attempts=2)


def target(rel_path):
    """Local path for a published path; refuses anything outside data/."""
    path = (DATA_DIR / rel_path).resolve()
    if not path.is_relative_to(DATA_DIR):
        raise ValueError(f"path outside data/: {rel_path}")
    return path


def write(rel_path, doc):
    path = target(rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def restore_live_layers(base_url):
    manifest = fetch(base_url, "pipeline/live/manifest.json")
    restored = 0
    for layer in manifest.get("layers", []):
        try:
            write(layer["path"], fetch(base_url, layer["path"]))
            restored += 1
        except Exception as e:  # noqa: BLE001 - best effort
            print(f"  skipped {layer.get('path')}: {e}")
    write("pipeline/live/manifest.json", manifest)
    print(f"Restored {restored} live layers published at {manifest.get('generatedAt')}")


def local_generated_at(rel_path):
    try:
        return json.loads(target(rel_path).read_text(encoding="utf-8")).get("generatedAt") or ""
    except (OSError, ValueError, AttributeError):
        return ""


def restore_risk_files(base_url):
    index = fetch(base_url, "pipeline/risk/index.json")
    restored = 0
    for entry in index.get("entries", []):
        path = entry.get("path", "")
        if (entry.get("generatedAt") or "") <= local_generated_at(path):
            continue
        try:
            write(path, fetch(base_url, path))
            restored += 1
        except Exception as e:  # noqa: BLE001 - best effort
            print(f"  skipped {path}: {e}")
    print(f"Restored {restored} newer risk files published at {index.get('builtAt')}")


def restore_briefing_files(base_url):
    index = fetch(base_url, "pipeline/briefing/index.json")
    restored = 0
    for entry in index.get("entries", []):
        path = entry.get("path", "")
        if (entry.get("generatedAt") or "") <= local_generated_at(path):
            continue
        try:
            write(path, fetch(base_url, path))
            restored += 1
        except Exception as e:  # noqa: BLE001 - best effort
            print(f"  skipped {path}: {e}")
    write("pipeline/briefing/index.json", index)
    print(f"Restored {restored} newer briefings published at {index.get('builtAt')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="published site URL, for example from actions/configure-pages")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    for step in (restore_live_layers, restore_risk_files, restore_briefing_files):
        try:
            step(base_url)
        except Exception as e:  # noqa: BLE001 - nothing published yet is a normal first run
            print(f"{step.__name__}: nothing restored ({e})")


if __name__ == "__main__":
    main()
