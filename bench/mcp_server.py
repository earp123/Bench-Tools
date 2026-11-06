"""MCP server exposing the bench to Claude (stdio).

Run:  python -m bench.mcp_server
Claude Code picks it up automatically from .mcp.json in the repo root.
"""
from __future__ import annotations

import threading

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import pico

server = _Server(
    "bench",
    instructions=(
        "Bench instruments on Sam's NucBox. ad_* = Analog Discovery 1 (2-ch scope +/-25 V, "
        "16 DIO 3.3 V, AWG, +/-5 V supplies). pico_* = Pico 2 sigrok logic analyzer for long "
        "captures. Scope results are summarized (levels, edges, timing, envelope); full data "
        "is saved to CSV. Never drive DIO into 12/24 V nets."
    ),
)

_ad = None
_lock = threading.Lock()


def _dev():
    global _ad
    if _ad is None:
        from .ad import AD
        _ad = AD()
    return _ad


def _guard(fn):
    try:
        with _lock:
            return fn()
    except Exception as e:  # report instead of killing the server
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _chans(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(s).split(",") if x.strip())


# ---------------------------------------------------------------- status
@server.tool()
def bench_status() -> dict:
    """What's connected: WaveForms devices and the Pico's serial port."""
    out: dict = {}
    try:
        from .ad import list_devices
        out["ad"] = list_devices()
        if _ad is not None:
            out["ad"]["open"] = _ad.info()
    except Exception as e:
        out["ad"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["pico"] = {"port": pico.find_port(), "sigrok_cli": pico.sigrok_cli()}
    except Exception as e:
        out["pico"] = {"error": str(e)}
    return out


# ---------------------------------------------------------------- AD scope
@server.tool()
def ad_scope(
    channels: str = "1",
    rate: float = 1e6,
    samples: int = 0,
    range_v: float = 50.0,
    offset_v: float = 0.0,
    trigger_ch: int = 1,
    level: float = 1.5,
    slope: str = "rise",
    pretrigger: float = 0.1,
    timeout_s: float = 2.0,
    stimulus_pin: int = -1,
    stimulus_level: int = 0,
    stimulus_ms: float = 50.0,
    points: int = 200,
) -> dict:
    """Single-shot AD1 scope capture with summary stats.

    channels "1", "2" or "1,2". range_v 50 = +/-25 V (use for 12/24 V), 5 = +/-2.5 V.
    trigger_ch 0 = free-run. samples 0 = max buffer. span = samples / rate.
    stimulus_pin >= 0 pulses that DIO pin to stimulus_level for stimulus_ms right after
    the scope arms (e.g. press reset, then catch the rail coming up).
    """
    stim = None if stimulus_pin < 0 else {
        "pin": stimulus_pin, "level": stimulus_level, "ms": stimulus_ms}
    return _guard(lambda: _dev().scope(
        channels=_chans(channels), rate=rate, samples=samples or None,
        range_v=range_v, offset_v=offset_v,
        trigger_ch=trigger_ch or None, level=level, slope=slope,
        pretrigger=pretrigger, timeout_s=timeout_s, stimulus=stim, points=points))


@server.tool()
def ad_volts(channels: str = "1,2", range_v: float = 50.0) -> dict:
    """DC voltage on scope channels (mean/min/max over ~40 ms)."""
    return _guard(lambda: _dev().volts(_chans(channels), range_v))


# ---------------------------------------------------------------- AD DIO
@server.tool()
def ad_dio_set(pin: int, level: int) -> dict:
    """Drive AD DIO pin (0-15) high (1) or low (0). 3.3 V logic only."""
    return _guard(lambda: (_dev().dio_set(pin, level), {"ok": True})[1])


@server.tool()
def ad_dio_pulse(pin: int, level: int = 0, ms: float = 50.0) -> dict:
    """Drive pin to level for ms, then release (tri-state). Button-press style."""
    return _guard(lambda: (_dev().dio_pulse(pin, level, ms), {"ok": True})[1])


@server.tool()
def ad_dio_release(pin: int) -> dict:
    """Stop driving a DIO pin (tri-state)."""
    return _guard(lambda: (_dev().dio_release(pin), {"ok": True})[1])


@server.tool()
def ad_dio_read() -> dict:
    """Read all 16 DIO input levels."""
    return _guard(lambda: _dev().dio_read())


# ---------------------------------------------------------------- AD power / AWG
@server.tool()
def ad_supplies(positive: bool, negative: bool) -> dict:
    """Switch the AD's fixed +5 V / -5 V user supplies (~100 mA total)."""
    return _guard(lambda: _dev().supplies(positive, negative))


@server.tool()
def ad_awg(channel: int = 1, func: str = "sine", freq: float = 1000.0,
           amplitude: float = 1.0, offset: float = 0.0) -> dict:
    """Start AWG output. func: dc, sine, square, triangle, rampup, rampdown, noise, pulse.
    Output limited to +/-5 V (amplitude + offset)."""
    return _guard(lambda: (_dev().awg(channel, func, freq, amplitude, offset), {"ok": True})[1])


@server.tool()
def ad_awg_stop(channel: int = 1) -> dict:
    """Stop AWG output on a channel."""
    return _guard(lambda: (_dev().awg_stop(channel), {"ok": True})[1])


@server.tool()
def ad_release() -> dict:
    """Close the AD so the WaveForms GUI can use it. Reopens on the next ad_* call."""
    def _close():
        global _ad
        if _ad is not None:
            _ad.close()
            _ad = None
        return {"ok": True}
    return _guard(_close)


# ---------------------------------------------------------------- Pico
@server.tool()
def pico_capture(
    channels: str = "D2",
    rate: str = "10m",
    samples: int = 100_000,
    trigger: str = "",
    decoder: str = "",
    decoder_args: dict | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Deep logic capture on the Pico 2 via sigrok.

    channels "D2,D3"; rate "1m"/"10m"/"50m"; trigger like "D2=f" (r/f/e/0/1).
    decoder "uart" {"rx","baud"} | "i2c" {"sda","scl"} | "spi" {"clk","mosi","miso","cs"};
    empty = edge list. Inputs are 3.3 V: 12/24 V lines need dividers.
    """
    return _guard(lambda: pico.capture(
        channels=channels, rate=rate, samples=samples, trigger=trigger or None,
        decoder=decoder or None, decoder_args=decoder_args, timeout_s=timeout_s))


@server.tool()
def pico_info() -> dict:
    """sigrok's view of the Pico: channel names, supported rates."""
    return _guard(lambda: {"ok": True, "show": pico.info()})


def main():
    server.run()


if __name__ == "__main__":
    main()
