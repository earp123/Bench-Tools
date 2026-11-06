"""Analog Discovery (AD1, legacy) control via the WaveForms SDK (pydwf).

Scope:   2 differential channels, 14-bit, 100 MS/s max, +/-25 V per input.
         Buffer is shallow: 8k samples/ch default config, up to 16k with the
         scope-heavy config (open with prefer="scope").
DIO:     16 pins, LVCMOS 3.3 V out, 5 V tolerant in. NOT for 12/24 V lines.
AWG:     2 channels, +/-5 V.
Supply:  fixed +5 V / -5 V, ~100 mA total.

Only one process can hold the device: close the WaveForms GUI before using this,
and call close() (or the MCP tool ad_release) before opening the GUI.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .analysis import summarize

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"


def _lazy():
    """Import pydwf lazily so the rest of the package works without WaveForms installed."""
    import pydwf
    from pydwf.utilities import openDwfDevice
    return pydwf, openDwfDevice


def list_devices() -> dict:
    pydwf, _ = _lazy()
    dwf = pydwf.DwfLibrary()
    n = dwf.deviceEnum.enumerateDevices()
    devs = []
    for i in range(n):
        devs.append({
            "index": i,
            "name": dwf.deviceEnum.deviceName(i),
            "serial": dwf.deviceEnum.serialNumber(i),
            "in_use": dwf.deviceEnum.deviceIsOpened(i),
        })
    return {"waveforms_version": dwf.getVersion(), "devices": devs}


class AD:
    """Thin, stateful wrapper. Keep one instance open; opening reloads the FPGA (~1-2 s)."""

    DIO_PINS = 16

    def __init__(self, serial: str | None = None, prefer: str | None = "scope"):
        pydwf, openDwfDevice = _lazy()
        self._p = pydwf
        self.dwf = pydwf.DwfLibrary()

        score = None
        if prefer == "scope":
            # pick the FPGA config with the deepest scope buffer
            score = lambda cfg: cfg[pydwf.DwfEnumConfigInfo.AnalogInBufferSize]  # noqa: E731
        self.dev = openDwfDevice(self.dwf, serial_number_filter=serial, score_func=score)
        self._oe = 0   # DIO output-enable mask
        self._out = 0  # DIO output value
        self.dev.digitalIO.reset()

    # ------------------------------------------------------------------ lifecycle
    def close(self):
        try:
            self.dev.analogOut.reset(-1)
        except Exception:
            pass
        try:
            self.dev.digitalIO.reset()
            self.dev.digitalIO.configure()
        except Exception:
            pass
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def info(self) -> dict:
        ai = self.dev.analogIn
        bmin, bmax = ai.bufferSizeInfo()
        fmin, fmax = ai.frequencyInfo()
        rmin, rmax, rsteps = ai.channelRangeInfo()
        return {
            "scope_channels": ai.channelCount(),
            "scope_buffer_max": bmax,
            "scope_rate_max": fmax,
            "scope_range_v": [rmin, rmax],
            "waveforms_version": self.dwf.getVersion(),
        }

    # ------------------------------------------------------------------ scope
    def scope(
        self,
        channels: tuple[int, ...] = (1,),
        rate: float = 1e6,
        samples: int | None = None,
        range_v: float = 50.0,
        offset_v: float = 0.0,
        trigger_ch: int | None = 1,
        level: float = 1.5,
        slope: str = "rise",
        pretrigger: float = 0.1,
        timeout_s: float = 2.0,
        stimulus: dict | None = None,
        points: int = 200,
        save: bool = True,
    ) -> dict:
        """Single-shot capture.

        channels   1-based scope channels to capture.
        range_v    full-scale peak-to-peak. 50 = low gain (+/-25 V), 5 = high gain (+/-2.5 V).
        trigger_ch 1-based channel to trigger on, or None for free-run (immediate).
        timeout_s  if no edge within this time, the scope auto-triggers; result has triggered=False.
        stimulus   optional {"pin": n, "level": 0|1, "ms": 50} DIO pulse fired after the scope
                   is armed, so "press reset and catch the rail" is one atomic shot.
        """
        p = self._p
        ai = self.dev.analogIn
        ai.reset()

        bmin, bmax = ai.bufferSizeInfo()
        n = int(min(samples or bmax, bmax))
        idx = [c - 1 for c in channels]
        for ch in range(ai.channelCount()):
            ai.channelEnableSet(ch, ch in idx)
        for ch in idx:
            ai.channelRangeSet(ch, range_v)
            ai.channelOffsetSet(ch, offset_v)

        ai.acquisitionModeSet(p.DwfAcquisitionMode.Single)
        ai.frequencySet(rate)
        ai.bufferSizeSet(n)
        fs = ai.frequencyGet()  # actual rate after quantization

        if trigger_ch is None:
            ai.triggerSourceSet(p.DwfTriggerSource.None_)
        else:
            ai.triggerSourceSet(p.DwfTriggerSource.DetectorAnalogIn)
            ai.triggerAutoTimeoutSet(timeout_s)
            ai.triggerChannelSet(trigger_ch - 1)
            ai.triggerTypeSet(p.DwfAnalogInTriggerType.Edge)
            ai.triggerConditionSet({
                "rise": p.DwfTriggerSlope.Rise,
                "fall": p.DwfTriggerSlope.Fall,
                "either": p.DwfTriggerSlope.Either,
            }[slope])
            ai.triggerLevelSet(level)
            ai.triggerHysteresisSet(max(0.01, 0.01 * range_v))
        # trigger position is time of buffer centre relative to trigger
        span = n / fs
        ai.triggerPositionSet((0.5 - pretrigger) * span)

        ai.configure(False, True)

        deadline = time.time() + timeout_s + 2 * span + 2.0
        fired = False
        while True:
            st = ai.status(True)
            if stimulus and not fired and st in (p.DwfState.Armed, p.DwfState.Triggered, p.DwfState.Done):
                self.dio_pulse(stimulus["pin"], stimulus.get("level", 0), stimulus.get("ms", 50))
                fired = True
            if st == p.DwfState.Done:
                break
            if time.time() > deadline:
                ai.reset()
                return {"ok": False, "error": "capture timed out", "state": st.name}
            time.sleep(0.002)

        auto = ai.statusAutoTriggered() if trigger_ch is not None else False
        t0 = -pretrigger * span if trigger_ch is not None else 0.0
        data = {c: ai.statusData(c - 1, n) for c in channels}

        result = {
            "ok": True,
            "triggered": (trigger_ch is None) or not auto,
            "rate": fs,
            "samples": n,
            "span_s": span,
            "t0_s": t0,
            "channels": {f"ch{c}": summarize(v, fs, t0, points) for c, v in data.items()},
        }
        if save:
            result["file"] = str(self._save_csv(data, fs, t0))
        return result

    def volts(self, channels: tuple[int, ...] = (1, 2), range_v: float = 50.0) -> dict:
        """DC voltmeter: short free-run capture, averaged."""
        r = self.scope(channels, rate=1e5, samples=4096, range_v=range_v,
                       trigger_ch=None, save=False, points=1)
        return {k: {"mean": v["mean"], "min": v["min"], "max": v["max"]}
                for k, v in r["channels"].items()}

    def _save_csv(self, data: dict, fs: float, t0: float) -> Path:
        CAPTURE_DIR.mkdir(exist_ok=True)
        path = CAPTURE_DIR / f"ad_{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
        chans = list(data)
        n = len(data[chans[0]])
        t = t0 + np.arange(n) / fs
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_s"] + [f"ch{c}" for c in chans])
            for i in range(n):
                w.writerow([f"{t[i]:.9g}"] + [f"{data[c][i]:.5g}" for c in chans])
        return path

    # ------------------------------------------------------------------ DIO
    def _dio_apply(self):
        dio = self.dev.digitalIO
        dio.outputEnableSet(self._oe)
        dio.outputSet(self._out)
        dio.configure()

    def dio_set(self, pin: int, level: int):
        """Drive a pin high/low (3.3 V logic)."""
        self._check_pin(pin)
        self._oe |= 1 << pin
        self._out = (self._out | (1 << pin)) if level else (self._out & ~(1 << pin))
        self._dio_apply()

    def dio_release(self, pin: int):
        """Tri-state a pin (stop driving it)."""
        self._check_pin(pin)
        self._oe &= ~(1 << pin)
        self._dio_apply()

    def dio_pulse(self, pin: int, level: int = 0, ms: float = 50):
        """Drive pin to `level` for `ms`, then release it (button-press style)."""
        self.dio_set(pin, level)
        time.sleep(ms / 1000)
        self.dio_release(pin)

    def dio_read(self) -> dict:
        dio = self.dev.digitalIO
        dio.status()
        val = dio.inputStatus()
        return {f"dio{i}": (val >> i) & 1 for i in range(self.DIO_PINS)}

    def _check_pin(self, pin: int):
        if not 0 <= pin < self.DIO_PINS:
            raise ValueError(f"DIO pin must be 0..{self.DIO_PINS - 1}")

    # ------------------------------------------------------------------ supplies
    def supplies(self, positive: bool, negative: bool) -> dict:
        """Switch the fixed +5 V / -5 V user supplies (~100 mA total)."""
        aio = self.dev.analogIO
        want = {"pos": positive, "neg": negative}
        done = {}
        for ch in range(aio.channelCount()):
            name, label = aio.channelName(ch)
            key = "pos" if ("+" in label or "Positive" in name) else \
                  "neg" if ("-" in label or "Negative" in name) else None
            if key is None:
                continue
            # find the node called "Enable" on this channel
            for node in range(aio.channelInfo(ch)):
                nname, _ = aio.channelNodeName(ch, node)
                if nname.lower().startswith("enable"):
                    aio.channelNodeSet(ch, node, 1.0 if want[key] else 0.0)
                    done[key] = want[key]
        aio.enableSet(positive or negative)
        aio.configure()
        return done

    # ------------------------------------------------------------------ AWG
    def awg(self, channel: int = 1, func: str = "sine", freq: float = 1e3,
            amplitude: float = 1.0, offset: float = 0.0):
        p = self._p
        ao = self.dev.analogOut
        ch = channel - 1
        node = p.DwfAnalogOutNode.Carrier
        funcs = {f.name.lower(): f for f in p.DwfAnalogOutFunction}
        ao.nodeEnableSet(ch, node, True)
        ao.nodeFunctionSet(ch, node, funcs[func.lower()])
        ao.nodeFrequencySet(ch, node, freq)
        ao.nodeAmplitudeSet(ch, node, amplitude)
        ao.nodeOffsetSet(ch, node, offset)
        ao.configure(ch, 1)

    def awg_stop(self, channel: int = 1):
        self.dev.analogOut.configure(channel - 1, 0)
