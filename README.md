# Bench-Tools

Lets Claude run the bench: scope, logic, stimulus, power — and get numbers back instead of screenshots.

| Instrument | Role | Path |
|---|---|---|
| Analog Discovery 1 | Analog eyes on 3.3 / 5 / 12 / 24 V lines, DIO "hands", AWG, ±5 V supplies | WaveForms SDK via `pydwf` |
| Pico 2 (sigrok-rp2350) | Long/deep logic captures + protocol decode | `sigrok-cli` |

Both run on the NucBox (native Windows). Claude reaches them through the `bench` MCP server.

```
Claude ──MCP(stdio)──> bench.mcp_server ──pydwf──> AD1 (USB)
                                        └─sigrok-cli─> Pico 2 (USB CDC)
```

## Setup (NucBox, Windows)

1. **WaveForms** (includes the SDK runtime + Adept): https://digilent.com/shop/software/digilent-waveforms/
   Open the WaveForms GUI once to confirm it sees the AD1, then **close it** (only one program can hold the device).
2. **sigrok-cli NIGHTLY** (0.7.2 lacks the Pico driver):
   https://sigrok.org/download/binary/sigrok-cli/sigrok-cli-NIGHTLY-x86_64-release-installer.exe
3. **Pico 2 firmware**: hold BOOTSEL, plug in, drop
   [`pico_sdk_sigrok_pico2.uf2`](https://github.com/c1570/sigrok-rp2350/tree/rp2350/pico_sdk_sigrok/build) on the `RP2350` drive.
   (Fork marks Pico 2 / RP2350A as untested — `python -m bench pico --show` is the check.)
4. Python 3.10+:
   ```
   pip install -r requirements.txt
   ```
5. Bring-up:
   ```
   python -m bench list          # AD1 + Pico port visible?
   python -m bench selftest      # wire AD V+ -> scope 1+, 1- -> GND; expect ~5 V PASS
   python -m bench pico --show   # Pico channel names / rates
   ```
6. Open Claude Code in this folder. `.mcp.json` registers the `bench` server; tools appear as `ad_*` / `pico_*`.

## MCP tools

| Tool | Does |
|---|---|
| `bench_status` | What's connected |
| `ad_scope` | Single-shot capture, trigger, optional DIO stimulus after arm → levels, edges, rise/fall, freq, duty, overshoot, envelope; CSV saved |
| `ad_volts` | DC volts on ch1/ch2 |
| `ad_dio_set` / `ad_dio_pulse` / `ad_dio_release` / `ad_dio_read` | Drive/press/read DIO 0–15 |
| `ad_supplies` | ±5 V user supplies on/off |
| `ad_awg` / `ad_awg_stop` | Signal injection |
| `ad_release` | Free the AD for the WaveForms GUI |
| `pico_capture` | Deep capture; UART / I²C / SPI decode or edge list; `.sr` saved |
| `pico_info` | sigrok `--show` |

Captures land in `captures/` (git-ignored).

## Wiring and limits — read before probing 12/24 V

**AD1 scope (12 / 24 V lines):** differential inputs, ±25 V max per input. Use `range_v=50` (low gain).
- `1+` → signal, `1−` → DUT GND, AD GND → DUT GND.
- 24 V nets with inductive loads (relays, solenoids, motors) can kick well past 25 V. For those, go through the BNC adapter with a 10× probe, or a 2:1 divider, and scale the result.

**AD1 DIO:** 3.3 V out, 5 V-tolerant in. **Never** tie DIO directly to 12/24 V nets. To press a 12/24 V input, drive a transistor, opto, or relay from DIO.

**AD1 supplies:** fixed +5 V / −5 V, ~100 mA total. Bigger DUTs need their own PSU.

**AD1 buffer:** 8k–16k samples/ch. `span = samples / rate` — e.g. 16k @ 1 MS/s = 16 ms. Longer than that → Pico.

**Pico 2 on 12/24 V lines:** 3.3 V GPIO. Divider per line (Thevenin ~10k, fine for these slow nets):

| Line | Top | Bottom | Nominal at pin | At +25% |
|---|---|---|---|---|
| 24 V | 100 kΩ | 12 kΩ | 2.57 V | 3.2 V |
| 12 V | 47 kΩ | 12 kΩ | 2.44 V | 3.05 V |
| 5 V | 10 kΩ | 20 kΩ | 3.33 V | — |

Add a BAT54S (or similar) clamp to 3V3/GND on lines that can spike. Tie Pico GND to DUT GND.

## Known risks

- **AD1 + current WaveForms:** legacy device; some SDK versions have had issues. If `bench list` doesn't see it, try an older WaveForms release.
- **pydwf ↔ libdwf version:** if calls fail with missing-function errors, pin a `pydwf` release that matches the installed WaveForms.
- **Untested on hardware** as of first commit — `selftest` and `pico --show` are the first gates.
