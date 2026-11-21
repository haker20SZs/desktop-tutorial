# Yandex Music RS + PulseSync (single EXE)

This repository builds one Windows x64 executable from the Rust client
[SpolzerMod/yamusic-rs](https://github.com/SpolzerMod/yamusic-rs), with a small
set of PulseSync-style features ported into the native Rust application.

## What is integrated in this first build

- **One native executable:** the build starts from the upstream Rust/Slint client; it does not launch or bundle a second Electron app.
- **Configurable global shortcuts:** default Ctrl+Alt+Space = play/pause, Ctrl+Alt+Left = previous, Ctrl+Alt+Right = next. Edit `%APPDATA%\\yamusic-rs\\hotkeys.json` to change them; supported modifiers are CTRL, ALT, SHIFT, WIN, and keys include letters, digits, F1-F24, SPACE, arrows, ENTER, TAB, ESC, HOME, END, PAGEUP and PAGEDOWN. Each binding must include at least one modifier. Hardware media keys remain enabled.

Example `hotkeys.json`:

```json
{
  "play_pause": "CTRL+ALT+SPACE",
  "previous": "CTRL+ALT+LEFT",
  "next": "CTRL+ALT+RIGHT"
}
```

Create the file in `%APPDATA%\\yamusic-rs\\` and restart the app for changes to take effect.
- **Safer reconnects:** only idempotent GET/HEAD API requests are retried after transport failures and transient HTTP statuses (408, 425, 429, 500, 502, 503, 504), with a short exponential backoff and support for Retry-After. Login failures and write requests are not retried.
- Discord Rich Presence, tray, mini-player and media keys are already provided by yamusic-rs.

This is an initial native port, not a full transplant of PulseSync's Electron internals.
Features such as Yandex Station / Ynison remote control, Last.fm, WASAPI exclusive
output and user-configurable hotkey UI need separate Rust implementations and are
not represented as completed here.

## Build

Push to the `pulsesync-rust` branch or run the **Build Windows EXE** workflow.
GitHub Actions checks out the pinned upstream tag `v1.1.4`, applies
`integration/apply_patches.py`, runs Rust tests, and builds:

`yamusic-rs/target/release/yamusic-rs.exe`

The workflow uploads the executable as a GitHub Actions artifact. To publish a
GitHub Release, push a tag named `pulsesync-v0.1.0` (or a later version).

## Lyrics

The fullscreen lyrics view is available for the current track even when Yandex does not advertise lyrics for it. The client tries sources in this order:

1. Yandex Music's own lyrics endpoint (when the account and track are allowed to access it).
2. A local file in `%APPDATA%\\yamusic-rs\\lyrics\\` (Windows). Name it either `<track-id>.lrc` / `<track-id>.txt` or `<artist> - <title>.lrc` / `.txt`. LRC files retain timestamp synchronization; TXT files are shown as plain text.
3. LRCLIB's public API, using track title, artist and duration. Availability and matching depend on that external catalogue.

Local files are user-supplied. Use lyrics you have permission to store and display. No subscription checks, DRM, or Yandex access controls are bypassed.

## Important service and media limitations

The client still uses the user's own Yandex account and the service's access
decisions. It does not bypass Plus, DRM, account age/content restrictions, or
server-side track availability. It cannot reconstruct an original vocal track
from an already censored/edited audio master. Network retries can reduce the
impact of temporary outages; they cannot make unavailable Yandex servers respond.

## Licensing and attribution

The Rust application is based on yamusic-rs, distributed under GPL-3.0-only.
The original PulseSync-mod repository is MIT-licensed; this project ports selected
feature behavior rather than embedding the Electron patcher or its app bundle.
Keep upstream notices and license files with redistributed builds.
