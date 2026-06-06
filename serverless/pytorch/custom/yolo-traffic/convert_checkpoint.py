"""
Merges the EMA weights from an MMDetection RTMDet checkpoint into state_dict
and saves a clean inference-ready checkpoint.

EMA (Exponential Moving Average) weights typically yield better accuracy than
the regular state_dict saved during training.

Usage:
    python convert_checkpoint.py epoch_200.pth
    python convert_checkpoint.py epoch_200.pth --output epoch_200_ema.pth
"""

import argparse
import sys
from pathlib import Path


def convert(src: Path, dst: Path) -> None:
    try:
        import torch
    except ImportError:
        sys.exit("torch not installed — run: pip install torch")

    print(f"Loading: {src}")
    ckpt = torch.load(str(src), map_location="cpu", weights_only=False)

    if "ema_state_dict" not in ckpt:
        sys.exit("No 'ema_state_dict' found — checkpoint may not be from MMDetection with EMAHook.")

    ema = ckpt["ema_state_dict"]
    # Strip the 'module.' prefix added by EMAHook
    ema_clean = {k.replace("module.", "", 1): v for k, v in ema.items() if k != "steps"}

    regular_keys = set(ckpt["state_dict"].keys())
    ema_keys = set(ema_clean.keys())
    if regular_keys != ema_keys:
        sys.exit(f"Key mismatch: state_dict has {len(regular_keys)}, ema has {len(ema_keys)} keys.")

    inference_ckpt = {
        "state_dict": ema_clean,
        "meta": ckpt["meta"],
    }

    torch.save(inference_ckpt, str(dst))

    classes = ckpt["meta"]["dataset_meta"]["classes"]
    print(f"Classes ({len(classes)}): {list(classes)}")
    print(f"Epoch:  {ckpt['meta']['epoch']}")
    print(f"Saved:  {dst}  ({dst.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge EMA weights into RTMDet checkpoint")
    parser.add_argument("checkpoint", type=Path, help="Path to the .pth checkpoint")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("epoch_200_ema.pth"),
        help="Output path (default: epoch_200_ema.pth)",
    )
    args = parser.parse_args()

    if not args.checkpoint.exists():
        sys.exit(f"File not found: {args.checkpoint}")

    convert(args.checkpoint, args.output)


if __name__ == "__main__":
    main()
