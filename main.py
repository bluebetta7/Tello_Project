from contextlib import ExitStack
import time  # [FPS-TEST] 측정 후 삭제
from pathlib import Path

from drone.video_stream import VideoStream
from control.keyboard_flight import KeyboardFlight
from inventory.inventory_screen import InventoryScreen
from inventory.stock_counter import StockCounter
from inventory.product_detector import ProductDetector
from drone.flight_controller import FlightController


# 사용할 가중치 및 인식 기준 설정
MODEL_PATH = Path(__file__).resolve().parent / "models" / "best_fine.pt"
MODEL_CONFIDENCE = 0.40


def main():
    detector = ProductDetector(MODEL_PATH, confidence=MODEL_CONFIDENCE)
    counter = StockCounter(confirmation_frames=3)
    dashboard = InventoryScreen(detector.class_names.values())
    controller = FlightController()
    camera = VideoStream(controller.tello)
    manual = KeyboardFlight(controller)

    try:
        with ExitStack() as cleanup:
            cleanup.callback(controller.disconnect)
            controller.connect()
            cleanup.callback(camera.stop)
            camera.start()
            cleanup.callback(controller.land)
            cleanup.callback(manual.stop)
            manual.start()

            print("[MODE] SNACK_RECOGNITION_START")
            print("[KEY] T=takeoff G=land WASD/arrows=move R=count/compare Q/ESC=exit")

            while not manual.exit_requested:
                if manual.consume_scan_request():
                    counter.start_scan()

                frame = camera.get_frame()

                # ===== [FPS-TEST] FPS 측정 시작 (측정 후 삭제) =====
                loop_count += 1
                total_loops += 1
                if frame is not last_frame:  # 텔로가 새로 보낸 프레임인지 확인
                    new_frames += 1
                    last_frame = frame
                elapsed = time.monotonic() - fps_start
                if elapsed >= 1.0:
                    print(f"[FPS] 처리 {loop_count / elapsed:.1f} | 텔로 수신 {new_frames / elapsed:.1f}")
                    loop_count = new_frames = 0
                    fps_start = time.monotonic()
                # ===== [FPS-TEST] FPS 측정 끝 =====

                detections = detector.detect(frame)
                live_counts = detector.count_by_class(detections)
                _, completed = counter.update(live_counts)
                if completed:
                    for message, _ in dashboard.add_scan(counter):
                        print(message)

                title = "LIVE COUNT - COUNTING..." if counter.scanning else "LIVE SNACK INVENTORY"
                camera_frame = detector.draw(frame.copy(), detections, live_counts, title)
                camera.show(dashboard.draw(camera_frame, counter, camera.get_window_size()))

            # ===== [FPS-TEST] 전체 평균 출력 (측정 후 삭제) =====
            total_elapsed = time.monotonic() - total_start
            if total_elapsed > 0:
                print(f"[FPS] 전체 평균 처리 {total_loops / total_elapsed:.1f} FPS ({total_elapsed:.0f}초 동안)")
            # ===== [FPS-TEST] 전체 평균 출력 끝 =====

    except KeyboardInterrupt:
        print("\n[MODE] INTERRUPTED")
    finally:
        print("[MODE] PROGRAM_END")


if __name__ == "__main__":
    main()
