import hashlib
import hmac
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# ── Auth config ───────────────────────────────────────────────────────────────
AUTH_USER = os.environ.get("AUTH_USER", "admin")
AUTH_PASS = os.environ.get("AUTH_PASS", "")
# Generate a random key on startup if not set — sessions are invalidated on restart.
# Set SECRET_KEY env var for persistent sessions across restarts.
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(32).hex()).encode()
SESSION_TTL = 7 * 24 * 3600  # 7 days


def _make_token(username: str) -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{username}:{expires}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_token(token: str) -> bool:
    try:
        last_colon = token.rfind(":")
        payload, sig = token[:last_colon], token[last_colon + 1:]
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        expires = int(payload.split(":")[1])
        return time.time() < expires
    except Exception:
        return False


def _check_auth(request: Request) -> bool:
    token = request.cookies.get("session")
    return bool(token and _verify_token(token))


# ── Job store ─────────────────────────────────────────────────────────────────
jobs: dict[str, dict] = {}

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# ── Preprocessing ─────────────────────────────────────────────────────────────

def _ssim_gray(a, b) -> float:
    import numpy as np

    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    a, b = a.astype(np.float64), b.astype(np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    sa, sb = a.std(), b.std()
    sab = ((a - mu_a) * (b - mu_b)).mean()
    return float(
        ((2 * mu_a * mu_b + C1) * (2 * sab + C2))
        / ((mu_a**2 + mu_b**2 + C1) * (sa**2 + sb**2 + C2))
    )


def _mask_hood(frame, ratio: float):
    if ratio <= 0:
        return frame
    out = frame.copy()
    h = out.shape[0]
    out[max(0, int(h * (1.0 - ratio))):, :] = 0
    return out


def _reencode_h264(src: str, dst: str, fps: float) -> bool:
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-r", str(fps), "-i", src,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-movflags", "+faststart", dst,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def preprocess(input_path: str, output_path: str, mask_ratio: float,
               progress_cb=None) -> tuple[int, int]:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    scores: list[float] = []
    cap = cv2.VideoCapture(input_path)
    prev_gray = None
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame = _mask_hood(frame, mask_ratio)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            scores.append(_ssim_gray(prev_gray, gray))
        prev_gray = gray
    cap.release()

    if progress_cb:
        progress_cb(30, "1. Durchlauf fertig — berechne Schwellwert...")

    arr = np.array(scores)
    threshold = float(np.clip(arr.mean() - 0.5 * arr.std(), 0.0, 0.999)) if len(arr) else 0.98

    if progress_cb:
        progress_cb(35, f"Schwellwert: {threshold:.4f} — filtere Frames...")

    tmp_path = output_path + ".raw.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_path, fourcc, fps, (width, height))
    cap = cv2.VideoCapture(input_path)
    prev_kept = None
    kept = 0
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        masked = _mask_hood(frame, mask_ratio)
        gray = cv2.cvtColor(masked, cv2.COLOR_BGR2GRAY)
        keep = prev_kept is None or _ssim_gray(prev_kept, gray) < threshold
        if keep:
            writer.write(masked)
            prev_kept = gray
            kept += 1
        idx += 1
        if progress_cb and idx % 50 == 0:
            pct = 35 + int(idx / max(total, 1) * 50)
            progress_cb(pct, f"Filtere Frames... {idx}/{total}")
    cap.release()
    writer.release()

    import shutil

    if total > 0 and kept / total < 0.05:
        Path(tmp_path).unlink(missing_ok=True)
        shutil.copy2(input_path, output_path)
        return total, total

    if _reencode_h264(tmp_path, output_path, fps):
        Path(tmp_path).unlink(missing_ok=True)
    else:
        shutil.move(tmp_path, output_path)

    return kept, total


# ── Background job ────────────────────────────────────────────────────────────

def _run_job(job_id: str, input_path: str, original_name: str,
             use_preprocessing: bool, mask_ratio: float) -> None:
    try:
        def upd(pct: int, msg: str) -> None:
            jobs[job_id].update({"progress": pct, "message": msg})

        stem = Path(original_name).stem
        suffix = Path(original_name).suffix or ".mp4"
        out_suffix = "_filtered" + suffix if use_preprocessing else suffix

        with tempfile.NamedTemporaryFile(suffix=out_suffix, delete=False) as tmp:
            out_path = tmp.name

        jobs[job_id]["output_path"] = out_path
        jobs[job_id]["download_name"] = stem + out_suffix

        if use_preprocessing:
            upd(5, "1. Durchlauf: Frame-Scores berechnen...")
            kept, total = preprocess(input_path, out_path, mask_ratio, upd)
            ratio = kept / max(total, 1) * 100
            upd(90, f"{kept}/{total} Frames behalten ({ratio:.0f} %)")
        else:
            import shutil
            upd(50, "Preprocessing deaktiviert — kopiere Original...")
            shutil.copy2(input_path, out_path)

        jobs[job_id].update({"status": "done", "progress": 100, "message": "Bereit zum Download."})

    except Exception as exc:
        jobs[job_id].update({"status": "error", "message": str(exc)})
    finally:
        Path(input_path).unlink(missing_ok=True)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = "") -> HTMLResponse:
    err_html = f'<p style="color:#ff4d4f;margin-top:12px;">{error}</p>' if error else ""
    return HTMLResponse(_login_html(err_html))


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if username == AUTH_USER and password == AUTH_PASS:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            "session", _make_token(username),
            httponly=True, samesite="lax", max_age=SESSION_TTL,
        )
        return resp
    return RedirectResponse("/login?error=Falscher+Benutzername+oder+Passwort", status_code=302)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ── App routes (protected) ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    if not _check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/process", response_class=HTMLResponse)
async def process(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preprocessing: str = Form("off"),
    mask_hood_enabled: str = Form("off"),
    mask_hood: float = Form(0.15),
) -> HTMLResponse:
    if not _check_auth(request):
        return HTMLResponse("Nicht angemeldet.", status_code=401)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running", "progress": 0,
        "message": "Startet...", "output_path": None, "download_name": None,
    }

    suffix = Path(file.filename or "video").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    use_prep = preprocessing == "on"
    mask_ratio = mask_hood if (mask_hood_enabled == "on" and use_prep) else 0.0

    background_tasks.add_task(
        _run_job, job_id, tmp_path, file.filename or "video.mp4", use_prep, mask_ratio
    )
    return HTMLResponse(_status_html(job_id, 0, "Startet...", "running"))


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str) -> HTMLResponse:
    if not _check_auth(request):
        return HTMLResponse("Nicht angemeldet.", status_code=401)
    job = jobs.get(job_id)
    if not job:
        return HTMLResponse("<p>Job nicht gefunden.</p>")
    return HTMLResponse(
        _status_html(job_id, job["progress"], job["message"], job["status"],
                     job.get("download_name"))
    )


@app.get("/download/{job_id}")
async def download(request: Request, job_id: str):
    if not _check_auth(request):
        return RedirectResponse("/login", status_code=302)
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job.get("output_path"):
        return HTMLResponse("Datei nicht gefunden.", status_code=404)
    return FileResponse(
        job["output_path"],
        media_type="video/mp4",
        filename=job["download_name"],
    )


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _login_html(error_html: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anmelden — Video Preprocessing</title>
<style>
  *,*::before,*::after{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#f5f5f5;margin:0;padding:60px 16px;color:#222}}
  .card{{background:#fff;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.1);
         max-width:360px;margin:0 auto;padding:32px}}
  h1{{margin:0 0 24px;font-size:1.3em;color:#222}}
  label{{display:block;font-size:.85em;font-weight:600;color:#555;
         text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}}
  input[type=text],input[type=password]{{width:100%;padding:9px 12px;
    border:1px solid #d9d9d9;border-radius:6px;font-size:1em;
    outline:none;margin-bottom:16px;transition:border-color .2s}}
  input:focus{{border-color:#1890ff;box-shadow:0 0 0 2px rgba(24,144,255,.15)}}
  button{{width:100%;padding:11px;background:#1890ff;color:#fff;border:none;
          border-radius:6px;font-size:1em;font-weight:600;cursor:pointer}}
  button:hover{{background:#096dd9}}
</style>
</head>
<body>
<div class="card">
  <h1>&#128274; Anmelden</h1>
  <form method="post" action="/login">
    <label>Benutzername</label>
    <input type="text" name="username" autofocus autocomplete="username">
    <label>Passwort</label>
    <input type="password" name="password" autocomplete="current-password">
    <button type="submit">Anmelden</button>
  </form>
  {error_html}
</div>
</body>
</html>"""


def _status_html(job_id: str, pct: int, msg: str, status: str,
                 download_name: str | None = None) -> str:
    if status == "done":
        return f"""
<div id="status-box">
  <p style="color:#52c41a;font-weight:bold;">&#10003; {msg}</p>
  <a href="/download/{job_id}"
     style="display:inline-block;padding:11px 24px;background:#52c41a;color:#fff;
            border-radius:6px;text-decoration:none;font-size:1em;font-weight:600;">
    &#8681; {download_name or "Video herunterladen"}
  </a>
</div>"""
    if status == "error":
        return f"""
<div id="status-box">
  <p style="color:#ff4d4f;font-weight:bold;">&#10007; Fehler</p>
  <pre style="background:#fff1f0;border:1px solid #ffa39e;padding:10px;
              border-radius:4px;white-space:pre-wrap;font-size:.85em;">{msg}</pre>
</div>"""
    return f"""
<div id="status-box"
     hx-get="/status/{job_id}"
     hx-trigger="every 1s"
     hx-swap="outerHTML">
  <p style="margin-bottom:8px;">{msg}</p>
  <div style="background:#f0f0f0;border-radius:4px;height:18px;width:100%;overflow:hidden;">
    <div style="background:#1890ff;height:18px;width:{pct}%;border-radius:4px;
                transition:width .4s ease;"></div>
  </div>
  <small style="color:#888;">{pct} %</small>
</div>"""
