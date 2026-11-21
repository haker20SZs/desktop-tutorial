#!/usr/bin/env python3
"""Apply small, checked native Rust integrations to the pinned yamusic-rs source."""

from pathlib import Path
import sys

def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"{label}: expected exactly one patch anchor, found {count}. "
            "The upstream source changed; review the patch instead of guessing."
        )
    return source.replace(old, new, 1)

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_patches.py <yamusic-rs-checkout>")

    root = Path(sys.argv[1]).resolve()
    api_path = root / "src" / "api.rs"
    tray_path = root / "src" / "tray.rs"
    if not api_path.is_file() or not tray_path.is_file():
        raise SystemExit(f"not a yamusic-rs source tree: {root}")

    api = api_path.read_text(encoding="utf-8")
    original_send = '''    fn send_text(&self, rb: RequestBuilder) -> Res<(reqwest::StatusCode, String)> {
        let resp = rb.send().map_err(|e| format!("сеть: {}", crate::config::chain(&e)))?;
        let status = resp.status();
        let text = resp.text().map_err(|e| format!("сеть: {}", crate::config::chain(&e)))?;
        Ok((status, text))
    }
'''
    patched_send = '''    fn send_text(&self, rb: RequestBuilder) -> Res<(reqwest::StatusCode, String)> {
        // Retry only idempotent requests. Never replay POST/PUT mutations after an
        // ambiguous transport failure: the server may already have applied them.
        let first = rb.build().map_err(|e| format!("сеть: {}", crate::config::chain(&e)))?;
        let retry_safe = matches!(*first.method(), Method::GET | Method::HEAD);
        let mut pending = Some(first);

        for attempt in 0..3 {
            let current = pending.take().ok_or_else(|| "повтор сетевого запроса невозможен".to_string())?;
            let retry = current.try_clone();
            match self.http.execute(current) {
                Ok(resp) => {
                    let status = resp.status();
                    let retry_after = resp
                        .headers()
                        .get(reqwest::header::RETRY_AFTER)
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| v.parse::<u64>().ok())
                        .map(|seconds| Duration::from_secs(seconds.min(8)));

                    let text = match resp.text() {
                        Ok(text) => text,
                        Err(e) if retry_safe && attempt < 2 => {
                            if let Some(next) = retry {
                                pending = Some(next);
                                std::thread::sleep(Duration::from_millis(300 * (1_u64 << attempt)));
                                continue;
                            }
                            return Err(format!("сеть: {}", crate::config::chain(&e)));
                        }
                        Err(e) => return Err(format!("сеть: {}", crate::config::chain(&e))),
                    };

                    let transient = matches!(status.as_u16(), 408 | 425 | 429 | 500 | 502 | 503 | 504);
                    if retry_safe && transient && attempt < 2 {
                        if let Some(next) = retry {
                            pending = Some(next);
                            std::thread::sleep(
                                retry_after.unwrap_or_else(|| Duration::from_millis(300 * (1_u64 << attempt))),
                            );
                            continue;
                        }
                    }
                    return Ok((status, text));
                }
                Err(e) if retry_safe && attempt < 2 => {
                    if let Some(next) = retry {
                        pending = Some(next);
                        std::thread::sleep(Duration::from_millis(300 * (1_u64 << attempt)));
                        continue;
                    }
                    return Err(format!("сеть: {}", crate::config::chain(&e)));
                }
                Err(e) => return Err(format!("сеть: {}", crate::config::chain(&e))),
            }
        }
        Err("сеть: запрос не удался после повторных попыток".into())
    }
'''
    api_new = replace_once(api, original_send, patched_send, "API retry")

    tray = tray_path.read_text(encoding="utf-8")
    original_keys = '''const MEDIA_KEYS: [(i32, u16); 3] = [(1, VK_MEDIA_PLAY_PAUSE), (2, VK_MEDIA_NEXT_TRACK), (3, VK_MEDIA_PREV_TRACK)];
'''
    new_keys = '''const MEDIA_KEYS: [(i32, u16); 3] = [(1, VK_MEDIA_PLAY_PAUSE), (2, VK_MEDIA_NEXT_TRACK), (3, VK_MEDIA_PREV_TRACK)];
// PulseSync-style global shortcuts, handled by the existing tray window/message loop.
const GLOBAL_KEYS: [(i32, u32, u32); 3] = [
    (101, 0x0001 | 0x0002, 0x20), // Ctrl+Alt+Space: play/pause
    (102, 0x0001 | 0x0002, 0x25), // Ctrl+Alt+Left: previous
    (103, 0x0001 | 0x0002, 0x27), // Ctrl+Alt+Right: next
];
'''
    tray_new = replace_once(tray, original_keys, new_keys, "global hotkey definitions")

    old_register = '''        for (id, vk) in MEDIA_KEYS {
            RegisterHotKey(hwnd, id, 0, vk as u32);
        }
'''
    new_register = '''        for (id, vk) in MEDIA_KEYS {
            RegisterHotKey(hwnd, id, 0, vk as u32);
        }
        for (id, modifiers, vk) in GLOBAL_KEYS {
            // If another program owns a combination, Windows simply rejects this binding.
            RegisterHotKey(hwnd, id, modifiers, vk);
        }
'''
    tray_new = replace_once(tray_new, old_register, new_register, "global hotkey registration")

    old_unregister = '''        for (id, _) in MEDIA_KEYS {
            UnregisterHotKey(t.hwnd, id);
        }
'''
    new_unregister = '''        for (id, _) in MEDIA_KEYS {
            UnregisterHotKey(t.hwnd, id);
        }
        for (id, _, _) in GLOBAL_KEYS {
            UnregisterHotKey(t.hwnd, id);
        }
'''
    tray_new = replace_once(tray_new, old_unregister, new_unregister, "global hotkey cleanup")

    old_dispatch = '''        WM_HOTKEY => {
            match wp {
                1 => emit(Event::PlayPause),
                2 => emit(Event::Next),
                3 => emit(Event::Prev),
                _ => {}
            }
            0
        }
'''
    new_dispatch = '''        WM_HOTKEY => {
            match wp {
                1 | 101 => emit(Event::PlayPause),
                2 => emit(Event::Next),
                3 => emit(Event::Prev),
                102 => emit(Event::Prev),
                103 => emit(Event::Next),
                _ => {}
            }
            0
        }
'''
    tray_new = replace_once(tray_new, old_dispatch, new_dispatch, "global hotkey dispatch")

    api_path.write_text(api_new, encoding="utf-8", newline="\n")
    tray_path.write_text(tray_new, encoding="utf-8", newline="\n")
    print("Applied native PulseSync-style hotkeys and safe GET/HEAD retries.")

if __name__ == "__main__":
    main()
