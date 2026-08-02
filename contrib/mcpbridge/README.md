# mcpbridge

A NetRadiant-custom plugin that exposes live editor state over newline-delimited
**JSON-RPC 2.0** on `127.0.0.1`, so an external process — a build script, a test
harness, an LLM agent — can read the open map and change it while a human watches.

It is **off by default, twice over**: it is not built unless you ask for it, and it
does not open a socket unless you switch it on at runtime.

- 3 sources plus a header-only JSON reader/writer. No new third-party dependency.
- Binds only to `include/` and `libs/` interfaces. **Zero changes to the core.**
- Every mutating request runs inside one `UndoableCommand`, so Ctrl+Z undoes an
  agent's whole operation rather than its last brush.

---

## Security: read this before enabling it

**While the bridge is listening, any process running as you can read and rewrite
the open map, move the camera, and save over the file on disk.**

Loopback is not an authorisation boundary. A local socket is reachable by every
process on the machine, including browser extensions, IDE plugins and anything that
happened to get onto your box.

Mitigations, in order of how much they buy you:

1. **Leave it off** unless you are actively driving the editor from a tool.
2. **Set a shared secret.** Export `NRC_MCPBRIDGE_SECRET` before starting Radiant.
   The first line a client sends must then be exactly that secret, or the
   connection is dropped. Without it the bridge logs a warning on every start.
   The secret is deliberately read from the environment and never written to the
   settings file.
3. The bridge binds `INADDR_LOOPBACK` explicitly and never sets `SO_REUSEADDR`,
   so it cannot be reached off-box and cannot silently take over a port some other
   process is already holding.
4. Connections are capped (`MCPBridge_MaxConnections`, default 4) and a client
   that sends a line longer than 1 MiB is disconnected.

What it does **not** do: encryption, per-method permissions, rate limiting, or any
attempt to distinguish one local caller from another beyond the shared secret.

---

## Building

The plugin is not in the default target. Apply the Makefile hunk below and build
with `MCPBRIDGE=yes`:

```sh
make MCPBRIDGE=yes binaries-radiant-plugins
```

`-DMCPBRIDGE_ENABLED` is what actually permits the socket to open. If the file is
compiled without it, the plugin still loads and still appears in the Plugins menu,
but `start()` refuses and says why. That redundancy is on purpose: "off by default"
should not depend on one line of a Makefile.

### The Makefile hunk

This repository keeps `netradiant-custom` vendored and unmodified, so the change is
recorded here instead of applied. Insert it after the `sunplug` recipe
(`Makefile:1188`, immediately before `terrain_generator`):

```diff
--- a/Makefile
+++ b/Makefile
@@ -1188,6 +1188,15 @@
 $(INSTALLDIR)/plugins/sunplug.$(DLL): \
 	contrib/sunplug/sunplug.o \

+# opt-in: exposes editor control on a loopback socket, see contrib/mcpbridge/README.md
+MCPBRIDGE ?= no
+ifeq ($(MCPBRIDGE),yes)
+binaries-radiant-plugins: $(INSTALLDIR)/plugins/mcpbridge.$(DLL)
+$(INSTALLDIR)/plugins/mcpbridge.$(DLL): LIBS_EXTRA := $(LIBS_GLIB) $(LIBS_QTWIDGETS)
+$(INSTALLDIR)/plugins/mcpbridge.$(DLL): CPPFLAGS_EXTRA := $(CPPFLAGS_GLIB) $(CPPFLAGS_QTWIDGETS) -Ilibs -Iinclude -DMCPBRIDGE_ENABLED
+$(INSTALLDIR)/plugins/mcpbridge.$(DLL): \
+	contrib/mcpbridge/mcpbridge.o \
+
+endif
+
 $(INSTALLDIR)/plugins/terrain_generator.$(DLL): LIBS_EXTRA := $(LIBS_GLIB) $(LIBS_QTWIDGETS)
```

Notes on that hunk:

- It appends to `binaries-radiant-plugins` rather than editing the list at
  `Makefile:446`, which keeps the whole change to one contiguous block and means
  the default build is byte-identical to before.
- `LIBS_QTWIDGETS` is `pkg-config Qt5Widgets --libs`, which resolves `Qt5Gui` and
  `Qt5Core` transitively. **The bridge needs no Qt module the editor does not
  already link.** In particular it does not use `QtNetwork`, which this build does
  not have: it uses BSD sockets plus `QSocketNotifier` from `QtCore`.
- `CPPFLAGS_EXTRA` is target-specific and therefore applies to the `.o`
  prerequisite, which is how every other plugin gets `-Ilibs -Iinclude`.

---

## Running

1. **Plugins → MCP Bridge → Start listening.** This sets the
   `MCPBridge_Enabled` preference, so the choice persists; on the next start the
   socket opens by itself once the event loop is up.
2. **Plugins → MCP Bridge → Stop listening** clears the preference and closes
   everything.
3. **About...** shows the port and whether a secret is in force.
4. **Log RPC usage** prints how many times each method was called this session.
   That number is the input to the pruning rule in the project spec (§10.1): a
   method nobody calls does not get submitted upstream.

Preferences, all stored in the normal Radiant settings file:

| Preference | Default | Meaning |
| --- | --- | --- |
| `MCPBridge_Enabled` | `false` | required for the socket to open |
| `MCPBridge_Port` | `27700` | loopback port |
| `MCPBridge_MaxConnections` | `4` | concurrent clients |
| `MCPBridge_LogCalls` | `true` | log every method name to the console |

There is no preferences *page*: adding one needs `PreferencesDialog_addSettingsPage`
from `radiant/preferences.h`, which is core. The menu is the toggle.

### Talking to it

```sh
export NRC_MCPBRIDGE_SECRET=$(openssl rand -hex 16)
# ... start radiant, enable the bridge ...
{ printf '%s\n' "$NRC_MCPBRIDGE_SECRET"
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"scene.stats"}'
  sleep 1
} | nc 127.0.0.1 27700
```

One JSON document per line, one response line per request. Batches work and are the
supported way to group work into a single undo step:

```json
[{"jsonrpc":"2.0","id":1,"method":"scene.select","params":{"classname":"info_ut_spawn"}},
 {"jsonrpc":"2.0","id":2,"method":"scene.transform","params":{"translate":[0,0,16]}}]
```

Everything in one line that mutates is wrapped in **one** `UndoableCommand`, named
`MCPBridge.batch` (or `MCPBridge.<method>` for a single call). Requests with no
`id` are notifications and get no reply.

---

## Method surface

`revision` is the map's undo change counter. Every reply that contains ids also
reports the revision they were built at; any request may pass `"revision": n` to
be rejected with `-32002` if the scene has moved on since. Ids are positional —
`"e3"` is an entity, `"e3.p7"` a primitive of it — and are rebuilt per request.

> **Ids are not `.map` indices.** The scene graph's instance map is keyed on node
> address, so traversal order is allocation order, not file order. Correlate by
> classname and keys, not by number.

| Method | Params | Result |
| --- | --- | --- |
| `scene.stats` | — | counts, world bounds, map name, saved flag, grid, revision |
| `scene.select` | `ids[]` \| `classname` \| `key`+`value` \| `all` \| `none`, plus `add` | selected ids |
| `scene.selection` | — | per-item id, type, bounds; entity classname and all keys |
| `scene.transform` | `translate[3]`, `rotate[3]` (euler degrees), `scale[3]` | new selection bounds |
| `scene.create_brush` | `planes[]`: `points[3][3]`, `shader`, `texdef`, `contents`, `flags`, `value` | new brush id |
| `scene.create_entity` | `classname`, `origin[3]`, `keys{}` | new entity id |
| `scene.set_keys` | `id`, `keys{}` (`null` erases) | the entity's resulting keys |
| `scene.delete` | `ids[]` | number deleted |
| `camera.get` | — | `origin`, `angles` |
| `camera.set` | `origin`, `angles` | the applied values |
| `map.path` | — | path, unnamed flag, saved flag, maps path |
| `map.save` | — | whether anything was written |
| `map.reload` | `discard_unsaved: true` | whether the graph was replaced |
| `undo.undo` | — | new revision and undo depth |

Error codes: the JSON-RPC reserved set, plus `-32000` no map open, `-32001` unknown
id, `-32002` stale revision, `-32003` reachable only through a core change.

### Shapes worth knowing

- **Shader names include the `textures/` prefix.** The `.map` writer is what strips
  it. `scene.create_brush` defaults a face's shader to whatever the texture browser
  has selected, which is what a hand-drawn brush would get.
- **A brush needs at least four planes**, given as three points each — the same
  form the `.map` format uses. Fewer produces the editor's "phantom brush".
- **`scene.create_entity` handles point entities only.** An unknown classname
  becomes one automatically (`EntityClass_Create_Default` with `has_brushes` false
  returns a fixed-size class), but a known group class such as `func_door` is
  refused; see below.
- **`map.reload` is a no-op unless the file on disk changed**, which is exactly the
  case that matters when an external tool rewrote the `.map`. It discards unsaved
  editor state and clears the undo stack, so it insists on `discard_unsaved`.

---

## What is missing, and why

Five methods from the designed surface are **not implemented**, because none of them
is reachable through the public interfaces and the zero-core-diff constraint wins.
Each is stated here rather than faked, because a method that silently does half of
what its name says is worse than one that is absent.

### `scene.set_texture` — needs a core change

`include/ibrush.h` does define `IBrush::addPlane` and a full `IBrushFace` with
`SetShader`/`SetTexdef`… inside `#if 0`. What is live is `BrushCreator`, and it
offers exactly three things: `createBrush`, `Brush_forEachFace` (which hands out
`const _QERFaceData&`, read-only) and `Brush_addFace`. There is **no way to change
a face on an existing brush**. The core does it with
`Scene_BrushSetShader_Selected` in `radiant/brushmanip.h`.

`PatchCreator::Patch_setShader` *is* public, so patch retexturing alone would work.
It is not shipped: a `set_texture` that handles patches and quietly ignores brushes
is a trap. Retexturing belongs in the external tool operating on the saved `.map`
until `ibrush.h` grows a setter.

### `view.render` — needs a core change

There is no way for a plugin to obtain the camera or 2D view. `include/igl.h` is a
table of GL entry points, not a renderer or a context; `CamWnd` and `XYWnd` live in
`radiant/`. `_QERFuncTable_1` offers `Camera_getOrigin` and
`XYWindow_windowToWorld` but nothing that draws. A screenshot RPC needs a core hook.

### `filter.set(name, bool)` — needs a core change

`include/ifilter.h`'s `FilterSystem` is `addFilter(Filter&, int mask)`,
`registerFilterable` and `unregisterFilterable`. There is no way to enumerate
filters, look one up by name, or read its state; the `EXCLUDE_*` bits are the
vocabulary and the core owns the mapping from menu items to `Filter` objects. A
name-keyed toggle does not exist to call.

### `undo.begin(label)` / `undo.end` — replaced, not blocked

`GlobalUndoSystem().start()` and `finish()` are public, so this *could* be
implemented. It is deliberately not. A begin/end pair that straddles RPCs leaves
the editor's undo system half-open if the client crashes between them, and the
editor has no way to notice. JSON-RPC batching already expresses the same intent
with no failure mode: one line, one `UndoableCommand`, one Ctrl+Z.

### `camera.set`'s `fov` — needs a core change

`_QERCameraTable` carries origin and angles only. `CameraView::setFieldOfView`
exists in `include/icamera.h`, but obtaining a `CameraView` requires the
`CameraModel` the core keeps to itself. Passing `fov` returns `-32003` rather than
being ignored.

---

## Implementation notes for reviewers

- **No threads.** Sockets are serviced by `QSocketNotifier` on the main thread, so
  every scene access happens where the editor expects it. The listening socket is
  non-blocking; accepted sockets are blocking with 5-second timeouts, which is far
  less code than an outgoing-buffer state machine and bounds the worst case.
- **`ScreenUpdates_Disable()` pumps the Qt event loop** during map load and save, so
  a notifier can fire in the middle of an editor operation. A re-entrancy guard
  disables the notifier and defers rather than re-entering.
- **`GlobalSceneGraph().currentLayer() != 0`** is the map-is-open probe.
  `iscenegraph.h` documents it as returning 0 when no root is inserted, and it is
  the only check in the public interface that does not assert. It matters because
  of the previous point.
- **`map.save` goes through `ireference.h`.** The current map's resource is cached
  under its own path, so `GlobalReferenceCache().capture(getMapName())` returns that
  same resource; `Resource::save()` writes it and marks the change tracker saved,
  which fires the core's `MapChanged()` and updates the window title. That is what
  `Map_Save()` does, without needing `Map_Save()`.
- **`classname` is rejected by `scene.set_keys`.** Changing it means replacing the
  node, not editing a key — `ientity.h`'s own `EntityCopyingVisitor` skips the key
  for the same reason.
