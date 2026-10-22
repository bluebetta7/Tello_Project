import threading
import unicodedata
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch


# 1위 과자와 이 값 이상 닮았으면 그 과자로 정하고, 아니면 억지로 맞히지 않고 "미확인"로 둔다.
MATCH_THRESHOLD = 0.70
# 1위가 2위보다 이만큼 더 닮아야 1위로 정한다. 0이면 1·2위가 비슷해도 더 닮은 쪽으로 표시한다.
# (0.03으로 두면 등록 안 된 과자를 다른 과자로 착각하는 경우가 46% → 12%로 줄지만,
#  맛만 다른 과자처럼 1·2위가 비슷한 봉지가 "미확인"로 자주 남는다.)
MATCH_MARGIN = 0.0
# 어느 등록 사진과도 이보다 덜 닮은 박스는 과자 봉지가 아닌 것(벽·옷걸이 등)으로 보고 버린다.
# (녹화 영상 기준: 배경 박스 0.13~0.35, 화면 끝에 잘린 실제 봉지도 0.66 이상)
NOT_SNACK_THRESHOLD = 0.6
# 같은 자리에서 이만큼 연속으로 잡힌 박스만 내보낸다. 한두 프레임만 나타나는 잘못된 박스를 거른다.
MIN_SEEN_FRAMES = 3
# 등록 사진과 충분히 닮지 않은 봉지의 이름. 화면에도 이 이름 그대로 나온다.
UNKNOWN = "미확인"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def clean_name(name):
    # 맥의 폴더 이름은 한글 자모가 풀려(NFD) 저장되기도 해서, 비교·화면 표시를 위해 하나로 맞춘다.
    return unicodedata.normalize("NFC", name)


def box_of(detection):
    return detection["x1"], detection["y1"], detection["x2"], detection["y2"]


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def load_dinov2():
    # 한 번 내려받은 코드·가중치(~/.cache/torch/hub)를 쓰므로 실행 중에는 인터넷이 필요 없다.
    local = Path(torch.hub.get_dir()) / "facebookresearch_dinov2_main"
    if local.is_dir():
        return torch.hub.load(str(local), "dinov2_vits14", source="local", verbose=False)
    return torch.hub.load("facebookresearch/dinov2", "dinov2_vits14", trust_repo=True, verbose=False)


class SnackRecognizer:
    """위치 찾기 YOLO가 찾은 봉지가 어떤 과자인지 등록 사진과 대조해 알아낸다.

    - 등록 사진: gallery/<과자 이름>/*.jpg. 폴더 이름이 곧 과자 이름(화면에 그대로 표시)이다.
      시작할 때 읽고, register()로 실행 중에도 추가한다.
    - 대조: 봉지 부분을 잘라 DINOv2 특징을 뽑고 가장 닮은 등록 사진의 과자로 정한다.
    - 같은 봉지는 최근 몇 번의 대조 결과로 다수결해 이름을 정한다. 이름이 정해진 뒤에도 가끔 다시 대조해
      잘못 정해진 이름이나 "미확인"를 바로잡는다. (미확인·결과가 엇갈릴 때·박스가 크게 바뀔 때는 자주)
    - 박스가 겹치면 겹친 부분을 지우고 대조해, 앞 봉지로 잘못 판정하는 것을 줄인다.
    - 잘못된 박스 거르기: 몇 프레임 연속으로 잡혀야 내보내고, 어느 과자와도 닮지 않은 박스는 버린다.
    - 깜빡임 줄이기: 보이던 봉지를 YOLO가 한두 프레임 놓쳐도 잠깐은 그대로 보여주고,
      한 번 정해진 이름은 다른 과자 이름이 더 많이 나올 때만 바꾼다. (미확인 결과로는 안 지움)
    """

    def __init__(self, gallery_dir, threshold=MATCH_THRESHOLD, margin=MATCH_MARGIN,
                 not_snack=NOT_SNACK_THRESHOLD, input_size=224,
                 vote_frames=5, settle_votes=3, recheck_frames=15, unsure_frames=2, moved_iou=0.7,
                 keep_frames=5, hold_frames=5, match_iou=0.5, min_box=16, min_seen=MIN_SEEN_FRAMES):
        self.gallery_dir = Path(gallery_dir)
        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        self.threshold = threshold
        self.margin = margin
        self.not_snack = not_snack
        self.input_size = input_size    # 대조 사진 크기. 168로 줄이면 약 2배 빠르고 정확도는 조금 낮아진다.
        self.vote_frames = vote_frames  # 같은 봉지의 최근 대조 결과를 이만큼 모아 다수결한다.
        self.settle_votes = settle_votes  # 처음 보는 봉지는 이만큼 대조할 때까지 매 프레임 대조한다.
        self.recheck_frames = recheck_frames  # 이름이 정해진 봉지는 이 프레임마다 다시 대조한다. (약 0.6초)
        self.unsure_frames = unsure_frames    # 미확인이거나 결과가 엇갈리는 봉지는 이 프레임마다 다시 대조한다.
        self.moved_iou = moved_iou      # 마지막 대조 때 박스와 이보다 덜 겹치면(가까워짐 등) 바로 다시 대조한다.
        self.keep_frames = keep_frames  # 탐지가 잠깐 끊겨도 이만큼은 같은 봉지로 기억한다.
        self.hold_frames = hold_frames  # 보이던 봉지를 YOLO가 놓쳐도 이만큼은 마지막 박스·이름으로 계속 보여준다.
        self.match_iou = match_iou      # 이전 프레임 박스와 이만큼 겹치면 같은 봉지로 본다.
        self.min_box = min_box          # 이보다 작은 박스(픽셀)는 대조하지 않는다.
        self.min_seen = min_seen        # 이만큼 연속으로 잡힌 박스만 내보낸다.

        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = load_dinov2().to(self.device).eval()

        self.lock = threading.Lock()    # 인식 스레드(recognize)와 등록(register)이 함께 쓴다.
        self.model_lock = threading.Lock()  # 두 스레드가 모델을 동시에 돌리지 않게 한다.
        self.generation = 0             # 등록 사진이 바뀔 때마다 올린다.
        self.names = []                 # 등록 사진마다 과자 이름
        self.vectors = np.zeros((0, 384), dtype=np.float32)
        self.tracks = []                # 이전 프레임까지 본 봉지들
        self.revote = False             # 새 사진이 등록됐으니 보이는 봉지를 모두 다시 대조하라는 표시
        self.frame_index = 0
        self.load_gallery()

    # ---------- 등록 사진 ----------

    def load_gallery(self):
        names, crops = [], []
        for folder in sorted(path for path in self.gallery_dir.iterdir() if path.is_dir()):
            for path in sorted(folder.iterdir()):
                # 한글 폴더 이름(직접 입력한 과자)도 읽을 수 있게 바이트로 읽어 디코딩한다.
                image = (cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
                         if path.suffix.lower() in IMAGE_EXTENSIONS else None)
                if image is not None:
                    names.append(clean_name(folder.name))
                    crops.append(image)
        vectors = self.embed(crops)
        with self.lock:
            self.names, self.vectors = names, vectors
            self.tracks = []
            self.generation += 1

    def register(self, frame, box, name):
        """frame의 box 부분을 gallery/<name>/에 저장하고 바로 대조 대상에 추가한다. 저장한 경로를 돌려준다."""
        x1, y1, x2, y2 = (int(value) for value in box)
        crop = frame[max(0, y1):y2, max(0, x1):x2]
        if crop.shape[0] < self.min_box or crop.shape[1] < self.min_box:
            raise ValueError("등록할 영역이 너무 작습니다.")
        name = clean_name(name)
        folder = self.gallery_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        ok, encoded = cv2.imencode(".jpg", crop)   # 한글 폴더 이름에도 저장되도록 바이트로 쓴다.
        if not ok:
            raise ValueError("사진을 저장하지 못했습니다.")
        path.write_bytes(encoded.tobytes())
        vector = self.embed([crop])
        with self.lock:
            self.names.append(name)
            self.vectors = np.vstack([self.vectors, vector])
            # 새 사진이 들어왔으니 지금 보이는 봉지도 다시 대조한다. 봉지 기억(연속으로 잡힌 횟수)은 그대로 둬서
            # 등록 직후 박스가 잠깐 사라지지 않게 한다. 대조 결과는 인식 스레드가 다음 프레임에 지운다.
            self.revote = True
            self.generation += 1
        return path

    @property
    def registered(self):
        # 과자 이름별 등록 사진 수
        with self.lock:
            return Counter(self.names)

    # ---------- 대조 ----------

    def prepare(self, crop):
        # 비율을 유지한 채 회색 여백으로 정사각형을 만든 뒤 모델 입력 형식으로 바꾼다.
        h, w = crop.shape[:2]
        side = max(h, w)
        square = np.full((side, side, 3), 127, np.uint8)
        top, left = (side - h) // 2, (side - w) // 2
        square[top:top + h, left:left + w] = crop
        square = cv2.resize(square, (self.input_size, self.input_size))
        rgb = square[:, :, ::-1].astype(np.float32) / 255.0
        return ((rgb - MEAN) / STD).transpose(2, 0, 1)

    @torch.no_grad()
    def embed(self, crops):
        if not crops:
            return np.zeros((0, 384), dtype=np.float32)
        batch = torch.from_numpy(np.stack([self.prepare(crop) for crop in crops]))
        with self.model_lock:
            vectors = torch.nn.functional.normalize(self.model(batch.to(self.device)), dim=1)
            return vectors.cpu().numpy()

    def best_match(self, vector, names, vectors):
        # 과자마다 가장 닮은 등록 사진의 유사도를 구해 1·2위를 비교한다.
        if not names:
            return UNKNOWN, 0.0
        similarity = vectors @ vector
        best = {}
        for name, score in zip(names, similarity):
            best[name] = max(best.get(name, -1.0), float(score))
        ranked = sorted(best.values(), reverse=True)
        name = max(best, key=best.get)
        score, second = ranked[0], (ranked[1] if len(ranked) > 1 else 0.0)
        if score < self.threshold or score - second < self.margin:
            return UNKNOWN, score
        return name, score

    def masked_crop(self, frame, box, others):
        # 옆 봉지와 일부 겹친 부분은 회색으로 지운다. (겹친 봉지의 모습이 섞이지 않게)
        # 이 박스의 절반 이상을 덮는 박스는 옆 봉지가 아니라 이 봉지를 감싼 큰 박스(잘못된 탐지 등)이므로
        # 지우지 않는다. 지우면 봉지 전체가 회색이 되어 대조할 수 없다.
        x1, y1, x2, y2 = box
        crop = frame[y1:y2, x1:x2].copy()
        area = (x2 - x1) * (y2 - y1)
        for ox1, oy1, ox2, oy2 in others:
            ix1, iy1, ix2, iy2 = max(x1, ox1), max(y1, oy1), min(x2, ox2), min(y2, oy2)
            if ix1 < ix2 and iy1 < iy2 and (ix2 - ix1) * (iy2 - iy1) * 2 < area:
                crop[iy1 - y1:iy2 - y1, ix1 - x1:ix2 - x1] = 127
        return crop

    def decide(self, track):
        # 최근 대조 결과로 이름을 정한다. 이미 정해진 이름은 다른 과자 이름이 더 많이 나올 때만 바꾼다.
        votes = track["votes"]
        known = Counter(name for name, _ in votes if name != UNKNOWN)
        current = track["label"]
        if current != UNKNOWN and known[current] and known[current] >= max(known.values()):
            name = current
        elif known and sum(known.values()) * 2 >= len(votes):
            name = known.most_common(1)[0][0]
        else:
            return UNKNOWN, max((score for _, score in votes), default=0.0)
        return name, max(score for vote, score in votes if vote == name)

    def needs_check(self, track, box):
        votes = track["votes"]
        if len(votes) < self.settle_votes or iou(box, track["checked_box"]) < self.moved_iou:
            return True
        # 미확인이거나 마지막 결과가 지금 이름과 다르면 자주, 이름이 안정되면 가끔 다시 대조한다.
        unsure = track["label"] == UNKNOWN or votes[-1][0] != track["label"]
        interval = self.unsure_frames if unsure else self.recheck_frames
        return self.frame_index - track["checked_at"] >= interval

    def recognize(self, frame, detections):
        """매 프레임 호출한다. 각 탐지의 "class_name"을 대조 결과로 바꾸고 "match_score"를 붙인다.

        잘못된 것으로 보이는 박스(연속으로 잡히지 않음, 과자 봉지와 닮지 않음)는 빼고 남은 탐지 목록을 돌려준다.
        등록 사진이 하나도 없으면 대조하지 않고 YOLO가 붙인 이름을 그대로 둔다. (처음 쓰기 시작할 때)
        """
        height, width = frame.shape[:2]
        boxes = []
        for detection in detections:
            x1, y1, x2, y2 = box_of(detection)
            boxes.append((max(0, x1), max(0, y1), min(width, x2), min(height, y2)))

        with self.lock:
            names, vectors, previous = list(self.names), self.vectors, self.tracks
            generation, revote, self.revote = self.generation, self.revote, False
        if revote:
            for track in previous:
                track["votes"].clear()

        # 1. 이전 프레임의 봉지와 짝짓기 (가장 많이 겹치는 것끼리)
        pairs = sorted(((iou(box, track["box"]), index, t)
                        for index, box in enumerate(boxes) for t, track in enumerate(previous)),
                       reverse=True)
        matched, used = {}, set()
        for overlap, index, t in pairs:
            if overlap < self.match_iou:
                break
            if index not in matched and t not in used:
                matched[index] = previous[t]
                used.add(t)

        self.frame_index += 1
        tracks = []
        for index, box in enumerate(boxes):
            track = matched.get(index) or {"votes": deque(maxlen=self.vote_frames), "label": UNKNOWN,
                                           "checked_at": 0, "checked_box": box, "seen": 0}
            track.update(box=box, missing=0, seen=track["seen"] + 1)
            tracks.append(track)

        # 2. 대조가 필요한 봉지만 대조한다. (새 봉지, 미확인, 결과가 엇갈림, 박스가 크게 바뀜, 오래됨)
        if names:
            todo = [index for index, track in enumerate(tracks)
                    if self.needs_check(track, boxes[index])
                    and boxes[index][2] - boxes[index][0] >= self.min_box
                    and boxes[index][3] - boxes[index][1] >= self.min_box]
            crops = [self.masked_crop(frame, boxes[index], [b for j, b in enumerate(boxes) if j != index])
                     for index in todo]
            for index, vector in zip(todo, self.embed(crops)):
                track = tracks[index]
                track["votes"].append(self.best_match(vector, names, vectors))
                track["checked_at"], track["checked_box"] = self.frame_index, boxes[index]

        # 3. 다수결로 이름 정하기. "미확인"보다 실제 과자 이름을 우선하되, 과반이 모르면 미확인.
        #    아직 연속으로 덜 잡혔거나, 어느 과자와도 닮지 않았으면 결과에서 뺀다.
        snacks = []
        for detection, track in zip(detections, tracks):
            track["shown"] = False
            if track["seen"] < self.min_seen:
                continue
            if not names:
                snacks.append(detection)
                continue
            name, score = self.decide(track)
            track["label"] = name
            detection["class_name"] = name
            detection["match_score"] = score
            if name != UNKNOWN or score >= self.not_snack:
                snacks.append(detection)
                track["shown"], track["shown_as"] = True, dict(detection)

        # 4. 이번에 안 보인 봉지도 keep_frames 동안은 기억해 둔다. (탐지가 잠깐 끊기는 경우)
        #    직전까지 화면에 보이던 봉지는 hold_frames 동안 마지막 박스·이름으로 계속 보여준다.
        for t, track in enumerate(previous):
            if t not in used and track["missing"] < self.keep_frames:
                track["missing"] += 1
                tracks.append(track)
                if track.get("shown_as") and track["missing"] <= self.hold_frames:
                    snacks.append(dict(track["shown_as"], held=True))
        with self.lock:
            if self.generation == generation:  # 대조 중에 새 사진이 등록됐으면 다음 프레임에 다시 대조한다.
                self.tracks = tracks
        return snacks
