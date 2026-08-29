"""Quick camera picker: open each index and show one frame so you can eyeball
which one is the USB webcam you want. Press any key to advance, Esc to quit."""
import cv2, sys

BACKENDS = [("MSMF", cv2.CAP_MSMF), ("DSHOW", cv2.CAP_DSHOW)]

for name, be in BACKENDS:
    for i in range(6):
        cap = cv2.VideoCapture(i, be)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"{name} idx={i}: opened but no frame")
            cap.release()
            continue
        h, w = frame.shape[:2]
        label = f"{name} index={i}  {w}x{h}  (press any key)"
        cv2.putText(frame, label, (20, 40), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow("camera picker", frame)
        print(label)
        k = cv2.waitKey(0) & 0xFF
        cap.release()
        if k == 27:
            cv2.destroyAllWindows()
            sys.exit(0)

cv2.destroyAllWindows()
