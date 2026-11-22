#!/usr/bin/env python3
"""Apply small, checked native Rust integrations to the pinned yamusic-rs source."""

from pathlib import Path
import re
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


    # Replace fixed PulseSync-style shortcuts with user-configurable bindings.
    # Config is intentionally separate from config.json to avoid touching auth tokens.
    old_keys = '''// PulseSync-style global shortcuts, handled by the existing tray window/message loop.
const GLOBAL_KEYS: [(i32, u32, u32); 3] = [
    (101, 0x0001 | 0x0002, 0x20), // Ctrl+Alt+Space: play/pause
    (102, 0x0001 | 0x0002, 0x25), // Ctrl+Alt+Left: previous
    (103, 0x0001 | 0x0002, 0x27), // Ctrl+Alt+Right: next
];
'''
    new_keys = '''// User-configurable PulseSync-style global shortcuts. The separate hotkeys.json
// file keeps this feature independent from account credentials and other settings.
const GLOBAL_KEY_IDS: [i32; 3] = [101, 102, 103];

fn parse_hotkey(value: &str) -> Option<(u32, u32)> {
    let mut modifiers = 0_u32;
    let mut key = None;
    for part in value.split('+').map(|s| s.trim().to_ascii_uppercase()) {
        match part.as_str() {
            "CTRL" | "CONTROL" => modifiers |= 0x0002,
            "ALT" => modifiers |= 0x0001,
            "SHIFT" => modifiers |= 0x0004,
            "WIN" | "META" => modifiers |= 0x0008,
            "SPACE" => key = Some(0x20),
            "LEFT" => key = Some(0x25),
            "UP" => key = Some(0x26),
            "RIGHT" => key = Some(0x27),
            "DOWN" => key = Some(0x28),
            "ENTER" => key = Some(0x0D),
            "TAB" => key = Some(0x09),
            "ESC" | "ESCAPE" => key = Some(0x1B),
            "HOME" => key = Some(0x24),
            "END" => key = Some(0x23),
            "PAGEUP" => key = Some(0x21),
            "PAGEDOWN" => key = Some(0x22),
            s if s.len() == 1 && s.as_bytes()[0].is_ascii_alphanumeric() => key = Some(s.as_bytes()[0] as u32),
            s if s.starts_with('F') => {
                let n = s[1..].parse::<u32>().ok()?;
                if !(1..=24).contains(&n) { return None; }
                key = Some(0x70 + n - 1);
            }
            _ => return None,
        }
    }
    if modifiers == 0 { return None; }
    Some((modifiers, key?))
}

fn global_keys() -> [(i32, u32, u32); 3] {
    let defaults = [
        (101, 0x0001 | 0x0002, 0x20),
        (102, 0x0001 | 0x0002, 0x25),
        (103, 0x0001 | 0x0002, 0x27),
    ];
    let path = crate::config::dir().join("hotkeys.json");
    let settings = std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok());
    let Some(settings) = settings else { return defaults; };
    let names = ["play_pause", "previous", "next"];
    std::array::from_fn(|i| {
        settings.get(names[i]).and_then(|v| v.as_str())
            .and_then(parse_hotkey)
            .map(|(mods, key)| (defaults[i].0, mods, key))
            .unwrap_or(defaults[i])
    })
}
'''
    tray_new = replace_once(tray_new, old_keys, new_keys, "configurable hotkey parser")

    old_register = '''        for (id, modifiers, vk) in GLOBAL_KEYS {
            // If another program owns a combination, Windows simply rejects this binding.
            RegisterHotKey(hwnd, id, modifiers, vk);
        }
'''
    new_register = '''        for (id, modifiers, vk) in global_keys() {
            // If another program owns a combination, Windows simply rejects this binding.
            if RegisterHotKey(hwnd, id, modifiers, vk) == 0 {
                crate::config::log(format!("не удалось зарегистрировать глобальную клавишу {id}; проверьте hotkeys.json"));
            }
        }
'''
    tray_new = replace_once(tray_new, old_register, new_register, "configurable hotkey registration")

    old_unregister = '''        for (id, _, _) in GLOBAL_KEYS {
            UnregisterHotKey(t.hwnd, id);
        }
'''
    new_unregister = '''        for id in GLOBAL_KEY_IDS {
            UnregisterHotKey(t.hwnd, id);
        }
'''
    tray_new = replace_once(tray_new, old_unregister, new_unregister, "configurable hotkey cleanup")


    # Modernize the sidebar profile card.
    navbar_path = root / "ui" / "navbar.slint"
    navbar = navbar_path.read_text(encoding="utf-8")
    old_profile = '''// Профиль: аватар 34 в кольце 2px через 2px, имя и «Плюс».
    Rectangle {
        x: 15px;
        y: parent.height - 61px;
        width: 42px;
        height: 42px;
        border-radius: 21px;
        background: @linear-gradient(135deg, #ff5c4d 0%, #eb469f 35%, #8341ef 70%, #3f68f9 100%);
        Rectangle {
            width: 38px;
            height: 38px;
            border-radius: 19px;
            background: Theme.background-color-primary-enabled-basic;
        }
        Cover {
            width: 34px;
            height: 34px;
            round: true;
            source: App.avatar;
        }
    }
    // Клик по профилю открывает меню Яндекс ID.
    TouchArea {
        x: 15px;
        y: parent.height - 61px;
        width: parent.width - 19px;
        height: 42px;
        mouse-cursor: MouseCursor.pointer;
        clicked => { App.account-open = !App.account-open; }
    }
    if !c: Text {
        x: 69px;
        y: parent.height - 62px;
        width: parent.width - self.x - 4px;
        text: App.login;
        overflow: elide;
        font-size: Theme.font-size-label-m;
        font-weight: 500;
        color: Theme.controls-color-primary-text-enabled-variant;
    }
    if App.plus && !c: Rectangle {
        x: 69px;
        y: parent.height - 38px;
        width: 66px;
        height: 20px;
        border-radius: 10px;
        background: @linear-gradient(90deg, #ff5c4d 0%, #eb469f 25%, #8341ef 72%, #4f4cf6 100%);
        Image { x: 4px; y: 3px; width: 14px; height: 14px; source: Icons.plus-badge; colorize: #fff; }
        Text { x: 21px; text: T.t("Плюс"); color: #fff; font-size: Theme.font-size-label-m; font-weight: 500; }
    }
'''
    new_profile = '''// Аккуратная карточка профиля с аватаром, подписью и отдельным бейджем Plus.
    Rectangle {
        x: 10px;
        y: parent.height - 58px;
        width: parent.width - 20px;
        height: 48px;
        border-radius: 16px;
        background: profile-hit.has-hover ? Theme.controls-color-secondary-default-hovered : Theme.controls-color-secondary-default-enabled;
        animate background { duration: App.motion ? Theme.duration-transition : 0ms; }
        Rectangle {
            x: 6px; y: 5px; width: 38px; height: 38px;
            border-radius: 19px;
            background: @linear-gradient(135deg, #ff5c4d 0%, #eb469f 35%, #8341ef 70%, #3f68f9 100%);
            Rectangle { x: 2px; y: 2px; width: 34px; height: 34px; border-radius: 17px; background: Theme.background-color-primary-enabled-basic; }
            Cover { x: 4px; y: 4px; width: 30px; height: 30px; round: true; source: App.avatar; }
        }
        if !c: Text {
            x: 52px; y: 7px; width: parent.width - 100px; height: 19px;
            text: App.login == "" || App.login == "None None" ? T.t("Профиль") : App.login;
            overflow: elide; font-size: Theme.font-size-label-m; font-weight: 650;
            color: Theme.controls-color-primary-text-enabled-variant;
        }
        if !c: Text {
            x: 52px; y: 26px; width: parent.width - 100px; height: 15px;
            text: App.plus ? T.t("Яндекс Плюс") : T.t("Аккаунт Яндекс Музыки");
            overflow: elide; font-size: 11px; color: Theme.controls-color-primary-text-enabled;
        }
        if App.plus && !c: Rectangle {
            x: parent.width - 42px; y: 14px; width: 34px; height: 20px;
            border-radius: 10px;
            background: @linear-gradient(90deg, #ff5c4d 0%, #eb469f 25%, #8341ef 72%, #4f4cf6 100%);
            Text { text: "PLUS"; font-size: 9px; font-weight: 700; color: #ffffff; horizontal-alignment: center; vertical-alignment: center; }
        }
        if !App.plus && !c: Icon {
            x: parent.width - 34px; y: 12px; source: Icons.arrow-right-small;
            size: 20px; colorize: Theme.controls-color-primary-text-enabled;
        }
        profile-hit := TouchArea {
            mouse-cursor: MouseCursor.pointer;
            clicked => { App.account-open = !App.account-open; }
        }
    }
'''
    navbar = replace_once(navbar, old_profile, new_profile, "profile card redesign")
    navbar_path.write_text(navbar, encoding="utf-8")

    # Remove Discord status UI and stop its IPC worker; retain old JSON fields for compatibility.
    account_path = root / "ui" / "account.slint"
    account = account_path.read_text(encoding="utf-8")
    account = replace_once(account, '''            SettingItem {
                title: T.t("Статус в Discord");
                desc: App.discord-on && App.discord-status != "" ? App.discord-status : T.t("Показывать в профиле Discord, что вы слушаете");
                toggle: true;
                on: App.discord-on;
                clicked => { App.setting("discord"); }
            }
''', "", "remove Discord settings row")
    account_path.write_text(account, encoding="utf-8")

    state_path = root / "ui" / "state.slint"
    state = state_path.read_text(encoding="utf-8")
    state = replace_once(state, '''    // Статус «Слушает» в профиле Discord и что с ним сейчас.
    in property <bool> discord-on;
    in property <string> discord-status;
''', "", "remove Discord UI state")
    state_path.write_text(state, encoding="utf-8")

    gui_path = root / "src" / "gui.rs"
    gui = gui_path.read_text(encoding="utf-8")
    gui = replace_once(gui, '    discord: Arc<crate::discord::Discord>,\n', "", "remove Discord context field")
    gui = replace_once(gui, '''        if app.get_page() == Page::Settings {
            app.set_discord_status(self.discord.status().into());
        }
''', "", "remove Discord status polling")
    gui = replace_once(gui, '''    let discord = crate::discord::Discord::spawn(
        player.clone(),
        crate::discord::DiscordCfg { enabled: cfg.discord_enabled, app_id: cfg.discord_app_id.clone() },
    );
    app.set_discord_on(cfg.discord_enabled);
''', "", "disable Discord runtime")
    gui = replace_once(gui, '        discord,\n', "", "remove Discord context initialization")
    gui = replace_once(gui, '''            "discord" => {
                let on = !app.get_discord_on();
                app.set_discord_on(on);
                self.discord.cfg.lock().unwrap().enabled = on;
                let mut st = self.st.borrow_mut();
                st.cfg.discord_enabled = on;
                st.cfg.save();
            }
''', "", "remove Discord settings action")
    gui_path.write_text(gui, encoding="utf-8")

    # Remove the Discord module entirely, not just its UI/runtime hooks.
    # First remove Rust module declarations so the deleted source is not compiled.
    src_root = root / "src"
    for rust_path in src_root.rglob("*.rs"):
        rust_source = rust_path.read_text(encoding="utf-8")
        rust_source_new = re.sub(r"(?m)^\s*(?:pub\s+)?mod\s+discord\s*;\s*\n", "", rust_source)
        if rust_source_new != rust_source:
            rust_path.write_text(rust_source_new, encoding="utf-8")

    discord_path = src_root / "discord.rs"
    if discord_path.exists():
        discord_path.unlink()
    remaining_refs = []
    for rust_path in src_root.rglob("*.rs"):
        if re.search(r"(?m)\b(?:crate::)?discord::", rust_path.read_text(encoding="utf-8")):
            remaining_refs.append(str(rust_path.relative_to(root)))
    if remaining_refs:
        raise SystemExit("Discord references remain after removal: " + ", ".join(remaining_refs))

    # Persistent switches for optional sidebar sections.
    config_path = root / "src" / "config.rs"
    config = config_path.read_text(encoding="utf-8")
    config, fields_added = re.subn(
        r"(?m)^([ \t]*pub navbar_collapsed: bool,)[ \t]*$",
        lambda m: m.group(1) + "\n    pub show_concerts: bool,\n    pub show_nonmusic: bool,",
        config,
    )
    if fields_added != 1:
        raise SystemExit(f"navigation config fields: expected one navbar_collapsed field, found {fields_added}")
    config, defaults_added = re.subn(
        r"(?m)^([ \t]*navbar_collapsed: false,)[ \t]*$",
        lambda m: m.group(1) + "\n            show_concerts: true,\n            show_nonmusic: true,",
        config,
    )
    if defaults_added != 1:
        raise SystemExit(f"navigation config defaults: expected one navbar_collapsed default, found {defaults_added}")
    config_path.write_text(config, encoding="utf-8")

    state_path = root / "ui" / "state.slint"
    state = state_path.read_text(encoding="utf-8")
    state = replace_once(state, '    in property <bool> nav-collapsed;\n', '    in property <bool> nav-collapsed;\n    in property <bool> show-concerts: true;\n    in property <bool> show-nonmusic: true;\n', "navigation UI state")
    state_path.write_text(state, encoding="utf-8")

    navbar_path = root / "ui" / "navbar.slint"
    navbar = navbar_path.read_text(encoding="utf-8")
    navbar = replace_once(navbar, '        NavLink { icon: Icons.nav-concerts; icon-selected: Icons.nav-concerts-selected; text: T.t("Концерты"); page: Page.concerts; }\n        NavLink { icon: Icons.nav-nonmusic; icon-selected: Icons.nav-nonmusic-selected; text: T.t("Книги и подкасты"); page: Page.nonmusic; }\n', '        if App.show-concerts: NavLink { icon: Icons.nav-concerts; icon-selected: Icons.nav-concerts-selected; text: T.t("Концерты"); page: Page.concerts; }\n        if App.show-nonmusic: NavLink { icon: Icons.nav-nonmusic; icon-selected: Icons.nav-nonmusic-selected; text: T.t("Книги и подкасты"); page: Page.nonmusic; }\n', "conditional sidebar links")
    navbar = replace_once(navbar, '        y: 76px + 6 * 48px + 5 * 4px + 24px;\n', '        y: 76px + (4 + (App.show-concerts ? 1 : 0) + (App.show-nonmusic ? 1 : 0)) * 48px + (3 + (App.show-concerts ? 1 : 0) + (App.show-nonmusic ? 1 : 0)) * 4px + 24px;\n', "sidebar pin offset")
    navbar_path.write_text(navbar, encoding="utf-8")

    account_path = root / "ui" / "account.slint"
    account = account_path.read_text(encoding="utf-8")
    anchor = '            SettingItem {\n                title: T.t("Меньше ИИ");\n                desc: T.t("В рекомендациях будет реже встречаться музыка, созданная с помощью ИИ");\n                toggle: true;\n                on: App.less-ai;\n                clicked => { App.setting("less-ai"); }\n            }\n'
    additions = anchor + '            SettingItem { title: T.t("Концерты"); desc: T.t("Показывать раздел в боковом меню"); toggle: true; on: App.show-concerts; clicked => { App.setting("show-concerts"); } }\n            SettingItem { title: T.t("Книги и подкасты"); desc: T.t("Показывать раздел в боковом меню"); toggle: true; on: App.show-nonmusic; clicked => { App.setting("show-nonmusic"); } }\n'
    account = replace_once(account, anchor, additions, "section visibility settings")
    account_path.write_text(account, encoding="utf-8")

    gui_path = root / "src" / "gui.rs"
    gui = gui_path.read_text(encoding="utf-8")
    gui = replace_once(gui, '    app.set_nav_collapsed(cfg.navbar_collapsed);\n', '    app.set_nav_collapsed(cfg.navbar_collapsed);\n    app.set_show_concerts(cfg.show_concerts);\n    app.set_show_nonmusic(cfg.show_nonmusic);\n', "initialize section visibility")
    gui = replace_once(gui, '            "about" => sys::open_url(env!("CARGO_PKG_REPOSITORY")),\n', '            "show-concerts" => { let on = !app.get_show_concerts(); app.set_show_concerts(on); let mut st = self.st.borrow_mut(); st.cfg.show_concerts = on; st.cfg.save(); if !on && app.get_page() == Page::Concerts { drop(st); self.navigate(Page::Trends, None, true); } }\n            "show-nonmusic" => { let on = !app.get_show_nonmusic(); app.set_show_nonmusic(on); let mut st = self.st.borrow_mut(); st.cfg.show_nonmusic = on; st.cfg.save(); if !on && app.get_page() == Page::Nonmusic { drop(st); self.navigate(Page::Trends, None, true); } }\n            "about" => sys::open_url(env!("CARGO_PKG_REPOSITORY")),\n', "section visibility actions")
    gui_path.write_text(gui, encoding="utf-8")
    api_path.write_text(api_new, encoding="utf-8")
    tray_path.write_text(tray_new, encoding="utf-8")
    # Lyrics fallback: Yandex first, then user-supplied local files.
    api_path = root / "src" / "api.rs"
    api_new = replace_once(api_new, "fn parse_lrc(text: &str) -> Vec<SyncLine> {", "pub(crate) fn parse_lrc(text: &str) -> Vec<SyncLine> {", "expose LRC parser")
    api_new = replace_once(api_new, "    pub lyrics: bool,\n", "", "remove unused Yandex lyrics availability flag")
    api_new = replace_once(api_new, '    let li = v.get("lyricsInfo");\n', "", "remove unused lyricsInfo parser variable")
    api_new = replace_once(api_new, '    let flag = |k: &str| li.and_then(|l| l.get(k)).and_then(|x| x.as_bool()).unwrap_or(false);\n', "", "remove unused lyrics availability parser closure")
    api_new = replace_once(api_new, '        lyrics: flag("hasAvailableSyncLyrics") || flag("hasAvailableTextLyrics"),\n', "", "remove lyrics availability initializer")
    api_path.write_text(api_new, encoding="utf-8")

    net_path = root / "src" / "net.rs"
    net = net_path.read_text(encoding="utf-8")
    net = replace_once(net, "fn fetch(api: &Api, uid: &str, key: &str) -> Res<Data> {", r'''
fn lyrics_slug(value: &str) -> String {
    value.chars().map(|c| {
        if c.is_alphanumeric() || c == ' ' || c == '-' { c } else { '_' }
    }).collect::<String>().split_whitespace().collect::<Vec<_>>().join(" ")
}

fn is_subscription_notice(lyrics: &Lyrics) -> bool {
    let text = lyrics.text.join(" ").to_lowercase();
    text.contains("подключите подписку")
        || text.contains("чтобы видеть тексты песен")
        || text.contains("subscribe to plus")
        || text.contains("subscribe to yandex plus")
}

fn local_lyrics(track_id: &str, title: &str, artists: &str) -> Lyrics {
    let mut out = Lyrics::default();
    let folder = crate::config::dir().join("lyrics");
    let _ = std::fs::create_dir_all(&folder);
    let stem = lyrics_slug(&format!("{artists} - {title}"));
    for path in [
        folder.join(format!("{track_id}.lrc")),
        folder.join(format!("{stem}.lrc")),
    ] {
        if let Ok(contents) = std::fs::read_to_string(path) {
            out.synced = crate::api::parse_lrc(&contents);
            if !out.synced.is_empty() {
                out.source = "Локальный файл".into();
                return out;
            }
        }
    }
    for path in [
        folder.join(format!("{track_id}.txt")),
        folder.join(format!("{stem}.txt")),
    ] {
        if let Ok(contents) = std::fs::read_to_string(path) {
            out.text = contents.lines().map(|line| line.trim_end().to_string()).filter(|line| !line.is_empty()).collect();
            if !out.text.is_empty() {
                out.source = "Локальный файл".into();
                return out;
            }
        }
    }

    out
}

fn fetch(api: &Api, uid: &str, key: &str) -> Res<Data> {'''.lstrip(), "lyrics fallback loader")
    net = replace_once(net, '        "lyrics" => api.lyrics(arg).map(Data::Lyrics),', r'''        "lyrics" => {
            let mut parts = arg.splitn(4, '|');
            let track_id = parts.next().unwrap_or("");
            let title = parts.next().unwrap_or("");
            let artists = parts.next().unwrap_or("");
            let _duration = parts.next();
            let mut lyrics = api.lyrics(track_id).unwrap_or_default();
            if lyrics.synced.is_empty() && (lyrics.text.is_empty() || is_subscription_notice(&lyrics)) {
                lyrics = local_lyrics(track_id, title, artists);
            }
            Ok(Data::Lyrics(lyrics))
        },''', "lyrics provider fallback")
    net_path.write_text(net, encoding="utf-8")

    gui_path = root / "src" / "gui.rs"
    gui = gui_path.read_text(encoding="utf-8")
    gui = replace_once(gui, '        let key = format!("lyrics:{}", t.id);', """        let key = format!("lyrics:{}|{}|{}|{}", t.id, t.title.replace('|', " "), t.artists.replace('|', " "), t.duration_ms / 1000);""", "pass track metadata to lyrics providers")
    gui = replace_once(gui, '            app.set_has_lyrics(t.lyrics);', '            app.set_has_lyrics(true);', "always enable lyrics panel for fallback providers")
    gui = replace_once(gui, '        if t.lyrics && current {', '        if current {', "always show lyrics action for current track")
    gui_path.write_text(gui, encoding="utf-8")

    # Remove every visible entry point for song lyrics; keep playback controls intact.
    playerbar_path = root / "ui" / "playerbar.slint"
    playerbar = playerbar_path.read_text(encoding="utf-8")
    playerbar = replace_once(
        playerbar,
        '            BarButton { x: parent.width - 193px; y: 24px; icon: Icons.lyrics; enabled: on && App.has-lyrics; clicked => { App.open-fullscreen(2); } }\n',
        "",
        "remove lyrics button from main player",
    )
    playerbar_path.write_text(playerbar, encoding="utf-8")

    fullscreen_path = root / "ui" / "fullscreen.slint"
    fullscreen = fullscreen_path.read_text(encoding="utf-8")
    fullscreen = replace_once(
        fullscreen,
        """            RoundButton {
                x: 168px;
                y: 320px;
                icon: Icons.lyrics-m;
                enabled: App.has-lyrics;
                ink: App.fs-mode == 2 ? #ffffff : #ffffff80;
                clicked => { App.fs-mode = App.fs-mode == 2 ? 0 : 2; }
            }
""",
        "",
        "remove lyrics button from fullscreen player",
    )
    fullscreen = replace_once(
        fullscreen,
        '        text: T.t("Текста этой песни пока нет");',
        '        text: "";',
        "remove lyrics empty-state message",
    )
    fullscreen_path.write_text(fullscreen, encoding="utf-8")



    # Remove the "About app" row from Settings without changing other settings.
    account_path = root / "ui" / "account.slint"
    account = account_path.read_text(encoding="utf-8")
    account = replace_once(
        account,
        '''            SettingItem {
                title: T.t("О приложении");
                desc: App.app-name + " · " + T.t("Версия ") + App.version;
                clicked => { App.setting("about"); }
            }
''',
        "",
        "remove About app settings row",
    )
    account_path.write_text(account, encoding="utf-8")

    # Collection: add a clear visual hero and a softly elevated liked-tracks panel.
    collection_path = root / "ui" / "collection.slint"
    collection = collection_path.read_text(encoding="utf-8")
    collection = replace_once(
        collection,
        '''            Text {
                text: T.t("Коллекция");
                font-family: "YS Music Headline";
                font-size: Theme.font-size-headline-xl;
                font-weight: 700;
                color: Theme.controls-color-primary-text-enabled-variant;
            }
            HorizontalLayout {
                alignment: start;
                Text {
                    text: T.t("У вашей музыки есть ");
                    font-size: Theme.font-size-label-m;
                    font-weight: 500;
                    color: Theme.controls-color-primary-text-enabled;
                }
                Text {
                    text: T.t("цвет");
                    font-size: Theme.font-size-label-m;
                    font-weight: 500;
                    color: #d317c7;
                }
            }
''',
        '''            // Большой заголовок с коротким пояснением вместо двух разрозненных строк.
            Rectangle {
                height: 116px;
                border-radius: 24px;
                background: @linear-gradient(110deg, #263c60 0%, #594078 55%, #9b4c78 100%);
                clip: true;
                Rectangle {
                    x: parent.width - 154px;
                    y: -54px;
                    width: 210px;
                    height: 210px;
                    border-radius: 105px;
                    background: #ffffff0d;
                }
                Rectangle {
                    x: parent.width - 104px;
                    y: 44px;
                    width: 94px;
                    height: 94px;
                    border-radius: 47px;
                    background: #ffffff0d;
                }
                Text {
                    x: 24px;
                    y: 20px;
                    text: T.t("Коллекция");
                    font-family: "YS Music Headline";
                    font-size: 30px;
                    font-weight: 700;
                    color: #ffffff;
                }
                Text {
                    x: 25px;
                    y: 64px;
                    text: T.t("Вся твоя музыка — в одном месте");
                    font-size: Theme.font-size-label-m;
                    font-weight: 500;
                    color: #ffffffc9;
                }
            }
''',
        "collection hero redesign",
    )
    collection = replace_once(
        collection,
        '''            Rectangle {
                height: 336px + 52px;
                Rectangle {
                    y: 52px;
''',
        '''            Rectangle {
                height: 388px;
                border-radius: 20px;
                background: Theme.controls-color-secondary-default-enabled;
                Rectangle {
                    x: 18px;
                    y: 18px;
''',
        "collection liked panel redesign",
    )
    collection_path.write_text(collection, encoding="utf-8")

    # Mini Player: volume controls move to the left of track information.
    # Add a dedicated seek hit-area at the bottom so it does not drag the window.
    playerbar_path = root / "ui" / "playerbar.slint"
    playerbar = playerbar_path.read_text(encoding="utf-8")
    playerbar = replace_once(
        playerbar,
        '''export component MiniPlayer inherits Rectangle {
    property <bool> on: App.has-track;
    property <float> drag-progress: -1;
    background: on ? App.tint : Theme.background-color-primary-enabled-player;
''',
        '''export component MiniPlayer inherits Rectangle {
    property <bool> on: App.has-track;
    property <float> drag-progress: -1;
    property <float> progress: App.duration > 0 ? App.position / App.duration : 0;
    property <float> shown: drag-progress >= 0 ? drag-progress : progress;
    background: on ? App.tint : Theme.background-color-primary-enabled-player;
''',
        "mini player seek state",
    )
    playerbar = replace_once(
        playerbar,
        '''        if on: Rectangle {
            x: 0;
            y: parent.height - 2px;
            width: App.duration > 0 ? parent.width * App.position / App.duration : 0;
            height: 2px;
            background: Theme.controls-color-primary-default-enabled;
        }
''',
        "",
        "remove mini player track progress bar",
    )
    playerbar = replace_once(
        playerbar,
        '''        VerticalLayout {
            x: on ? 88px : 16px;
            width: parent.width - self.x - 144px;
            alignment: center;
            spacing: 2px;
''',
        '''        VerticalLayout {
            x: on ? 176px : 16px;
            width: parent.width - self.x - 136px;
            alignment: center;
            spacing: 0px;
''',
        "move mini player track info right of volume",
    )
    playerbar = replace_once(
        playerbar,
        '''        // Громкость при наведении: значок выключает звук, полоску можно тянуть.
        if ta.has-hover || vol-ta.pressed: BarButton {
            x: parent.width - 172px;
            y: 2px;
            width: 24px;
            height: 24px;
            icon: App.volume <= 0 ? Icons.volume-off : Icons.volume;
            icon-size: 16px;
            clicked => { App.toggle-mute(); }
        }
        Rectangle {
            x: parent.width - 144px;
            y: 2px;
            width: 80px;
            height: 24px;
            visible: ta.has-hover || vol-ta.pressed;
''',
        '''        // Громкость теперь слева от названия трека; значок всегда доступен.
        BarButton {
            x: 88px;
            y: 32px;
            width: 24px;
            height: 24px;
            icon: App.volume <= 0 ? Icons.volume-off : Icons.volume;
            icon-size: 16px;
            clicked => { App.toggle-mute(); }
        }
        Rectangle {
            x: 116px;
            y: 32px;
            width: 52px;
            height: 24px;
            visible: ta.has-hover || vol-ta.pressed;
''',
        "move mini player volume controls left",
    )
    playerbar = replace_once(
        playerbar,
        '''        if ta.has-hover: BarButton { x: parent.width - 52px; y: 2px; width: 24px; height: 24px; icon: Icons.fullscreen; icon-size: 16px; clicked => { App.toggle-mini(); } }
''',
        '''        // Отдельная интерактивная полоса перемотки внизу мини-плеера.
        seek-ta := TouchArea {
            x: 0;
            y: parent.height - 12px;
            width: parent.width;
            height: 12px;
            enabled: on && App.duration > 0;
            mouse-cursor: MouseCursor.pointer;
            pointer-event(e) => {
                if e.button == PointerEventButton.left && e.kind == PointerEventKind.down {
                    root.drag-progress = max(0, min(1, self.mouse-x / root.width));
                }
                if e.button == PointerEventButton.left && e.kind == PointerEventKind.up && root.drag-progress >= 0 {
                    App.seek(root.drag-progress * App.duration);
                    root.drag-progress = -1;
                }
            }
            moved => {
                if self.pressed {
                    root.drag-progress = max(0, min(1, self.mouse-x / root.width));
                }
            }
        }
        if ta.has-hover: BarButton { x: parent.width - 52px; y: 2px; width: 24px; height: 24px; icon: Icons.fullscreen; icon-size: 16px; clicked => { App.toggle-mini(); } }
''',
        "mini player seek interaction",
    )
    playerbar_path.write_text(playerbar, encoding="utf-8")


    # Completely remove automatic update checks, download/install flow, and app version labels.
    main_path = root / "src" / "main.rs"
    main = main_path.read_text(encoding="utf-8")
    main = replace_once(main, '''    // После обновления старая версия ещё закрывается: ждём её до 15 секунд.
    let after_update = std::env::args().any(|a| a == update::AFTER_UPDATE);
    let mut claimed = sys::claim_instance();
    for _ in 0..60 {
        if claimed || !after_update {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
        claimed = sys::claim_instance();
    }
''', '''    let claimed = sys::claim_instance();
''', "remove updater-specific relaunch wait")
    main = replace_once(main, "mod update;\n", "", "remove update module declaration")
    main = replace_once(main, "    update::cleanup();\n", "", "remove old updater executable cleanup")
    main_path.write_text(main, encoding="utf-8")

    gui_path = root / "src" / "gui.rs"
    gui = gui_path.read_text(encoding="utf-8")
    gui = replace_once(gui, '''thread_local! {
    /// Найденный новый релиз — для кнопки «Обновить».
    static UPDATE: RefCell<Option<crate::update::Release>> = const { RefCell::new(None) };
}

/// Проверка обновлений в фоне (первая — через 10 с после запуска) и кнопка «Обновить» в тосте.
fn start_updates(ui: &AppWindow) {
    let weak = ui.as_weak();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        loop {
            if !crate::update::ENABLED.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_secs(60));
                continue;
            }
            match crate::update::check() {
                Ok(Some(r)) => {
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(ui) = weak.upgrade() {
                            ui.global::<App>().set_update_version(r.version.clone().into());
                            UPDATE.with(|u| *u.borrow_mut() = Some(r));
                        }
                    });
                }
                Ok(None) => {}
                Err(e) => log(format!("обновления: {e}")),
            }
            std::thread::sleep(crate::update::POLL);
        }
    });
    ui.global::<App>().on_update_changes(|| {
        if let Some(r) = UPDATE.with(|u| u.borrow().clone()) {
            sys::open_url(&crate::update::changes_url(&r.version));
        }
    });
    ui.global::<App>().on_update_install(|| {
        let Some(r) = UPDATE.with(|u| u.borrow().clone()) else { return };
        // В релизе нет exe — обновить можно только руками, ведём на страницу.
        if r.exe.is_none() {
            sys::open_url(&r.page);
            return;
        }
        with_ctx(|c| c.ui().global::<App>().set_update_busy(true));
        std::thread::spawn(move || {
            let res = crate::update::install(&r).and_then(|exe| crate::update::relaunch(&exe));
            let _ = slint::invoke_from_event_loop(move || match res {
                // Новая версия ждёт, пока мы закроемся.
                Ok(()) => quit(),
                Err(e) => {
                    log(format!("обновление: {e}"));
                    with_ctx(|c| {
                        c.ui().global::<App>().set_update_busy(false);
                        c.toast(format!("Не удалось обновить: {e}"));
                    });
                    sys::open_url(&r.page);
                }
            });
        });
    });
}

''', "", "remove background updater and install callbacks")
    gui = replace_once(gui, "    crate::update::ENABLED.store(cfg.check_updates, Ordering::Relaxed);\n    start_updates(&ui);\n", "", "remove updater startup")
    gui = replace_once(gui, "    app.set_version(env!(\"CARGO_PKG_VERSION\").into());\n", "", "remove app version display binding")
    gui = replace_once(gui, "    app.set_check_updates(cfg.check_updates);\n", "", "remove update setting initialization")
    gui = replace_once(gui, "                Event::Updates => sys::open_url(concat!(env!(\"CARGO_PKG_REPOSITORY\"), \"/releases\")),\n", "", "remove tray update event handler")
    gui = replace_once(gui, '''            "check-updates" => {
                let on = !app.get_check_updates();
                app.set_check_updates(on);
                crate::update::ENABLED.store(on, Ordering::Relaxed);
                if !on {
                    app.set_update_version(SharedString::new());
                    UPDATE.with(|u| *u.borrow_mut() = None);
                }
                let mut st = self.st.borrow_mut();
                st.cfg.check_updates = on;
                st.cfg.save();
            }
''', "", "remove update settings action")
    gui_path.write_text(gui, encoding="utf-8")

    config_path = root / "src" / "config.rs"
    config = config_path.read_text(encoding="utf-8")
    config = replace_once(config, "    /// Раз в полчаса спрашивать GitHub о новой версии.\n    pub check_updates: bool,\n", "", "remove update preference from config")
    config = replace_once(config, "            check_updates: true,\n", "", "remove update preference default")
    config_path.write_text(config, encoding="utf-8")

    tray_path = root / "src" / "tray.rs"
    tray = tray_path.read_text(encoding="utf-8")
    tray = replace_once(tray, "    Updates,\n", "", "remove tray update event")
    tray = replace_once(tray, '        add(menu, 2, &tr("Проверить обновления"), true);\n', "", "remove tray update menu item")
    tray = replace_once(tray, "            2 => Event::Updates,\n", "", "remove tray update menu dispatch")
    tray_path.write_text(tray, encoding="utf-8")

    account_path = root / "ui" / "account.slint"
    account = account_path.read_text(encoding="utf-8")
    account = replace_once(account, '''            SettingItem {
                title: T.t("Проверять обновления");
                desc: T.t("Раз в полчаса спрашивать GitHub, нет ли новой версии");
                toggle: true;
                on: App.check-updates;
                clicked => { App.setting("check-updates"); }
            }
''', "", "remove update setting row")
    account_path.write_text(account, encoding="utf-8")

    app_path = root / "ui" / "app.slint"
    app = app_path.read_text(encoding="utf-8")
    app = replace_once(app, '''    // «Доступна версия …» — как NotificationUpdate оригинала: та же плашка уведомления,
    // справа кнопка «Обновить». Висит, пока не обновятся.
    if App.update-version != "": Rectangle {
        property <float> k: App.motion ? 0 : 1;
        init => { k = 1; }
        animate k { duration: App.motion ? 250ms : 0ms; easing: ease-out; }
        opacity: k;
        x: (parent.width - self.width) / 2;
        y: Theme.spacer-size-m - (1 - k) * 16px;
        width: min(parent.width * 0.9, update-text.preferred-width + Theme.spacer-size-l + Theme.spacer-size-m + changes.width + Theme.spacer-size-m + update-btn.width + Theme.spacer-size-xxs);
        height: update-btn.height + 2 * Theme.spacer-size-xxs;
        border-radius: Theme.radius-size-l;
        background: Theme.background-color-primary-enabled-popover;
        border-width: 1px;
        border-color: Theme.controls-color-secondary-outline-selected;
        drop-shadow-blur: 20px;
        drop-shadow-offset-y: 1px;
        drop-shadow-color: #0000003d;
        update-text := Text {
            x: Theme.spacer-size-l;
            width: changes.x - self.x - Theme.spacer-size-m;
            text: T.t("Доступна версия ") + App.update-version;
            overflow: elide;
            vertical-alignment: center;
            color: Theme.controls-color-primary-text-enabled-variant;
            font-size: Theme.font-size-label-m;
            font-weight: 500;
        }
        // Ссылка на список изменений, чтобы перед обновлением было видно, что поменялось в коде.
        changes := Text {
            x: update-btn.x - self.width - Theme.spacer-size-m;
            text: T.t("Что изменилось");
            vertical-alignment: center;
            color: changes-ta.has-hover ? Theme.controls-color-primary-text-enabled-variant : Theme.controls-color-primary-text-enabled;
            font-size: Theme.font-size-label-m;
            font-weight: 500;
            changes-ta := TouchArea {
                mouse-cursor: MouseCursor.pointer;
                clicked => { App.update-changes(); }
            }
        }
        update-btn := Rectangle {
            x: parent.width - self.width - Theme.spacer-size-xxs;
            width: max(126px, update-label.preferred-width + 2 * Theme.spacer-size-l);
            height: 32px;
            border-radius: self.height / 2;
            background: update-ta.pressed ? Theme.controls-color-secondary-default-pressed
                : update-ta.has-hover ? Theme.controls-color-secondary-default-hovered
                : Theme.controls-color-secondary-default-enabled;
            opacity: App.update-busy ? 0.6 : 1;
            update-label := Text {
                text: App.update-busy ? T.t("Загрузка…") : T.t("Обновить");
                color: Theme.controls-color-secondary-on-default-enabled;
                font-size: Theme.font-size-label-m;
                font-weight: 500;
            }
            update-ta := TouchArea {
                enabled: !App.update-busy;
                mouse-cursor: MouseCursor.pointer;
                clicked => { App.update-install(); }
            }
        }
    }

''', "", "remove update notification banner")
    app_path.write_text(app, encoding="utf-8")

    state_path = root / "ui" / "state.slint"
    state = state_path.read_text(encoding="utf-8")
    state = replace_once(state, '''    // Новая версия на GitHub (src/update.rs): пусто — обновлений нет.
    in property <string> update-version;
    // Установщик скачивается.
    in property <bool> update-busy;
    callback update-install();
    // «Что изменилось»: сравнение запущенной и новой версии на GitHub.
    callback update-changes();
    // «Проверять обновления» в настройках.
    in property <bool> check-updates: true;
''', "", "remove updater UI state")
    state = replace_once(state, "    in property <string> version;\n", "", "remove application version state")
    state_path.write_text(state, encoding="utf-8")

    build_path = root / "build.rs"
    build = build_path.read_text(encoding="utf-8")
    build = replace_once(build, '        let script = format!("#pragma code_page(65001)\\n1 ICON \\"app.ico\\"\\n{}", version_info());\n', '        let script = "#pragma code_page(65001)\\n1 ICON \\"app.ico\\"\\n".to_string();\n', "remove executable version resource")
    version_fn = '''    fn version_info() -> String {
        let version = std::env::var("CARGO_PKG_VERSION").unwrap();
        let exe = std::env::var("CARGO_PKG_NAME").unwrap();
        let nums: Vec<&str> = version.split(['.', '-']).take(3).collect();
        let num = format!("{},0", nums.join(","));
        format!(
            r#"1 VERSIONINFO
FILEVERSION {num}
PRODUCTVERSION {num}
FILEOS 0x40004
FILETYPE 0x1
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "041904B0"
    BEGIN
      VALUE "FileDescription", "{APP_NAME}"
      VALUE "ProductName", "{APP_NAME}"
      VALUE "FileVersion", "{version}"
      VALUE "ProductVersion", "{version}"
      VALUE "InternalName", "{exe}"
      VALUE "OriginalFilename", "{exe}.exe"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x419, 1200
  END
END
"#
        )
    }

'''
    build = replace_once(build, version_fn, "", "remove Windows executable version metadata generator")
    build = replace_once(build, "    use super::APP_NAME;\n", "", "remove unused APP_NAME import")
    build_path.write_text(build, encoding="utf-8")

    update_path = root / "src" / "update.rs"
    if not update_path.is_file():
        raise SystemExit("updater source file missing; expected pinned upstream v1.1.4")
    update_path.unlink()


    wave_path = root / "ui" / "wave.slint"
    wave = wave_path.read_text(encoding="utf-8")
    wave = replace_once(wave, '''    // Версия в правом нижнем углу, как у официального приложения.
    Rectangle {
        x: parent.width - self.width;
        y: parent.height - 38px;
        width: ver.preferred-width + 16px;
        height: 26px;
        border-radius: 16px;
        background: #ffffff14;
        ver := Text {
            text: App.version;
            font-size: Theme.font-size-label-s;
            font-weight: 500;
            color: #ffffff80;
        }
    }
''', "", "remove version label from wave screen")
    wave_path.write_text(wave, encoding="utf-8")

    print("Removed updater, update controls, app version UI, and executable version metadata.")

    print("Applied native PulseSync-style hotkeys, safe GET/HEAD retries, and other integrations.")

if __name__ == "__main__":
    main()
