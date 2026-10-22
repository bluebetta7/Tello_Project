import cv2
import numpy as np
from PIL import Image, ImageDraw

from inventory.inventory_screen import font


# 마커 번호 → 매대 정보. 마커는 각 선반 층의 오른쪽 아래에 붙인다.
#   name: 매대(층) 이름
#   next: 이 매대 집계가 끝난 뒤 이동할 방향과 다음으로 찾을 매대(마커 번호)
#         방향: up, down, left, right, forward, back
#   end:  True면 이 매대를 마지막으로 스캔을 끝낸다.
# 마커 번호와 층 번호를 같게 맞춘다 (1층 = 1번 마커). 숫자 키도 같은 번호를 누른다.
SHELF_MARKERS = {
    1: {"name": "1층", "next": {"direction": "up", "shelf": 2}},
    2: {"name": "2층", "next": {"direction": "up", "shelf": 3}},
    3: {"name": "3층", "end": True},
}

# 자동 스캔을 시작할 첫 매대. 사용자가 이 매대 앞까지 드론을 직접 옮긴 뒤 M 키를 누른다.
START_SHELF = 1

# 다음 매대를 찾을 때 한 번에 이동하는 거리(cm)와 최대 이동 횟수.
# 텔로의 거리 이동 명령은 20cm가 최소라 20cm 단위로 이동한다.
SEARCH_STEP_CM = 20
SEARCH_MAX_STEPS = 6   # 20cm x 6 = 최대 120cm 이동해도 못 찾으면 중단

# 자동 스캔에서 집계 전 높이 맞추기. 과자는 마커 위에 있으므로 마커가 화면 아래쪽에 와야
# 그 층 과자가 화면에 모두 담긴다. 마커 중심의 세로 위치(0 = 맨 위, 1 = 맨 아래)가
# 이 값보다 위에 있으면 천천히 상승하다가, 이 값에 닿는 순간 멈추고 집계한다.
MARKER_TARGET_POSITION = 0.70
ALIGN_SPEED = 15       # 높이 맞추기 상승 속도 (RC 명령값, 키보드 상승은 25)
ALIGN_TIMEOUT = 8.0    # 이 시간(초) 안에 목표 위치에 못 닿으면 멈추고 그 위치에서 집계한다.
ALIGN_LOST_SECONDS = 0.5  # 상승 중 마커가 이 시간 넘게 안 보이면 멈추고 다시 찾는다.

# 자동 스캔 중 좌우·거리 유지. 마커가 한 줄로 붙어 있어 좌우로 흘러가면 다음 마커를 놓치고,
# 거리가 변하면 한 층이 화면에 다 안 담기거나(가까움) 과자를 못 찾는다(멂).
MARKER_TARGET_X = 0.75       # 마커 중심의 가로 위치 목표 (0 = 왼쪽, 1 = 오른쪽). 마커가 선반 오른쪽에 있으므로
MARKER_X_TOLERANCE = 0.05    # 목표에서 이만큼(화면 폭의 5%)까지는 맞은 것으로 본다.
MARKER_SIZE_TOLERANCE = 0.12 # 처음 찾은 마커 크기에서 ±12%까지는 같은 거리로 본다.
STRAFE_SPEED = 12            # 좌우 맞추기 최대 속도 (RC 명령값)
DISTANCE_SPEED = 12          # 앞뒤 맞추기 최대 속도 (RC 명령값)

ZONE_NAMES = {marker_id: info["name"] for marker_id, info in SHELF_MARKERS.items()}


def shelf_name(shelf_id):
    return ZONE_NAMES.get(shelf_id, f"구역 {shelf_id}")

# 4x4 비트 사전은 칸이 커서 텔로처럼 해상도가 낮은 영상에서도 잘 인식된다.
MARKER_DICTIONARY = cv2.aruco.DICT_4X4_50


class ZoneMarkerDetector:
    def __init__(self, min_marker_size=30):
        dictionary = cv2.aruco.getPredefinedDictionary(MARKER_DICTIONARY)
        self.detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())

        # 너무 멀리 있는(작게 보이는) 마커는 잘못 읽힐 수 있어 무시한다. 단위: 픽셀(한 변 길이)
        self.min_marker_size = min_marker_size

    def detect(self, frame):
        # BGR 프레임에서 마커를 찾아 번호, 구역 이름, 중심, 크기를 돌려준다.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self.detector.detectMarkers(gray)

        markers = []
        if ids is None:
            return markers

        for marker_corners, marker_id in zip(corners, ids.flatten()):
            points = marker_corners.reshape(4, 2)
            size = float(np.mean(np.linalg.norm(points - np.roll(points, 1, axis=0), axis=1)))
            if size < self.min_marker_size:
                continue

            marker_id = int(marker_id)
            center = points.mean(axis=0)
            markers.append(
                {
                    "id": marker_id,
                    "zone": ZONE_NAMES.get(marker_id, f"미등록 구역 {marker_id}"),
                    "corners": points.astype(int),
                    "center": (int(center[0]), int(center[1])),
                    "size": size,
                }
            )

        # 안내 상자에 번호 순으로 보이도록 정렬한다.
        markers.sort(key=lambda marker: marker["id"])
        return markers

    @staticmethod
    def assign_zones(detections, markers):
        # 마커는 각 층의 오른쪽 아래에 붙어 있으므로, 과자는 자기보다 아래에 있는 마커 중
        # 가장 가까운 마커의 층에 속한다. 결과는 각 탐지 항목의 "zone_id"에 저장된다.
        for detection in detections:
            center_y = (detection["y1"] + detection["y2"]) / 2
            below = [marker for marker in markers if marker["center"][1] >= center_y]
            if below:
                detection["zone_id"] = min(below, key=lambda marker: marker["center"][1] - center_y)["id"]
            else:
                detection["zone_id"] = None
        return detections

    @staticmethod
    def draw(frame, markers, target_id=None, target_lines=None):
        # 마커 테두리는 OpenCV로, 한글 구역 이름은 PIL로 그린다.
        # 스캔 대상 마커는 주황색 굵은 테두리로, 나머지 마커는 파란색으로 구분한다.
        # target_lines: 스캔 대상 층에서 탐지 중인 과자 (한 줄에 하나, 비어 있으면 "-")
        marker_color, target_color = (255, 140, 0), (0, 140, 255)  # BGR 파랑, 주황
        for marker in markers:
            is_target = marker["id"] == target_id
            cv2.polylines(frame, [marker["corners"]], True,
                          target_color if is_target else marker_color, 5 if is_target else 3)

        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(image)
        label_font, info_font = font(20, True), font(18)

        for marker in markers:
            is_target = marker["id"] == target_id
            label = f"ID {marker['id']} · {marker['zone']}"
            x = int(marker["corners"][:, 0].min())
            y = max(0, int(marker["corners"][:, 1].min()) - 30)
            width = label_font.getlength(label)
            draw.rectangle((x, y, x + width + 12, y + 28),
                           fill=(255, 140, 0) if is_target else (0, 140, 255))
            draw.text((x + 6, y + 2), label, font=label_font, fill=(255, 255, 255))

        # 오른쪽 위 안내 상자: 스캔 중이면 대상 층의 과자, 아니면 화면에 보이는 마커 목록
        if target_id is not None:
            title = f"스캔: {shelf_name(target_id)} (ID {target_id})"
            lines = target_lines or ["-"]
        else:
            title = "인식된 마커"
            lines = [f"ID {marker['id']} · {marker['zone']}" for marker in markers] or ["-"]
        width = max([label_font.getlength(title)] + [info_font.getlength(line) for line in lines])
        right = image.width - 10
        left = right - width - 24
        draw.rectangle((left, 10, right, 54 + 26 * len(lines)), fill=(0, 0, 0))
        draw.text((left + 12, 16), title, font=label_font,
                  fill=(255, 140, 0) if target_id is not None else (255, 255, 255))
        for index, line in enumerate(lines):
            draw.text((left + 12, 50 + index * 26), line, font=info_font, fill=(230, 230, 230))

        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
