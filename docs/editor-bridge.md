# The editor bridge: design note

`contrib/mcpbridge` implements spec §9. This note records why it has the shape it
has, what it deliberately refuses to do, and how it should be offered upstream.
Operational detail — enabling it, the method table, the Makefile hunk — lives in
`contrib/mcpbridge/README.md` and is not repeated here.

Everything below was checked against upstream source at
`vendor/netradiant-custom/`. Where the spec and the source disagree, the source wins
and the disagreement is recorded, as in `docs/spec-corrections.md`.

---

## 1. What the bridge is for

The rest of this project operates on the saved `.map` file. That is the right default:
the kernel is lossless, testable offline, and cannot crash someone's editor. The
bridge exists for the two things a file cannot do.

**Seeing what the human sees.** An agent that is going to modify a map should be
able to ask what is selected, where the camera is, and what the map's bounds are.
None of that is in the file.

**Being watched.** A change applied through the bridge appears immediately, inside
one `UndoableCommand`, and a human can reject it with Ctrl+Z. That property is the
whole argument for the bridge existing. Spec §4.2 calls visual feedback
non-negotiable; this is the cheapest form of it that exists.

Everything else — analysis, validation, geometry, optimisation — is computed in
`nrc-mcp` against the saved file. The bridge is a window, not a second
implementation of anything.

## 2. Constraints that decided the design

Four, in priority order. Later ones lose.

1. **Zero core diffs.** One Makefile hunk, one new directory, nothing else. This is
   not modesty; it is the only version of this plugin with a chance of being merged,
   and the only version that keeps working as a downstream patch if it is not.
2. **No new dependencies.** Nothing to vendor, nothing to `pkg-config`.
3. **Off unless asked, twice.** A build flag and a runtime preference.
4. **Feature completeness.** Last. Where §9.2 names something the public headers
   cannot do, the method is absent and the README says which header would have to
   change.

Constraint 1 removed five of the nineteen methods §9.2 lists. That is the design
working, not the design failing.

## 3. Consequences worth defending

### It is single-threaded, on the main thread

A socket server wants a thread. The scene graph cannot survive one: nothing in
Radiant is thread-safe, and `UndoableCommand` is a stack discipline over global
state.

So there is no thread. BSD sockets are registered with `QSocketNotifier`, which is
`QtCore`, so reads are delivered as main-thread event-loop callbacks — the same
context a menu command runs in. No locks, no marshalling, no lifetime questions
about who owns a `scene::Node`.

`QTcpServer` would have been tidier and is not available: the build `pkg-config`s
`Qt5Core`, `Qt5Gui`, `Qt5Widgets` and `Qt5Svg`, and **not `Qt5Network`**. Adding it
would have violated constraint 2 and forced a second Makefile hunk in the variables
block. Raw sockets plus `QSocketNotifier` need neither.

The one sharp edge that follows: `ScreenUpdates_Disable()` — which every map load
and save goes through — calls `process_gui()`, which calls
`QCoreApplication::processEvents()`. A notifier can therefore fire while the editor
is halfway through replacing the scene root. There is a re-entrancy guard, and
`GlobalSceneGraph().currentLayer() != 0` is checked as the map-is-open probe because
`root()` asserts. Both exist because of this single fact about the core.

### Object ids are positional and expire

The public interface has no persistent handle for a brush. Options were: raw node
pointers (a freed-and-reused address silently becomes a different object — a
mutating API must not have that), a plugin-side handle table (same hazard, plus a
cache that can dangle), or positional ids rebuilt per request.

Positional won. `"e3"` is the fourth entity in traversal order; `"e3.p7"` its eighth
primitive. The index is rebuilt from a graph walk on every request that needs it,
which costs one traversal and removes an entire class of bug.

Staleness is then handled explicitly rather than hidden: every reply carrying ids
also carries `revision`, the map's undo change counter, and any request may pin it.
A pinned request against a moved scene fails with `-32002` instead of editing the
wrong brush.

**A finding that surprised us and that clients must know:** these ids are *not*
`.map` entity indices. `CompiledGraph` stores instances in a
`std::map<PathConstReference, scene::Instance*>`, and the comparison chain ends at
`scene::Node::operator<`, which compares `this` against `&other`. Traversal order is
therefore *node allocation order*. It is stable while nothing is inserted or erased
and otherwise arbitrary. Anything correlating bridge state with file state must do
it by classname and keys.

### One line of JSON is one undo step

§9.2 lists `undo.begin(label)` and `undo.end`. `GlobalUndoSystem().start()` and
`finish()` are public, so they were implementable — and are not implemented.

An explicit begin/end pair spanning several requests has a failure mode with no
recovery: if the client dies in the middle, the editor is left with an open undo
operation and no way to know it should close it. JSON-RPC 2.0 already has batching,
so a batch array is the unit: everything in one received line that mutates runs
inside exactly one `UndoableCommand`. Same expressiveness, no half-open state, and
zero extra methods.

`undo.undo` is kept, and does precisely what the core's `Undo()` does —
`GlobalUndoSystem().undo()` then `SceneChangeNotify()`.

### The secret is not a preference

`MCPBridge_Enabled`, the port and the connection cap are preferences.
`NRC_MCPBRIDGE_SECRET` is an environment variable, because a preference would be
written to the settings file in plaintext, and a credential that survives to disk
by default is a worse bug than not having one.

The handshake is a bare first line, not an RPC method. That keeps the method surface
at what §9.2 lists and means an unauthenticated peer never reaches the dispatcher.

### JSON is hand-rolled although rapidjson is already in the tree

`libs/rapidjson/` exists — q3map2's `-json` uses it — and `-Ilibs` is already in the
plugin recipe, so `#include "rapidjson/document.h"` would have compiled.

`contrib/mcpbridge/json.h` is ~500 lines instead. Two reasons. It makes the plugin
reviewable as one self-contained directory, with no argument about whether a
`contrib/` plugin may reach into a library the compilers pulled in for themselves.
And the subset a JSON-RPC line needs is small: parse one document, build one
response. rapidjson's DOM, allocators and SAX layers are all cost with no return
here. The parser has a depth limit, rejects what JSON rejects (leading zeroes,
non-finite numbers, raw control characters in strings), and does not throw, because
the build is `-fno-exceptions -fno-rtti`.

## 4. What it deliberately does not do

Not "not yet" — these are decisions.

- **No geometry.** No CSG, no vertex editing, no texture fitting, no patch
  manipulation. All of it belongs in `nrc-core` against the file, where it can be
  differentially tested. The bridge's job is to say what is there and to move,
  create and delete whole objects.
- **No queries the file can answer.** No entity dumps, no shader lists, no leak
  reports. If a call could have been served from the saved `.map`, it should have
  been; the usage log exists partly to catch calls that shouldn't be here.
- **No arbitrary transform matrix.** §9.2 says `scene.transform(matrix)`.
  `SelectionSystem` offers `translateSelected`, `rotateSelected(Quaternion)` and
  `scaleSelected` — there is no general matrix entry point, and `Transformable`
  (the other route) is the same three operations. The method takes the three
  components. A 4×4 parameter would have implied a capability that does not exist.
- **No renders, no filters, no brush retexturing.** Not reachable without a core
  change. Each is documented in the README with the header that would have to move.
- **No preferences page.** `PreferencesDialog_addSettingsPage` is in
  `radiant/preferences.h`. The Plugins menu is the toggle.
- **No auto-start on install.** The socket opens only after someone chooses
  *Start listening* once, which is also what persists the preference.

## 5. Honest state of it

**Not compiled.** There is no Qt5 development environment on the machine this was
written on, and the host compiler is GCC 9, which cannot build this codebase at all
(`libs/generic/arrayrange.h` needs `<span>`, `libs/eclasslib.h` needs
`std::ranges`, and `libs/generic/callback.h` uses `return R{}` with `R = void`,
which GCC accepted only from 10 onward). Every signature the plugin binds to was
read out of the real headers, and `json.h` was compiled and unit-tested standalone
under `-std=c++2a -fno-exceptions -fno-rtti -Wall -Wextra`. Nothing else has been
through a compiler. Treat first-build warnings as expected.

**Two version sensitivities to watch.** `QSocketNotifier::activated` gained a second
overload in Qt 5.15 and the `int` one is deprecated there; the connect is
`static_cast`-disambiguated, which works on both but would need revisiting for Qt 6.
And the `-Ilibs` headers are not a stable interface — see §7.

**Over the line budget.** §10.2 targets under 900 added lines. This is about 1,770
lines of code (≈1,290 in `mcpbridge.cpp`, ≈460 in `json.h`, plus the header). §6
below is how that comes down; it comes down by removing methods, not by removing
comments.

## 6. Getting to a submittable size

§10.1's rule is the mechanism: **any method with zero real-session usage is cut
before submission.** `MCPBridge_LogCalls` and *Plugins → MCP Bridge → Log RPC usage*
exist to produce that evidence, and the counts are per-method precisely so the cut
is decided by data rather than by taste.

The expected order of removal, if the numbers agree — roughly 700 lines:

1. `scene.create_brush` (~80 lines). The heaviest single method and the most likely
   to be redundant: `nrc-core` writes brushes into the `.map` losslessly, and
   `map.reload` brings them in. Creating geometry through an RPC is convenient, not
   necessary.
2. `scene.create_entity` (~75 lines), for the same reason, and it only handles point
   entities anyway.
3. `scene.set_keys` (~60 lines) if entity edits turn out to always be file edits.
4. `camera.set` and `undo.undo` (~40 lines) if nothing uses them.

What should survive under any plausible usage: `scene.stats`, `scene.selection`,
`scene.select`, `camera.get`, `map.path`, `map.save`, `map.reload`. That is the
irreducible core — "what is the human looking at" and "we agree about the file on
disk" — and it is around 500 lines. Ship the proven minimum.

## 7. What breaks it, and how we find out early

An out-of-tree plugin dies silently when an interface it binds to changes. §10.1's
interface-hash watcher should cover exactly this list, because these are the
declarations the bridge would fail to compile or, worse, misbehave against:

| Header | Depended on |
| --- | --- |
| `include/qerplugin.h` | `_QERFuncTable_1`: `getMapName`, `getMapsPath`, `getGridSize`, `getMapWorldEntity`, `TextureBrowser_getSelectedShader`, `m_pfnMessageBox` |
| `include/iplugin.h` | `_QERPluginTable`, all five entry points |
| `include/iscenegraph.h` | `Graph::root`, `find`, `traverse`, `currentLayer`, `sceneChanged` |
| `include/iselection.h` | `countSelected`, `countSelectedStuff`, `setSelectedAll`, `getBoundsSelected`, `translateSelected`, `rotateSelected`, `scaleSelected` |
| `include/iundo.h` | `UndoSystem::start`, `finish`, `undo`, `size`; `UndoableCommand` |
| `include/ientity.h` | `Entity` key access and `Visitor`; `EntityCreator::createEntity` |
| `include/ieclass.h` | `EntityClassManager::findOrInsert` |
| `include/ibrush.h` | `BrushCreator::createBrush`, `Brush_addFace`; `_QERFaceData` |
| `include/icamera.h` | `_QERCameraTable::m_pfnGetCamera`, `m_pfnSetCamera` |
| `include/ireference.h` | `ReferenceCache::capture`, `release`; `Resource::save`, `refresh` |
| `include/mapfile.h` | `MapFile::changes`, `saved` |
| `include/preferencesystem.h` | `registerPreference` |
| `include/modulesystem.h` | `GlobalModule`, `GlobalModuleRef`, `SingletonModule`, `initialiseModule` |

Three more things the watcher should treat as interface, even though they are not in
`include/`:

- **`libs/scenelib.h`** — `Node_isEntity`/`isPrimitive`/`isPatch`,
  `Instance_setSelected`, `Instance_isSelected`, `Entity_setSelected`,
  `Instance_getTransformable`, `Path_deleteTop`, `NodeSmartReference`. Every
  `contrib/` plugin binds to these; the recipe puts `-Ilibs` on the command line.
  They are nonetheless not a promised interface, so a change here is a real risk.
- **`libs/eclasslib.h`** — `EntityClass::fixedsize`, which decides whether
  `scene.create_entity` accepts a class. `ieclass.h` only forward-declares
  `EntityClass`.
- **Behaviour, not signatures.** Three of the bridge's load-bearing assumptions are
  facts about implementations that could change without any header moving:
  `MapResource::save()` calling the change tracker (so `map.save` updates the title),
  `Resource::refresh()` reloading only when the disk timestamp moved (so `map.reload`
  is conditional), and `ScreenUpdates_Disable()` pumping the event loop (so the
  re-entrancy guard is needed). These deserve a comment in the watcher's output,
  since no hash will catch them.

## 8. How to offer it upstream

Straight from §9.3, unchanged, because it is right.

**Phase 1 — earn the argument.** Ship phases 1–4 of the project as a purely
external tool. Use it on real maps. The case for the plugin is a workflow that
demonstrably works and demonstrably wants live editor state; it is not a design
document.

**Phase 2 — open a Discussion, not a PR.** Describe the plugin, its scope, the
zero-core-diff constraint, and ask whether it is wanted in-tree at all. A PR that
arrives unannounced asks a maintainer to review a decision and an implementation at
once, and they will quite reasonably decline both.

**Phase 3 — if yes: one PR, one plugin.** Off by default. A short `docs/` note and
a demo. Only the methods with real usage behind them. No core refactors, no build
system cleanups, nothing "while I was in there". `uncrustify.cfg` clean, and green
on all three CI platforms the repo already builds — the continuous-rebase branch
from §10.1 exists so that this is never in question.

**Phase 4 — if no: keep it downstream.** A single directory plus a one-block
Makefile change is about the cheapest patch to carry, and §7's watcher is what keeps
it carryable. Nothing in this project blocks on the answer: the external MCP works
either way, and only live-editor sync is lost.

Garux is an active maintainer with a specific vision for this fork. Self-contained,
opt-in and touching nothing is the only shape worth proposing.
