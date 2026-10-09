import threading

from pynput import keyboard


class KeyboardFlight:
    def __init__(
        self,
        controller,
        speed=50,
        vertical_speed=25,
        rotation_speed=32,
        control_interval=0.05,
        acceleration_step=10,
        deceleration_step=5,
        vertical_deceleration_step=5,
        rotation_deceleration_step=10,
        lateral_speed=40,
    ):
        self.controller = controller
        self.control_interval = control_interval
        self.acceleration_step = acceleration_step
        self.deceleration_step = deceleration_step
        # RC 순서: 좌우, 앞뒤, 상하, 회전. 각 축의 키·속도·감속을 묶는다.
        self.axes = (
            ("d", "a", lateral_speed, deceleration_step),
            ("w", "s", speed, deceleration_step),
            (keyboard.Key.up, keyboard.Key.down, vertical_speed, vertical_deceleration_step),
            (keyboard.Key.right, keyboard.Key.left, rotation_speed, rotation_deceleration_step),
        )
        self.movement_keys = {key for positive, negative, _, _ in self.axes for key in (positive, negative)}
        self.pressed = set()
        self.targets = (0, 0, 0, 0)
        self.current = (0, 0, 0, 0)
        self.exit_requested = False
        self.scan_requests = 0
        self.scan_key_pressed = False
        # 별도 촬영 프로그램에서 사용하는 V 키 기능도 유지한다.
        self.recording_toggle_requested = False
        self.recording_key_pressed = False
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.listener = None
        self.control_thread = None

    def update_targets(self):
        self.targets = tuple(
            speed * ((positive in self.pressed) - (negative in self.pressed))
            for positive, negative, speed, _ in self.axes
        )

    def next_speed(self, current, target, deceleration_step=None):
        if target == 0:
            step = self.deceleration_step if deceleration_step is None else deceleration_step
            if current > 0:
                return max(current - step, 0)
            return min(current + step, 0)
        if current * target < 0:
            current = 0
        if current < target:
            return min(current + self.acceleration_step, target)
        return max(current - self.acceleration_step, target)

    def control_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                self.current = tuple(
                    self.next_speed(current, target, axis[3])
                    for current, target, axis in zip(self.current, self.targets, self.axes)
                )
                command = self.current
            # 이동 명령은 한 스레드에서 일정한 간격으로 전송한다.
            self.controller.move(*command)
            self.stop_event.wait(self.control_interval)

    def clear_movement(self):
        with self.lock:
            self.pressed.clear()
            self.targets = self.current = (0, 0, 0, 0)
        self.controller.stop()

    @staticmethod
    def key_value(key):
        char = getattr(key, "char", None)
        return char.lower() if char else key

    def on_press(self, key):
        key = self.key_value(key)
        if key in self.movement_keys:
            with self.lock:
                self.pressed.add(key)
                self.update_targets()
        elif key == "t":
            self.controller.takeoff()
        elif key == "g":
            self.clear_movement()
            try:
                self.controller.land()
            except Exception as error:
                # 착륙 실패 시 리스너를 유지해 G 키로 재시도할 수 있게 한다.
                print(f"[SAFETY] LAND_FAILED: {type(error).__name__}: {error}")
        elif key in ("q", keyboard.Key.esc):
            self.exit_requested = True
            self.clear_movement()
        elif key == "v" and not self.recording_key_pressed:
            self.recording_key_pressed = True
            self.recording_toggle_requested = True
        elif key == "r":
            # 키를 누르고 있을 때 발생하는 반복 입력은 한 번만 처리한다.
            with self.lock:
                if not self.scan_key_pressed:
                    self.scan_key_pressed = True
                    self.scan_requests += 1

    def on_release(self, key):
        key = self.key_value(key)
        if key in self.movement_keys:
            with self.lock:
                self.pressed.discard(key)
                self.update_targets()
        elif key == "v":
            self.recording_key_pressed = False
        elif key == "r":
            with self.lock:
                self.scan_key_pressed = False
        # 키를 떼면 기존과 같은 감속으로 급제동 반동을 줄인다.

    def consume_scan_request(self):
        with self.lock:
            if self.scan_requests == 0:
                return False
            self.scan_requests -= 1
            return True

    def start(self):
        self.stop_event.clear()
        self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
        self.control_thread.start()
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.listener.start()
        return self.listener

    def stop(self):
        self.stop_event.set()
        if self.listener is not None:
            self.listener.stop()
        if self.control_thread is not None:
            self.control_thread.join(timeout=1)
        self.controller.stop()
