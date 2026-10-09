from collections import deque
from datetime import datetime
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


SNACK_NAMES = {
    "Concho": "콘초", "HoneyButterChip": "허니버터칩",
    "BananaKick": "바나나킥", "Pocachip": "포카칩",
}
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


@lru_cache(maxsize=16)
def font(size, bold=False):
    return ImageFont.truetype(FONT_PATH, size, index=4 if bold else 0)


@lru_cache(maxsize=1024)
def wrap_log(message, width, size):
    lines, line = [], ""
    for word in message.split():
        candidate = f"{line} {word}".strip()
        if line and font(size).getlength(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return tuple(lines + [line])


class InventoryScreen:
    """창 크기에 맞춘 상단 카메라 / 아래 재고 표와 한글 로그."""

    def __init__(self, class_names, width=960, height=1040):
        self.class_names = list(class_names)
        self.width, self.height = width, height
        self.logs = deque(maxlen=200)
        self.last_scan_at = None
        self.text_color, self.muted, self.accent = "#191f28", "#6b7684", "#3182f6"

    def text(self, draw, message, position, color=None, size=20, bold=False):
        draw.text(position, str(message), font=font(size, bold),
                  fill=color or self.text_color, anchor="lt")

    def add_scan(self, counter):
        # 동일한 기록을 UI와 터미널에 사용하며 R 확정 때만 추가한다.
        self.last_scan_at = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{self.last_scan_at}]"
        entries = [(f"{prefix} 재고 집계 | 총 {sum(counter.confirmed.values())}개", self.accent)]
        if counter.scan_number == 1:
            summary = ", ".join(f"{SNACK_NAMES.get(name, name)} {count}개"
                                for name, count in counter.confirmed.items())
            entries.append((f"{prefix} {summary or '인식된 과자 없음'}", self.muted))
        elif not counter.changes:
            entries.append((f"{prefix} 재고 변화 없음", self.muted))
        for name, difference in counter.changes.items():
            before, after = counter.baseline.get(name, 0), counter.confirmed.get(name, 0)
            action = "증가" if difference > 0 else "감소"
            message = f"{prefix} {SNACK_NAMES.get(name, name)} {abs(difference)}개 {action} ({before}개 → {after}개)"
            entries.append((message, "#18794e" if difference > 0 else "#d93649"))
        self.logs.extend(entries)
        return entries

    def draw(self, camera_frame, counter, size=None):
        width, height = size or (self.width, self.height)
        scale = min(width / 960, height / 720)
        unit = lambda value: max(1, int(value * scale))
        margin, padding, radius = unit(16), unit(20), unit(20)
        bottom_y = height - max(unit(280), int(height * 0.28)) - margin
        middle = width // 2
        canvas = Image.new("RGB", (width, height), "#f2f4f6")
        draw = ImageDraw.Draw(canvas)
        cards = ((margin, margin, width - margin, bottom_y - margin),
                 (margin, bottom_y, middle - margin // 2, height - margin),
                 (middle + margin // 2, bottom_y, width - margin, height - margin))
        for x1, y1, x2, y2 in cards:
            draw.rounded_rectangle((x1, y1 + unit(3), x2, y2 + unit(3)),
                                   radius, fill="#e5e8eb")
            draw.rounded_rectangle((x1, y1, x2, y2), radius, fill="#ffffff")

        # 카메라는 원본 비율을 유지하고 카드의 둥근 모서리 안쪽에 배치한다.
        inset = unit(10)
        available_w = width - 2 * (margin + inset)
        available_h = bottom_y - 2 * margin - 2 * inset
        frame_h, frame_w = camera_frame.shape[:2]
        ratio = min(available_w / frame_w, available_h / frame_h)
        image = Image.fromarray(cv2.cvtColor(camera_frame, cv2.COLOR_BGR2RGB))
        image = image.resize((max(1, int(frame_w * ratio)), max(1, int(frame_h * ratio))),
                             Image.Resampling.BILINEAR)
        canvas.paste(image, (margin + inset + (available_w - image.width) // 2,
                             margin + inset + (available_h - image.height) // 2))

        left, left_end = margin + padding, middle - margin // 2 - padding
        total = "--" if counter.baseline is None else sum(counter.confirmed.values())
        self.text(draw, "현재 재고", (left, bottom_y + unit(20)), size=unit(22), bold=True)
        badge_x = left_end - unit(100)
        draw.rounded_rectangle((badge_x, bottom_y + unit(12), left_end, bottom_y + unit(49)),
                               unit(12), fill="#e8f3ff")
        self.text(draw, f"총 {total}개", (badge_x + unit(12), bottom_y + unit(20)),
                  self.accent, unit(21), True)
        if counter.scanning or counter.baseline is None:
            status = "집계 중 · 이전 확정 수량 표시" if counter.scanning else "아직 집계된 재고 없음"
            self.text(draw, status, (left, bottom_y + unit(57)), self.muted, unit(15))
        table_width = left_end - left
        columns = (left, left + int(table_width * 0.65), left + int(table_width * 0.90))
        draw.rounded_rectangle((left - unit(8), bottom_y + unit(84),
                                left_end + unit(8), bottom_y + unit(112)),
                               unit(8), fill="#f9fafb")
        for label, x in zip(("과자", "현재", "증감"), columns):
            self.text(draw, label, (x, bottom_y + unit(90)), self.muted, unit(14))
        for index, name in enumerate(self.class_names):
            y = bottom_y + unit(126 + index * 34)
            self.text(draw, SNACK_NAMES.get(name, name), (columns[0], y), size=unit(19))
            values = ("--",) * 2 if counter.baseline is None else (
                counter.confirmed.get(name, 0),
                f"{counter.changes.get(name, 0):+d}")
            difference = counter.changes.get(name, 0)
            delta_color = self.accent if difference > 0 else "#d93649" if difference < 0 else self.muted
            for value, x, color in zip(values, columns[1:],
                                       (self.text_color, delta_color)):
                self.text(draw, value, (x, y), color, unit(20), True)
            if index < len(self.class_names) - 1:
                draw.line((left, y + unit(26), left_end, y + unit(26)), fill="#f2f4f6")

        right, right_end = middle + margin // 2 + padding, width - margin - padding
        log_size = unit(16)
        self.text(draw, "재고 변화 기록", (right, bottom_y + unit(20)), size=unit(22), bold=True)
        draw.line((right, bottom_y + unit(84), right_end, bottom_y + unit(84)), fill="#f2f4f6")
        lines = [(line, color) for message, color in self.logs
                 for line in wrap_log(message, right_end - right, log_size)]
        line_height = unit(26)
        limit = max(1, (height - margin - bottom_y - unit(112)) // line_height)
        shown = lines[-limit:]
        for index, (message, color) in enumerate(shown or [("아직 기록 없음", self.muted)]):
            self.text(draw, message, (right, bottom_y + unit(100) + index * line_height),
                      color, log_size)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)
