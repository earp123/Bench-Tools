"""Pico 2 (sigrok-rp2350 firmware) deep logic capture via sigrok-cli.

Pico GPIO is 3.3 V logic. 12/24 V lines MUST go through a divider (see README).
Channel names come from the firmware; run info() once to confirm (D2..D22 on the
upstream RP2040 build).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"
PICO_VID = "2E8A"  # Raspberry Pi USB vendor ID

_WIN_DEFAULTS = [
    r"C:\Program Files\sigrok\sigrok-cli\sigrok-cli.exe",
    r"C:\Program Files (x86)\sigrok\sigrok-cli\sigrok-cli.exe",
]


class PicoError(RuntimeError):
    pass


def sigrok_cli() -> str:
    exe = os.environ.get("SIGROK_CLI") or shutil.which("sigrok-cli")
    if exe:
        return exe
    for p in _WIN_DEFAULTS:
        if os.path.exists(p):
            return p
    raise PicoError("sigrok-cli not found (install the NIGHTLY build, or set SIGROK_CLI)")


def find_port() -> str:
    from serial.tools import list_ports
    for p in list_ports.comports():
        if PICO_VID in (p.hwid or "").upper():
            return p.device
    raise PicoError("No Pico on a serial port (sigrok firmware flashed? cable data-capable?)")


def _run(args: list[str], timeout: float | None = None) -> str:
    r = subprocess.run([sigrok_cli()] + args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise PicoError(f"sigrok-cli failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def info(port: str | None = None) -> str:
    """Driver/channel listing straight from sigrok."""
    port = port or find_port()
    return _run(["-d", f"raspberrypi-pico:conn={port}", "--show"], timeout=15)


DECODERS = {
    # name -> (-P template, -A annotation filter)
    "uart": ("uart:rx={rx}:baudrate={baud}", "uart=rx-data"),
    "i2c":  ("i2c:sda={sda}:scl={scl}", "i2c=address-read,address-write,data-read,data-write"),
    "spi":  ("spi:clk={clk}:mosi={mosi}" , "spi=mosi-data,miso-data"),
}


def capture(
    channels: str = "D2",
    rate: str = "10m",
    samples: int = 100_000,
    trigger: str | None = None,
    decoder: str | None = None,
    decoder_args: dict | None = None,
    port: str | None = None,
    timeout_s: float = 30.0,
    max_edges: int = 500,
) -> dict:
    """Capture to a .sr file, then decode (or emit an edge list).

    trigger       sigrok spec, e.g. "D2=f" (r/f/e/0/1). Blocks until it fires or timeout_s.
    decoder       "uart" | "i2c" | "spi" | None (edges)
    decoder_args  e.g. {"rx": "D2", "baud": 115200} / {"sda": "D2", "scl": "D3"} /
                  {"clk": "D2", "mosi": "D3", "miso": "D4", "cs": "D5"}
    """
    port = port or find_port()
    CAPTURE_DIR.mkdir(exist_ok=True)
    sr = CAPTURE_DIR / f"pico_{datetime.now():%Y%m%d-%H%M%S-%f}.sr"

    cmd = ["-d", f"raspberrypi-pico:conn={port}", "-c", f"samplerate={rate}",
           "-C", channels, "--samples", str(samples), "-o", str(sr)]
    if trigger:
        cmd += ["--triggers", trigger, "--wait-trigger"]

    t = time.time()
    try:
        _run(cmd, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"no trigger/data within {timeout_s}s", "port": port}
    took = time.time() - t

    out = {"ok": True, "file": str(sr), "port": port, "rate": rate,
           "samples": samples, "capture_s": round(took, 3)}

    if decoder:
        tmpl, ann = DECODERS[decoder]
        a = dict(decoder_args or {})
        pd = tmpl.format(**a)
        if decoder == "spi":
            for k in ("miso", "cs"):
                if k in a:
                    pd += f":{k}={a[k]}"
        text = _run(["-i", str(sr), "-P", pd, "-A", ann], timeout=60)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        out["decoded"] = lines[:2000]
        out["decoded_count"] = len(lines)
        return out

    # edge list: one entry per change of any channel
    raw = _run(["-i", str(sr), "-O", "csv:header=false:time=true"], timeout=60)
    edges, last = [], None
    for line in raw.splitlines():
        if not line or line.startswith(";"):
            continue
        tcol, *vals = line.split(",")
        if vals != last:
            if last is not None:
                try:
                    ts: float | str = float(tcol)
                except ValueError:
                    ts = tcol.strip()
                edges.append({"t": ts, "v": "".join(v.strip() for v in vals)})
            last = vals
    out["channels"] = channels
    out["edge_count"] = len(edges)
    out["edges"] = edges[:max_edges]
    out["truncated"] = len(edges) > max_edges
    return out
