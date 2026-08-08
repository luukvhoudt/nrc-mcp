# Scenario tapes

End-to-end tests of the MCP surface that cost nothing to run.

A session is a model choosing tools plus the tools doing the work. Only the second half can
be wrong in a way this repository can fix, and only the second half is cheap to run — so it
is tested on its own. A **tape** is the tool sequence with the model removed: a starting
map, an ordered list of `{tool, args}`, and the properties the result must have.

```sh
mise run test:tapes                                  # all of them, seconds, no compiler
mise run test:tapes -- --tape catches-interior-clip -v
mise run test:tapes -- --compile                     # also compile and check the map seals
```

## What a tape does and does not prove

It **does** prove that opening, sculpting, editing, saving and packaging still work through
exactly the functions the server exposes, and that the resulting `.map` has the properties
it should. Every step is a real call into `nrc_mcp.server`; nothing is mocked.

It **does not** prove that a model would make those calls. That is the gap Tier 2 closes —
a model in the loop with only this MCP attached, graded by the same invariants — and it is
the only part that costs tokens. Nothing here is a substitute for it, and the tapes should
not be described as if they were.

## The tapes

| Tape | Covers |
| --- | --- |
| `build-from-scratch` | A bare slab to a two-room block: hollow, cut a doorway, join. |
| `continue-existing-level` | Adding to a finished map without costing the old work anything. |
| `refactor-one-region` | `solid_edit_param` — change one parameter, leave the rest alone. |
| `improve-engine-performance` | The optimisation pass. **Measuring half only** — see below. |
| `catches-interior-clip` | The `ut4_dofa` failure, small. Expects `map_save` to **refuse**. |

`catches-interior-clip` is the one that matters most. A guard nobody tests against a real
failure is a guard nobody knows is connected: delete the playable-space check and that tape
goes red, while all the others stay green.

`improve-engine-performance` stops at measurement on purpose. `structural_audit` recommends
marking brushes detail, and there is no tool on the surface that marks a brush detail — so
no tape can apply the fix. The tape says so in its own `note` rather than quietly covering
the half that happens to be reachable.

## Writing one

```json
{
  "name": "my-scenario",
  "note": "what this covers, and what it deliberately does not",
  "fixture": "corpus/real/ut4_woolis.map",
  "steps": [
    {
      "tool": "map_open",
      "as": "open",
      "args": {"path": "{work}"},
      "expect": {"round_trip.identical": true}
    }
  ],
  "invariants": [{"name": "no_playable_space_regression", "args": {"max_lost_fraction": 0.01}}]
}
```

- `fixture` is copied into a throwaway workspace. `{work}` is that copy, `{workspace}` its
  directory, `{repo}` the repo root. A pristine second copy becomes the invariants' baseline,
  so "before" survives the tape overwriting the working file.
- `expect` maps a dotted path through the tool's own result to an expected value. A value may
  be a comparison: `{"$gte": 4}`, `{"$lt": 10}`, `{"$ne": null}`, `{"$contains": "x"}`.
- `as` names a step so `tool_result` invariants can refer to it later.
- Steps stop at the first mismatch unless the step sets `continue_on_mismatch`.
- Invariants run only if every step passed. A tape that broke halfway proves nothing about
  its end state, and reporting green invariants underneath a red step would be a lie.

## Invariants

Run `python tools/tapes.py --help`, or read `nrc_mcp.playspace`; `playspace.available()` is
the live list. The ones that need `q3map2` skip cleanly when it is absent and report as
skipped rather than passed — the same convention `test:diff` uses for its semantic half.

## Recording instead of writing

A tape is just a tool sequence, so a real session can produce one. That recorder does not
exist yet; when it does, an afternoon of real work becomes a regression test for free, and
the `ut4_dofa` session becomes the tape that would have caught it.
