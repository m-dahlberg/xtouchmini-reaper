# Per-control feel

## The problem

The bridge speaks to REAPER in **normalised** values: every parameter is a
number from 0 to 1, and one encoder detent moves it by a fixed 1/128. But a
fixed step in the normalised domain is not a fixed step in what the parameter
actually *means*, because each plugin maps that 0..1 onto its own units however
it likes.

Measured on ReaEQ's `Freq-Low Shelf`:

| normalised | frequency | ratio over the previous 0.1 |
|---|---|---|
| 0.00 | 20 Hz | — |
| 0.10 | 69 Hz | **×3.46** |
| 0.20 | 159 Hz | ×2.30 |
| 0.30 | 322 Hz | ×2.03 |
| 0.50 | 1160 Hz | ×1.87 |
| 1.00 | 24000 Hz | ×1.82 |

Above roughly 500 Hz the ratio is constant, so equal detents *are* equal
distance on a log-frequency display. Below that the curve is compressed: the
mapping is exponential **with an offset** (it fits `58.9·e^(6.01n) − 38.9`), and
the offset dominates near the bottom. One detent moves about **14% at 20 Hz**
but only **4% at 1 kHz** — so a sweep tears through the bottom octaves and
crawls at the top.

## The setting

Each mapped slot has a **Feel**: a taper and a sensitivity, in the Surface tab.

| Taper | Effect | Use for |
|---|---|---|
| `linear` | Every detent the same size. The default. | Most parameters — anything already even across its range |
| `log` | Detents get finer towards the **minimum** | Frequency, time, anything exponential from a low floor |
| `exp` | Detents get finer towards the **maximum** | Parameters that bunch up at the top |

Sensitivity is a plain multiplier (0.25× to 4×) applied on top, for controls
that are simply too fast or too slow.

The curve is `floor + (1 − floor)·√value` for `log`, mirrored for `exp`, with
`floor` from `[feel] taper_floor` (default 0.25, i.e. quarter speed at the fine
end). It never reaches zero — a curve that did would leave the control stuck at
the end of its travel with no way back, and there is a test for that.

## What this is not

**It is not an exact linearisation.** The daemon only ever sees a normalised
number; it has no idea whether the plugin is displaying hertz, decibels or
milliseconds, and REAPER exposes no way to ask. `log` is a shape that happens to
suit exponential-from-a-floor parameters well, not a computed inverse of the
plugin's own curve.

So it is tuned by feel: set the taper, sweep the control, adjust sensitivity.
For ReaEQ frequency, `log` at 1× is a good starting point.

An exact version would need the plugin's value curve, which could only be
recovered by writing sample values and reading back the formatted result — a
destructive probe that would spam the undo history and touch the user's project
every time a mapping was resolved. Not worth it for a feel adjustment.
