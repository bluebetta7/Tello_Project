from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from ultralytics import YOLO

from inventory.inventory_screen import SNACK_NAMES, font


class ProductDetector:
    def __init__(self, model_path, confidence=0.40, image_size=640):
        model_path = Path(model_path).expanduser().resolve()
        if not model_path.is_file():
            raise FileNotFoundError(f"가중치 파일이 없습니다: {model_path}")

        self.model = YOLO(str(model_path))
        self.confidence = confidence
        self.image_size = image_size
        self.class_names = self.model.names
        # 현재 환경에서 GPU(MPS)를 이용하여 추론하여 프레임 처리 속도를 높인다.
        self.device = self.select_device()
        # 첫 추론은 초기화 때문에 느리므로 실행 전에 한 번 미리 돌려 둔다.
        self.model.predict(np.zeros((720, 960, 3), dtype=np.uint8), imgsz=self.image_size,
                           device=self.device, verbose=False)

    @staticmethod
    def select_device():
        try:
            import torch
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def detect(self, frame):
        # confidence보다 낮은 예측은 YOLO 단계에서 먼저 제거한다.
        result = self.model.predict(
            frame,
            conf=self.confidence,
            imgsz=self.image_size,
            device=self.device,
            iou=0.5,
            # 과자 종류는 SnackRecognizer가 정하므로, 한 봉지에 종류만 다른 박스가 겹치지 않게
            # 종류와 관계없이 겹친 박스를 하나로 합친다. (봉지 1종 모델에서는 차이 없음)
            agnostic_nms=True,
            verbose=False,
        )[0]
        detections = []

        if result.boxes is None:
            return detections

        # OpenCV 처리에 사용할수있도록 추론 결과를 일반 Python 값으로 변환.
        boxes = result.boxes.xyxy.cpu().tolist()
        confidences = result.boxes.conf.cpu().tolist()
        class_ids = result.boxes.cls.int().cpu().tolist()

        for coordinates, confidence, class_id in zip(
            boxes,
            confidences,
            class_ids,
        ):
            x1, y1, x2, y2 = coordinates
            detections.append(
                {
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "class_id": class_id,
                    "class_name": self.class_names[class_id],
                    "confidence": float(confidence),
                }
            )

        return detections

    @staticmethod
    def count_by_class(detections):
        # 현재 한 프레임에 보이는 개수이며 영상 전체의 추적 수량은 아니다.
        counts = {}

        for detection in detections:
            name = detection["class_name"]
            counts[name] = counts.get(name, 0) + 1

        return counts

    @staticmethod
    def draw(frame, detections, counts, title="실시간 탐지", changes=None):
        # 호출부가 전달한 화면 복사본에 탐지 결과와 수량을 표시한다.
        # OpenCV는 한글을 그릴 수 없어 박스는 OpenCV로, 글자는 PIL로 그린다.
        # R 키 집계에 포함된 과자("counted")는 보라색 굵은 박스로 구분한다.
        box_color, counted_color = (0, 200, 0), (176, 39, 156)  # BGR 초록, 보라
        for detection in detections:
            counted = detection.get("counted")
            cv2.rectangle(
                frame,
                (detection["x1"], detection["y1"]),
                (detection["x2"], detection["y2"]),
                counted_color if counted else box_color,
                4 if counted else 2,
            )

        # 프레임(BGR)을 변환하지 않고 그리므로 PIL에서도 색상은 BGR 순서로 지정한다.
        image = Image.fromarray(frame)
        draw = ImageDraw.Draw(image)

        # 박스 위 라벨: 한글 과자 이름 + 점수 (등록 사진과 대조했으면 닮은 정도, 아니면 YOLO 신뢰도)
        label_font = font(20, True)
        for detection in detections:
            name = SNACK_NAMES.get(detection["class_name"], detection["class_name"])
            label = f"{name} {detection.get('match_score', detection['confidence']):.2f}"
            left, top, right, bottom = draw.textbbox((0, 0), label, font=label_font)
            bar_h = bottom + 6  # 글꼴마다 높이가 달라 실제 글자 크기에 맞춘다.
            x, y = detection["x1"], max(0, detection["y1"] - bar_h)
            draw.rectangle((x, y, x + right + 10, y + bar_h),
                           fill=counted_color if detection.get("counted") else box_color)
            draw.text((x + 5, y + 2), label, font=label_font, fill=(255, 255, 255))

        # 왼쪽 위 실시간 수량 상자
        changes = changes or {}
        names = sorted((counts or {}).keys() | changes.keys())
        if names:
            rows = []
            for name in names:
                row = f"{SNACK_NAMES.get(name, name)} {counts.get(name, 0)}개"
                if name in changes:
                    row += f" ({changes[name]:+d})"
                rows.append((row, (255, 255, 255)))
        else:
            rows = [("대기 중" if counts is None else "탐지된 과자 없음", (200, 200, 200))]

        title_font, row_font = font(22, True), font(20)
        panel_width = max([title_font.getlength(title)] + [row_font.getlength(r) for r, _ in rows]) + 32
        panel_height = 50 + 30 * len(rows)
        draw.rectangle((10, 10, 10 + panel_width, 10 + panel_height), fill=(0, 0, 0))
        draw.text((26, 22), title, font=title_font, fill=(255, 255, 255))
        for index, (row, color) in enumerate(rows):
            draw.text((26, 56 + index * 30), row, font=row_font, fill=color)

        return np.asarray(image).copy()
