"""GitHub 'latest' 릴리스를 이용한 자동 업데이트.

릴리스에는 플랫폼별 바이너리(PhotoReceiver.dmg / .exe)와 update-<mac|win>.json 이 올라간다.
앱은 json의 빌드 시각이 자기 것보다 새로우면 업데이트로 보고, 바이너리를 받아
sha256을 검증한 뒤 실행 중인 앱을 교체하고 다시 시작한다.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "jcha0607/PhotoReceiver"
BASE = os.environ.get("PHOTORECEIVER_UPDATE_BASE",
                      f"https://github.com/{REPO}/releases/download/latest")
RELEASE_PAGE = f"https://github.com/{REPO}/releases/tag/latest"

try:
    from build_info import BUILT, COMMIT  # 빌드 스크립트가 생성
except ImportError:
    COMMIT, BUILT = "dev", 0

PLATFORM = "mac" if sys.platform == "darwin" else "win" if sys.platform.startswith("win") else "other"
ASSETS = {"mac": "PhotoReceiver.dmg", "win": "PhotoReceiver.exe"}
FROZEN = bool(getattr(sys, "frozen", False))


class UpdateError(Exception):
    pass


def _open(url: str, timeout: int = 15):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "PhotoReceiver"}),
                                  timeout=timeout)


def check() -> dict | None:
    """새 버전이 있으면 manifest(dict)를, 없으면 None을 반환한다."""
    if PLATFORM not in ASSETS:
        raise UpdateError("이 운영체제는 자동 업데이트를 지원하지 않습니다.")
    try:
        with _open(f"{BASE}/update-{PLATFORM}.json") as r:
            m = json.load(r)
    except Exception as e:  # noqa: BLE001 - 네트워크/JSON 오류를 한 메시지로
        raise UpdateError(f"업데이트 정보를 가져오지 못했습니다.\n{e}") from e
    if m.get("arch") and m["arch"] != platform.machine():
        return None  # 다른 CPU 아키텍처용 빌드는 설치하지 않는다
    return m if int(m.get("built", 0)) > BUILT else None


def download(m: dict, dest_dir: Path, progress) -> Path:
    path = dest_dir / ASSETS[PLATFORM]
    digest = hashlib.sha256()
    with _open(f"{BASE}/{ASSETS[PLATFORM]}", timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or m.get("size") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress(done, total)
    if m.get("sha256") and digest.hexdigest() != m["sha256"]:
        raise UpdateError("내려받은 파일의 무결성 검증에 실패했습니다. 잠시 후 다시 시도하세요.")
    return path


def _run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise UpdateError(f"{cmd[0]} 실패: {(r.stderr or r.stdout).strip()}")


MAC_SWAP = r'''
for i in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
mv "$APP" "$APP.old" || exit 1
if mv "$NEW" "$APP"; then rm -rf "$APP.old"; else mv "$APP.old" "$APP"; exit 1; fi
xattr -cr "$APP" 2>/dev/null
rm -rf "$(dirname "$NEW")"
[ -n "$RELAUNCH" ] && open "$APP"
exit 0
'''

WIN_SWAP = r'''@echo off
set n=0
:retry
ping 127.0.0.1 -n 2 >nul
move /y "{new}" "{cur}" >nul 2>&1
if errorlevel 1 (
  set /a n+=1
  if %n% lss 30 goto retry
  exit /b 1
)
start "" "{cur}"
(goto) 2>nul & del "%~f0"
'''


def install_mac(dmg: Path, relaunch: bool = True, app: Path | None = None):
    app = app or Path(sys.executable).resolve().parents[2]  # X.app/Contents/MacOS/exe
    if app.suffix != ".app":
        raise UpdateError("앱 번들(.app)에서 실행 중이 아닙니다.")
    if not os.access(app.parent, os.W_OK):
        raise UpdateError(f"{app.parent} 에 쓸 수 없습니다. 앱을 응용 프로그램 폴더로 옮긴 뒤 다시 시도하세요.")
    mnt = tempfile.mkdtemp(prefix="pr_mnt_")
    stage_dir = Path(tempfile.mkdtemp(prefix="pr_new_"))
    _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", mnt, str(dmg)])
    try:
        src = next(Path(mnt).glob("*.app"), None)
        if src is None:
            raise UpdateError("DMG 안에서 앱을 찾지 못했습니다.")
        _run(["ditto", str(src), str(stage_dir / src.name)])
    finally:
        subprocess.run(["hdiutil", "detach", "-force", mnt], capture_output=True)
    env = {**os.environ, "PID": str(os.getpid()), "APP": str(app),
           "NEW": str(stage_dir / src.name), "RELAUNCH": "1" if relaunch else ""}
    subprocess.Popen(["/bin/bash", "-c", MAC_SWAP], env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_win(exe: Path):
    cur = Path(sys.executable).resolve()
    if not os.access(cur.parent, os.W_OK):
        raise UpdateError(f"{cur.parent} 에 쓸 수 없습니다. 쓰기 가능한 폴더로 옮겨 실행하세요.")
    new = cur.with_name(cur.stem + ".new.exe")
    shutil.move(str(exe), new)
    bat = Path(tempfile.mkdtemp(prefix="pr_upd_")) / "update.bat"
    bat.write_text(WIN_SWAP.format(new=new, cur=cur), encoding="mbcs")
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})


def apply(m: dict, progress):
    """내려받아 교체 스크립트를 띄운다. 성공하면 호출 쪽이 앱을 종료해야 한다."""
    if not FROZEN:
        raise UpdateError("소스에서 실행 중이라 자동 설치는 지원하지 않습니다.")
    tmp = Path(tempfile.mkdtemp(prefix="pr_dl_"))
    path = download(m, tmp, progress)
    (install_mac if PLATFORM == "mac" else install_win)(path)
