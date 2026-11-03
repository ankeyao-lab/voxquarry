"""Load the pinned official compaction plugin offline from an existing package cache.

No package installation, Docker, provider calls, or SDK source modification.
Pass the cache directory containing @deepseek-ai. Native summary/session-restore
integration remains the separate native_sdk_smoke.py fixture.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-cache", type=Path, required=True)
    args = parser.parse_args()
    packages = args.sdk_cache.resolve() / "@deepseek-ai"
    package = json.loads((packages / "dsh-compaction-basic/package.json").read_text())
    if package.get("version") != "0.1.5-rc.1":
        parser.error("The fixture requires the pinned official 0.1.5-rc.1 package cache")
    node = shutil.which("node")
    if not node:
        parser.error("Node.js is required")
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="voxquarry-native-compaction-") as directory:
        work = Path(directory)
        (work / "node_modules").mkdir()
        (work / "node_modules/@deepseek-ai").symlink_to(packages, target_is_directory=True)
        shutil.copyfile(root / "compaction_policy_fixture.mjs", work / "fixture.mjs")
        result = subprocess.run([node, "--preserve-symlinks", str(work / "fixture.mjs"), str(root / "research.patch.yml")],
                                check=True, capture_output=True, text=True, timeout=30)
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
