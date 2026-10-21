import cv2


class VideoStream:
    def __init__(self, tello):
        self.tello = tello
        self.frame_read = None
        self.window_open = False
        self.window_name = "Tello Control"

    def start(self):
        self.tello.streamon()
        self.frame_read = self.tello.get_frame_read()

    def raw_frame(self):
        # 변환 전 원본. djitellopy는 새 프레임마다 새 배열을 만들므로 `is`로 새 프레임인지 비교할 수 있다.
        return self.frame_read.frame

    @staticmethod
    def to_bgr(raw):
        # djitellopy는 RGB 순서로 프레임을 주므로 OpenCV·YOLO가 쓰는 BGR로 변환한다.
        return cv2.cvtColor(raw, cv2.COLOR_RGB2BGR)

    def get_frame(self):
        return self.to_bgr(self.raw_frame())

    def show(self, frame):
        # OpenCV 창 표시는 학습 데이터 촬영 프로그램에서만 사용한다. (본 프로그램은 Qt 창)
        if not self.window_open:
            cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
            self.window_open = True
        cv2.imshow(self.window_name, frame)
        cv2.waitKey(1)

    def stop(self):
        self.tello.streamoff()
        if self.window_open:
            cv2.destroyAllWindows()
            self.window_open = False
