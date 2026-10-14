"""PhotoReceiver: 아이폰 사진을 QR코드 + 웹 업로드로 받아 리사이징하는 데스크톱 앱."""
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import qrcode
from flask import Flask, jsonify, request
from PIL import Image, ImageOps, ImageTk

import updater

try:
    from send2trash import send2trash
except ImportError:  # 없으면 원본 삭제 옵션을 비활성화
    send2trash = None

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC 지원 없이 동작

PORT = 8765
BASE_DIR = Path.home() / "Pictures" / "PhotoReceiver"
RECEIVED_DIR = BASE_DIR / "received"
DEFAULT_OUT_DIR = BASE_DIR / "resized"

UPLOAD_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>사진 보내기</title>
<style>
 body{font-family:-apple-system,sans-serif;margin:0;padding:24px;background:#f5f5f7;color:#1d1d1f}
 h1{font-size:22px;margin:0 0 16px}
 label.btn,button{display:block;width:100%;box-sizing:border-box;padding:16px;border:0;border-radius:12px;
  background:#0071e3;color:#fff;font-size:17px;text-align:center;margin-bottom:12px}
 button:disabled{background:#999}
 input[type=file]{display:none}
 #list div{background:#fff;border-radius:10px;padding:10px 12px;margin-bottom:8px;font-size:14px;word-break:break-all}
 .ok{color:#1a8f3c}.err{color:#d70015}
</style></head><body>
<h1>사진 보내기</h1>
<label class="btn">사진 선택<input id="f" type="file" accept="image/*" multiple></label>
<button id="go" disabled>보내기</button>
<div id="list"></div>
<script>
const f=document.getElementById('f'),go=document.getElementById('go'),list=document.getElementById('list');
f.onchange=()=>{list.innerHTML='';[...f.files].forEach((x,i)=>{const d=document.createElement('div');
 d.id='r'+i;d.textContent=x.name;list.appendChild(d)});go.disabled=!f.files.length;
 go.textContent='보내기 ('+f.files.length+'장)'};
go.onclick=async()=>{go.disabled=true;
 for(let i=0;i<f.files.length;i++){const d=document.getElementById('r'+i);d.textContent+=' ...';
  const fd=new FormData();fd.append('photo',f.files[i]);
  try{const r=await fetch('/upload',{method:'POST',body:fd});if(!r.ok)throw 0;
   d.className='ok';d.textContent='✓ '+f.files[i].name}catch(e){d.className='err';d.textContent='✗ '+f.files[i].name}}
 go.textContent='완료';};
</script></body></html>"""


# ---------------------------------------------------------------- 유틸
MAC = sys.platform == "darwin"
WIN = sys.platform.startswith("win")
FORMAT_LABEL = {"JPEG": "JPG", "MPO": "JPG", "HEIF": "HEIC", "PNG": "PNG",
                "WEBP": "WEBP", "BMP": "BMP", "GIF": "GIF", "TIFF": "TIFF"}


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ips() -> list[str]:
    """접속 주소 후보. 기본 경로(유선/무선 무관)의 IP를 맨 앞에 둔다."""
    cand = [local_ip()]
    try:
        cand += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    ips: list[str] = []
    for ip in cand:
        if not ip.startswith(("127.", "169.254.")) and ip not in ips:
            ips.append(ip)
    return ips or ["127.0.0.1"]


SETTINGS_FILE = BASE_DIR / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 설정을 못 저장해도 앱은 계속 동작


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[:120 - len(ext)] + ext
    return name or "photo.jpg"


def unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    n = 1
    while path.exists():
        path = directory / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return path


def fmt_size(n: float) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if WIN:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.run(["open" if MAC else "xdg-open", str(path)], check=False)


# ---------------------------------------------------------------- 이미지 처리
def encode(im: Image.Image, fmt: str, exif: bytes | None = None, **kw) -> bytes:
    buf = io.BytesIO()
    if exif:
        kw["exif"] = exif
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def fit_jpeg(im: Image.Image, limit: int, exif: bytes | None = None) -> tuple[bytes, tuple[int, int]]:
    """limit(byte) 이하가 되도록 품질을 이진 탐색하고, 품질 30으로도 크면 해상도를 줄인다."""
    while True:
        best = encode(im, "JPEG", exif, quality=30, optimize=True)
        if len(best) <= limit or min(im.size) < 64:
            break
        im = im.resize((int(im.width * 0.85), int(im.height * 0.85)), Image.LANCZOS)
    lo, hi = 30, 95
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data = encode(im, "JPEG", exif, quality=mid, optimize=True)
        if len(data) <= limit:
            lo, best = mid, data
        else:
            hi = mid - 1
    return best, im.size


def build_exif(im: Image.Image, created: float) -> bytes | None:
    """원본 EXIF를 유지하되(회전 정보·크기 태그 제외) 촬영일(생성일)이 반드시 들어가게 한다."""
    try:
        ex = im.getexif()
        for tag in (274, 256, 257):  # Orientation(이미 반영됨), ImageWidth/Length
            ex.pop(tag, None)
        sub = ex.get_ifd(0x8769)
        for tag in (40962, 40963):  # PixelXDimension/YDimension
            sub.pop(tag, None)
        stamp = datetime.fromtimestamp(created).strftime("%Y:%m:%d %H:%M:%S")
        for tag in (36867, 36868):  # DateTimeOriginal / DateTimeDigitized
            sub.setdefault(tag, stamp)
        ex.setdefault(306, stamp)  # DateTime
        return ex.tobytes()
    except Exception:  # noqa: BLE001 - EXIF가 깨져 있어도 리사이즈는 진행
        return None


def set_file_times(path: Path, ts: float):
    """파일 수정/생성 시각을 원본 생성일로 맞춘다 (EXIF가 없는 환경의 정렬 보조)."""
    os.utime(path, (ts, ts))  # macOS는 수정일을 과거로 돌리면 생성일도 함께 내려간다
    if WIN:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.CreateFileW.restype = wintypes.HANDLE
            h = k32.CreateFileW(str(path), 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
            if h and h != wintypes.HANDLE(-1).value:
                ft = ctypes.c_ulonglong(int((ts + 11644473600) * 10_000_000))
                k32.SetFileTime(h, ctypes.byref(ft), None, None)
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001 - 생성 시각 설정 실패는 무시
            pass


def process_image(src: Path, mode: str, value: int, fmt: str, quality: int,
                  max_kb: int) -> tuple[bytes, str, tuple[int, int], float]:
    """mode: 'long'(긴 변 px) | 'width'(가로 px) | 'percent'.
    (데이터, 확장자, 최종 해상도, 원본 생성일 timestamp) 반환."""
    with Image.open(src) as im:
        created = content_date(im, src)
        im = ImageOps.exif_transpose(im)
        exif = build_exif(im, created)
        w, h = im.size
        if mode == "long":
            scale = value / max(w, h)
        elif mode == "width":
            scale = value / w
        else:
            scale = value / 100
        scale = min(scale, 1.0)  # 확대하지 않음
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

        if fmt == "PNG" or (fmt == "원본 유지" and src.suffix.lower() == ".png"):
            return encode(im, "PNG", exif), ".png", im.size, created
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max_kb > 0:
            data, size = fit_jpeg(im, max_kb * 1024, exif)
            return data, ".jpg", size, created
        return encode(im, "JPEG", exif, quality=quality, optimize=True), ".jpg", im.size, created


def resize_image(src: Path, out_dir: Path, *settings) -> Path:
    data, ext, _, created = process_image(src, *settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = unique_path(out_dir, src.stem + ext)
    out.write_bytes(data)
    set_file_times(out, created)
    return out


# ---------------------------------------------------------------- 사진 정보
THUMB_MAX = 256  # 저장해 두는 썸네일 최대 크기 (그리드 크기 슬라이더의 상한)
THUMB_MIN, THUMB_STEP = 80, 8
LIST_THUMB = 44


@dataclass
class Photo:
    path: Path
    size: int
    kind: str
    created: float
    width: int
    height: int
    thumb: Image.Image
    tk_big: ImageTk.PhotoImage | None = None
    tk_big_size: int = 0
    tk_small: ImageTk.PhotoImage | None = None


def content_date(im: Image.Image, path: Path) -> float:
    """콘텐츠 생성일: EXIF 촬영 시각, 없으면 파일 생성/수정 시각."""
    try:
        exif = im.getexif()
        v = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if v:
            return datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:  # noqa: BLE001 - EXIF가 없거나 깨진 경우
        pass
    st = path.stat()
    return getattr(st, "st_birthtime", None) or min(st.st_ctime, st.st_mtime)


def load_photo(path: Path) -> Photo:
    with Image.open(path) as im:
        kind = FORMAT_LABEL.get(im.format or "", im.format or path.suffix.strip(".").upper())
        created = content_date(im, path)
        w, h = im.size
        if im.getexif().get(274, 1) in (5, 6, 7, 8):  # 회전된 사진은 가로/세로 교환
            w, h = h, w
        im.draft("RGB", (THUMB_MAX * 2, THUMB_MAX * 2))  # JPEG 디코딩 가속
        thumb = ImageOps.exif_transpose(im).convert("RGB")
        thumb.thumbnail((THUMB_MAX, THUMB_MAX))
    return Photo(path, path.stat().st_size, kind, created, w, h, thumb)


# ---------------------------------------------------------------- 서버
def create_server(on_received) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    @app.get("/")
    def index():
        return UPLOAD_PAGE

    @app.post("/upload")
    def upload():
        file = request.files.get("photo")
        if not file or not file.filename:
            return jsonify(error="no file"), 400
        RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
        path = unique_path(RECEIVED_DIR, safe_name(file.filename))
        file.save(path)
        on_received(path)
        return jsonify(ok=True)

    return app


# ---------------------------------------------------------------- UI
SEL_BG, SEL_OUT = "#cfe3ff", "#0071e3"
MODE_DEFAULT = {"long": 1920, "width": 1920, "percent": 50}
MODE_UNIT = {"long": "px", "width": "px", "percent": "%"}
MODE_PRESETS = {"long": (800, 1280, 1920, 2560, 3840), "width": (800, 1280, 1920, 2560, 3840),
                "percent": (25, 50, 75)}
MODE_RANGE = {"long": (200, 4096), "width": (200, 4096), "percent": (5, 100)}
TOGGLE_MASK = 0x8 if MAC else 0x4  # macOS: Command, 그 외: Ctrl


def shorten(name: str, limit=24) -> str:
    return name if len(name) <= limit else name[:limit // 2] + "…" + name[-(limit // 2 - 2):]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PhotoReceiver")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.photos: dict[Path, Photo] = {}
        self.order: list[Path] = []
        self.selected: set[Path] = set()
        self.anchor: Path | None = None
        self.cols = 1
        self.est_gen = 0
        self.est_job = None
        self.busy = False
        self._syncing = False
        self._closing = False

        self._build_ui()
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        min_w, min_h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.minsize(min_w, min_h)  # 모든 설정 항목이 잘리지 않는 최소 크기
        w, h = min(max(1120, min_w), sw), min(max(800, min_h), sh - 80)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")  # 화면 중앙

        threading.Thread(target=self._serve, daemon=True).start()
        self.after(150, self._poll)
        self._schedule_estimate()
        if updater.FROZEN:  # 소스 실행(개발 중)에서는 자동 확인하지 않는다
            self.after(3000, self._auto_check)

    # ---- 화면 구성 ----
    def _build_ui(self):
        self.settings = load_settings()
        self.auto_update = tk.BooleanVar(value=self.settings.get("auto_update", True))
        menubar = tk.Menu(self)
        um = tk.Menu(menubar, tearoff=False)
        um.add_command(label="업데이트 확인...", command=lambda: self._check_update(True))
        um.add_checkbutton(label="시작할 때 자동으로 업데이트 확인", variable=self.auto_update,
                           command=self._save_auto_update)
        um.add_separator()
        um.add_command(label="릴리스 페이지 열기", command=lambda: webbrowser.open(updater.RELEASE_PAGE))
        um.add_command(label="버전 정보", command=self._about)
        menubar.add_cascade(label="업데이트", menu=um)
        self.config(menu=menubar)

        left = ttk.Frame(self, padding=(12, 8))
        left.pack(side="left", fill="y")

        self.ips = local_ips()
        self.ip_var = tk.StringVar(value=self.ips[0])
        row = ttk.Frame(left); row.pack(fill="x")
        ttk.Label(row, text="접속 주소").pack(side="left")
        cb = ttk.Combobox(row, textvariable=self.ip_var, values=self.ips, width=16, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._update_qr())
        self.qr_label = ttk.Label(left)
        self.qr_label.pack(pady=(6, 0))
        self.url_label = ttk.Label(left, foreground="#0071e3")
        self.url_label.pack()
        ttk.Label(left, text="아이폰과 같은 공유기/네트워크에 연결되어 있으면 됩니다.\n"
                  "PC는 유선(LAN)이어도 됩니다.", foreground="#888",
                  justify="center").pack(pady=(0, 6))
        self._update_qr()

        box = ttk.LabelFrame(left, text="리사이즈 설정", padding=8)
        box.pack(fill="x")
        self.mode = tk.StringVar(value="percent")
        self.mode_values = dict(MODE_DEFAULT)
        self._prev_mode = "percent"
        for text, val in (("비율", "percent"), ("긴 변 기준", "long"), ("가로 기준", "width")):
            ttk.Radiobutton(box, text=text, value=val, variable=self.mode,
                            command=self._on_mode).pack(anchor="w")

        vrow = ttk.Frame(box); vrow.pack(fill="x", pady=(6, 0))
        self.value = tk.IntVar(value=MODE_DEFAULT["percent"])
        self.spin = ttk.Spinbox(vrow, from_=1, to=20000, textvariable=self.value, width=7)
        self.spin.pack(side="left")
        self.unit = ttk.Label(vrow, text="px", width=3)
        self.unit.pack(side="left", padx=(4, 0))
        self.scale = ttk.Scale(box, from_=200, to=4096, command=self._on_scale)
        self.scale.pack(fill="x", pady=4)
        self.preset_row = ttk.Frame(box); self.preset_row.pack(fill="x")

        row = ttk.Frame(box); row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="형식").pack(side="left")
        self.fmt = tk.StringVar(value="JPEG")
        ttk.Combobox(row, textvariable=self.fmt, values=["JPEG", "PNG", "원본 유지"],
                     width=9, state="readonly").pack(side="left", padx=4)
        row = ttk.Frame(box); row.pack(fill="x", pady=4)
        ttk.Label(row, text="JPEG 품질").pack(side="left")
        self.quality = tk.IntVar(value=85)
        ttk.Spinbox(row, from_=1, to=100, textvariable=self.quality, width=5).pack(side="left", padx=4)
        row = ttk.Frame(box); row.pack(fill="x")
        self.limit_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="용량 제한 (KB)", variable=self.limit_on).pack(side="left")
        self.limit_kb = tk.IntVar(value=500)
        ttk.Spinbox(row, from_=10, to=50000, increment=50, textvariable=self.limit_kb,
                    width=7).pack(side="left", padx=4)

        self.delete_orig = tk.BooleanVar(value=bool(self.settings.get("delete_original")) and send2trash is not None)
        ttk.Checkbutton(box, text="리사이즈 후 원본을 휴지통으로 이동", variable=self.delete_orig,
                        command=self._save_delete_orig,
                        state="normal" if send2trash else "disabled").pack(anchor="w", pady=(6, 0))

        ttk.Separator(box).pack(fill="x", pady=6)
        self.est_text = tk.StringVar()
        ttk.Label(box, textvariable=self.est_text, justify="left", width=40).pack(anchor="w")

        ttk.Label(left, text="저장 폴더").pack(anchor="w", pady=(8, 0))
        self.out_dir = tk.StringVar(value=str(DEFAULT_OUT_DIR))
        ttk.Entry(left, textvariable=self.out_dir, width=34).pack(fill="x")
        row = ttk.Frame(left); row.pack(fill="x")
        ttk.Button(row, text="폴더 변경", command=self._pick_dir).pack(side="right")
        ttk.Button(row, text="폴더 열기",
                   command=lambda: open_folder(Path(self.out_dir.get()))).pack(side="right", padx=4)

        right = ttk.Frame(self, padding=(4, 8, 12, 8))
        right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right); bar.pack(fill="x")
        ttk.Label(bar, text="받은 사진").pack(side="left")
        self.view = tk.StringVar(value="grid")
        for text, val in (("▦ 그리드", "grid"), ("☰ 리스트", "list")):
            ttk.Radiobutton(bar, text=text, value=val, variable=self.view, style="Toolbutton",
                            command=self._show_view).pack(side="left", padx=(8 if val == "grid" else 0, 0))
        self.thumb_size = int(self.settings.get("thumb_size", 160))
        self.thumb_size = min(THUMB_MAX, max(THUMB_MIN, self.thumb_size))
        self.cell_w, self.cell_h = self.thumb_size + 16, self.thumb_size + 44
        self.size_frame = ttk.Frame(bar)
        ttk.Label(self.size_frame, text="▫", foreground="#888").pack(side="left")
        self.size_scale = ttk.Scale(self.size_frame, from_=THUMB_MIN, to=THUMB_MAX, length=110,
                                    command=self._on_thumb_scale)
        self.size_scale.set(self.thumb_size)
        self.size_scale.pack(side="left", padx=2)
        ttk.Label(self.size_frame, text="▣", foreground="#888").pack(side="left")
        self.size_scale.bind("<ButtonRelease-1>", lambda _e: self._save_thumb_size())
        self.sort_desc = tk.BooleanVar(value=True)
        self.order_btn = ttk.Button(bar, width=9, command=self._toggle_order)
        self.order_btn.pack(side="right")
        self.sort_key = tk.StringVar(value="생성일")
        sc = ttk.Combobox(bar, textvariable=self.sort_key, values=["생성일", "파일 이름"],
                          width=9, state="readonly")
        sc.pack(side="right", padx=4)
        sc.bind("<<ComboboxSelected>>", lambda _e: self._on_sort_key())
        ttk.Label(bar, text="정렬").pack(side="right")

        self.holder = ttk.Frame(right)
        self.holder.pack(fill="both", expand=True, pady=6)
        self._build_grid()
        self._build_list()

        btns = ttk.Frame(right); btns.pack(fill="x")
        ttk.Button(btns, text="파일 추가...", command=self._add_files).pack(side="left")
        ttk.Button(btns, text="목록 비우기", command=self._clear).pack(side="left", padx=4)
        ttk.Button(btns, text="전체 선택", command=self._select_all).pack(side="left")
        ttk.Button(btns, text="선택 리사이즈", command=lambda: self._resize(True)).pack(side="right")
        ttk.Button(btns, text="전체 리사이즈", command=lambda: self._resize(False)).pack(side="right", padx=4)
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(right, textvariable=self.status, foreground="#555").pack(anchor="w", pady=(6, 0))

        for var in (self.mode, self.value, self.fmt, self.quality, self.limit_on, self.limit_kb):
            var.trace_add("write", lambda *_: self._schedule_estimate())
        self.value.trace_add("write", lambda *_: self._sync_scale())
        self._on_mode()
        self._refresh_order_btn()
        for seq in ("<Command-a>", "<Command-A>", "<Control-a>", "<Control-A>"):
            try:
                self.bind_all(seq, self._key_select_all)
            except tk.TclError:
                pass  # 이 플랫폼에 없는 수식어(Command 등)
        self._build_context_menu()

    def _build_grid(self):
        self.grid_frame = ttk.Frame(self.holder)
        self.canvas = tk.Canvas(self.grid_frame, bg="white", highlightthickness=1,
                                highlightbackground="#bbb", width=560, height=360)
        sb = ttk.Scrollbar(self.grid_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._draw_grid())
        self.canvas.bind("<Button-1>", self._grid_click)
        self.canvas.bind("<MouseWheel>", self._wheel)
        if self._has_event("<TouchpadScroll>"):
            self.canvas.bind("<TouchpadScroll>", self._touchpad)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"))

    def _build_list(self):
        self.list_frame = ttk.Frame(self.holder)
        ttk.Style().configure("Photo.Treeview", rowheight=LIST_THUMB + 8)
        cols = ("kind", "size", "date", "res")
        self.tree = ttk.Treeview(self.list_frame, columns=cols, style="Photo.Treeview",
                                 selectmode="extended")
        self.tree.heading("#0", text="이름", anchor="w", command=lambda: self._sort_by("파일 이름"))
        self.tree.heading("kind", text="종류")
        self.tree.heading("size", text="파일 크기")
        self.tree.heading("date", text="생성일", command=lambda: self._sort_by("생성일"))
        self.tree.heading("res", text="해상도")
        self.tree.column("#0", width=240, stretch=True)
        for c, w in (("kind", 60), ("size", 80), ("date", 130), ("res", 110)):
            self.tree.column(c, width=w, anchor="center", stretch=False)
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._tree_select)
        self.tree.bind("<MouseWheel>", self._tree_wheel)
        if self._has_event("<TouchpadScroll>"):
            self.tree.bind("<TouchpadScroll>", self._tree_touchpad)
        self._show_view()

    def _show_view(self):
        self.grid_frame.pack_forget()
        self.list_frame.pack_forget()
        grid = self.view.get() == "grid"
        (self.grid_frame if grid else self.list_frame).pack(fill="both", expand=True)
        if grid:
            self.size_frame.pack(side="left", padx=12)
        else:
            self.size_frame.pack_forget()
        self._redraw()

    def _on_thumb_scale(self, v):
        size = min(THUMB_MAX, max(THUMB_MIN, round(float(v) / THUMB_STEP) * THUMB_STEP))
        if size == self.thumb_size:
            return
        self.thumb_size = size
        self.cell_w, self.cell_h = size + 16, size + 44
        if getattr(self, "_thumb_job", None) is None:
            self._thumb_job = self.after(30, self._apply_thumb_size)

    def _apply_thumb_size(self):
        self._thumb_job = None
        top = self.canvas.yview()[0]  # 크기를 바꿔도 보던 위치 비율을 유지
        self._draw_grid()
        self.canvas.yview_moveto(top)

    def _save_thumb_size(self):
        self.settings["thumb_size"] = self.thumb_size
        save_settings(self.settings)

    def _has_event(self, seq: str) -> bool:
        try:  # Tk 8.6에는 TouchpadScroll 이벤트가 없다
            self.bind_class("PRProbe", seq, lambda _e: None)
            return True
        except tk.TclError:
            return False

    @staticmethod
    def _wheel_steps(delta: int) -> float:
        return delta / 120 if abs(delta) >= 120 else float(delta)  # Windows/Tk9: 120 단위, 구 macOS: 1 단위

    def _scroll_px(self, px: float):
        c = self.canvas
        region = c.cget("scrollregion").split()
        total = float(region[3]) if len(region) == 4 else 0.0
        if total <= c.winfo_height():
            return
        top = c.yview()[0] * total
        c.yview_moveto(max(0.0, min(1.0, (top + px) / total)))

    def _wheel(self, e):  # 마우스 휠
        self._scroll_px(-self._wheel_steps(e.delta) * self.cell_h / 3)
        return "break"

    def _precise(self, e) -> tuple[float, float]:
        dx, dy = (float(v) for v in self.tk.splitlist(self.tk.call("tk::PreciseScrollDeltas", e.delta)))
        return dx, dy

    def _touchpad(self, e):  # macOS 트랙패드 두 손가락 스크롤 (Tk 9)
        self._scroll_px(-self._precise(e)[1])
        return "break"

    _tree_acc = 0.0

    def _tree_wheel(self, e):
        self.tree.yview_scroll(int(-self._wheel_steps(e.delta) * 3), "units")
        return "break"

    def _tree_touchpad(self, e):
        self._tree_acc += -self._precise(e)[1] / (LIST_THUMB + 8)
        rows = int(self._tree_acc)
        if rows:
            self._tree_acc -= rows
            self.tree.yview_scroll(rows, "units")
        return "break"

    # ---- 전체 선택 / 우클릭 메뉴 ----
    def _key_select_all(self, _e):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry)):
            return None  # 입력칸에서는 글자 전체 선택(기본 동작)
        self._select_all()
        return "break"

    def _select_none(self):
        self.selected.clear()
        self._redraw()
        self._selection_changed()

    def _build_context_menu(self):
        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="전체 선택", accelerator="⌘A" if MAC else "Ctrl+A",
                             command=self._select_all)
        self.ctx.add_command(label="선택 해제", command=self._select_none)
        seqs = ("<Button-2>", "<Button-3>", "<Control-Button-1>") if MAC else ("<Button-3>",)
        for seq in seqs:
            self.canvas.bind(seq, self._grid_context)
            self.tree.bind(seq, self._tree_context)

    def _popup(self, e):
        try:
            self.ctx.tk_popup(e.x_root, e.y_root)
        finally:
            self.ctx.grab_release()
        return "break"

    def _grid_context(self, e):
        self.canvas.focus_set()
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        col, row = int(x // self.cell_w), int(y // self.cell_h)
        idx = row * self.cols + col
        if col < self.cols and 0 <= idx < len(self.order) and self.order[idx] not in self.selected:
            self.selected = {self.order[idx]}
            self.anchor = self.order[idx]
            self._draw_grid()
            self._selection_changed()
        return self._popup(e)

    def _tree_context(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        return self._popup(e)

    # ---- QR / 서버 ----
    def _update_qr(self):
        url = f"http://{self.ip_var.get()}:{PORT}"
        self.qr_img = ImageTk.PhotoImage(qrcode.make(url).resize((190, 190)))
        self.qr_label.configure(image=self.qr_img)
        self.url_label.configure(text=url)

    def _serve(self):
        try:
            create_server(lambda p: self.q.put(("new", p))).run(host="0.0.0.0", port=PORT, threaded=True)
        except OSError as e:
            self.q.put(("server_error", str(e)))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                getattr(self, "_on_" + msg[0])(*msg[1:])
        except queue.Empty:
            pass
        if not self._closing:
            self.after(150, self._poll)

    # ---- 사진 목록 ----
    def _on_new(self, path: Path):
        if path in self.photos:
            return

        def work():
            try:
                self.q.put(("loaded", load_photo(path)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("load_failed", path, str(e)))
        self.pool.submit(work)

    def _on_loaded(self, photo: Photo):
        small = photo.thumb.copy()
        small.thumbnail((LIST_THUMB, LIST_THUMB))
        photo.tk_small = ImageTk.PhotoImage(small)
        self.photos[photo.path] = photo
        self.order.append(photo.path)
        self._sort()
        self._redraw()
        self.status.set(f"받음: {photo.path.name}")
        self._schedule_estimate()

    def _on_load_failed(self, path: Path, err: str):
        self.status.set(f"열 수 없는 파일: {path.name} ({err})")

    def _on_server_error(self, err: str):
        messagebox.showerror("서버 오류", f"포트 {PORT}를 열 수 없습니다.\n{err}")

    def _add_files(self):
        for p in filedialog.askopenfilenames(
                filetypes=[("이미지", "*.jpg *.jpeg *.png *.heic *.heif *.webp *.bmp")]):
            self._on_new(Path(p))

    def _clear(self):
        self.photos.clear()
        self.order.clear()
        self.selected.clear()
        self._redraw()
        self._schedule_estimate()

    # ---- 정렬 ----
    def _sort(self):
        if self.sort_key.get() == "파일 이름":
            key = lambda p: p.name.lower()  # noqa: E731
        else:
            key = lambda p: self.photos[p].created  # noqa: E731
        self.order.sort(key=key, reverse=self.sort_desc.get())

    def _refresh_order_btn(self):
        if self.sort_key.get() == "파일 이름":
            self.order_btn.configure(text="Z → A" if self.sort_desc.get() else "A → Z")
        else:
            self.order_btn.configure(text="최신순" if self.sort_desc.get() else "오래된순")

    def _on_sort_key(self):
        self.sort_desc.set(self.sort_key.get() == "생성일")  # 생성일은 최신순, 이름은 A→Z가 기본
        self._refresh_order_btn()
        self._sort()
        self._redraw()

    def _sort_by(self, key: str):
        if self.sort_key.get() == key:
            self._toggle_order()
        else:
            self.sort_key.set(key)
            self._on_sort_key()

    def _toggle_order(self):
        self.sort_desc.set(not self.sort_desc.get())
        self._refresh_order_btn()
        self._sort()
        self._redraw()

    # ---- 그리기 ----
    def _redraw(self):
        if self.view.get() == "grid":
            self._draw_grid()
        else:
            self._fill_tree()

    def _big(self, ph: Photo) -> ImageTk.PhotoImage:
        size = self.thumb_size
        if ph.tk_big is None or ph.tk_big_size != size:
            t = ph.thumb.copy()
            t.thumbnail((size, size), Image.LANCZOS)
            ph.tk_big, ph.tk_big_size = ImageTk.PhotoImage(t), size
        return ph.tk_big

    def _draw_grid(self):
        c = self.canvas
        c.delete("all")
        cw, ch, size = self.cell_w, self.cell_h, self.thumb_size
        width = max(c.winfo_width(), cw)
        self.cols = max(1, width // cw)
        rows = -(-len(self.order) // self.cols)
        c.configure(scrollregion=(0, 0, width, max(rows * ch, 1)))
        for i, p in enumerate(self.order):
            ph = self.photos[p]
            r, col = divmod(i, self.cols)
            x0, y0 = col * cw, r * ch
            sel = p in self.selected
            c.create_rectangle(x0 + 4, y0 + 4, x0 + cw - 4, y0 + ch - 4,
                               fill=SEL_BG if sel else "", outline=SEL_OUT if sel else "", width=2)
            c.create_image(x0 + cw // 2, y0 + 10 + size // 2, image=self._big(ph))
            c.create_text(x0 + cw // 2, y0 + size + 32, text=shorten(p.name, max(10, cw // 8)),
                          width=cw - 16, fill="black")

    def _fill_tree(self):
        self._syncing = True
        self.tree.delete(*self.tree.get_children())
        for p in self.order:
            ph = self.photos[p]
            self.tree.insert("", "end", iid=str(p), text="  " + p.name, image=ph.tk_small, values=(
                ph.kind, fmt_size(ph.size),
                datetime.fromtimestamp(ph.created).strftime("%Y-%m-%d %H:%M"),
                f"{ph.width} × {ph.height}"))
        self.tree.selection_set([str(p) for p in self.order if p in self.selected])
        self._syncing = False

    # ---- 선택 ----
    def _grid_click(self, e):
        self.canvas.focus_set()
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        col, row = int(x // self.cell_w), int(y // self.cell_h)
        idx = row * self.cols + col
        p = self.order[idx] if col < self.cols and 0 <= idx < len(self.order) else None
        shift, toggle = e.state & 0x1, e.state & TOGGLE_MASK
        if p is None:
            if not (shift or toggle):
                self.selected.clear()
        elif shift and self.anchor in self.order:
            a, b = sorted((self.order.index(self.anchor), idx))
            self.selected = set(self.order[a:b + 1])
        elif toggle:
            self.selected ^= {p}
            self.anchor = p
        else:
            self.selected = {p}
            self.anchor = p
        self._draw_grid()
        self._selection_changed()

    def _tree_select(self, _e):
        if self._syncing:
            return
        self.selected = {Path(i) for i in self.tree.selection()}
        self._selection_changed()

    def _select_all(self):
        self.selected = set(self.order)
        self._redraw()
        self._selection_changed()

    def _selection_changed(self):
        n = len(self.selected)
        self.status.set(f"{n}장 선택됨" if n else "선택 없음 (리사이즈는 전체 대상)")
        self._schedule_estimate()

    # ---- 설정 / 예상 용량 ----
    def _on_mode(self):
        self.mode_values[self._prev_mode] = self._safe_value(self._prev_mode)
        mode = self.mode.get()
        self._prev_mode = mode
        lo, hi = MODE_RANGE[mode]
        self.spin.configure(from_=1, to=hi if mode == "percent" else 20000)
        self.scale.configure(from_=lo, to=hi)
        self.unit.configure(text=MODE_UNIT[mode])
        self.value.set(self.mode_values[mode])
        for w in self.preset_row.winfo_children():
            w.destroy()
        for v in MODE_PRESETS[mode]:
            ttk.Button(self.preset_row, text=f"{v}{MODE_UNIT[mode]}", width=6,
                       command=lambda v=v: self.value.set(v)).pack(side="left", padx=1)
        self._sync_scale()

    def _safe_value(self, mode: str) -> int:
        try:
            return int(self.value.get())
        except (tk.TclError, ValueError):
            return MODE_DEFAULT[mode]

    def _on_scale(self, v):
        if self._syncing_scale:
            return
        self.value.set(int(float(v)))

    _syncing_scale = False

    def _sync_scale(self):
        try:
            v = int(self.value.get())
        except (tk.TclError, ValueError):
            return
        self._syncing_scale = True
        self.scale.set(v)
        self._syncing_scale = False

    def _pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    def _settings(self):
        """현재 설정을 (mode, value, fmt, quality, max_kb)로 반환. 입력이 비정상이면 None."""
        try:
            value = max(1, int(self.value.get()))
            quality = min(100, max(1, int(self.quality.get())))
            max_kb = max(1, int(self.limit_kb.get())) if self.limit_on.get() else 0
        except (tk.TclError, ValueError):
            return None
        return self.mode.get(), value, self.fmt.get(), quality, max_kb

    def _targets(self) -> list[Path]:
        return [p for p in self.order if p in self.selected] or list(self.order)

    def _schedule_estimate(self):
        if self.est_job:
            self.after_cancel(self.est_job)
        self.est_job = self.after(400, self._start_estimate)

    def _start_estimate(self):
        self.est_job = None
        self.est_gen += 1
        gen, settings, targets = self.est_gen, self._settings(), self._targets()
        if not targets:
            self.est_text.set("사진을 받으면 리사이즈 후 예상 용량이\n여기에 표시됩니다.\n\n\n")
            return
        if settings is None:
            self.est_text.set("설정 값을 확인하세요.\n\n\n\n")
            return
        self.est_text.set("예상 용량 계산 중...\n\n\n\n")
        step = -(-len(targets) // 8)  # 많으면 8장만 샘플링해 비율로 추정
        sample = [(p, self.photos[p].size) for p in targets[::step][:8]]
        total = sum(self.photos[p].size for p in targets)
        picked = len(self.selected) > 0

        def work():
            try:
                res = [process_image(p, *settings) for p, _ in sample]
            except Exception as e:  # noqa: BLE001
                self.q.put(("estimate", gen, f"계산 실패: {e}\n\n\n\n"))
                return
            orig = sum(s for _, s in sample)
            est = sum(len(r[0]) for r in res)
            est_total = est / orig * total if orig else 0
            ph = self.photos[sample[0][0]]
            lines = [
                f"대상: {'선택' if picked else '전체'} {len(targets)}장",
                f"용량: {fmt_size(total)} → 약 {fmt_size(est_total)} ({est_total / total * 100:.0f}%)"
                if total else "",
                f"첫 사진: {ph.width}×{ph.height} → {res[0][2][0]}×{res[0][2][1]}, 약 {fmt_size(len(res[0][0]))}",
                f"(전체 중 {len(sample)}장 샘플 기준 추정)" if len(sample) < len(targets) else "",
            ]
            self.q.put(("estimate", gen, "\n".join(lines) + "\n"))
        self.pool.submit(work)

    def _on_estimate(self, gen: int, text: str):
        if gen == self.est_gen:
            self.est_text.set(text)

    # ---- 리사이즈 실행 ----
    def _resize(self, selected_only: bool):
        if self.busy:
            return
        files = [p for p in self.order if p in self.selected] if selected_only else list(self.order)
        if not files:
            messagebox.showinfo("알림", "리사이즈할 사진이 없습니다.")
            return
        settings = self._settings()
        if settings is None:
            messagebox.showwarning("설정 오류", "설정 값을 확인하세요.")
            return
        out_dir = Path(self.out_dir.get())
        trash = self.delete_orig.get() and send2trash is not None
        if trash and not messagebox.askokcancel(
                "원본 삭제", f"리사이즈에 성공한 원본 {len(files)}장을 휴지통으로 옮깁니다.\n계속할까요?"):
            return
        self.busy = True

        def work():
            ok, errors, trashed = 0, [], []
            for i, f in enumerate(files, 1):
                self.q.put(("progress", f"리사이즈 중... {i}/{len(files)}"))
                try:
                    resize_image(f, out_dir, *settings)
                    ok += 1
                except Exception as e:  # noqa: BLE001 - 파일별 오류를 모아서 표시
                    errors.append(f"{f.name}: {e}")
                    continue
                if trash:  # 리사이즈에 성공한 파일만 휴지통으로
                    try:
                        send2trash(str(f))
                        trashed.append(f)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{f.name}: 휴지통 이동 실패 ({e})")
            self.q.put(("done", ok, errors, str(out_dir), trashed))
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, text: str):
        self.status.set(text)

    def _on_done(self, ok: int, errors: list, out_dir: str, trashed: list):
        self.busy = False
        msg = f"{ok}장 리사이즈 완료 → {out_dir}"
        if trashed:
            gone = set(trashed)
            self.order = [p for p in self.order if p not in gone]
            self.selected -= gone
            for p in gone:
                self.photos.pop(p, None)
            self._redraw()
            self._schedule_estimate()
            msg += f" (원본 {len(trashed)}장 휴지통으로 이동)"
        self.status.set(msg)
        if errors:
            messagebox.showwarning("일부 실패", "\n".join(errors[:10]))

    # ---- 업데이트 ----
    @staticmethod
    def _fmt_built(ts) -> str:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if ts else "알 수 없음"

    def _save_delete_orig(self):
        self.settings["delete_original"] = self.delete_orig.get()
        save_settings(self.settings)

    def _save_auto_update(self):
        self.settings["auto_update"] = self.auto_update.get()
        save_settings(self.settings)

    def _about(self):
        messagebox.showinfo("버전 정보", f"PhotoReceiver\n빌드: {updater.COMMIT} ({self._fmt_built(updater.BUILT)})\n"
                            "업데이트: GitHub 'latest' 릴리스")

    def _auto_check(self):
        if self.auto_update.get():
            self._check_update(False)

    def _check_update(self, manual: bool):
        if manual:
            self.status.set("업데이트 확인 중...")

        def work():
            try:
                self.q.put(("update_result", updater.check(), manual, None))
            except updater.UpdateError as e:
                self.q.put(("update_result", None, manual, str(e)))
        self.pool.submit(work)

    def _on_update_result(self, m, manual: bool, err):
        if err:
            if manual:
                self.status.set("업데이트 확인 실패")
                messagebox.showwarning("업데이트", err)
            return
        if m is None:
            if manual:
                self.status.set("최신 버전입니다")
                messagebox.showinfo("업데이트", f"최신 버전입니다.\n빌드: {updater.COMMIT} ({self._fmt_built(updater.BUILT)})")
            return
        self.status.set("새 버전이 있습니다")
        info = (f"새 버전이 있습니다.\n\n현재: {updater.COMMIT} ({self._fmt_built(updater.BUILT)})\n"
                f"최신: {m.get('commit', '?')} ({self._fmt_built(m.get('built'))})\n\n")
        if not updater.FROZEN:
            if messagebox.askyesno("업데이트", info + "소스에서 실행 중이라 자동 설치는 안 됩니다.\n릴리스 페이지를 열까요?"):
                webbrowser.open(updater.RELEASE_PAGE)
        elif self.busy:
            messagebox.showinfo("업데이트", info + "리사이즈가 끝난 뒤 메뉴에서 다시 업데이트하세요.")
        elif messagebox.askyesno("업데이트", info + "지금 업데이트하고 앱을 다시 시작할까요?"):
            self._start_update(m)

    def _start_update(self, m: dict):
        self.busy = True

        def progress(done, total):
            self.q.put(("progress", f"업데이트 내려받는 중... {done * 100 // total}%" if total
                        else f"업데이트 내려받는 중... {fmt_size(done)}"))

        def work():
            try:
                updater.apply(m, progress)
                self.q.put(("update_exit",))
            except updater.UpdateError as e:
                self.q.put(("update_failed", str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("update_failed", f"예상치 못한 오류: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_failed(self, err: str):
        self.busy = False
        self.status.set("업데이트 실패")
        if messagebox.askyesno("업데이트 실패", f"{err}\n\n릴리스 페이지에서 직접 받으시겠어요?"):
            webbrowser.open(updater.RELEASE_PAGE)

    def _on_update_exit(self):
        self._closing = True
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
