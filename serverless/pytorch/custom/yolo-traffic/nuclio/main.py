import base64
import io
import json
import os
import tempfile

import numpy as np
import torch
import yaml
from PIL import Image


def init_context(context):
    context.logger.info("Init context...  0%")

    with open("/opt/nuclio/function.yaml", "rb") as f:
        functionconfig = yaml.safe_load(f)
    labels_spec = functionconfig["metadata"]["annotations"]["spec"]
    labels = {item["id"]: item["name"] for item in json.loads(labels_spec)}

    # Extract the self-contained MMDetection config from the checkpoint meta
    ckpt = torch.load("/opt/nuclio/epoch_200_ema.pth", map_location="cpu", weights_only=False)
    cfg_str = ckpt["meta"]["cfg"]

    cfg_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    cfg_file.write(cfg_str)
    cfg_file.close()

    from mmdet.apis import init_detector

    model = init_detector(cfg_file.name, "/opt/nuclio/epoch_200_ema.pth", device="cpu")
    model.eval()
    os.unlink(cfg_file.name)

    context.user_data.model = model
    context.user_data.labels = labels
    context.logger.info("Init context...100%")


def handler(context, event):
    context.logger.info("Run RTMDet traffic detector")

    from mmdet.apis import inference_detector

    data = event.body
    buf = io.BytesIO(base64.b64decode(data["image"]))
    threshold = float(data.get("threshold", 0.5))
    image = np.array(Image.open(buf).convert("RGB"))

    model = context.user_data.model
    labels = context.user_data.labels

    result = inference_detector(model, image)
    instances = result.pred_instances

    boxes = instances.bboxes.cpu().numpy()
    scores = instances.scores.cpu().numpy()
    cls_ids = instances.labels.cpu().numpy()

    img_h, img_w = image.shape[:2]
    output = []
    for box, score, cls_id in zip(boxes, scores, cls_ids):
        if float(score) >= threshold:
            x1, y1, x2, y2 = box.tolist()
            output.append({
                "confidence": str(round(float(score), 6)),
                "label": labels.get(int(cls_id), "unknown"),
                "points": [
                    max(0, int(x1)),
                    max(0, int(y1)),
                    min(img_w, int(x2)),
                    min(img_h, int(y2)),
                ],
                "type": "rectangle",
            })

    return context.Response(
        body=json.dumps(output),
        headers={},
        content_type="application/json",
        status_code=200,
    )
