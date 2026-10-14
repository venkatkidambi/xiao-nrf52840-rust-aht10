//! Legacy, nonconnectable BLE advertisement. No scan response is needed.
pub const PACKET_LEN: usize = 37;

pub fn encode(
    address: [u8; 6],
    boot: u32,
    sequence: u32,
    sleep_seconds: u32,
    sample: Result<(i16, u16), u8>,
) -> [u8; PACKET_LEN] {
    let mut p = [0; PACKET_LEN];
    p[0] = 0x42; // ADV_NONCONN_IND, TxAdd = random static address.
    p[1] = 35; // AdvA (6) + advertising data (29).
    p[2..8].copy_from_slice(&address);
    p[8..15].copy_from_slice(&[2, 0x01, 0x06, 25, 0xff, 0xff, 0xff]);
    // Manufacturer 0xffff is for local testing only. Payload is little endian.
    p[15..19].copy_from_slice(b"AHT1");
    p[19] = 1;
    let (temperature, humidity) = match sample {
        Ok(values) => values,
        Err(status) => {
            p[20] = status;
            (0, 0)
        }
    };
    p[21..25].copy_from_slice(&boot.to_le_bytes());
    p[25..29].copy_from_slice(&sequence.to_le_bytes());
    p[29..31].copy_from_slice(&temperature.to_le_bytes());
    p[31..33].copy_from_slice(&humidity.to_le_bytes());
    p[33..37].copy_from_slice(&sleep_seconds.to_le_bytes());
    p
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_format_matches_mac_decoder() {
        let p = encode([1, 2, 3, 4, 5, 0xc6], 0x12345678, 42, 60, Ok((-1234, 5678)));
        assert_eq!(p[1] as usize, p.len() - 2);
        assert_eq!(&p[8..15], &[2, 1, 6, 25, 255, 255, 255]);
        assert_eq!(
            &p[15..],
            &[
                65, 72, 84, 49, 1, 0, 0x78, 0x56, 0x34, 0x12, 42, 0, 0, 0, 0x2e, 0xfb, 0x2e, 0x16,
                60, 0, 0, 0,
            ]
        );
    }

    #[test]
    fn failure_does_not_send_stale_measurement() {
        let p = encode([0xc0; 6], 1, u32::MAX, 604800, Err(2));
        assert_eq!(p[20], 2);
        assert_eq!(&p[29..33], &[0; 4]);
        assert_eq!(&p[25..29], &[255; 4]);
    }
}
