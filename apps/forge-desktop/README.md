# Forge Agents (desktop)

A desktop app for the chat agents organizations share as **chat pages**
(`/chat/<cp_ token>` in the web console). People add an agent by pasting its
page's link, and chat with it in a window of its own. The agent looks and
behaves as its page does in a browser, because the window draws the same
screen.

It's a [Tauri 2](https://v2.tauri.app) app: a small Rust shell
(`src-tauri/`) around the system's web view (WebKit on macOS and Linux,
WebView2 on Windows). The screen is the web console's own chat page code,
built separately:

| Piece | Where |
|---|---|
| The Rust shell: window, links in and out, kept data | `apps/forge-desktop/src-tauri/` |
| The screen: frame, sidebar of agents and conversations, the agent picker, offered-agent dialog, sign-in, kept agents | `apps/forge-web/src/desktop/`, `apps/forge-web/desktop.html` |
| Its build | `apps/forge-web/vite.desktop.config.ts` → `apps/forge-web/dist-desktop/` |
| The chat page it shows | `apps/forge-web/src/features/agents/components/chat-page.tsx` (`ChatPage` with `runtime`) |

The screen is a second build of forge-web rather than an npm project of its
own. Both use one `node_modules`, so the chat code isn't copied, and React
isn't bundled twice.

## How it talks to agents

The app signs in as a person, and offers the published agents of the
organizations they're a member of. **Add agents** lists them, grouped by
organization, each with an Add button: `GET {server}/me/chat-agents`. Each
agent added runs as the person through the hosted runtime
(`{server}/runtime`, ADK's run API): their conversations are under their
own user ID (`GET /me`'s `subject`), with their token on every call. The
runtime decides whether they may run it, as it does for the console.

- **Signing in (for now).** Single sign-on will hand the app a token, sent
  as `Authorization: Bearer <token>`. Until it does, a development token
  stands in: `VITE_DESKTOP_API_TOKEN`. `make desktop` (the dev server) uses
  the console's own, `VITE_API_TOKEN` from `make web-token`, which signs in
  as the seeded site administrator. A build takes only a token it's given,
  so no installer carries a person's token by accident. Everything about
  signing in is in `apps/forge-web/src/desktop/session.ts`, the one place
  single sign-on will change. Your initials at the foot of the rail say who's
  signed in.
- **The server.** Agents come from this build's server: `VITE_DESKTOP_API_URL`
  (`make desktop`: `make admin`'s, `API_PROXY_TARGET` + `/api/v1`). Nobody
  types a server or a link.
- **Chat pages too.** A chat page's "Open in the desktop app" link still adds
  that page's agent, which runs as an anonymous visitor through the page's
  link (`{server}/runtime/pages/{token}`), its conversations under a random
  `visitor_…` ID. The sidebar's hover title says which kind an agent is.
- **CORS.** The server must allow the app's origins in
  `FORGE_ADMIN_CORS_ORIGINS`:
  - `tauri://localhost` (macOS, Linux)
  - `http://tauri.localhost` (Windows)
  - `http://localhost:5195` (the dev server)

  `apps/forge-admin-api/.env.example` has them; restart the admin API after
  changing them.

The added agents and the one open are kept in `agents.json` in the app's
data folder (Tauri's store plugin). On macOS that's
`~/Library/Application Support/dev.forge.agents/`. Visitor IDs and each
agent's theme choice stay in the web view's storage.

Links in answers open in the person's browser. The window never navigates
away from its own screen (`src/main.rs`).

**The window.** A quiet frame around one panel, after Codex's desktop app:

- **Title strip (macOS):** the screen draws under the system's title bar
  (`titleBarStyle: Overlay`, `hiddenTitle`). The traffic lights sit at the
  start of a 40px strip (`trafficLightPosition` x 16, y 22: their centre
  sits about 2.5pt above y, so they line up with the sidebar's show/hide
  button beside them; measured from a screenshot).
- **Icon rail:** Chats, and Add an agent. On Windows and Linux, which keep
  their own title bar, the sidebar button sits at the top of the rail.
- **The panel:** the sidebar and the open agent's chat.
  - The sidebar starts with New chat (a new conversation with the open agent).
  - **Agents:** every added agent, with its latest conversations under it
    (three, then Show more).
  - **Recents:** the latest conversations across all agents.
  - Each row's "…" menu removes the agent or deletes the conversation.
  - Lists are read from each page's runtime as this app's visitor
    (`src/desktop/conversations.ts`), and read again as the open chat starts
    a conversation or finishes a reply.
- **The chat:** the chat page itself, without its own history
  (`ChatPage history={false}`). The sidebar opens conversations through the
  chat's runtime (`onRuntime`), including another agent's.

The window moves by its frame (strip, rail, sidebar header), the chat's top
bar and the empty welcome screen, and a double click zooms it
(`data-tauri-drag-region="deep"`, `src/desktop/window-chrome.ts`). To see the
macOS layout in a browser, open
http://localhost:5195/desktop.html?titlebar=overlay.

## Its links: "Open in the desktop app"

The app registers the `forge-agents` link scheme with the system
(`tauri.conf.json` → `plugins.deep-link`):

```text
forge-agents://add?server=https%3A%2F%2Fforge.example.com%2Fapi%2Fv1&token=cp_…
```

- **On the web:** a chat page opens one from its "Open in the desktop app"
  button (the computer icon beside the theme switch, or over the corner
  without a top bar). The button shows only when the page's **Offer the
  desktop app** option is on: `desktop_link` in its settings, off by default,
  in What it shows. The link names the console's API, made absolute.
  `chatPageDesktopLink` in `features/agents/lib/chat-page.ts` builds it, and
  `features/agents/lib/desktop-link.ts` holds the format both ends read.
- **In the app:** the link reaches the app whether it's running or not. On
  Windows and Linux a second start hands its link to the running app
  (single-instance plugin) and stops. The shell brings the window forward.
  The screen (`src/desktop/deep-links.ts`) takes only links of the right
  shape: an http(s) server with no credentials or query, and a token as the
  API makes them.
- **Asking first.** An agent the app already has just opens. Otherwise the
  app shows the agent's name and description and the server it's on, and
  the person adds it or doesn't. Any website can open such a link, so it's
  never added on its own.
- **Not in the link:** the visitor ID. It's the key to a visitor's
  conversations, so the app's conversations stay separate from the
  browser's.

Testing on macOS: the system learns the scheme from an installed or
registered `.app`, not from `tauri dev`. Build one
(`npx tauri build --debug --bundles app`), register it with
`lsregister -f "src-tauri/target/debug/bundle/macos/Forge Agents.app"`
(in `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/`),
then `open 'forge-agents://add?server=…&token=cp_…'`. On Windows and Linux,
`tauri dev` registers the scheme itself.

## Run it

You need Rust (`rustup`), the web console's dependencies (`make
web-install`), and the admin API running (`make admin`).

```bash
make desktop-install   # the Tauri CLI, once
make desktop           # opens the window, on the screen's dev server (5195)
```

In development the app talks to `make admin`'s server (`API_PROXY_TARGET`,
default `http://localhost:8101`, + `/api/v1`) and signs in with the
console's development token (`make web-token` first, if it has none or it
expired). A build signs in only with a token it's given:

```bash
VITE_DESKTOP_API_TOKEN=<a development token> make desktop-build
```

```bash
VITE_DESKTOP_API_URL=https://forge.example.com/api/v1 make desktop-build
```

The installers land in `src-tauri/target/release/bundle/`: `.app` and `.dmg`
on macOS, `.msi` and `.exe` on Windows, `.deb`, `.rpm` and `.AppImage` on
Linux. Each platform builds its own; there's no cross-compiling. For
installers people can open without warnings:

- **macOS:** Developer ID signing and notarization.
- **Windows:** a code signing certificate.

Neither is set up yet.

`make desktop-check` runs `cargo fmt --check`, clippy and the screen's
typecheck.

## Changing it

- **Chat page changes** (`features/agents`, `components/forge/assistant`)
  show up in both places. Check the desktop screen too
  (`npm run dev:desktop` in `apps/forge-web`, then
  http://localhost:5195/desktop.html in a browser, where the agents are kept
  in `localStorage` instead).
- **Icons:** edit `icon.svg`, then run `npm run icons`.
- **Shell permissions:** what the screen may ask of the shell is in
  `src-tauri/capabilities/default.json`.
- **Content Security Policy:** none for now (`app.security.csp`), the same as
  the web console. The servers it talks to are only known at run time.

## Not done yet

- Notifications when a long reply or workflow run finishes.
- A quick-ask window on a keyboard shortcut, and a tray icon.
- Automatic updates.
- Signing the installers.
- Single sign-on: the app takes a development token for now (`session.ts`),
  and the admin API checks HS256 tokens it shares a secret with; an SSO
  provider's tokens need their own check.
