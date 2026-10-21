import signal
import sys
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtWidgets import QApplication

from control.auto_scan import AutoScanner
from control.keyboard_flight import KeyboardFlight
from control.manual_scan import ManualScanner
from drone.flight_controller import FlightController
from drone.video_stream import VideoStream
from gui.main_window import MainWindow
from inventory.inventory_screen import InventoryScreen
from inventory.product_detector import ProductDetector
from inventory.screen_renderer import ScreenRenderer
from inventory.shelf_inventory import ShelfInventory
from inventory.snack_recognizer import SnackRecognizer
from location.zone_marker import ZoneMarkerDetector


# 사용할 가중치 및 인식 기준 설정
MODEL_PATH = Path(__file__).resolve().parent / "models" / "best_fine.pt"
MODEL_CONFIDENCE = 0.35  # 봉지 1종 모델 기준: 0.40이면 82%, 0.25면 97%의 봉지를 찾음 (녹화 영상 39개)
MODEL_IMAGE_SIZE = 960  # 텔로 화면 원래 크기. 640보다 멀리 있는 작은 봉지를 잘 찾지만 YOLO가 약 17ms 느림
# 과자별 등록 사진 폴더 (gui의 "물품 등록"으로 추가)
GALLERY_DIR = Path(__file__).resolve().parent / "gallery"


def main():
    # 인식
    detector = ProductDetector(MODEL_PATH, confidence=MODEL_CONFIDENCE, image_size=MODEL_IMAGE_SIZE)
    zones = ZoneMarkerDetector()
    recognizer = SnackRecognizer(GALLERY_DIR)
    # 재고·화면
    inventory = ShelfInventory(InventoryScreen(detector.class_names.values()))
    screen = ScreenRenderer(detector, zones, inventory)
    # 드론·조작
    controller = FlightController()
    camera = VideoStream(controller.tello)
    keys = KeyboardFlight(controller)
    manual = ManualScanner(inventory)
    auto = AutoScanner(controller, keys, inventory)

    app = QApplication(sys.argv)
    try:
        with ExitStack() as cleanup:
            cleanup.callback(controller.disconnect)
            controller.connect()
            cleanup.callback(camera.stop)
            camera.start()
            cleanup.callback(controller.land)
            cleanup.callback(keys.stop)
            # 키 입력은 pynput 대신 Qt 창이 받아서 넘겨준다. (창에 포커스가 있을 때만 조종)
            keys.start(listen_keyboard=False)

            # 인식 → 집계 → 화면 출력 반복은 창의 tick()이 맡는다.
            # 창을 닫거나 Q·ESC를 누르면 app.exec()가 끝나고 위의 정리(착륙·연결 해제)가 실행된다.
            window = MainWindow(controller, camera, keys, detector, zones, inventory, screen,
                                manual, auto, recognizer)
            # 터미널의 Ctrl+C도 창을 닫는 것과 같게 처리한다.
            signal.signal(signal.SIGINT, lambda *_: window.close())
            window.show()
            app.exec()

    except KeyboardInterrupt:
        print("\n[MODE] INTERRUPTED")
    finally:
        print("[MODE] PROGRAM_END")


if __name__ == "__main__":
    main()
