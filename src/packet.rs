//! Version 2 GATT reading: 20 bytes, below the default ATT read limit (22 bytes).
pub const PACKET_LEN: usize = 20;
pub type Packet = [u8; PACKET_LEN];

pub fn encode(
    boot: u32,
    sequence: u32,
    sleep_seconds: u32,
    sample: Result<(i16, u16), u8>,
) -> Packet {
    let mut p = [0; PACKET_LEN];
    p[..2].copy_from_slice(b"AH");
    p[2] = 2;
    let (temperature, humidity) = match sample {
        Ok(values) => values,
        Err(status) => {
            p[3] = status;
            (0, 0)
        }
    };
    p[4..8].copy_from_slice(&boot.to_le_bytes());
    p[8..12].copy_from_slice(&sequence.to_le_bytes());
    p[12..14].copy_from_slice(&temperature.to_le_bytes());
    p[14..16].copy_from_slice(&humidity.to_le_bytes());
    p[16..20].copy_from_slice(&sleep_seconds.to_le_bytes());
    p
}

pub fn matches_ack(packet: &Packet, ack: &[u8]) -> bool {
    ack == &packet[4..12]
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wire_format_matches_mac_decoder() {
        let p = encode(0x12345678, 42, 60, Ok((-1234, 5678)));
        assert_eq!(
            p,
            [
                65, 72, 2, 0, 0x78, 0x56, 0x34, 0x12, 42, 0, 0, 0, 0x2e, 0xfb, 0x2e, 0x16, 60, 0,
                0, 0
            ]
        );
        assert!(matches_ack(&p, &p[4..12]));
        assert!(!matches_ack(&p, &p[4..11]));
        assert!(!matches_ack(&p, &encode(1, 42, 60, Ok((0, 0)))[4..12]));
    }
    #[test]
    fn failure_does_not_send_stale_measurement() {
        let p = encode(1, u32::MAX, 604800, Err(2));
        assert_eq!(p[3], 2);
        assert_eq!(&p[12..16], &[0; 4]);
        assert_eq!(&p[8..12], &[255; 4]);
    }
}
