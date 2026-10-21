import threading
import time
import traceback

from PySide6.QtCore import QObject, Signal


class DetectionWorker(QObject):
    """과자(YOLO)·마커 인식을 별도 스레드에서 돌린다.

    추론이 화면 갱신을 막지 않도록 분리해, 영상은 카메라 속도로 표시하고
    탐지 결과는 끝나는 대로 신호로 화면(메인 스레드)에 넘긴다.
    YOLO가 봉지 위치를 찾으면 SnackRecognizer가 등록 사진과 대조해 과자 이름을 붙인다.
    """

    detected = Signal(object)  # (프레임 높이, 과자 탐지 목록, 마커 목록, 탐지에 쓴 프레임)
    failed = Signal(str)

    def __init__(self, camera, detector, zones, recognizer):
        super().__init__()
        self.camera, self.detector, self.zones = camera, detector, zones
        self.recognizer = recognizer
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2)

    def run(self):
        last = None
        try:
            while self.running:
                raw = self.camera.raw_frame()
                if raw is last:
                    # 이미 추론한 프레임이면 새 프레임이 올 때까지 잠깐 기다린다.
                    time.sleep(0.003)
                    continue
                last = raw
                frame = self.camera.to_bgr(raw)
                detections = self.recognizer.recognize(frame, self.detector.detect(frame))
                result = (frame.shape[0], detections, self.zones.detect(frame), frame)
                if self.running:
                    self.detected.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())
