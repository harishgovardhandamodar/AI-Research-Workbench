# Focus view — simplified session experimentation

The **🎯 Focus** tab (next to 💬 Chat) is an optional simplified workbench:
one screen per session with five tabs, reusing the same backend as the full
views — no separate APIs, no duplicated state.

## The five tabs

| Tab | Contents | Backend used |
|---|---|---|
| 💬 Chat | Last 50 messages + send box | `GET messages`, chat socket via `sendChat()` |
| 🧪 Code | Run picker + full per-tool executed code | `GET runs`, `GET runs/{id}?include_code=true` |
| 🌲 Branches | Chronological parent-linked run list; click a node to load its code | `GET experiments/graph` |
| 📊 Tracking | Last 30 runs (status, experiment, metrics) | `GET experiments` + `GET runs` |
| 🖥 Remote | Host picker, code box, require-GPU, Run | `GET /api/remote/hosts`, `POST /api/remote/run` |

Cross-tab jumps keep the flow in one click: a branch node opens the Code tab
with that run loaded; **Send to remote ↓** copies the code into the Remote
tab and opens it. Remote runs are recorded as `kind="remote"` and appear in
Tracking after the run finishes.

## Session focus

The toolbar session selector follows the active session and switches the
whole view (same `switchProject()` path as the main session menu). **Refresh**
reloads all five tabs on demand — there are no background pollers. After
sending chat, the list reloads immediately plus once more after a few seconds
so the reply lands without a manual refresh.

## Top bar and layout notes

- The view switcher is a horizontally scrolling tab strip: it never squeezes,
  shows edge fades while overflowed, and auto-scrolls the active tab into
  view. Below 1100px tabs collapse to icons.
- The **model picker** (🧠) and **session switch** are compact overlay
  controls — full lists live in popover menus, freeing tab-strip room.
- Below 1100px the side rail becomes an overlay drawer (same toggle).
- Chat messages cap at ~1000px centered for readability.
- All topbar controls read at a uniform 12px with matched heights.
