# Tier 2 scenarios

A model, this MCP over stdio, a natural-language brief, and the tape invariants as the
grader. This is the only thing here that costs money, and the only thing that answers the
question the tapes cannot: **would a model, given only these tools, choose the right calls?**

`ut4_dofa` is why the question matters. The tools were correct and the session still produced
an unplayable map.

```sh
mise run bench:tier2 -- --check      # free: server starts, tools list, grading runs
mise run bench:tier2 -- --estimate   # one count_tokens call per scenario
mise run bench:tier2 -- --run --scenario seal-the-roofs -v
mise run bench:tier2 -- --run --out bench/results/tier2.json
```

Never part of `mise run test`. `--run` has to be asked for.

## The scenarios

| Scenario | Asks |
| --- | --- |
| `seal-the-roofs` | Make a roof unstandable without touching the interior or the plaza. The brief spells out the trap. |
| `seal-the-roofs-terse` | The same task in the words a mapper actually uses: "block off roof access". |
| `continue-existing-level` | Add a room to a finished map without costing the old work anything. |

The first two share a fixture and a grader and differ only in wording. That pair is the
measurement worth having: if the careful brief passes and the terse one does not, the result
came from the prompt describing the trap, not from the tools preventing it — and the fix
belongs in the tool descriptions, not in the brief.

## Grading

`nrc_mcp.playspace` invariants — the same ones the tapes use. No model judges anything; the
grader is arithmetic over a voxel grid. Each scenario's goal is expressed as an invariant that
is **false on the untouched fixture**, and `--check` verifies that: a scenario every invariant
already passes cannot tell success from doing nothing, and it says so rather than reporting a
green run.

`positions_not_walkable` is the goal in `seal-the-roofs`. Note what it claims — not
*unreachable*, only *not standable*. A roof a player can still jump onto from a crate is a
routing question, and connectivity is the tool for that.

## Cost

In descending order of how much it saves:

1. **Small fixtures.** `plaza_building.map` is 9 brushes. It reproduces the `ut4_dofa` trap at
   roughly a fortieth of the size, and a trap does not need a city to be a trap.
2. **A cached prefix.** 48 tool schemas are the bulk of every request and never change within a
   run, so `cache_control` goes on the last one and later turns read them at a tenth of the
   price. Per-turn `cache_read_input_tokens` is reported; if the total is zero the run says so,
   because a silently invalidated prefix costs full price on every turn and looks like nothing.
3. **A deterministic grader.** An LLM judge would roughly double the spend and add a second
   thing that can be wrong.
4. **Hard caps.** `max_turns` and `max_output_tokens` per scenario. A run stopped by a cap
   reports `budget_exhausted`, never `FAIL` — a scenario that never finished has not been shown
   to fail at the task, and conflating the two would make the suite overstate what it knows.

The caps are enforced client-side, which keeps this off a beta surface but means the model does
not know it is running out and gets cut off rather than wrapping up. `output_config.task_budget`
is the API-side alternative if that trade stops being worth it.

## Adding one

Same shape as a tape, with `prompt` instead of `steps`:

```json
{
  "name": "my-scenario",
  "note": "what this measures, and what it does not",
  "fixture": "bench/scenarios/fixtures/plaza_building.map",
  "prompt": "Open the map at {work} and ...",
  "max_turns": 20,
  "max_output_tokens": 120000,
  "allowed_tools": ["map_open", "map_save"],
  "invariants": [{"name": "positions_not_walkable", "args": {"positions": [[384, 256, 256]]}}]
}
```

`{work}` is the fixture copied into a throwaway workspace; `{workspace}` is its directory.
`allowed_tools` is optional and narrows the surface the model is offered — useful for asking
whether a task is possible with fewer tools, and for cutting the cached prefix down.

Then run `--check` before `--run`. It costs nothing and catches the mistakes that are worth
catching for free: a renamed tool, a moved fixture, an invariant that does not exist, and a
goal that was already true.
