"""Waveform reduction: turn a raw scope buffer into numbers an agent can reason about."""
from __future__ import annotations

import numpy as np


def _levels(v: np.ndarray) -> tuple[float, float]:
    """Robust low/high levels (5th / 95th percentile)."""
    return float(np.percentile(v, 5)), float(np.percentile(v, 95))


def _crossings(v: np.ndarray, thresh: float, hyst: float) -> list[tuple[int, str]]:
    """Edge indices with hysteresis. Returns [(index, 'rise'|'fall'), ...]."""
    hi, lo = thresh + hyst, thresh - hyst
    state = v[0] > thresh
    out = []
    for i in range(1, len(v)):
        x = v[i]
        if not state and x > hi:
            state = True
            out.append((i, "rise"))
        elif state and x < lo:
            state = False
            out.append((i, "fall"))
    return out


def _edge_time(v: np.ndarray, idx: int, lo: float, hi: float, rising: bool, fs: float) -> float | None:
    """10-90% transition time around a detected edge, in seconds."""
    span = hi - lo
    a, b = lo + 0.1 * span, lo + 0.9 * span
    if not rising:
        a, b = b, a
    # walk back to the start level, forward to the end level
    i0 = idx
    while i0 > 0 and ((v[i0] > a) if rising else (v[i0] < a)):
        i0 -= 1
    i1 = idx
    while i1 < len(v) - 1 and ((v[i1] < b) if rising else (v[i1] > b)):
        i1 += 1
    if i1 <= i0:
        return None
    return (i1 - i0) / fs


def summarize(v: np.ndarray, fs: float, t0: float, points: int = 200) -> dict:
    """Stats, edges and a min/max envelope for one channel.

    t0 is the time of sample 0 relative to the trigger (negative = pretrigger).
    """
    v = np.asarray(v, dtype=float)
    lo, hi = _levels(v)
    vpp = float(v.max() - v.min())
    out: dict = {
        "min": round(float(v.min()), 4),
        "max": round(float(v.max()), 4),
        "mean": round(float(v.mean()), 4),
        "rms": round(float(np.sqrt(np.mean(v * v))), 4),
        "vpp": round(vpp, 4),
        "low_level": round(lo, 4),
        "high_level": round(hi, 4),
    }

    # Only look for edges if there is a real two-level signal (> 10% of span or > 0.2 V).
    swing = hi - lo
    if swing > max(0.2, 0.1 * max(abs(hi), abs(lo), 1e-9)):
        mid = (hi + lo) / 2
        edges = _crossings(v, mid, 0.1 * swing)
        out["threshold"] = round(mid, 4)
        out["edge_count"] = len(edges)
        out["edges"] = [
            {"t": round(t0 + i / fs, 9), "dir": d} for i, d in edges[:64]
        ]
        rises = [i for i, d in edges if d == "rise"]
        falls = [i for i, d in edges if d == "fall"]
        if rises:
            rt = _edge_time(v, rises[0], lo, hi, True, fs)
            if rt is not None:
                out["rise_time_s"] = rt
        if falls:
            ft = _edge_time(v, falls[0], lo, hi, False, fs)
            if ft is not None:
                out["fall_time_s"] = ft
        if len(rises) >= 2:
            period = float(np.mean(np.diff(rises))) / fs
            out["freq_hz"] = round(1.0 / period, 3)
            highs = [f - r for r, f in zip(rises, [x for x in falls if x > rises[0]]) if f > r]
            if highs:
                out["duty"] = round(float(np.mean(highs)) / fs / period, 4)
        # overshoot / undershoot relative to settled levels
        out["overshoot_v"] = round(float(v.max() - hi), 4)
        out["undershoot_v"] = round(float(lo - v.min()), 4)
    else:
        out["edge_count"] = 0

    # min/max envelope, decimated to `points` buckets
    n = len(v)
    b = max(1, n // points)
    m = (n // b) * b
    blocks = v[:m].reshape(-1, b)
    out["envelope"] = {
        "t0": round(t0, 9),
        "dt": b / fs,
        "min": [round(float(x), 3) for x in blocks.min(axis=1)],
        "max": [round(float(x), 3) for x in blocks.max(axis=1)],
    }
    return out
