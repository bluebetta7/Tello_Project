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
        forward_deceleration_step=50,
    ):
        self.controller = controller
        self.control_interval = control_interval
        self.acceleration_step = acceleration_step
        self.deceleration_step = deceleration_step
        # RC 순서: 좌우, 앞뒤, 상하, 회전. 각 축의 키·속도·감속을 묶는다.
        self.axes = (
            ("d", "a", lateral_speed, deceleration_step),
            # 앞뒤 축은 별도 감속값을 사용한다. (50 = 키를 떼면 즉시 정지)
            ("w", "s", speed, forward_deceleration_step),
            (keyboard.Key.up, keyboard.Key.down, vertical_speed, vertical_deceleration_step),
            (keyboard.Key.right, keyboard.Key.left, rotation_speed, rotation_deceleration_step),
        )
        self.movement_keys = {key for positive, negative, _, _ in self.axes for key in (positive, negative)}
        self.pressed = set()
        self.targets = (0, 0, 0, 0)
        self.current = (0, 0, 0, 0)
        self.exit_requested = False
        # 숫자 키: 해당 번호의 마커가 붙은 층을 스캔한다. 누른 번호를 순서대로 쌓아 둔다.
        self.scan_requests = []
        self.scan_keys_pressed = set()
        # 자동 스캔: M 키로 시작, 탐색 중 다시 누르면 종료
        self.auto_requests = 0
        self.auto_key_pressed = False
        # 자동 스캔의 거리 이동 중에는 RC 명령을 보내지 않는다 (이동 명령과 충돌 방지).
        self.paused = threading.Event()
        # 별도 촬영 프로그램에서 사용하는 V 키 기능도 유지한다.
        self.recording_toggle_requested = False
        self.recording_key_pressed = False
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.listener = None
        self.control_thread = None
        self.takeoff_thread = None
        self.land_thread = None

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
            if not self.paused.is_set():
                self.controller.move(*command)
            self.stop_event.wait(self.control_interval)

    def clear_movement(self):
        with self.lock:
            self.pressed.clear()
            self.targets = self.current = (0, 0, 0, 0)
        self.controller.stop()

    def safe_takeoff(self):
        try:
            self.controller.takeoff()
        except Exception as error:
            # 이륙 실패가 키보드 입력 처리를 멈추지 않도록 오류만 출력한다.
            print(f"[SAFETY] TAKEOFF_FAILED: {type(error).__name__}: {error}")

    def safe_land(self):
        try:
            self.controller.land()
        except Exception as error:
            # 착륙 실패 시 G 키·착륙 버튼으로 재시도할 수 있게 오류만 출력한다.
            print(f"[SAFETY] LAND_FAILED: {type(error).__name__}: {error}")

    # 아래 동작은 키 입력과 GUI 버튼이 함께 사용한다.
    def takeoff(self):
        # 이륙은 응답을 기다리는 동안 입력이 막히지 않도록 별도 스레드에서 처리한다.
        if self.takeoff_thread is None or not self.takeoff_thread.is_alive():
            self.takeoff_thread = threading.Thread(target=self.safe_takeoff, daemon=True)
            self.takeoff_thread.start()

    def land(self):
        self.clear_movement()
        # 착륙도 응답을 기다리는 동안 입력·화면이 멈추지 않도록 별도 스레드에서 처리한다.
        if self.land_thread is None or not self.land_thread.is_alive():
            self.land_thread = threading.Thread(target=self.safe_land, daemon=True)
            self.land_thread.start()

    def request_exit(self):
        self.exit_requested = True
        self.clear_movement()

    def request_auto(self):
        with self.lock:
            self.auto_requests += 1

    def request_scan(self, shelf_id):
        with self.lock:
            self.scan_requests.append(shelf_id)

    def release_all(self):
        # 창이 포커스를 잃으면 키를 뗀 신호가 오지 않으므로 눌린 키를 모두 뗀 것으로 처리한다.
        with self.lock:
            self.pressed.clear()
            self.scan_keys_pressed.clear()
            self.auto_key_pressed = False
            self.update_targets()

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
            self.takeoff()
        elif key == "g":
            self.land()
        elif key in ("q", keyboard.Key.esc):
            self.request_exit()
        elif key == "v" and not self.recording_key_pressed:
            self.recording_key_pressed = True
            self.recording_toggle_requested = True
        elif key == "m":
            with self.lock:
                if self.auto_key_pressed:
                    return
                self.auto_key_pressed = True
            self.request_auto()
        elif isinstance(key, str) and key.isdigit():
            # 키를 누르고 있을 때 발생하는 반복 입력은 한 번만 처리한다.
            with self.lock:
                if key in self.scan_keys_pressed:
                    return
                self.scan_keys_pressed.add(key)
            self.request_scan(int(key))

    def on_release(self, key):
        key = self.key_value(key)
        if key in self.movement_keys:
            with self.lock:
                self.pressed.discard(key)
                self.update_targets()
        elif key == "v":
            self.recording_key_pressed = False
        elif isinstance(key, str) and key.isdigit():
            with self.lock:
                self.scan_keys_pressed.discard(key)
        elif key == "m":
            with self.lock:
                self.auto_key_pressed = False
        # 키를 떼면 기존과 같은 감속으로 급제동 반동을 줄인다.

    def consume_scan_request(self):
        # 가장 먼저 누른 마커 번호를 돌려준다. 입력이 없으면 None.
        with self.lock:
            return self.scan_requests.pop(0) if self.scan_requests else None

    def consume_auto_request(self):
        with self.lock:
            if self.auto_requests == 0:
                return False
            self.auto_requests -= 1
            return True

    def start(self, listen_keyboard=True):
        # listen_keyboard=False: 키 입력을 pynput 대신 GUI 창이 on_press/on_release로 넘겨준다.
        self.stop_event.clear()
        self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
        self.control_thread.start()
        if listen_keyboard:
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
