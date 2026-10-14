// Host-only tests: rustc --edition=2024 --test tests/protocol.rs -o target/protocol-tests
#[path = "../src/delivery.rs"]
mod delivery;
#[path = "../src/packet.rs"]
mod packet;
