import threading
import time

from control.manual_scan import COUNTED_HIGHLIGHT_SECONDS
from location.zone_marker import (ALIGN_LOST_SECONDS, ALIGN_SPEED, ALIGN_TIMEOUT, DISTANCE_SPEED,
                                  MARKER_SIZE_TOLERANCE, MARKER_TARGET_POSITION, MARKER_TARGET_X,
                                  MARKER_X_TOLERANCE, SEARCH_MAX_STEPS, SEARCH_STEP_CM, SHELF_MARKERS,
                                  START_SHELF, STRAFE_SPEED, shelf_name)


class AutoScanner:
    """ArUco 마커에 적힌 순서대로 매대를 집계하는 자동 스캔.

    1. 사용자가 첫 매대(START_SHELF) 앞까지 드론을 옮기고 M 키를 누른다.
       이때 처음 찾은 마커의 크기를 기준 거리로 기억한다.
    2. 찾을 매대의 마커를 확인 → 위치 맞추기: 마커가 화면 MARKER_TARGET_POSITION(70%) 높이에
       올 때까지 상승하면서, 가로 MARKER_TARGET_X(75%) 위치와 기준 크기(±12%)도 맞춘다.
       (과자는 마커 위에 있으므로 마커가 화면 위쪽에 있으면 과자가 화면 밖으로 잘린다)
       → 그 매대 물품 집계. 집계하는 동안에도 좌우·거리가 벗어나면 계속 바로잡는다.
    3. 마커 정보의 방향으로 SEARCH_STEP_CM씩 이동하며 다음 매대 마커를 찾는다.
       한 번 이동할 때마다 잠시 멈춰 마커를 찾고, 없으면 한 칸 더 이동한다.
       찾는 동안 다른 층 마커가 보이면(마커가 한 줄로 붙어 있으므로) 그 마커로 좌우·거리를 바로잡는다.
    4. end 표시가 있는 매대를 집계하면 제자리 비행 상태로 끝난다.
    탐색 중 M 키나 자동 스캔 버튼을 다시 누르면 종료한다. (이동 중이면 그 이동이 끝난 뒤 종료)
    첫 매대를 찾는 동안에는 방향키로 위치를 잡을 수 있고, 그 뒤로는 자동 스캔이 위치를 맞춘다.
    """

    IDLE, FIND, ALIGN, COUNT, MOVE = "idle", "find", "align", "count", "move"

    def __init__(self, controller, manual, inventory, find_timeout=10.0, step_search_time=2.0,
                 count_timeout=8.0, confirmation_frames=3):
        self.controller = controller
        self.manual = manual
        self.inventory = inventory              # ShelfInventory: 매대별 집계·기록
        self.find_timeout = find_timeout        # 첫 매대 마커가 이 시간 안에 안 보이면 중단
        self.step_search_time = step_search_time  # 한 칸 이동 후 마커를 찾는 시간
        self.steps_taken = 0
        self.current_move = None                # 다음 매대로 가는 이동 정보 (없으면 첫 매대 탐색)
        self.count_timeout = count_timeout      # 수량이 이 시간 안에 안정되지 않으면 중단
        self.confirmation_frames = confirmation_frames

        self.state = self.IDLE
        self.target_shelf = None   # 지금 찾거나 집계하는 매대(마커 번호)
        self.seen_frames = 0
        self.state_started = 0.0
        self.last_seen_at = 0.0    # 위치 맞추기 중 마지막으로 마커를 본 시각
        self.reference_size = None # 처음 찾은 마커의 크기(픽셀). 이 크기를 유지해 거리를 일정하게 한다.
        self.size_samples = []     # 기준 크기를 정할 때 쓰는 최근 프레임의 마커 크기
        self.correcting = {"x": False, "size": False}  # 좌우·거리를 바로잡는 중인지 (흔들림 방지)
        self.steering = False      # 자동 스캔이 RC 명령을 보내는 중인지
        self.highlight_shelf = None  # 집계 직후 보라색 박스로 표시할 매대
        self.highlight_until = 0.0
        self.move_thread = None
        self.move_error = None
        self.stop_after_move = False

    @property
    def active(self):
        return self.state != self.IDLE

    @property
    def shelf_id(self):
        # 화면 재고표에 보여줄 매대 (찾는 중에도 다음 매대를 표시)
        return self.target_shelf if self.active else None

    def log(self, message):
        print(f"[AUTO] {message}")
        self.inventory.note(message)

    def enter(self, state):
        self.state = state
        self.state_started = time.monotonic()
        self.seen_frames = 0

    def toggle(self):
        # M 키·자동 스캔 버튼: 대기 중이면 시작, 탐색 중이면 종료
        if not self.active:
            self.start()
        elif self.state == self.MOVE:
            self.stop_after_move = True
            self.log("종료 요청 - 현재 이동이 끝나면 멈춥니다.")
        else:
            self.stop("사용자 요청으로 종료")

    def start(self):
        if not self.controller.is_flying:
            self.log("이륙한 뒤에 자동 스캔을 시작할 수 있습니다.")
            return
        if START_SHELF not in SHELF_MARKERS:
            self.log(f"시작 매대({START_SHELF})가 마커 정보에 없습니다.")
            return
        self.target_shelf = START_SHELF
        self.current_move = None
        self.reference_size = None
        self.enter(self.FIND)
        self.log(f"자동 스캔 시작 - {shelf_name(START_SHELF)} 마커를 찾는 중")

    def stop(self, reason):
        if not self.active:
            return
        self.hold()                 # 위치 맞추던 RC 명령을 멈춘다.
        self.manual.paused.clear()
        self.state = self.IDLE
        self.target_shelf = None
        self.stop_after_move = False
        self.log(f"자동 스캔 종료 - {reason}")

    def update(self, view):
        """매 프레임 호출한다. view: ShelfView

        반환: 이번 프레임에 집계가 끝난 매대 번호 (없으면 None)
        """
        finished = self.step(view)
        # 집계 중인 매대와 방금 집계를 마친 매대의 과자는 수동 스캔과 같이 보라색으로 표시한다.
        shelves = {self.target_shelf} if self.state == self.COUNT else set()
        if time.monotonic() < self.highlight_until:
            shelves.add(self.highlight_shelf)
        for shelf_id in shelves:
            for detection in view.detections_on(shelf_id) or []:
                detection["counted"] = True
        return finished

    def step(self, view):
        if not self.active:
            return None
        markers, shelf_counts, counter_for = view.markers, view.counts, self.inventory.counter_for
        self.inventory.display_shelf = self.target_shelf
        if not self.controller.is_flying and self.state != self.MOVE:
            self.stop("착륙함")
            return None

        elapsed = time.monotonic() - self.state_started

        if self.state == self.FIND:
            # 다른 마커는 무시하고 지정된 매대의 마커만 찾는다.
            target = view.marker_info(self.target_shelf)
            if self.current_move is None:
                self.manual.paused.clear()   # 첫 매대를 찾는 동안에는 사용자가 방향키로 위치를 잡는다.
            if target is not None:
                self.seen_frames += 1
                self.size_samples = (self.size_samples + [target["size"]])[-self.confirmation_frames:]
                if self.current_move is not None:
                    self.steer(target)
                if self.seen_frames >= self.confirmation_frames:
                    if self.reference_size is None:
                        # 처음 찾은 마커의 크기(확인한 몇 프레임의 가운데 값) = 유지할 기준 거리
                        self.reference_size = sorted(self.size_samples)[len(self.size_samples) // 2]
                    self.start_align()
            else:
                self.seen_frames = 0
                other = view.marker_info()
                if self.current_move is not None:
                    # 이동 뒤 찾는 마커가 안 보이면, 보이는 다른 층 마커로 좌우·거리부터 바로잡는다.
                    if other is not None:
                        self.steer(other)
                    else:
                        self.hold()
                if self.current_move is None:
                    # 첫 매대: 사용자가 옮겨 놓은 위치에서 기다리며 찾는다.
                    if elapsed > self.find_timeout:
                        self.stop(f"{self.find_timeout:.0f}초 동안 "
                                  f"{shelf_name(self.target_shelf)} 마커를 찾지 못함")
                elif elapsed > self.step_search_time:
                    # 다음 매대: 이번 위치에 없으면 한 칸 더 이동한다.
                    if self.steps_taken >= SEARCH_MAX_STEPS:
                        self.stop(f"{SEARCH_STEP_CM * SEARCH_MAX_STEPS}cm 이동했지만 "
                                  f"{shelf_name(self.target_shelf)} 마커를 찾지 못함")
                    else:
                        self.start_step()

        elif self.state == self.ALIGN:
            # 마커가 목표 높이까지 내려올 때까지 천천히 상승하면서 좌우·거리도 맞춘다.
            # (드론이 오르면 화면 속 마커는 내려간다)
            now = time.monotonic()
            target = view.marker_info(self.target_shelf)
            if target is not None:
                self.last_seen_at = now
                if self.steer(target, rise=True) and target["y"] >= MARKER_TARGET_POSITION:
                    self.start_count()     # 세 방향 모두 맞으면 그 위치에서 집계한다.
                elif elapsed > ALIGN_TIMEOUT:
                    self.start_count()     # 시간 안에 다 못 맞추면 그 위치에서 집계한다.
            elif now - self.last_seen_at > ALIGN_LOST_SECONDS:
                # 마커를 놓치면 멈추고 다시 찾는다.
                self.hold()
                self.enter(self.FIND)
            else:
                # 마커가 잠깐 안 보이는 프레임에는 멈추고 제자리에서 기다린다.
                self.hold()

        elif self.state == self.COUNT:
            # 집계하는 동안에도 좌우·거리가 벗어나면 바로잡는다. (높이는 그대로)
            target = view.marker_info(self.target_shelf)
            if target is not None:
                self.steer(target)
            else:
                self.hold()
            counts = shelf_counts(self.target_shelf)
            if counts is not None:
                _, completed = counter_for(self.target_shelf).update(counts)
                if completed:
                    finished = self.target_shelf
                    self.inventory.record(finished)
                    self.highlight_shelf = finished
                    self.highlight_until = time.monotonic() + COUNTED_HIGHLIGHT_SECONDS
                    info = SHELF_MARKERS[finished]
                    if info.get("end"):
                        self.stop("마지막 매대 집계 완료")
                    elif info.get("next") and info["next"].get("shelf") in SHELF_MARKERS:
                        self.current_move = info["next"]
                        self.target_shelf = info["next"]["shelf"]
                        self.steps_taken = 0
                        self.start_step()
                    else:
                        self.stop(f"{shelf_name(finished)}의 다음 매대 정보가 없음")
                    return finished
            if elapsed > self.count_timeout:
                counter_for(self.target_shelf).scanning = False
                self.stop(f"{shelf_name(self.target_shelf)} 수량이 안정되지 않음")

        elif self.state == self.MOVE:
            if not self.move_thread.is_alive():
                self.manual.paused.clear()
                if self.move_error is not None:
                    self.stop(f"이동 실패 ({self.move_error})")
                elif self.stop_after_move:
                    self.stop("사용자 요청으로 종료")
                else:
                    self.enter(self.FIND)

        return None

    def start_align(self):
        self.enter(self.ALIGN)
        self.last_seen_at = time.monotonic()

    def axis_speed(self, axis, error, tolerance, max_speed):
        # 오차가 허용 범위를 넘으면 바로잡기 시작하고, 허용 범위의 절반 안으로 들어오면 멈춘다.
        # (경계에서 움직였다 멈췄다를 반복하지 않게 한다) 오차가 클수록 빠르게, 최대 max_speed.
        if self.correcting[axis]:
            self.correcting[axis] = abs(error) > tolerance / 2
        else:
            self.correcting[axis] = abs(error) > tolerance
        if not self.correcting[axis]:
            return 0
        speed = max(6, min(max_speed, int(max_speed * abs(error) / (3 * tolerance))))
        return speed if error > 0 else -speed

    def steer(self, marker, rise=False):
        """마커가 가로 목표 위치·기준 크기(·rise면 목표 높이)에 오도록 RC 명령을 보낸다. 다 맞으면 True."""
        # 마커가 목표보다 오른쪽에 보이면 드론을 오른쪽으로 (드론이 움직이면 화면 속 마커는 반대로 간다)
        left_right = self.axis_speed("x", marker["x"] - MARKER_TARGET_X, MARKER_X_TOLERANCE, STRAFE_SPEED)
        forward_back = 0
        if self.reference_size:
            # 마커가 기준보다 크게 보이면(가까우면) 뒤로, 작게 보이면(멀면) 앞으로
            ratio = marker["size"] / self.reference_size - 1
            forward_back = -self.axis_speed("size", ratio, MARKER_SIZE_TOLERANCE, DISTANCE_SPEED)
        up_down = ALIGN_SPEED if rise and marker["y"] < MARKER_TARGET_POSITION else 0
        if left_right or forward_back or up_down:
            # 이 클래스가 RC 명령을 직접 보내므로 키보드 RC 전송을 멈춘다.
            self.manual.paused.set()
            self.controller.move(left_right, forward_back, up_down, 0)
            self.steering = True
            return False
        self.hold()
        return True

    def hold(self):
        # 바로잡기를 멈추고 제자리 비행한다.
        if self.steering:
            self.controller.stop()
            self.steering = False
        self.correcting = {"x": False, "size": False}

    def start_count(self):
        self.hold()
        self.manual.paused.set()   # 집계 중에는 자동 스캔이 위치를 유지하므로 키보드 조종을 멈춘다.
        self.enter(self.COUNT)
        self.inventory.counter_for(self.target_shelf).start_scan()
        self.log(f"{shelf_name(self.target_shelf)} 마커 확인 - 집계 시작")

    def start_step(self):
        # 다음 매대 방향으로 한 칸(SEARCH_STEP_CM) 이동한다. 이동이 끝나면 FIND 상태로 돌아간다.
        direction = self.current_move["direction"]
        self.steps_taken += 1
        self.hold()
        self.enter(self.MOVE)
        self.move_error = None
        # 거리 이동 중에는 키보드 RC 전송을 멈춰 이동 명령과 충돌하지 않게 한다.
        self.manual.paused.set()
        self.controller.stop()

        def run():
            try:
                self.controller.move_by(direction, SEARCH_STEP_CM)
            except Exception as error:
                self.move_error = f"{type(error).__name__}: {error}"

        self.move_thread = threading.Thread(target=run, daemon=True)
        self.move_thread.start()
