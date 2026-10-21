from collections import deque
from datetime import datetime
from functools import lru_cache

from PIL import ImageFont


SNACK_NAMES = {
    "Concho": "콘초", "HoneyButterChip": "허니버터칩",
    "BananaKick": "바나나킥", "Pocachip": "포카칩",
    "alsaewoochip": "알새우칩", "bananakick": "바나나킥",
    "beoljippizza": "벌집핏자", "cheetos_maecom": "치토스 매콤달콤한맛",
    "cheetos_smoke": "치토스 스모키바베큐맛", "chickenpop": "치킨팝",
    "churus": "츄러스", "conchip": "콘칩", "concho": "콘초",
    "ddangkonggangjeong": "땅콩강정", "gamjakkang": "감자깡",
    "gogumakkang": "고구마깡", "goowunyangpa": "구운양파",
    "honeybutterchip": "허니버터칩", "indianbab": "인디안밥",
    "jagabee": "자가비", "jagalchi": "자갈치", "jjolbyeong": "쫄병스낵",
    "jochungyugwa": "조청유과", "jollypong": "죠리퐁",
    "kanchuricon": "캔츄리콘", "kkobukchip_con": "꼬북칩 콘스프맛",
    "kkokkalcon_goso": "꼬깔콘 고소한맛", "kkokkalcon_gun": "꼬깔콘 군옥수수맛",
    "kkokkalcon_maecom": "꼬깔콘 매콤달콤한맛", "kkotgaerang": "꽃게랑",
    "kkulkkwabaegi": "꿀꽈배기", "matdongsan": "맛동산",
    "mooddukdduk": "무뚝뚝 감자칩", "nuneulgamja": "눈을감자",
    "ohgamja_gamja": "오감자", "ohgamja_yangnyeom": "오감자 양념바베큐맛",
    "ojingeoddangkong": "오징어땅콩", "ojingeojib": "오징어집", "osatsu": "오사쯔",
    "pokachip_onion": "포카칩 어니언", "pokachip_origin": "포카칩 오리지널",
    "postick": "포스틱", "potaetochip": "포테토칩", "ppusyeoppusyeo": "뿌셔뿌셔",
    "ppusyeoppusyeo_yangnyeomchicken": "뿌셔뿌셔 양념치킨맛",
    "saewookkang": "새우깡", "saewookkang_spicy": "매운새우깡", "shinzzang": "신짱",
    "ssalrobyul": "쌀로별", "ssun": "썬칩", "ssun_garlic": "썬칩 갈릭",
    "sumichip_onion": "수미칩 어니언", "swingchip": "스윙칩",
    "yangparing": "양파링", "zzanggu": "짱구",
    "unknown": "미확인",  # 등록 사진과 충분히 닮지 않은 봉지 (inventory/snack_recognizer.py)
}
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
                      key=lambda name: (SNACK_NAMES.get(name, name), name))

    def add_scan(self, counter, shelf=None):
        # 동일한 기록을 UI와 터미널에 사용하며 집계가 확정될 때만 추가한다.
        self.last_scan_at = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{self.last_scan_at}]" + (f" {shelf}" if shelf else "")
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
        self.version += 1
        return entries

    def add_note(self, message):
        # 자동 스캔 진행 상황 같은 안내 문구를 기록에 추가한다.
        self.logs.append((f"[{datetime.now().strftime('%H:%M:%S')}] {message}", self.muted))
        self.version += 1
