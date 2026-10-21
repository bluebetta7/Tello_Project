import threading
import time

from control.manual_scan import COUNTED_HIGHLIGHT_SECONDS
from location.zone_marker import (ALIGN_LOST_SECONDS, ALIGN_SPEED, ALIGN_TIMEOUT,
                                  MARKER_TARGET_POSITION, SEARCH_MAX_STEPS, SEARCH_STEP_CM,
                                  SHELF_MARKERS, START_SHELF, shelf_name)


class AutoScanner:
    """ArUco 마커에 적힌 순서대로 매대를 집계하는 자동 스캔.

    1. 사용자가 첫 매대(START_SHELF) 앞까지 드론을 옮기고 M 키를 누른다.
    2. 찾을 매대의 마커를 확인 → 마커가 화면 위쪽에 있으면 천천히 상승하다가
       화면 MARKER_TARGET_POSITION(70%) 위치에 닿으면 멈춤 → 그 매대 물품 집계
       (과자는 마커 위에 있으므로 마커가 화면 위쪽에 있으면 과자가 화면 밖으로 잘린다)
    3. 마커 정보의 방향으로 SEARCH_STEP_CM씩 이동하며 다음 매대 마커를 찾는다.
       한 번 이동할 때마다 잠시 멈춰 마커를 찾고, 없으면 한 칸 더 이동한다.
    4. end 표시가 있는 매대를 집계하면 제자리 비행 상태로 끝난다.
    탐색 중 M 키나 자동 스캔 버튼을 다시 누르면 종료한다. (이동 중이면 그 이동이 끝난 뒤 종료)
    찾기·집계 단계에서는 방향키로 위치를 미세 조정할 수 있다.
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
        self.last_seen_at = 0.0    # 높이 맞추기 중 마지막으로 마커를 본 시각
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
        self.enter(self.FIND)
        self.log(f"자동 스캔 시작 - {shelf_name(START_SHELF)} 마커를 찾는 중")

    def stop(self, reason):
        if not self.active:
            return
        if self.state == self.ALIGN:
            self.controller.stop()  # 상승 중이던 RC 명령을 멈춘다.
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
            if any(marker["id"] == self.target_shelf for marker in markers):
                self.seen_frames += 1
                if self.seen_frames >= self.confirmation_frames:
                    if view.marker_position(self.target_shelf) < MARKER_TARGET_POSITION:
                        self.start_align()
                    else:
                        self.start_count()
            else:
                self.seen_frames = 0
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
            # 마커가 목표 위치까지 내려올 때까지 천천히 상승한다. (드론이 오르면 화면 속 마커는 내려간다)
            now = time.monotonic()
            position = view.marker_position(self.target_shelf)
            if position is not None:
                self.last_seen_at = now
            if position is not None and position >= MARKER_TARGET_POSITION or elapsed > ALIGN_TIMEOUT:
                # 목표 위치에 닿았거나 시간이 지나면 그 위치에서 집계한다.
                self.end_align()
                self.start_count()
            elif now - self.last_seen_at > ALIGN_LOST_SECONDS:
                # 마커를 놓치면 멈추고 다시 찾는다.
                self.end_align()
                self.enter(self.FIND)
            else:
                # 마커가 잠깐 안 보이는 프레임에는 상승을 멈추고 제자리에서 기다린다.
                self.controller.move(0, 0, ALIGN_SPEED if position is not None else 0, 0)

        elif self.state == self.COUNT:
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
        # 상승은 이 클래스가 RC 명령으로 직접 보내므로 키보드 RC 전송을 멈춘다.
        self.manual.paused.set()

    def end_align(self):
        self.controller.stop()
        self.manual.paused.clear()

    def start_count(self):
        self.enter(self.COUNT)
        self.inventory.counter_for(self.target_shelf).start_scan()
        self.log(f"{shelf_name(self.target_shelf)} 마커 확인 - 집계 시작")

    def start_step(self):
        # 다음 매대 방향으로 한 칸(SEARCH_STEP_CM) 이동한다. 이동이 끝나면 FIND 상태로 돌아간다.
        direction = self.current_move["direction"]
        self.steps_taken += 1
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
