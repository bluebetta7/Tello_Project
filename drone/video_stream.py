import cv2
from AppKit import NSApplication


class VideoStream:
    def __init__(self, tello):
        self.tello = tello
        self.frame_read = None
        self.window = None
        self.window_name = "Tello Control"

    def start(self):
        self.tello.streamon()
        self.frame_read = self.tello.get_frame_read()

    def get_frame(self):
        return self.frame_read.frame

    def get_window_size(self):
        if self.window is None:
            return None
        size = self.window.contentView().bounds().size
        return max(1, int(size.width)), max(1, int(size.height))

    def show(self, frame):
        if self.window is None:
            cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
            self.window = next(window for window in NSApplication.sharedApplication().windows()
                               if window.title() == self.window_name)
        cv2.imshow(self.window_name, frame)
        cv2.waitKey(1)

    def stop(self):
        self.tello.streamoff()
        cv2.destroyAllWindows()
        self.window = None
