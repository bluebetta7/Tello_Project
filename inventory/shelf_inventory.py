from inventory.product_detector import ProductDetector
from inventory.snack_recognizer import UNKNOWN
from inventory.stock_counter import StockCounter
from location.zone_marker import ZoneMarkerDetector, shelf_name


class ShelfView:
    """한 프레임의 과자 탐지 결과를 매대(마커) 단위로 나눠 보여준다."""

    def __init__(self, detections, markers, frame_height, frame_width=None):
        self.detections = ZoneMarkerDetector.assign_zones(detections, markers)
        self.markers = markers
        self.visible = {marker["id"] for marker in markers}
        self.frame_height = frame_height
        self.frame_width = frame_width or frame_height * 4 // 3   # 텔로 화면은 4:3

    def marker_info(self, shelf_id=None):
        """마커의 화면 속 위치와 크기. x·y: 0~1 (왼쪽·위가 0), size: 한 변 길이(픽셀).

        shelf_id를 주면 그 마커, 안 주면 보이는 마커 중 하나. 안 보이면 None.
        """
        for marker in self.markers:
            if shelf_id is None or marker["id"] == shelf_id:
                return {"x": marker["center"][0] / self.frame_width,
                        "y": marker["center"][1] / self.frame_height, "size": marker["size"]}
        return None

    def marker_position(self, shelf_id):
        # 마커 중심의 세로 위치. 0 = 화면 맨 위, 1 = 맨 아래. 마커가 안 보이면 None.
        for marker in self.markers:
            if marker["id"] == shelf_id:
                return marker["center"][1] / self.frame_height
        return None

    def detections_on(self, shelf_id):
        # 해당 매대에 배정된 과자만 고른다. 마커가 안 보이는 프레임은 건너뛴다(None).
        if shelf_id not in self.visible:
            return None
        return [detection for detection in self.detections if detection["zone_id"] == shelf_id]

    def counts(self, shelf_id):
        # 재고 집계용 수량. 종류를 모르는 봉지("미확인")는 재고표·총 개수·기록에 넣지 않는다.
        selected = self.detections_on(shelf_id)
        if selected is None:
            return None
        return ProductDetector.count_by_class([d for d in selected if d["class_name"] != UNKNOWN])

    def lines(self, shelf_id):
        # 해당 층에서 탐지 중인 과자를 한 줄에 하나씩. 마커가 안 보이면 빈 목록.
        counts = self.counts(shelf_id) or {}
        return [f"{name} {count}개" for name, count in sorted(counts.items())]


class ShelfInventory:
    """매대(마커 번호)별 재고 집계와 화면 기록을 관리한다."""

    def __init__(self, dashboard, confirmation_frames=3):
        self.dashboard = dashboard
        self.confirmation_frames = confirmation_frames
        self.counters = {}
        self.display_shelf = None  # 아래 재고표에 보여줄 매대

    def counter_for(self, shelf_id):
        if shelf_id not in self.counters:
            self.counters[shelf_id] = StockCounter(confirmation_frames=self.confirmation_frames)
        return self.counters[shelf_id]

    @property
    def scanning(self):
        return any(counter.scanning for counter in self.counters.values())

    def record(self, shelf_id):
        # 집계가 확정된 매대의 결과를 화면 기록과 터미널에 남긴다.
        for message, _ in self.dashboard.add_scan(self.counter_for(shelf_id), shelf_name(shelf_id)):
            print(message)

    def note(self, message):
        self.dashboard.add_note(message)
