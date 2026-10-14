"""업데이트 manifest 생성: make_manifest.py <mac|win> <바이너리> <출력.json>"""
import hashlib
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, ".")
from build_info import BUILT, COMMIT  # noqa: E402
from version import __version__  # noqa: E402

plat, asset, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
Path(out).write_text(json.dumps({
    "version": __version__, "commit": COMMIT, "built": BUILT, "arch": platform.machine(),
    "asset": asset.name, "size": asset.stat().st_size,
    "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
}, indent=2))
print("manifest:", out)
