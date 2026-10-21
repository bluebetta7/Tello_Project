from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from inventory.inventory_screen import SNACK_NAMES
from inventory.snack_recognizer import UNKNOWN


BOX, SELECTED = QColor("#00c800"), QColor("#ff8c00")
DRAG_START = 8      # 이 픽셀보다 많이 끌면 클릭이 아니라 직접 그리기로 본다.
MIN_BOX = 16        # 직접 그린 박스의 최소 크기 (원본 픽셀)


def snack_choices():
    # 등록할 수 있는 과자 이름 (한글 이름 순). 예전 4종 모델의 대문자 이름과 "미확인"는 뺀다.
    names = [name for name in SNACK_NAMES if name.islower() and name != UNKNOWN]
    return sorted(names, key=lambda name: SNACK_NAMES[name])


class CaptureView(QWidget):
    """실시간 영상 또는 찍은 사진 위에 봉지 박스를 보여준다.

    찍은 사진(live=False)에서만 박스를 클릭해 고르거나, 끌어서 직접 그릴 수 있다.
    """

    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.frame = self.image = None
        self.live = True         # True: 실시간 영상 표시 중 (고르기 불가)
        self.boxes = []
        self.selected = None     # 고른 박스 (원본 좌표 x1, y1, x2, y2)
        self.drag_from = None
        self.drawing = None      # 끌고 있는 중인 박스 (원본 좌표)
        self.setMinimumSize(640, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_capture(self, frame, boxes, live):
        self.frame, self.boxes, self.live = frame, list(boxes), live
        height, width = frame.shape[:2]
        self.image = QImage(frame.data, width, height, frame.strides[0],
                            QImage.Format.Format_BGR888).copy()
        self.selected = self.drawing = None
        self.selection_changed.emit()
        self.update()

    def image_rect(self):
        # 사진을 원본 비율 그대로 가운데에 그릴 영역
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def to_image(self, point):
        area = self.image_rect()
        x = (point.x() - area.x()) * self.image.width() / area.width()
        y = (point.y() - area.y()) * self.image.height() / area.height()
        return (min(max(0, int(x)), self.image.width() - 1), min(max(0, int(y)), self.image.height() - 1))

    def to_screen(self, box):
        area = self.image_rect()
        scale = area.width() / self.image.width()
        x1, y1, x2, y2 = box
        return QRect(area.x() + int(x1 * scale), area.y() + int(y1 * scale),
                     int((x2 - x1) * scale), int((y2 - y1) * scale))

    def mousePressEvent(self, event):
        if self.image is not None and not self.live and event.button() == Qt.MouseButton.LeftButton:
            self.drag_from = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self.drag_from is None:
            return
        point = event.position().toPoint()
        if (point - self.drag_from).manhattanLength() >= DRAG_START:
            (ax, ay), (bx, by) = self.to_image(self.drag_from), self.to_image(point)
            self.drawing = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
            self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_from is None:
            return
        self.drag_from = None
        if self.drawing is not None:
            # 직접 그린 박스
            x1, y1, x2, y2 = self.drawing
            self.drawing = None
            if x2 - x1 >= MIN_BOX and y2 - y1 >= MIN_BOX:
                self.selected = (x1, y1, x2, y2)
        else:
            # 클릭: 그 자리의 박스 중 가장 작은 것을 고른다. (박스가 겹쳐 있을 때 안쪽 것)
            x, y = self.to_image(event.position().toPoint())
            hits = [box for box in self.boxes if box[0] <= x <= box[2] and box[1] <= y <= box[3]]
            self.selected = min(hits, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if hits else None
        self.selection_changed.emit()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if self.image is None:
            painter.setPen(QColor("#6b7684"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "영상 대기 중")
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.image_rect(), self.image)
        painter.setPen(QPen(BOX, 2))
        for box in self.boxes:
            if box != self.selected:
                painter.drawRect(self.to_screen(box))
        if self.selected is not None:
            painter.setPen(QPen(SELECTED, 4))
            painter.drawRect(self.to_screen(self.selected))
        if self.drawing is not None:
            painter.setPen(QPen(SELECTED, 2, Qt.PenStyle.DashLine))
            painter.drawRect(self.to_screen(self.drawing))


class RegisterDialog(QDialog):
    """물품 등록 창: 실시간 영상을 보다가 원하는 순간 촬영 → 봉지를 고르고 → 과자 이름을 골라 저장한다.

    capture(): (프레임, 봉지 박스 목록). 메인 창이 가장 최근 인식 결과를 넘겨준다.
    저장하면 SnackRecognizer.register()가 gallery/<과자 이름>/에 잘라 저장하고 바로 대조에 쓴다.
    """

    def __init__(self, parent, capture, recognizer):
        super().__init__(parent)
        self.capture, self.recognizer = capture, recognizer
        self.setWindowTitle("물품 등록")
        self.resize(1180, 680)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        self.view = CaptureView()
        self.view.selection_changed.connect(self.update_buttons)
        layout.addWidget(self.view, stretch=1)

        side = QVBoxLayout()
        side.setSpacing(10)
        title = QLabel("물품 등록")
        title.setObjectName("title")
        guide = QLabel("1. 실시간 화면을 보다가 촬영을 누릅니다.\n"
                       "2. 등록할 봉지의 박스를 클릭합니다.\n"
                       "   박스가 없으면 끌어서 직접 그립니다.\n"
                       "3. 과자 이름을 고르고 저장합니다.\n\n"
                       "각도·거리를 바꿔 2~3장 등록하면\n더 정확해집니다.")
        guide.setObjectName("muted")
        self.capture_button = QPushButton("촬영")
        self.capture_button.clicked.connect(self.toggle_capture)
        self.names = QComboBox()
        for name in snack_choices():
            self.names.addItem(SNACK_NAMES[name], name)
        self.names.currentIndexChanged.connect(self.update_count)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        self.save_button = QPushButton("저장")
        self.save_button.clicked.connect(self.save)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close)
        for widget in (title, guide, self.capture_button, QLabel("과자 이름"), self.names,
                       self.count_label, self.save_button, self.result_label):
            side.addWidget(widget)
        side.addStretch()
        side.addWidget(close_button)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(280)
        layout.addWidget(side_widget)

        # 촬영 전에는 실시간 영상을 계속 보여준다. (새 인식 결과가 오면 갱신)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.show_live)
        self.go_live()
        self.update_count()

    def show_live(self):
        frame, boxes = self.capture()
        if frame is not None and frame is not self.view.frame:
            self.view.set_capture(frame, boxes, live=True)

    def go_live(self):
        self.capture_button.setText("촬영")
        self.result_label.setText("원하는 순간에 촬영을 누르세요.")
        self.show_live()
        self.timer.start(30)

    def toggle_capture(self):
        # 실시간 화면이면 지금 장면을 멈춰 찍고, 찍은 사진이면 다시 실시간 화면으로 돌아간다.
        if not self.view.live:
            self.go_live()
            return
        frame, boxes = self.capture()
        if frame is None:
            self.result_label.setText("아직 카메라 영상이 없습니다.")
            return
        self.timer.stop()
        self.view.set_capture(frame, boxes, live=False)
        self.capture_button.setText("다시 촬영")
        self.result_label.setText(f"봉지 {len(boxes)}개를 찾았습니다." if boxes
                                  else "찾은 봉지가 없습니다. 끌어서 직접 그려 주세요.")

    def update_count(self):
        name = self.names.currentData()
        self.count_label.setText(f"등록된 사진 {self.recognizer.registered.get(name, 0)}장")

    def update_buttons(self):
        self.save_button.setEnabled(not self.view.live and self.view.selected is not None)

    def done(self, result):
        # 닫기 버튼·창 닫기·ESC 모두 여기를 거친다.
        self.timer.stop()
        super().done(result)

    def save(self):
        name, box = self.names.currentData(), self.view.selected
        if box is None:
            return
        try:
            self.recognizer.register(self.view.frame, box, name)
        except ValueError as error:
            self.result_label.setText(str(error))
            return
        self.result_label.setText(f"저장했습니다: {SNACK_NAMES[name]}")
        # 같은 봉지를 두 번 저장하지 않도록 선택을 푼다.
        self.view.selected = None
        self.view.update()
        self.update_buttons()
        self.update_count()
