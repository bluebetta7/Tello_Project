import sys
import traceback

from pynput import keyboard
from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gui.detection_worker import DetectionWorker
from gui.register_dialog import RegisterDialog
from inventory.inventory_screen import SNACK_NAMES
from inventory.shelf_inventory import ShelfView
from location.zone_marker import SHELF_MARKERS, shelf_name


TEXT, MUTED, ACCENT, RED = "#191f28", "#6b7684", "#3182f6", "#d93649"
STOCK_PAGE_SIZE = 5

STYLE = f"""
QMainWindow {{ background: #f2f4f6; }}
QWidget {{ font-family: "Apple SD Gothic Neo"; color: {TEXT}; }}
QFrame#card {{ background: #ffffff; border-radius: 16px; }}
QLabel#title {{ font-size: 18px; font-weight: bold; }}
QLabel#muted {{ font-size: 13px; color: {MUTED}; }}
QLabel#mode {{ font-size: 17px; font-weight: bold; }}
QLabel#badge {{ font-size: 16px; font-weight: bold; color: {ACCENT};
               background: #e8f3ff; border-radius: 10px; padding: 4px 10px; }}
QPushButton {{ font-size: 15px; font-weight: bold; padding: 10px; border-radius: 10px;
              background: #f2f4f6; border: none; }}
QPushButton:hover {{ background: #e5e8eb; }}
QPushButton:disabled {{ color: #b0b8c1; background: #f9fafb; }}
QPushButton#tab {{ font-size: 14px; padding: 6px; color: {MUTED}; }}
QPushButton#tab:checked {{ background: {ACCENT}; color: white; }}
QPushButton#pager {{ font-size: 13px; padding: 6px 14px; }}
QTableWidget, QListWidget {{ border: none; font-size: 15px; background: #ffffff; }}
QHeaderView::section {{ background: #f9fafb; color: {MUTED}; border: none; padding: 6px;
                        font-size: 13px; }}
"""

# Qt 키 → KeyboardFlight가 쓰는 키 값 (pynput과 같은 값을 넘겨 기존 키 처리를 그대로 쓴다)
SPECIAL_KEYS = {
    Qt.Key.Key_Up.value: keyboard.Key.up, Qt.Key.Key_Down.value: keyboard.Key.down,
    Qt.Key.Key_Left.value: keyboard.Key.left, Qt.Key.Key_Right.value: keyboard.Key.right,
    Qt.Key.Key_Escape.value: keyboard.Key.esc,
}

# macOS 키보드의 물리 키 번호 → 키 값. 한글 입력 상태에서는 Qt의 key()가 W 대신 'ㅈ'처럼
# 입력될 글자로 바뀌므로, 이때는 입력기와 관계없는 물리 키 번호로 판단한다.
MAC_KEYS = {
    0x0D: "w", 0x00: "a", 0x01: "s", 0x02: "d", 0x11: "t", 0x05: "g", 0x0C: "q", 0x2E: "m",
    0x09: "v", 0x12: "1", 0x13: "2", 0x14: "3", 0x15: "4", 0x17: "5", 0x16: "6", 0x1A: "7",
    0x1C: "8", 0x19: "9", 0x1D: "0", 0x35: keyboard.Key.esc, 0x7E: keyboard.Key.up,
    0x7D: keyboard.Key.down, 0x7B: keyboard.Key.left, 0x7C: keyboard.Key.right,
}

AUTO_STATES = {"find": "마커 찾는 중", "align": "높이 맞추는 중", "count": "집계 중", "move": "이동 중"}


def flight_key(event):
    key = event.key()
    if key in SPECIAL_KEYS:
        return SPECIAL_KEYS[key]
    if Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value or Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
        return chr(key).lower()
    # 한글 입력 상태라 key()가 'ㅈ' 같은 글자로 온 경우에는 물리 키 번호로 판단한다.
    if sys.platform == "darwin":
        return MAC_KEYS.get(event.nativeVirtualKey())
    return None


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    return frame, layout


def label(text="", name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


class VideoView(QWidget):
    """카메라 영상을 원본 비율 그대로 영역 가운데에 그린다."""

    def __init__(self):
        super().__init__()
        self.image = None
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, frame):
        height, width = frame.shape[:2]
        # numpy 배열이 바뀌어도 화면이 깨지지 않도록 복사본을 보관한다.
        self.image = QImage(frame.data, width, height, frame.strides[0],
                            QImage.Format.Format_BGR888).copy()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if self.image is None:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "영상 대기 중")
            return
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (self.width() - size.width()) // 2, (self.height() - size.height()) // 2
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRect(x, y, size.width(), size.height()), self.image)


class MainWindow(QMainWindow):
    """왼쪽 카메라 영상 / 오른쪽 상태·자동 스캔 버튼·층별 재고표·변화 기록.

    - 인식: DetectionWorker가 별도 스레드에서 과자·마커를 찾아 on_detected()로 넘긴다.
      on_detected()가 집계(수동·자동 스캔)와 패널 갱신을 한다.
    - 영상: show_frame()이 새 카메라 프레임마다 가장 최근 인식 결과를 그려 바로 표시한다.
      추론을 기다리지 않으므로 영상은 카메라 속도로 매끄럽게 나온다.
    버튼과 키 입력은 KeyboardFlight에 요청만 넣고, on_detected()가 다음 인식 결과 때 꺼내 처리한다.
    """

    def __init__(self, controller, camera, keys, detector, zones, inventory, screen, manual, auto,
                 recognizer):
        super().__init__()
        self.controller, self.camera, self.keys = controller, camera, keys
        self.detector, self.zones, self.screen = detector, zones, screen
        self.inventory, self.manual, self.auto = inventory, manual, auto
        self.recognizer = recognizer  # 물품 등록 창이 등록 사진을 저장할 때 쓴다.
        self.capture = (None, [])     # 물품 등록용: 가장 최근 인식에 쓴 프레임과 봉지 박스
        self.table_key = None
        self.stock_page = 0
        self.stock_revision = None
        self.selected_shelf = min(SHELF_MARKERS)  # 재고표에 보여줄 층
        self.followed_shelf = None                # 마지막으로 따라간 스캔 대상 층
        self.log_version = None
        self.failed = False
        self.view = None        # 가장 최근 인식 결과 (ShelfView)
        self.last_raw = None    # 마지막으로 표시한 카메라 원본 프레임

        self.setWindowTitle("Tello 재고 스캔")
        self.setStyleSheet(STYLE)
        self.resize(1280, 800)
        self.build()
        # 버튼·표가 키 입력을 가져가지 않도록 포커스는 창에만 둔다.
        for widget in self.findChildren(QWidget):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.worker = DetectionWorker(camera, detector, zones, recognizer)
        self.worker.detected.connect(self.on_detected)
        self.worker.failed.connect(self.on_worker_failed)
        self.worker.start()
        # 새 카메라 프레임이 왔는지 5ms마다 확인한다. (텔로 영상은 초당 30프레임)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.show_frame)
        self.timer.start(5)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(1000)
        self.update_status()

    # ----- 화면 구성 -----
    def build(self):
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        self.setCentralWidget(root)

        video_card, video_layout = card()
        video_layout.setContentsMargins(10, 10, 10, 10)
        self.video = VideoView()
        video_layout.addWidget(self.video)
        layout.addWidget(video_card, stretch=1)

        side = QVBoxLayout()
        side.setSpacing(12)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(380)
        side.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(side_widget)

        # 상태: 현재 모드, 배터리·고도·비행 상태
        status_card, status_layout = card()
        self.mode_label = label("수동 조종", "mode")
        self.status_label = label("드론 상태 확인 중", "muted")
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.mode_label)
        mode_row.addStretch()
        self.register_button = QPushButton("물품 등록")
        self.register_button.setObjectName("pager")
        self.register_button.clicked.connect(self.open_register)
        mode_row.addWidget(self.register_button)
        status_layout.addLayout(mode_row)
        status_layout.addWidget(self.status_label)
        self.auto_button = QPushButton("자동 스캔 시작")
        self.auto_button.clicked.connect(self.keys.request_auto)
        status_layout.addWidget(self.auto_button)
        side.addWidget(status_card)

        # 현재 재고: 층 버튼으로 고른 층의 확정 수량과 증감 (스캔이 시작되면 그 층으로 자동 전환)
        stock_card, stock_layout = card()
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.shelf_buttons = {}
        for shelf_id in sorted(SHELF_MARKERS):
            button = QPushButton(shelf_name(shelf_id))
            button.setObjectName("tab")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, shelf_id=shelf_id: self.select_shelf(shelf_id))
            tabs.addWidget(button)
            self.shelf_buttons[shelf_id] = button
        stock_layout.addLayout(tabs)
        header = QHBoxLayout()
        self.stock_title = label("현재 재고", "title")
        self.total_badge = label("총 --개", "badge")
        header.addWidget(self.stock_title)
        header.addStretch()
        header.addWidget(self.total_badge)
        stock_layout.addLayout(header)
        self.stock_status = label("", "muted")
        stock_layout.addWidget(self.stock_status)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["과자", "현재", "증감"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.verticalHeader().setDefaultSectionSize(34)
        header_view = self.table.horizontalHeader()
        header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # 모델의 클래스 수와 무관하게 다섯 줄 높이로 고정한다.
        self.table.setFixedHeight(36 + STOCK_PAGE_SIZE * 34)
        stock_layout.addWidget(self.table)
        pager = QHBoxLayout()
        self.previous_page = QPushButton("이전")
        self.next_page = QPushButton("다음")
        self.page_label = label("1 / 1 페이지", "muted")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for button, offset in ((self.previous_page, -1), (self.next_page, 1)):
            button.setObjectName("pager")
            button.clicked.connect(lambda _=False, offset=offset: self.change_stock_page(offset))
        pager.addWidget(self.previous_page)
        pager.addWidget(self.page_label, stretch=1)
        pager.addWidget(self.next_page)
        stock_layout.addLayout(pager)
        side.addWidget(stock_card)

        # 재고 변화 기록
        log_card, log_layout = card()
        log_layout.addWidget(label("재고 변화 기록", "title"))
        self.log_list = QListWidget()
        self.log_list.setWordWrap(True)
        self.log_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        log_layout.addWidget(self.log_list)
        side.addWidget(log_card, stretch=1)

    # ----- 반복 처리 -----
    def on_detected(self, result):
        # 인식 결과가 올 때마다 집계한다. (main.py의 예전 while 반복 한 바퀴)
        if self.failed or not self.timer.isActive():
            return
        try:
            if self.keys.consume_auto_request():
                self.auto.toggle()
            # 1. 인식 결과를 매대별로 나눈다.
            height, detections, markers, frame = result
            self.capture = (frame, [(d["x1"], d["y1"], d["x2"], d["y2"]) for d in detections])
            view = ShelfView(detections, markers, height)
            # 2. 집계: 스캔 요청(자동 스캔 중에는 멈춤)과 자동 스캔
            self.manual.update(self.keys.consume_scan_request(), view, blocked=self.auto.active)
            self.auto.update(view)
            self.view = view
            self.update_panels(view)
        except Exception:
            self.fail(traceback.format_exc())

    def show_frame(self):
        # 3. 화면 출력: 새 카메라 프레임에 가장 최근 인식 결과를 그려 표시한다.
        try:
            if self.keys.exit_requested:
                self.close()
                return
            raw = self.camera.raw_frame()
            if raw is self.last_raw or self.view is None:
                return
            self.last_raw = raw
            target = self.auto.shelf_id if self.auto.active else self.manual.target
            self.video.set_frame(self.screen.render(self.camera.to_bgr(raw), self.view, target))
        except Exception:
            self.fail(traceback.format_exc())

    def on_worker_failed(self, message):
        self.fail(message)

    def fail(self, message):
        # 오류가 나면 창을 닫아 main.py의 정리(착륙·연결 해제)로 넘어간다.
        if not self.failed:
            self.failed = True
            print(message, end="")
            print("[SAFETY] ERROR_EXIT")
            self.close()

    def update_panels(self, view):
        auto_active, scanning = self.auto.active, self.manual.scanning
        # 새 층의 스캔이 시작되면 재고표도 그 층으로 넘긴다. 그 뒤에는 층 버튼으로 자유롭게 본다.
        if self.inventory.display_shelf != self.followed_shelf:
            self.followed_shelf = self.inventory.display_shelf
            if self.followed_shelf is not None:
                self.selected_shelf = self.followed_shelf
        self.auto_button.setText("자동 스캔 종료" if auto_active else "자동 스캔 시작")

        if auto_active:
            mode = f"자동 스캔 · {shelf_name(self.auto.target_shelf)} {AUTO_STATES.get(self.auto.state, '')}"
        elif scanning:
            mode = f"수동 스캔 · {shelf_name(self.manual.shelf_id)} 집계 중"
        else:
            mode = "수동 조종"
        self.mode_label.setText(mode)

        self.update_table()
        self.update_logs()

    def open_register(self):
        # 등록 창이 떠 있는 동안 인식·영상은 계속 돌고, 드론은 키 입력이 끊겨 제자리 비행한다.
        RegisterDialog(self, lambda: self.capture, self.recognizer).exec()

    def select_shelf(self, shelf_id):
        self.selected_shelf = shelf_id
        self.update_table()

    def change_stock_page(self, offset):
        self.stock_page += offset
        self.update_table()

    def update_table(self):
        shelf = self.selected_shelf
        for shelf_id, button in self.shelf_buttons.items():
            button.setChecked(shelf_id == shelf)
        counter = self.inventory.counter_for(shelf)
        # 새 집계나 다른 층을 열면 첫 페이지에서 시작한다.
        revision = (shelf, counter.scan_number)
        if revision != self.stock_revision:
            self.stock_revision = revision
            self.stock_page = 0
        names = self.inventory.dashboard.visible_names(counter)
        page_count = max(1, (len(names) + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE)
        self.stock_page = min(max(0, self.stock_page), page_count - 1)
        key = (shelf, self.stock_page, counter.scan_number, counter.scanning, counter.baseline is None,
               tuple(sorted(counter.confirmed.items())), tuple(sorted(counter.changes.items())))
        if key == self.table_key:
            return
        self.table_key = key

        empty = counter.baseline is None
        self.stock_title.setText(f"현재 재고 · {shelf_name(shelf)}")
        self.total_badge.setText(f"총 {'--' if empty else sum(counter.confirmed.values())}개")
        if counter.scanning:
            self.stock_status.setText("집계 중..." if empty else "집계 중 · 이전 확정 수량 표시")
        elif empty:
            self.stock_status.setText(f"{shelf_name(shelf)}은 아직 집계된 재고 없음")
        elif not names:
            self.stock_status.setText("집계된 과자 없음")
        self.stock_status.setVisible(counter.scanning or empty or not names)
        self.page_label.setText(f"{self.stock_page + 1} / {page_count} 페이지")
        self.previous_page.setEnabled(self.stock_page > 0)
        self.next_page.setEnabled(self.stock_page + 1 < page_count)

        bold = QFont()
        bold.setBold(True)
        start = self.stock_page * STOCK_PAGE_SIZE
        page_names = names[start:start + STOCK_PAGE_SIZE]
        self.table.setRowCount(len(page_names))
        for row, name in enumerate(page_names):
            self.table.setItem(row, 0, QTableWidgetItem(SNACK_NAMES.get(name, name)))
            difference = counter.changes.get(name, 0)
            values = (("--", MUTED), ("--", MUTED)) if empty else (
                (str(counter.confirmed.get(name, 0)), TEXT),
                (f"{difference:+d}", ACCENT if difference > 0 else RED if difference < 0 else MUTED))
            for column, (value, color) in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setFont(bold)
                item.setForeground(QColor(color))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)

    def update_logs(self):
        dashboard = self.inventory.dashboard
        if dashboard.version == self.log_version:
            return
        self.log_version = dashboard.version
        self.log_list.clear()
        for message, color in dashboard.logs or [("아직 기록 없음", MUTED)]:
            item = QListWidgetItem(message)
            item.setForeground(QColor(color))
            self.log_list.addItem(item)
        self.log_list.scrollToBottom()

    def update_status(self):
        # 배터리·고도는 드론이 계속 보내는 상태 정보에서 읽으므로 명령을 보내지 않는다.
        tello = self.controller.tello
        try:
            battery, height = tello.get_battery(), tello.get_height()
        except Exception:
            self.status_label.setText("드론 상태 정보 없음")
            return
        flight = "비행 중" if self.controller.is_flying else "착륙 상태"
        self.status_label.setText(f"배터리 {battery}% · 고도 {height}cm · {flight}")
        self.status_label.setStyleSheet(f"color: {RED};" if battery <= 20 else "")

    # ----- 키 입력·창 이벤트 -----
    def keyPressEvent(self, event):
        key = flight_key(event)
        if key is None or event.isAutoRepeat():
            return super().keyPressEvent(event)
        self.keys.on_press(key)

    def keyReleaseEvent(self, event):
        key = flight_key(event)
        if key is None or event.isAutoRepeat():
            return super().keyReleaseEvent(event)
        self.keys.on_release(key)

    def changeEvent(self, event):
        # 다른 창으로 포커스가 넘어가면 키를 뗀 신호가 오지 않으므로 이동을 멈춘다.
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.keys.release_all()
        super().changeEvent(event)

    def closeEvent(self, event):
        # 창을 닫으면 반복을 멈춘다. 착륙·연결 해제는 main.py의 정리 단계에서 처리한다.
        self.timer.stop()
        self.status_timer.stop()
        self.worker.stop()  # 카메라를 끄기 전에 인식 스레드를 먼저 멈춘다.
        self.keys.release_all()
        event.accept()
