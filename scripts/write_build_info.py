"""build_info.py 를 생성한다 (앱이 자신의 빌드 식별자/시각을 알도록)."""
import os
import subprocess
import time
from pathlib import Path

commit = os.environ.get("BUILD_COMMIT", "")
if not commit:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                         stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "dev"
Path("build_info.py").write_text(f'COMMIT = "{commit[:7]}"\nBUILT = {int(time.time())}\n')
print("build_info.py:", commit[:7])
