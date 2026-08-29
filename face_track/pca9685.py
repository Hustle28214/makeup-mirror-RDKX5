"""PCA9685 driver for RDK X5 via smbus2 on /dev/i2c-5.

Byte-by-byte writes (no auto-increment dependency), safer defaults.
Ref: NXP PCA9685 datasheet + Adafruit_Python_PCA9685 reference algorithm.

40-pin: Pin 3 = SDA5, Pin 5 = SCL5. Default I2C addr 0x40.
"""
import time
from smbus2 import SMBus

# Registers
MODE1       = 0x00
MODE2       = 0x01
PRESCALE    = 0xFE
LED0_ON_L   = 0x06
LED0_ON_H   = 0x07
LED0_OFF_L  = 0x08
LED0_OFF_H  = 0x09

# MODE1 bits
ALLCALL     = 0x01
SLEEP       = 0x10
AI          = 0x20   # auto-increment (unused now, kept for reference)
RESTART     = 0x80

# MODE2 bits
OUTDRV      = 0x04   # totem-pole output (required for driving servos)


class PCA9685:
    def __init__(self, bus=5, address=0x40, freq_hz=50):
        self._bus = SMBus(bus)
        self._addr = address
        self._freq = freq_hz
        self._reset()
        self.set_pwm_freq(freq_hz)
        # Park all outputs OFF so servos see clean pulses from the start.
        self.set_all_pwm(0, 0)

    # --- raw I/O ---
    def _write8(self, reg, val):
        self._bus.write_byte_data(self._addr, reg, val & 0xFF)

    def _read8(self, reg):
        return self._bus.read_byte_data(self._addr, reg)

    # --- init / freq ---
    def _reset(self):
        # MODE2: totem-pole outputs
        self._write8(MODE2, OUTDRV)
        # MODE1: enable ALLCALL, wake up (SLEEP=0)
        self._write8(MODE1, ALLCALL)
        time.sleep(0.005)  # oscillator stabilize

    def set_pwm_freq(self, hz):
        # prescale = round(25MHz / (4096 * hz)) - 1
        prescaleval = 25_000_000.0 / 4096.0 / float(hz) - 1.0
        prescale = int(round(prescaleval))
        oldmode = self._read8(MODE1)
        newmode = (oldmode & 0x7F) | SLEEP        # enter sleep to write PRESCALE
        self._write8(MODE1, newmode)
        self._write8(PRESCALE, prescale)
        self._write8(MODE1, oldmode)              # restore (wakes up)
        time.sleep(0.005)                          # oscillator stabilize (>=500us)
        self._write8(MODE1, oldmode | RESTART)    # restart PWM channels
        self._freq = hz

    # --- PWM output ---
    def set_pwm(self, ch, on, off):
        """Set ON count (0..4095) and OFF count (0..4095) for a channel."""
        on = int(on) & 0xFFF
        off = int(off) & 0xFFF
        base = LED0_ON_L + 4 * ch
        self._write8(base + 0, on & 0xFF)
        self._write8(base + 1, (on >> 8) & 0x0F)
        self._write8(base + 2, off & 0xFF)
        self._write8(base + 3, (off >> 8) & 0x0F)

    def set_all_pwm(self, on, off):
        on = int(on) & 0xFFF
        off = int(off) & 0xFFF
        self._write8(0xFA, on & 0xFF)
        self._write8(0xFB, (on >> 8) & 0x0F)
        self._write8(0xFC, off & 0xFF)
        self._write8(0xFD, (off >> 8) & 0x0F)

    def set_pulse_us(self, ch, us):
        period_us = 1_000_000.0 / self._freq
        off = int(round(us / period_us * 4096))
        off = max(0, min(4095, off))
        self.set_pwm(ch, 0, off)

    def release(self, ch):
        """Cut PWM output on a channel — servo goes limp."""
        self.set_pwm(ch, 0, 0)

    def close(self):
        try:
            self._bus.close()
        except Exception:
            pass


class Servo:
    """Map deg to pulse width us. MG90S safe defaults: 1000-2000us for 0-180."""

    def __init__(self, pca, channel, min_us=1000, max_us=2000,
                 min_deg=0, max_deg=180, invert=False):
        self.pca = pca
        self.ch = channel
        self.min_us = min_us
        self.max_us = max_us
        self.min_deg = min_deg
        self.max_deg = max_deg
        self.invert = invert
        self._angle = None

    def set_angle(self, deg):
        deg = max(self.min_deg, min(self.max_deg, deg))
        d = (self.max_deg - deg) if self.invert else deg
        us = self.min_us + (self.max_us - self.min_us) * (d - self.min_deg) / (self.max_deg - self.min_deg)
        self.pca.set_pulse_us(self.ch, us)
        self._angle = deg

    @property
    def angle(self):
        return self._angle

    def release(self):
        self.pca.release(self.ch)
