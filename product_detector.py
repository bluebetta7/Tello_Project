from pathlib import Path

import cv2
from ultralytics import YOLO


class ProductDetector:
    def __init__(self, model_path, confidence=0.40):
        model_path = Path(model_path).expanduser().resolve()
        if not model_path.is_file():
            raise FileNotFoundError(f"가중치 파일이 없습니다: {model_path}")

        self.model = YOLO(str(model_path))
        self.confidence = confidence
        self.class_names = self.model.names

    def detect(self, frame):
        # confidence보다 낮은 예측은 YOLO 단계에서 먼저 제거한다.
        result = self.model.predict(
            frame,
            conf=self.confidence,
            iou=0.5,
            agnostic_nms=False,
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
    def draw(frame, detections, counts, title="VISIBLE OBJECT INVENTORY", changes=None):
        # 호출부가 전달한 화면 복사본에 탐지 결과와 수량을 표시.
        for detection in detections:
            color = (0, 200, 0)
            cv2.rectangle(
                frame,
                (detection["x1"], detection["y1"]),
                (detection["x2"], detection["y2"]),
                color,
                2,
            )
            label = (
                f"{detection['class_name']} "
                f"{detection['confidence']:.2f}"
            )
            cv2.putText(
                frame,
                label,
                (detection["x1"], max(25, detection["y1"] - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                color,
                2,
            )

        changes = changes or {}
        names = sorted((counts or {}).keys() | changes.keys())
        panel_height = 55 + 28 * max(1, len(names))
        cv2.rectangle(frame, (10, 10), (390, panel_height), (0, 0, 0), -1)
        cv2.putText(
            frame,
            title,
            (20, 38),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.60,
            (255, 255, 255),
            2,
        )

        if not names:
            cv2.putText(
                frame,
                "Waiting..." if counts is None else "No products",
                (20, 66),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (200, 200, 200),
                1,
            )
        else:
            for index, name in enumerate(names):
                label = f"{name}: {counts.get(name, 0)}"
                if name in changes:
                    label += f" ({changes[name]:+d})"
                cv2.putText(
                    frame,
                    label,
                    (20, 66 + index * 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (255, 255, 255),
                    1,
                )

        return frame
