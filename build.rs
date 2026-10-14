use std::{env, fs, path::PathBuf};

fn setting(name: &str, default: u32, min: u32, max: u32) -> u32 {
    println!("cargo:rerun-if-env-changed={name}");
    let value = env::var(name).map_or(default, |s| s.parse().expect("expected an integer"));
    assert!((min..=max).contains(&value), "{name} must be {min}..={max}");
    value
}

fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let sleep = setting("SLEEP_SECONDS", 10, 1, 604800);
    let events = setting("ADV_EVENTS", 3, 1, 100);
    fs::write(
        out.join("settings.rs"),
        format!("pub const SLEEP_SECONDS: u32 = {sleep};\npub const ADV_EVENTS: u32 = {events};\n"),
    )
    .unwrap();
    fs::copy("memory.x", out.join("memory.x")).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=memory.x");
}
