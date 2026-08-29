"""Interactive servo tool for continuous-rotation calibration / test.

Continuous-rotation control (calibrated 2026-08-30, loaded):
  stop_us = 1560
  yaw:  omega_deg_s ~= 1.86 * (pulse-1560) - 62,  min |Δus|=60 to move
  tilt: omega_deg_s ~= 1.76 * (pulse-1560) - 64,  min |Δus|=90 to move
  pulse > 1560 -> CCW-as-viewed (yaw pans LEFT, tilt looks DOWN)

Note: face_track.py auto-zeros theta at startup — this tool no longer
needs a `zero` command. Physical homing is a user action (park mechanism
at desired origin BEFORE launching face_track.py).

Commands:
  us <ch> <us>          # raw pulse width on ch (holds until you change it)
  stop <ch>             # set ch to stop_us (1560)
  stop                  # stop both
  jog <ch> <dus> <ms>   # drive ch with pulse=1560+dus for <ms>, then stop
                        #   e.g. `jog 0 60 400` = ch0 CCW (Δus=+60) 400ms
                        #        `jog 1 -80 300` = ch1 CW  (Δus=-80) 300ms
  release               # cut PWM (servo goes limp; can turn by hand)
  q                     # quit (auto-stops both first)
"""
import time

from pca9685 import PCA9685

BUS = 5
ADDR = 0x40
STOP_US = 1560


def _jog(pca, ch, dus, ms):
    pulse = STOP_US + int(dus)
    pca.set_pulse_us(ch, pulse)
    time.sleep(max(0, ms) / 1000.0)
    pca.set_pulse_us(ch, STOP_US)


def main():
    pca = PCA9685(bus=BUS, address=ADDR, freq_hz=50)
    pca.set_pulse_us(0, STOP_US)
    pca.set_pulse_us(1, STOP_US)
    print(f"[init] PCA9685 on /dev/i2c-{BUS} @ 0x{ADDR:02x}, 50 Hz")
    print(f"[init] both channels parked at stop_us={STOP_US}")
    print("commands: 'us <ch> <us>' | 'stop [ch]' | 'jog <ch> <dus> <ms>' "
          "| 'release' | 'q'")

    try:
        while True:
            try:
                line = input("srv> ").strip()
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
                    pca.release(0); pca.release(1)
                    print("  released both")
                elif cmd == "stop":
                    if len(parts) == 1:
                        pca.set_pulse_us(0, STOP_US); pca.set_pulse_us(1, STOP_US)
                        print("  both -> stop")
                    else:
                        ch = int(parts[1])
                        pca.set_pulse_us(ch, STOP_US)
                        print(f"  ch{ch} -> stop")
                elif cmd == "us" and len(parts) == 3:
                    ch = int(parts[1]); us = int(parts[2])
                    pca.set_pulse_us(ch, us)
                    print(f"  ch{ch} pulse={us}us")
                elif cmd == "jog" and len(parts) == 4:
                    ch = int(parts[1]); dus = int(parts[2]); ms = int(parts[3])
                    _jog(pca, ch, dus, ms)
                    print(f"  ch{ch} jog Δus={dus:+d} for {ms}ms -> stop")
                else:
                    print("  ? try: us 0 1620 | stop | jog 0 60 400 | "
                          "release | q")
            except (ValueError, KeyError, IndexError) as e:
                print(f"  err: {e}")
    finally:
        pca.set_pulse_us(0, STOP_US); pca.set_pulse_us(1, STOP_US)
        time.sleep(0.1)
        pca.release(0); pca.release(1)
        pca.close()
        print("[bye] stopped, released, closed")


if __name__ == "__main__":
    main()
