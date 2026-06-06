#!/usr/bin/env python3
"""
Video preprocessing pipeline: filter redundant frames based on SSIM or MSE.

Usage:
    python preprocess_video.py input.mp4 output.mp4
    python preprocess_video.py input.mp4 output.mp4 --threshold 0.95 --metric ssim
    python preprocess_video.py input.mp4 output.mp4 --dynamic --upload-to-cvat \
        --cvat-host http://localhost:8080 --cvat-user admin --cvat-pass password \
        --cvat-project 1 --task-name "My Task"
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path


def compute_ssim_gray(frame_a, frame_b):
    """Compute SSIM between two grayscale frames. Returns value in [0, 1]."""
    import numpy as np

    # Constants for stability
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    a = frame_a.astype(np.float64)
    b = frame_b.astype(np.float64)

    mu_a = a.mean()
    mu_b = b.mean()
    sigma_a = a.std()
    sigma_b = b.std()
    sigma_ab = ((a - mu_a) * (b - mu_b)).mean()

    ssim = ((2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)) / (
        (mu_a**2 + mu_b**2 + C1) * (sigma_a**2 + sigma_b**2 + C2)
    )
    return float(ssim)


def compute_mse(frame_a, frame_b):
    """Compute normalized MSE between two grayscale frames. Returns value in [0, 1]."""
    import numpy as np

    diff = frame_a.astype(np.float64) - frame_b.astype(np.float64)
    mse = (diff**2).mean() / (255.0**2)
    return float(mse)


def load_frames(video_path):
    """Yield (index, frame_bgr) tuples from a video file."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        yield idx, frame
        idx += 1

    cap.release()
    return fps, width, height, total


def get_video_meta(video_path):
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return fps, width, height, total


def apply_hood_mask(frame, ratio):
    """Black out the bottom `ratio` fraction of a frame (in-place copy)."""
    if ratio <= 0.0:
        return frame
    import numpy as np

    out = frame.copy()
    h = out.shape[0]
    cutoff = max(0, int(h * (1.0 - ratio)))
    out[cutoff:, :] = 0
    return out


def compute_frame_scores(video_path, metric, mask_ratio=0.0):
    """First pass: compute similarity scores between consecutive frames."""
    import cv2
    import numpy as np

    scores = []
    prev_gray = None
    cap = cv2.VideoCapture(str(video_path))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame = apply_hood_mask(frame, mask_ratio)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            if metric == "ssim":
                score = compute_ssim_gray(prev_gray, gray)
            else:  # mse
                score = compute_mse(prev_gray, gray)
            scores.append(score)
        prev_gray = gray

    cap.release()
    return scores


def resolve_threshold(scores, metric, threshold, dynamic):
    """
    Determine the effective threshold.

    For SSIM: keep frame when ssim < threshold (too different from last kept).
    For MSE:  keep frame when mse  > threshold (too different from last kept).

    When --dynamic is used the threshold is derived from the score distribution:
      SSIM: threshold = mean - 1.5 * std   (adaptive low-similarity cutoff)
      MSE:  threshold = mean + 1.5 * std   (adaptive high-difference cutoff)
    """
    if not dynamic or not scores:
        return threshold

    import numpy as np

    arr = np.array(scores)
    mean, std = arr.mean(), arr.std()

    if metric == "ssim":
        derived = float(mean - 0.5 * std)
        derived = max(0.0, min(derived, 0.999))
    else:
        derived = float(mean + 0.5 * std)
        derived = max(0.0, derived)

    print(
        f"Dynamic threshold derived from score stats "
        f"(mean={mean:.4f}, std={std:.4f}): {derived:.4f}"
    )
    return derived


def _reencode_h264(src, dst, fps):
    """Re-encode src to dst as H.264/AAC MP4 using ffmpeg."""
    import subprocess

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-r", str(fps),
            "-i", str(src),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-movflags", "+faststart",
            str(dst),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def filter_video(video_path, output_path, metric, threshold, mask_ratio=0.0, min_keep_ratio=0.05):
    """
    Second pass: write only frames that differ enough from the last kept frame.

    Returns (kept_count, total_count).
    """
    import cv2
    import shutil

    fps, width, height, total = get_video_meta(video_path)

    # Write raw frames to a temp file first, then re-encode to H.264
    tmp_path = Path(str(output_path) + ".tmp.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_path), fourcc, fps, (width, height))

    cap = cv2.VideoCapture(str(video_path))
    prev_kept_gray = None
    kept = 0

    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        masked = apply_hood_mask(frame, mask_ratio)
        gray = cv2.cvtColor(masked, cv2.COLOR_BGR2GRAY)
        should_keep = False

        if prev_kept_gray is None:
            # Always keep the first frame
            should_keep = True
        else:
            if metric == "ssim":
                score = compute_ssim_gray(prev_kept_gray, gray)
                should_keep = score < threshold
            else:
                score = compute_mse(prev_kept_gray, gray)
                should_keep = score > threshold

        if should_keep:
            writer.write(masked)
            prev_kept_gray = gray
            kept += 1

        idx += 1
        if idx % 100 == 0:
            pct = idx / max(total, 1) * 100
            print(f"\r  Processing: {idx}/{total} frames ({pct:.1f}%)  ", end="", flush=True)

    print()
    cap.release()
    writer.release()

    # Guard: if filter was too aggressive, re-encode the full video
    if total > 0 and kept / total < min_keep_ratio:
        print(
            f"Warning: only {kept}/{total} frames kept ({kept/total*100:.1f}%). "
            "Threshold may be too aggressive. Keeping all frames."
        )
        tmp_path.unlink(missing_ok=True)
        shutil.copy2(str(video_path), str(output_path))
        return total, total

    # Re-encode raw mp4v → H.264 so the file plays in all players/browsers
    if _reencode_h264(tmp_path, output_path, fps):
        tmp_path.unlink(missing_ok=True)
    else:
        # ffmpeg not available or failed — keep the raw file
        shutil.move(str(tmp_path), str(output_path))

    return kept, total


def upload_to_cvat(video_path, host, user, password, project_id, task_name):
    """Upload a video file to CVAT as a new task via REST API."""
    import requests
    from requests.auth import HTTPBasicAuth

    host = host.rstrip("/")
    auth = HTTPBasicAuth(user, password)
    session = requests.Session()

    # Authenticate and get CSRF token
    login_url = f"{host}/api/auth/login"
    resp = session.post(
        login_url,
        json={"username": user, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("key")
    session.headers.update({"Authorization": f"Token {token}"})

    # Create task
    task_payload = {"name": task_name, "project_id": project_id}
    resp = session.post(f"{host}/api/tasks", json=task_payload, timeout=30)
    resp.raise_for_status()
    task_id = resp.json()["id"]
    print(f"Created CVAT task: id={task_id}, name='{task_name}'")

    # Upload video data
    with open(video_path, "rb") as f:
        resp = session.post(
            f"{host}/api/tasks/{task_id}/data",
            data={"image_quality": 70, "use_zip_chunks": "false"},
            files={"client_files[0]": (Path(video_path).name, f, "video/mp4")},
            timeout=300,
        )
    resp.raise_for_status()
    print(f"Uploaded video to task {task_id}. Upload status: {resp.status_code}")
    print(f"Task URL: {host}/tasks/{task_id}")
    return task_id


def main():
    parser = argparse.ArgumentParser(
        description="Filter redundant frames from MP4 using SSIM or MSE."
    )
    parser.add_argument("input", help="Input MP4 file")
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output MP4 file. Optional when --upload-to-cvat is set (temp file used).",
    )

    # Filtering options
    filt = parser.add_argument_group("Filtering")
    filt.add_argument(
        "--no-filter",
        action="store_true",
        help="Skip preprocessing entirely and upload/copy the original video as-is.",
    )
    filt.add_argument(
        "--metric",
        choices=["ssim", "mse"],
        default="ssim",
        help="Similarity metric (default: ssim)",
    )
    filt.add_argument(
        "--threshold",
        type=float,
        default=0.98,
        help=(
            "SSIM: keep frame when SSIM < threshold (default 0.98, conservative). "
            "MSE: keep frame when MSE > threshold (default 0.001 when --metric mse)."
        ),
    )
    filt.add_argument(
        "--no-dynamic",
        action="store_true",
        help="Use a fixed threshold instead of deriving it from the score distribution.",
    )
    filt.add_argument(
        "--mask-hood",
        type=float,
        default=0.0,
        metavar="RATIO",
        help=(
            "Mask the bottom RATIO fraction of each frame (0.0–1.0). "
            "E.g. --mask-hood 0.15 blacks out the bottom 15%% (Motorhaube). "
            "Applies to both the output video and the similarity score computation."
        ),
    )

    # CVAT upload options
    cvat = parser.add_argument_group("CVAT upload (optional)")
    cvat.add_argument("--upload-to-cvat", action="store_true")
    cvat.add_argument("--cvat-host", default="http://localhost:8080")
    cvat.add_argument("--cvat-user", default="admin")
    cvat.add_argument("--cvat-pass", default="")
    cvat.add_argument("--cvat-project", type=int, default=1)
    cvat.add_argument("--task-name", default="")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Resolve output path: explicit arg > temp file (upload-only) > error
    use_tempfile = False
    if args.output:
        output_path = Path(args.output)
    elif args.upload_to_cvat:
        import tempfile
        _tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        _tmp.close()
        output_path = Path(_tmp.name)
        use_tempfile = True
    else:
        parser.error("output is required unless --upload-to-cvat is set")

    fps, width, height, total = get_video_meta(input_path)
    print(f"Input:  {input_path}  ({total} frames, {fps:.2f} fps, {width}x{height})")

    dynamic = not args.no_dynamic

    if args.no_filter:
        print("Preprocessing skipped (--no-filter). Using original video.")
        import shutil
        shutil.copy2(str(input_path), str(output_path))
    else:
        # Resolve MSE default threshold when user did not override
        if args.metric == "mse" and args.threshold == 0.98:
            args.threshold = 0.001

        mask_ratio = max(0.0, min(1.0, args.mask_hood))
        if mask_ratio > 0.0:
            print(f"Hood mask: bottom {mask_ratio*100:.0f}% of each frame will be blacked out.")

        effective_threshold = args.threshold
        if dynamic:
            print("First pass: computing frame scores for dynamic threshold...")
            scores = compute_frame_scores(input_path, args.metric, mask_ratio=mask_ratio)
            effective_threshold = resolve_threshold(
                scores, args.metric, args.threshold, dynamic=True
            )

        print(f"Filtering with metric={args.metric}, threshold={effective_threshold:.4f} ...")
        kept, total_processed = filter_video(
            input_path, output_path, args.metric, effective_threshold, mask_ratio=mask_ratio
        )

        ratio = kept / max(total_processed, 1) * 100
        print(f"Output: {output_path}  ({kept}/{total_processed} frames kept, {ratio:.1f}%)")

    if args.upload_to_cvat:
        task_name = args.task_name or input_path.stem
        print(f"\nUploading to CVAT at {args.cvat_host} ...")
        upload_to_cvat(
            output_path,
            host=args.cvat_host,
            user=args.cvat_user,
            password=args.cvat_pass,
            project_id=args.cvat_project,
            task_name=task_name,
        )

    if use_tempfile:
        output_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
