from collections import deque
from datetime import datetime
from functools import lru_cache

from PIL import ImageFont


FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


@lru_cache(maxsize=16)
def font(size, bold=False):
    return ImageFont.truetype(FONT_PATH, size, index=4 if bold else 0)


class InventoryScreen:
    """재고표에 보여줄 과자 목록과 한글 재고 변화 기록. 화면 표시는 gui/main_window.py가 맡는다."""

    def __init__(self, class_names):
        self.class_names = list(class_names)
        self.logs = deque(maxlen=200)  # (문구, 글자색)
        self.last_scan_at = None
        self.muted, self.accent = "#6b7684", "#3182f6"
        # 기록이 추가될 때마다 올려서 화면이 기록 목록을 다시 그릴 때를 알 수 있게 한다.
        self.version = 0

    @staticmethod
    def visible_names(counter):
        # 이번에 0개가 된 과자도 감소 내역을 보여줘야 하므로 증감 항목은 남긴다.
        names = counter.confirmed.keys() | counter.changes.keys()
        return sorted((name for name in names
                       if counter.confirmed.get(name, 0) > 0 or counter.changes.get(name, 0) != 0),
                      key=lambda name: name)

    def add_scan(self, counter, shelf=None):
        # 동일한 기록을 UI와 터미널에 사용하며 집계가 확정될 때만 추가한다.
        self.last_scan_at = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{self.last_scan_at}]" + (f" {shelf}" if shelf else "")
        entries = [(f"{prefix} 재고 집계 | 총 {sum(counter.confirmed.values())}개", self.accent)]
        if counter.scan_number == 1:
            summary = ", ".join(f"{name} {count}개"
                                for name, count in counter.confirmed.items())
            entries.append((f"{prefix} {summary or '인식된 과자 없음'}", self.muted))
        elif not counter.changes:
            entries.append((f"{prefix} 재고 변화 없음", self.muted))
        for name, difference in counter.changes.items():
            before, after = counter.baseline.get(name, 0), counter.confirmed.get(name, 0)
            action = "증가" if difference > 0 else "감소"
            message = f"{prefix} {name} {abs(difference)}개 {action} ({before}개 → {after}개)"
            entries.append((message, "#18794e" if difference > 0 else "#d93649"))
        self.logs.extend(entries)
        self.version += 1
        return entries

    def add_note(self, message):
        # 자동 스캔 진행 상황 같은 안내 문구를 기록에 추가한다.
        self.logs.append((f"[{datetime.now().strftime('%H:%M:%S')}] {message}", self.muted))
        self.version += 1
