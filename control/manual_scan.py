import time

from location.zone_marker import ZONE_NAMES, shelf_name


# 스캔이 끝난 뒤 집계된 과자를 보라색 박스로 계속 보여주는 시간(초)
COUNTED_HIGHLIGHT_SECONDS = 3.0
# 스캔 중인 층의 마커가 이 시간(초) 넘게 계속 안 보이면 스캔을 취소한다.
MARKER_LOST_SECONDS = 1.0


class ManualScanner:
    """숫자 키로 고른 마커의 층만 집계하는 수동 스캔.

    누른 번호의 마커가 화면에 보일 때만 시작한다. 등록되지 않았거나 화면에 없는 번호,
    스캔 중 추가 입력은 아무 안내 없이 무시한다.
    """

    def __init__(self, inventory):
        self.inventory = inventory
        self.shelf_id = None       # 스캔 중인(또는 마지막으로 스캔한) 매대
        self.target = None         # 이번 프레임에 화면에서 강조할 매대
        self.highlight_until = 0.0
        self.last_seen_at = 0.0

    @property
    def scanning(self):
        return self.shelf_id is not None and self.inventory.counter_for(self.shelf_id).scanning

    def update(self, requested, view, blocked=False):
        """매 프레임 호출한다.

        requested: 이번 프레임에 누른 마커 번호 (없으면 None)
        view: ShelfView
        blocked: True면(자동 스캔 중) 입력과 집계를 모두 멈춘다.
        """
        self.target = None
        if blocked:
            return

        now = time.monotonic()
        if (requested is not None and not self.scanning
                and requested in ZONE_NAMES and requested in view.visible):
            self.start(requested, now)
        if self.scanning:
            self.count(view, now)

        # 스캔 중과 스캔 직후에는 집계 대상 과자를 보라색으로 표시한다.
        if self.shelf_id is not None and (self.scanning or now < self.highlight_until):
            self.target = self.shelf_id
            for detection in view.detections_on(self.shelf_id) or []:
                detection["counted"] = True

    def start(self, shelf_id, now):
        self.shelf_id = shelf_id
        self.inventory.counter_for(shelf_id).start_scan()
        self.inventory.display_shelf = shelf_id
        self.highlight_until = 0.0
        self.last_seen_at = now

    def count(self, view, now):
        counter = self.inventory.counter_for(self.shelf_id)
        counts = view.counts(self.shelf_id)
        if counts is not None:
            self.last_seen_at = now
            _, completed = counter.update(counts)
            if completed:
                self.inventory.record(self.shelf_id)
                self.highlight_until = now + COUNTED_HIGHLIGHT_SECONDS
        elif now - self.last_seen_at > MARKER_LOST_SECONDS:
            # 마커가 잠깐 가려진 프레임은 건너뛰고, 계속 안 보이면 취소한다.
            counter.scanning = False
            self.inventory.note(f"{shelf_name(self.shelf_id)} 마커를 놓쳐 스캔을 취소했습니다.")
