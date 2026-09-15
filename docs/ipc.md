# IPC contract

Two processes, one direction of file flow.

```
panel (Lua, inside REAPER)  --writes-->  ~/.config/xtouchmini/state.json
                                                    |
                                                    v  (mtime poll)
                                         daemon (Python)
```

`mappings.json` is Lua-only persistence. **The daemon reads exactly one file,
`state.json`**, and the panel writes into it everything the daemon needs. A
daemon that had to join two files could see a half-updated pair.

## Why files and not ExtState

REAPER rewrites `reaper-extstate.ini` on exit, clobbering anything written from
outside — the reason recorded in `ShuttleXpress utility/shuttlexpress/jogconf.py:3-8`.
ExtState is still the right channel for the *transient* panel flags that the
reserved-button action scripts set, because both ends of that are inside REAPER.

## Why the daemon does not ask REAPER directly

The panel is the only component that knows about plugins, and it is the only
place the strict-focus rule is enforced. The daemon is a translator: it never
parses a plugin name, never decides what is active, and never maps a parameter
by name. Keeping that knowledge in one process is what stops REAPER's idea of
the focused FX and the panel's from drifting apart.

## `state.json`

Written by the panel on change only, never on a timer. Written atomically
(`.tmp` + `os.rename`) so the daemon can never read a half-written file.

```json
{
  "version": 1,
  "seq": 412,
  "active": true,
  "layer_hint": "A",

  "track": { "index": 3, "name": "Vox", "volume": 0.7161 },

  "fader": {
    "mode": "pickup",
    "sensitivity": 1.0,
    "end_zone": 3,
    "recal_timeout": 1.0
  },

  "fx": {
    "index": 0,
    "ident": "ReaComp.vst3",
    "name": "VST: ReaComp (Cockos)"
  },

  "layers": {
    "A": {
      "encoders": [
        { "param": 12, "name": "Threshold", "value": 0.42, "default": 1.0 },
        null, null, null, null, null, null, null
      ],
      "buttons": [
        { "param": 5, "name": "Bypass", "value": 0.0, "mode": "toggle" },
        null, null, null, null, null, null, null
      ]
    },
    "B": { "encoders": [ ... 8 ... ], "buttons": [ ... 8 ... ] }
  },

  "reserved": [
    { "kind": "builtin", "id": "bank_prev" },
    { "kind": "action",  "id": "_RS1a2b3c..." },
    null, null, null, null, null, null
  ]
}
```

| Field | Meaning |
|---|---|
| `seq` | Increments on every write. The daemon uses it to spot a change it might otherwise miss if two writes land inside one filesystem mtime granule. |
| `active` | False when no FX is focused. Encoders and mappable buttons go dead; the fader and reserved buttons keep working. |
| `layer_hint` | The layer the panel *believes* is live. Advisory only — the daemon knows the truth, because the hardware sends different CC/note numbers per layer. |
| `track.*` | The **selected** track, which the fader follows whether or not a plugin is focused. It is NOT necessarily the track the focused plugin is on — see `fx.track`. |
| `track.volume` | A **fader position**, 0..1 along REAPER's own taper — the same thing `/track/N/volume` means in both directions. Not `D_VOL`, which is a linear gain factor, and not `D_VOL/4`: unity sits at **0.716** on this scale, at 1.0 on the first and at 0.25 on the second. The panel converts with `DB2SLIDER` in `xt/volume.lua`. Seeds fader soft pickup, and in `relative` mode is the base every nudge is added to, so the daemon ignores it for `echo_suppress` seconds after one of its own writes — the panel's copy lags, and adding to the lagging figure makes the next nudge step backwards. |
| `fader.mode` | `absolute`, `pickup` or `relative`. Anything else reads as `pickup`, which is the behaviour the bridge had before the modes existed. Edited in the panel's Fader tab and stored in `mappings.json`; it lives here rather than in `config.toml` because the panel is the authority on what the daemon should be doing, the same rule that puts resolved parameter indices here. |
| `fader.sensitivity` | `relative` only. Volume moved per unit of fader travel; `1.0` is one full sweep per full range. |
| `fader.end_zone` | `relative` only. How many of the 128 raw steps at each end mean "out of road": reaching one suspends output until the fader is walked back. `0` disables the pause. |
| `fader.recal_timeout` | `relative` only. Seconds of stillness that end a walk-back. Reversing direction ends one too, and that reversing move counts as real. |
| `fx.index` | Index into the track's FX chain, used for the explicit write address. |
| `fx.track` | The track the focused plugin is **on**, from `GetFocusedFX2`. This is what the daemon addresses its writes with. Using `track.index` instead makes every encoder follow the selection: select another track and the writes go to whatever plugin sits at the same chain position there — nothing if that chain is shorter, the wrong plugin if it is not. The two coincide only while the plugin's own track is selected, which is why this was invisible for so long. `-1` when nothing is focused. |
| `encoders[i]` | `null` = unassigned. `param` is the FX parameter index; `name` is stored so a mapping survives a plugin reordering its parameters. `value`/`default` seed the LED ring and the push-to-reset gesture. |
| `buttons[i].mode` | `toggle` or `momentary`. |
| `reserved[i]` | Global, independent of the focused FX, so these stay live when `active` is false. `builtin` ids are handled by firing the matching registered ReaScript; `action` ids are fired straight at REAPER as `/action/<id>`. |

**One scale, everywhere.** Three quantities get called "volume" here and they
are not interchangeable: the `D_VOL` gain factor, dB, and the fader position
above. The daemon's shadow is fed from both this file and REAPER's OSC
feedback, so the two must agree — when they did not, the shadow flipped between
0.25 and 0.716 for the same level, and soft pickup spent its time waiting for
the fader to reach somewhere the track was not. `tools/fader_test.py scales`
prints all three side by side and says whether they agree.

The `fader` block is always written whole, never as a diff: a missing field
defaults on the daemon's side, so a partial block would silently reset whatever
it left out rather than leaving it alone. An older panel that writes no block
at all gets every default, which is the pre-modes behaviour.

Both layers are always present and fully resolved. The hardware layer button is
handled entirely inside the device — layer B simply emits different CC and note
numbers — so the daemon must hold both mappings at once and select by the
number that arrived, not by any state the panel tells it.

## `mappings.json`

Panel-only. Keyed by plugin ident, which comes from
`TrackFX_GetNamedConfigParm(tr, fx, "fx_ident")` and is stable across instances
and projects — that is what makes a mapping global.

```json
{
  "version": 1,
  "reserved": [ { "kind": "builtin", "id": "bank_prev" }, ... ],
  "plugins": {
    "ReaComp.vst3": {
      "display": "VST: ReaComp (Cockos)",
      "layers": {
        "A": {
          "encoders": [ { "param": 12, "name": "Threshold" }, ... ],
          "buttons":  [ { "param": 5, "name": "Bypass", "mode": "toggle" }, ... ]
        },
        "B": { "encoders": [ ... ], "buttons": [ ... ] }
      }
    }
  }
}
```

`param` and `name` are both stored. On load, if the parameter at `param` no
longer reports `name`, the panel searches the chain by name before giving up —
plugins do reorder their parameters between versions, and a mapping that
silently starts driving the wrong control is worse than one that reports itself
broken.
