"""Servo sanity test on PCA9685 CH0 (base/yaw) and CH1 (tilt).

Usage:
  python3 servo_test.py                 # center both, sweep both
  python3 servo_test.py --ch 0          # only servo 0
  python3 servo_test.py --center-only   # just park at 90/90
"""
import argparse
import time

from pca9685 import PCA9685, Servo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", type=int, default=5)
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x40)
    ap.add_argument("--ch", type=int, choices=[0, 1, -1], default=-1,
                    help="Only exercise this channel (-1 = both)")
    ap.add_argument("--min", type=float, default=30.0)
    ap.add_argument("--max", type=float, default=150.0)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--dwell", type=float, default=0.04)
    ap.add_argument("--min-us", type=int, default=500)
    ap.add_argument("--max-us", type=int, default=2500)
    ap.add_argument("--center-only", action="store_true")
    ap.add_argument("--hold", type=float, default=0.3)
    args = ap.parse_args()

    pca = PCA9685(bus=args.bus, address=args.addr, freq_hz=50)
    yaw = Servo(pca, 0, min_us=args.min_us, max_us=args.max_us)
    tilt = Servo(pca, 1, min_us=args.min_us, max_us=args.max_us)

    print(f"[init] PCA9685 on /dev/i2c-{args.bus} @ 0x{args.addr:02x}, 50 Hz")
    print("[step] center both servos to 90 deg")
    yaw.set_angle(90)
    tilt.set_angle(90)
    time.sleep(args.hold)

    if args.center_only:
        print("[done] centered. holding pulse for 2 s then releasing.")
        time.sleep(2.0)
        yaw.release(); tilt.release(); pca.close()
        return

    def sweep(label, servo):
        print(f"[sweep] {label}: {args.min} -> {args.max} -> {args.min} -> 90")
        a = args.min
        while a <= args.max:
            servo.set_angle(a); time.sleep(args.dwell); a += args.step
        a = args.max
        while a >= args.min:
            servo.set_angle(a); time.sleep(args.dwell); a -= args.step
        servo.set_angle(90); time.sleep(args.hold)

    try:
        if args.ch in (-1, 0):
            sweep("CH0 base/yaw (rotate around Z, camera pans left/right)", yaw)
        if args.ch in (-1, 1):
            sweep("CH1 tilt      (rotate around Y, camera looks up/down)", tilt)
        print("[done] both servos parked at 90 deg; releasing.")
    finally:
        yaw.release(); tilt.release()
        pca.close()


if __name__ == "__main__":
    main()
