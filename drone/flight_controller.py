from djitellopy import Tello
import logging
import time


class FlightController:
    def __init__(self):
        Tello.LOGGER.setLevel(logging.WARNING)
        
        self.tello = Tello()
        self.connected = False
        self.is_flying = False
        self.control_ready_at = 0

    def connect(self):
        self.tello.connect()
        self.connected = True
        print(f"배터리: {self.tello.get_battery()}%")

    def takeoff(self):
        if self.is_flying:
            return
        self.stop()
        try:
            self.tello.takeoff()
        except Exception as error:
            # 응답만 늦고 실제로는 이륙한 경우가 있어 고도로 비행 여부를 다시 판단한다.
            try:
                height = self.tello.get_height()
            except Exception:
                height = 0
            if height <= 0:
                raise
            print(f"[SAFETY] TAKEOFF_RESPONSE_ERROR (고도 {height}cm, 비행 중으로 처리): {error}")
        self.is_flying = True
        self.control_ready_at = time.monotonic() + 2.0

    def land(self):
        if not self.connected:
            return
        # 비행 상태 값과 관계없이 착륙 명령을 보낸다. (상태 값이 어긋나도 착륙 가능)
        try:
            self.tello.land()
        except Exception:
            if self.is_flying:
                raise  # 비행 중 착륙 실패는 호출한 쪽에서 재시도할 수 있게 알린다.
            return  # 지상에서 보낸 착륙 명령의 오류는 무시한다.
        self.is_flying = False

    def move(self, left_right=0, forward_back=0, up_down=0, yaw=0):
        if not self.connected:
            return

        command = (left_right, forward_back, up_down, yaw)

        if (
            self.is_flying
            and time.monotonic() < self.control_ready_at
            and any(command)
        ):
            # 이륙 직후 2초간 이동 차단은 로그 없이 그대로 유지한다.
            command = (0, 0, 0, 0)

        self.tello.send_rc_control(*command)

    def stop(self):
        self.move(0, 0, 0, 0)

    def move_by(self, direction, distance):
        # 자동 스캔용 거리 이동. 텔로가 이동을 마치고 응답할 때까지 기다린다.
        actions = {
            "up": self.tello.move_up,
            "down": self.tello.move_down,
            "left": self.tello.move_left,
            "right": self.tello.move_right,
            "forward": self.tello.move_forward,
            "back": self.tello.move_back,
            "cw": self.tello.rotate_clockwise,
            "ccw": self.tello.rotate_counter_clockwise,
        }
        if direction not in actions:
            raise ValueError(f"알 수 없는 이동 방향: {direction}")
        if not self.is_flying:
            raise RuntimeError("비행 중이 아니라 이동할 수 없습니다.")
        actions[direction](int(distance))

    def disconnect(self):
        if not self.connected:
            return

        self.stop()
        self.tello.end()
        self.connected = False
