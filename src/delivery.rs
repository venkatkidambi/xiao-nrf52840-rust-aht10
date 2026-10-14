//! Bounded RAM outbox and retry pacing. No flash wear and no unbounded retries.
use crate::packet::{PACKET_LEN, Packet, matches_ack};

pub const CAPACITY: usize = 64;
pub struct Outbox {
    packets: [Packet; CAPACITY],
    head: usize,
    len: usize,
}
impl Outbox {
    pub const fn new() -> Self {
        Self {
            packets: [[0; PACKET_LEN]; CAPACITY],
            head: 0,
            len: 0,
        }
    }
    pub fn push(&mut self, packet: Packet) {
        self.push_with_policy(packet, false);
    }

    #[cfg(any(feature = "ble-persistent", test))]
    pub fn push_preserving_head(&mut self, packet: Packet) {
        self.push_with_policy(packet, true);
    }

    fn push_with_policy(&mut self, packet: Packet, preserve_head: bool) {
        if self.len == CAPACITY {
            let next = (self.head + 1) % CAPACITY;
            if preserve_head {
                // Keep a reading being committed by the receiver stable. Drop
                // the next-oldest instead, with one copy rather than a shift.
                self.packets[next] = self.packets[self.head];
            }
            self.head = next;
            self.len -= 1;
        }
        self.packets[(self.head + self.len) % CAPACITY] = packet;
        self.len += 1;
    }
    pub fn front(&self) -> Option<&Packet> {
        if self.len == 0 {
            None
        } else {
            Some(&self.packets[self.head])
        }
    }
    pub fn acknowledge(&mut self, ack: &[u8]) -> bool {
        if !self.front().is_some_and(|p| matches_ack(p, ack)) {
            return false;
        }
        self.head = (self.head + 1) % CAPACITY;
        self.len -= 1;
        true
    }
}

pub struct Retry {
    spacing: u8,
    skip: u8,
}

/// Per-connection delivery watchdog. Only a committed head ACK advances it;
/// reads, subscriptions, bad ACKs and newly sampled data cannot extend it.
#[cfg(any(feature = "ble-persistent", test))]
pub struct Session {
    offered: Option<[u8; 8]>,
    deadline_ms: Option<u64>,
}

#[cfg(any(feature = "ble-persistent", test))]
impl Session {
    pub const fn new() -> Self {
        Self {
            offered: None,
            deadline_ms: None,
        }
    }

    pub fn observe(&mut self, pending: bool, now_ms: u64, timeout_ms: u32) -> Option<u64> {
        if pending {
            self.deadline_ms
                .get_or_insert(now_ms + u64::from(timeout_ms));
        } else {
            self.deadline_ms = None;
            self.offered = None;
        }
        self.deadline_ms
    }

    pub fn offer(&mut self, packet: &Packet) {
        self.offered = Some(packet[4..12].try_into().unwrap());
    }

    pub fn acknowledge(&mut self, outbox: &mut Outbox, ack: &[u8]) -> bool {
        if !self.offered.as_ref().is_some_and(|id| id == ack) || !outbox.acknowledge(ack) {
            return false;
        }
        self.offered = None;
        self.deadline_ms = None;
        true
    }
}
impl Retry {
    pub const fn new() -> Self {
        Self {
            spacing: 1,
            skip: 0,
        }
    }
    pub fn due(&mut self) -> bool {
        if self.skip == 0 {
            true
        } else {
            self.skip -= 1;
            false
        }
    }
    pub fn finish(&mut self, made_progress: bool) {
        self.spacing = if made_progress {
            1
        } else {
            (self.spacing * 2).min(8)
        };
        self.skip = self.spacing - 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::packet::encode;
    fn p(sequence: u32) -> Packet {
        encode(1, sequence, 10, Ok((123, 456)))
    }
    #[test]
    fn only_exact_head_ack_removes_data() {
        let mut q = Outbox::new();
        q.push(p(0));
        q.push(p(1));
        for ack in [&p(1)[4..12], &p(0)[4..11], &[0; 8]] {
            assert!(!q.acknowledge(ack));
        }
        assert_eq!(q.front(), Some(&p(0)));
        assert!(q.acknowledge(&p(0)[4..12]));
        assert!(!q.acknowledge(&p(0)[4..12]));
        assert_eq!(q.front(), Some(&p(1)));
        assert!(q.acknowledge(&p(1)[4..12]));
        assert_eq!(q.front(), None);
    }
    #[test]
    fn outage_overflow_keeps_newest_and_wraps() {
        let mut q = Outbox::new();
        for n in 0..200 {
            q.push(p(n));
        }
        for n in 136..200 {
            assert_eq!(q.front(), Some(&p(n)));
            assert!(q.acknowledge(&p(n)[4..12]));
        }
        assert_eq!(q.front(), None);
        q.push(p(u32::MAX));
        q.push(p(0));
        assert!(q.acknowledge(&p(u32::MAX)[4..12]));
        assert!(q.acknowledge(&p(0)[4..12]));
    }

    #[test]
    fn connected_overflow_keeps_in_flight_head_and_newest_pending_samples() {
        let mut q = Outbox::new();
        q.push(p(0));
        for n in 1..200 {
            q.push_preserving_head(p(n));
        }
        assert_eq!(q.front(), Some(&p(0)));
        assert!(q.acknowledge(&p(0)[4..12]));
        for n in 137..200 {
            assert_eq!(q.front(), Some(&p(n)));
            assert!(q.acknowledge(&p(n)[4..12]));
        }
        assert_eq!(q.front(), None);
    }
    #[test]
    fn failure_backoff_is_capped_and_progress_resets_it() {
        let mut retry = Retry::new();
        assert!(retry.due());
        for spacing in [2, 4, 8, 8, 8] {
            retry.finish(false);
            for _ in 1..spacing {
                assert!(!retry.due());
            }
            assert!(retry.due());
        }
        retry.finish(true);
        assert!(retry.due());
    }

    #[test]
    fn persistent_idle_has_no_deadline_and_only_offered_head_ack_resets_it() {
        let mut q = Outbox::new();
        let mut session = Session::new();
        assert_eq!(session.observe(false, 0, 5000), None);
        q.push(p(0));
        assert_eq!(session.observe(true, 100_000, 5000), Some(105_000));
        assert!(!session.acknowledge(&mut q, &p(0)[4..12]));
        session.offer(&p(0));
        q.push(p(1));
        for ack in [&p(1)[4..12], &p(0)[4..11], &[0; 8]] {
            assert!(!session.acknowledge(&mut q, ack));
        }
        session.offer(&p(0)); // Repeated reads/subscriptions must not buy time.
        assert_eq!(session.observe(true, 104_999, 5000), Some(105_000));
        assert!(session.acknowledge(&mut q, &p(0)[4..12]));
        assert_eq!(session.observe(true, 104_999, 5000), Some(109_999));
        assert!(!session.acknowledge(&mut q, &p(1)[4..12]));
        session.offer(&p(1));
        assert!(session.acknowledge(&mut q, &p(1)[4..12]));
        assert_eq!(session.observe(false, 105_000, 5000), None);
        assert_eq!(session.observe(false, 900_000, 5000), None);
        q.push(p(2));
        assert_eq!(session.observe(true, 900_001, 5000), Some(905_001));
    }

    #[test]
    fn overflow_cannot_extend_watchdog_or_ack_a_new_unoffered_head() {
        let mut q = Outbox::new();
        let mut session = Session::new();
        q.push(p(0));
        session.offer(&p(0));
        assert_eq!(session.observe(true, 0, 5000), Some(5000));
        for n in 1..=CAPACITY as u32 {
            q.push(p(n));
        }
        assert!(!session.acknowledge(&mut q, &p(0)[4..12]));
        assert!(!session.acknowledge(&mut q, &p(1)[4..12]));
        session.offer(&p(1));
        assert_eq!(session.observe(true, 5001, 5000), Some(5000));
        // A reconnect gets fresh delivery state, but retains the outbox.
        let mut reconnect = Session::new();
        assert_eq!(reconnect.observe(true, 10_000, 5000), Some(15_000));
        assert!(!reconnect.acknowledge(&mut q, &p(1)[4..12]));
        reconnect.offer(&p(1));
        assert!(reconnect.acknowledge(&mut q, &p(1)[4..12]));
    }
}
