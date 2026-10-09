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
        if not self.is_flying:
            self.stop()
            self.tello.takeoff()
            self.is_flying = True
            self.control_ready_at = time.monotonic() + 2.0

    def land(self):
        if self.is_flying:
            self.tello.land()
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

    def disconnect(self):
        if not self.connected:
            return

        self.stop()
        self.tello.end()
        self.connected = False
