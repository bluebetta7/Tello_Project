class ScreenRenderer:
    """카메라 영상 위에 과자 박스·실시간 수량·마커 표시를 그린다."""

    def __init__(self, detector, zones, inventory):
        self.detector = detector
        self.zones = zones
        self.inventory = inventory

    def render(self, frame, view, target):
        # target: 오른쪽 위 안내 상자에 과자 목록을 보여줄 매대 (없으면 인식된 마커 목록)
        title = "재고 집계 중..." if self.inventory.scanning else "실시간 탐지"
        image = self.detector.draw(frame.copy(), view.detections,
                                   self.detector.count_by_class(view.detections), title)
        return self.zones.draw(image, view.markers, target,
                               None if target is None else view.lines(target))
