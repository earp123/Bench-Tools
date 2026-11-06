"""CLI for bring-up and quick manual checks.

  python -m bench list                      # WaveForms devices + Pico port
  python -m bench volts 1,2                 # DC volts on scope ch1/ch2
  python -m bench scope 1 --level 12 --rate 1e6
  python -m bench pico --channels D2 --rate 1m --samples 20000
  python -m bench selftest                  # AD +5 V supply -> scope ch1 loopback
"""
import argparse
import json
import sys


def _print(x):
    print(json.dumps(x, indent=1, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    v = sub.add_parser("volts")
    v.add_argument("channels", nargs="?", default="1,2")

    s = sub.add_parser("scope")
    s.add_argument("channels", nargs="?", default="1")
    s.add_argument("--rate", type=float, default=1e6)
    s.add_argument("--samples", type=int, default=0)
    s.add_argument("--range", type=float, default=50.0)
    s.add_argument("--trigger-ch", type=int, default=1)
    s.add_argument("--level", type=float, default=1.5)
    s.add_argument("--slope", default="rise")
    s.add_argument("--timeout", type=float, default=2.0)

    p = sub.add_parser("pico")
    p.add_argument("--channels", default="D2")
    p.add_argument("--rate", default="1m")
    p.add_argument("--samples", type=int, default=20000)
    p.add_argument("--trigger")
    p.add_argument("--show", action="store_true", help="just print sigrok --show")

    sub.add_parser("selftest")

    a = ap.parse_args(argv)

    if a.cmd == "list":
        from . import pico
        out = {}
        try:
            from .ad import list_devices
            out["ad"] = list_devices()
        except Exception as e:
            out["ad"] = f"{type(e).__name__}: {e}"
        try:
            out["pico_port"] = pico.find_port()
            out["sigrok_cli"] = pico.sigrok_cli()
        except Exception as e:
            out["pico"] = str(e)
        _print(out)

    elif a.cmd == "volts":
        from .ad import AD
        with AD() as ad:
            _print(ad.volts(tuple(int(c) for c in a.channels.split(","))))

    elif a.cmd == "scope":
        from .ad import AD
        with AD() as ad:
            r = ad.scope(tuple(int(c) for c in a.channels.split(",")), rate=a.rate,
                         samples=a.samples or None, range_v=a.range,
                         trigger_ch=a.trigger_ch or None, level=a.level,
                         slope=a.slope, timeout_s=a.timeout, points=40)
            _print(r)

    elif a.cmd == "pico":
        from . import pico
        if a.show:
            print(pico.info())
        else:
            _print(pico.capture(a.channels, a.rate, a.samples, a.trigger))

    elif a.cmd == "selftest":
        # Wire: AD V+ -> scope 1+, scope 1- -> GND.
        from .ad import AD
        with AD() as ad:
            print("info:", json.dumps(ad.info()))
            ad.supplies(True, False)
            import time
            time.sleep(0.3)
            v = ad.volts((1,))["ch1"]["mean"]
            ad.supplies(False, False)
            ok = 4.7 < v < 5.3
            print(f"V+ on scope ch1: {v:.3f} V -> {'PASS' if ok else 'FAIL (check wiring)'}")
            sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
