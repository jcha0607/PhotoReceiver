"""PhotoReceiver: 아이폰 사진을 QR코드 + 웹 업로드로 받아 리사이징하는 데스크톱 앱."""
import queue
import socket
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from werkzeug.utils import secure_filename

import qrcode
from flask import Flask, jsonify, request
from PIL import Image, ImageOps, ImageTk

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


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    n = 1
    while path.exists():
        path = directory / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return path


def fit_jpeg(im: Image.Image, limit: int) -> dict:
    """용량 제한(byte) 이하가 되도록 품질을 이진 탐색하고, 부족하면 해상도를 줄인다.

    반환값은 (im 은 호출 쪽에서 그대로 저장) save() 인자.
    """
    import io
    def size(img, q):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        return buf.tell()
    while True:
        if size(im, 30) <= limit:
            lo, hi = 30, 95
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if size(im, mid) <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            return {"quality": lo, "optimize": True}
        if min(im.size) < 64:
            return {"quality": 30, "optimize": True}
        im.thumbnail((int(im.width * 0.85), int(im.height * 0.85)), Image.LANCZOS)


def resize_image(src: Path, out_dir: Path, mode: str, value: int,
                 fmt: str, quality: int, max_kb: int = 0) -> Path:
    """mode: 'long' (긴 변 px) | 'width' (가로 px) | 'percent'. 결과 파일 경로를 반환."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        if mode == "long":
            scale = value / max(w, h)
        elif mode == "width":
            scale = value / w
        else:
            scale = value / 100
        scale = min(scale, 1.0)  # 확대하지 않음
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        im = im.resize(new_size, Image.LANCZOS)

        ext = src.suffix.lower()
        if fmt == "PNG" or (fmt == "원본 유지" and ext == ".png"):
            out_ext, save_fmt = ".png", "PNG"
        else:
            out_ext, save_fmt = ".jpg", "JPEG"
        if save_fmt == "JPEG" and im.mode != "RGB":
            im = im.convert("RGB")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = unique_path(out_dir, src.stem + out_ext)
        if save_fmt == "JPEG" and max_kb > 0:
            im.save(out, "JPEG", **fit_jpeg(im, max_kb * 1024))
            return out
        kwargs = {"quality": quality, "optimize": True} if save_fmt == "JPEG" else {}
        im.save(out, save_fmt, **kwargs)
        return out


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
        name = secure_filename(file.filename) or "photo.jpg"
        path = unique_path(RECEIVED_DIR, name)
        file.save(path)
        on_received(path)
        return jsonify(ok=True)

    return app


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PhotoReceiver")
        self.geometry("760x520")
        self.events: "queue.Queue[Path]" = queue.Queue()
        self.files: list[Path] = []

        self._build_ui()
        threading.Thread(target=self._serve, daemon=True).start()
        self.after(300, self._poll)

    # ---- UI ----
    def _build_ui(self):
        left = ttk.Frame(self, padding=12)
        left.pack(side="left", fill="y")
        url = f"http://{local_ip()}:{PORT}"
        qr = qrcode.make(url).resize((220, 220))
        self.qr_img = ImageTk.PhotoImage(qr)
        ttk.Label(left, image=self.qr_img).pack()
        ttk.Label(left, text="아이폰 카메라로 QR을 스캔하세요").pack(pady=(6, 0))
        ttk.Label(left, text=url, foreground="#0071e3").pack()
        ttk.Label(left, text="(PC와 같은 Wi-Fi여야 합니다)",
                  foreground="#888").pack(pady=(0, 12))

        self.mode = tk.StringVar(value="long")
        box = ttk.LabelFrame(left, text="리사이즈 설정", padding=8)
        box.pack(fill="x")
        for text, val in (("긴 변 기준 (px)", "long"), ("가로 기준 (px)", "width"),
                          ("비율 (%)", "percent")):
            ttk.Radiobutton(box, text=text, value=val, variable=self.mode).pack(anchor="w")
        self.value = tk.IntVar(value=1920)
        ttk.Spinbox(box, from_=1, to=20000, textvariable=self.value, width=8).pack(anchor="w", pady=4)
        row = ttk.Frame(box); row.pack(fill="x")
        ttk.Label(row, text="형식").pack(side="left")
        self.fmt = tk.StringVar(value="JPEG")
        ttk.Combobox(row, textvariable=self.fmt, values=["JPEG", "PNG", "원본 유지"],
                     width=9, state="readonly").pack(side="left", padx=4)
        row2 = ttk.Frame(box); row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="JPEG 품질").pack(side="left")
        self.quality = tk.IntVar(value=85)
        ttk.Spinbox(row2, from_=1, to=100, textvariable=self.quality, width=5).pack(side="left", padx=4)

        row3 = ttk.Frame(box); row3.pack(fill="x")
        self.limit_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="용량 제한 (KB)", variable=self.limit_on).pack(side="left")
        self.limit_kb = tk.IntVar(value=500)
        ttk.Spinbox(row3, from_=10, to=50000, increment=50, textvariable=self.limit_kb,
                    width=7).pack(side="left", padx=4)

        self.out_dir = tk.StringVar(value=str(DEFAULT_OUT_DIR))
        ttk.Label(left, text="저장 폴더").pack(anchor="w", pady=(8, 0))
        ttk.Entry(left, textvariable=self.out_dir, width=32).pack(fill="x")
        ttk.Button(left, text="폴더 변경", command=self._pick_dir).pack(anchor="e")

        right = ttk.Frame(self, padding=12)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="받은 사진").pack(anchor="w")
        self.listbox = tk.Listbox(right, selectmode="extended")
        self.listbox.pack(fill="both", expand=True)
        btns = ttk.Frame(right); btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="파일 추가...", command=self._add_files).pack(side="left")
        ttk.Button(btns, text="목록 비우기", command=self._clear).pack(side="left", padx=4)
        ttk.Button(btns, text="선택 리사이즈", command=lambda: self._resize(True)).pack(side="right")
        ttk.Button(btns, text="전체 리사이즈", command=lambda: self._resize(False)).pack(side="right", padx=4)
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(right, textvariable=self.status, foreground="#555").pack(anchor="w", pady=(6, 0))

    # ---- 서버 / 이벤트 ----
    def _serve(self):
        create_server(self.events.put).run(host="0.0.0.0", port=PORT, threaded=True)

    def _poll(self):
        try:
            while True:
                self._add(self.events.get_nowait(), from_phone=True)
        except queue.Empty:
            pass
        self.after(300, self._poll)

    def _add(self, path: Path, from_phone=False):
        self.files.append(path)
        self.listbox.insert("end", path.name)
        self.status.set(f"받음: {path.name}" if from_phone else f"추가: {path.name}")

    def _add_files(self):
        for p in filedialog.askopenfilenames(
                filetypes=[("이미지", "*.jpg *.jpeg *.png *.heic *.heif *.webp *.bmp")]):
            self._add(Path(p))

    def _clear(self):
        self.files.clear()
        self.listbox.delete(0, "end")

    def _pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    # ---- 리사이즈 ----
    def _resize(self, selected_only: bool):
        if selected_only:
            files = [self.files[i] for i in self.listbox.curselection()]
        else:
            files = list(self.files)
        if not files:
            messagebox.showinfo("알림", "리사이즈할 사진이 없습니다.")
            return
        self._resize_files(files)

    def _resize_files(self, files):
        ok, errors = 0, []
        for f in files:
            try:
                resize_image(f, Path(self.out_dir.get()), self.mode.get(),
                             int(self.value.get()), self.fmt.get(), int(self.quality.get()),
                             int(self.limit_kb.get()) if self.limit_on.get() else 0)
                ok += 1
            except Exception as e:  # noqa: BLE001 - 사용자에게 파일별 오류 표시
                errors.append(f"{f.name}: {e}")
        self.status.set(f"{ok}장 리사이즈 완료 → {self.out_dir.get()}")
        if errors:
            messagebox.showwarning("일부 실패", "\n".join(errors[:10]))


if __name__ == "__main__":
    App().mainloop()
