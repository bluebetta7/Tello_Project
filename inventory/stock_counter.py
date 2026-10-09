class StockCounter:
    def __init__(self, confirmation_frames=3):
        self.confirmation_frames = confirmation_frames
        self.candidate = None
        self.candidate_frames = 0
        self.confirmed = {}
        self.baseline = None
        self.changes = {}
        self.scanning = False
        self.scan_number = 0

    def start_scan(self):
        if self.scanning:
            return False
        # 직전 화면의 확정 수량을 재사용하지 않고 새 프레임에서 집계한다.
        self.candidate = None
        self.candidate_frames = 0
        self.scanning = True
        return True

    def update(self, counts):
        if not self.scanning:
            return self.confirmed.copy(), False

        # 정렬해 클래스 입력 순서가 달라도 같은 결과로 비교한다.
        candidate = dict(sorted(counts.items()))

        if candidate == self.candidate:
            self.candidate_frames += 1
        else:
            self.candidate = candidate
            self.candidate_frames = 1

        if self.candidate_frames < self.confirmation_frames:
            return self.confirmed.copy(), False

        # 새 집계가 확정될 때만 직전 수량을 비교 기준으로 보관한다.
        self.baseline = self.confirmed.copy()
        self.confirmed = candidate.copy()
        self.changes = {} if self.scan_number == 0 else {
            name: self.confirmed.get(name, 0) - self.baseline.get(name, 0)
            for name in sorted(self.baseline.keys() | self.confirmed.keys())
            if self.confirmed.get(name, 0) != self.baseline.get(name, 0)
        }
        self.scanning = False
        self.scan_number += 1
        return self.confirmed.copy(), True

    def format_changes(self):
        return ", ".join(
            f"{name}: {self.baseline.get(name, 0)} -> "
            f"{self.confirmed.get(name, 0)} ({difference:+d})"
            for name, difference in self.changes.items()
        ) or "none"

    @staticmethod
    def format_counts(counts):
        if not counts:
            return "none"

        return " ".join(
            f"{name}={count}"
            for name, count in sorted(counts.items())
        )
