"""Interactive servo angle tool for calibration.

Type commands like:
  0 90        # ch0 to 90 deg
  1 45        # ch1 to 45 deg
  b 90 90     # both channels
  step 0 5    # nudge ch0 by +5 deg from current
  step 1 -10  # nudge ch1 by -10 deg
  us 0 1500   # raw pulse width on ch0 (bypass angle mapping)
  release     # de-energize both
  q           # quit
"""
import sys
import time
from pca9685 import PCA9685, Servo

BUS = 5
ADDR = 0x40
MIN_US = 1000   # MG90S safe range
MAX_US = 2000


def main():
    pca = PCA9685(bus=BUS, address=ADDR, freq_hz=50)
    servos = {
        0: Servo(pca, 0, min_us=MIN_US, max_us=MAX_US),
        1: Servo(pca, 1, min_us=MIN_US, max_us=MAX_US),
    }
    print(f"[init] PCA9685 on /dev/i2c-{BUS} @ 0x{ADDR:02x}, 50 Hz")
    print("[init] centering both to 90 deg")
    servos[0].set_angle(90); servos[1].set_angle(90)
    time.sleep(0.3)
    print("commands: '<ch> <deg>' | 'b <yaw> <tilt>' | 'step <ch> <d>' | "
          "'us <ch> <us>' | 'release' | 'q'")

    try:
        while True:
            try:
                line = input("angle> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()

            try:
                if cmd == "q":
                    break
                elif cmd == "release":
                    for s in servos.values():
                        s.release()
                    print("  released")
                elif cmd == "b" and len(parts) == 3:
                    a0, a1 = float(parts[1]), float(parts[2])
                    servos[0].set_angle(a0); servos[1].set_angle(a1)
                    print(f"  ch0={a0} ch1={a1}")
                elif cmd == "step" and len(parts) == 3:
                    ch = int(parts[1]); d = float(parts[2])
                    cur = servos[ch].angle if servos[ch].angle is not None else 90
                    new = cur + d
                    servos[ch].set_angle(new)
                    print(f"  ch{ch}: {cur} -> {servos[ch].angle}")
                elif cmd == "us" and len(parts) == 3:
                    ch = int(parts[1]); us = int(parts[2])
                    pca.set_pulse_us(ch, us)
                    print(f"  ch{ch} pulse={us}us")
                elif len(parts) == 2:
                    ch = int(parts[0]); deg = float(parts[1])
                    servos[ch].set_angle(deg)
                    print(f"  ch{ch}={servos[ch].angle}")
                else:
                    print("  ? unrecognized. try:  0 90  |  1 45  |  b 90 90  |"
                          "  step 0 5  |  us 0 1500  |  release  |  q")
            except (ValueError, KeyError, IndexError) as e:
                print(f"  err: {e}")
    finally:
        for s in servos.values():
            s.release()
        pca.close()
        print("[bye] released and closed")


if __name__ == "__main__":
    main()
