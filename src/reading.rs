#[derive(Clone, Copy)]
pub struct Reading {
    pub temperature_c: f32,
    pub humidity_percent: f32,
}

impl Reading {
    /// Decode after the driver has checked the busy and calibration bits.
    pub fn from_frame(bytes: [u8; 6]) -> Self {
        // Six-byte AHT10 frame; no AHT20-style seventh CRC byte.
        let humidity =
            ((bytes[1] as u32) << 12) | ((bytes[2] as u32) << 4) | (bytes[3] as u32 >> 4);
        let temperature =
            (((bytes[3] & 0x0F) as u32) << 16) | ((bytes[4] as u32) << 8) | bytes[5] as u32;
        Self {
            temperature_c: temperature as f32 * (200.0 / 1_048_576.0) - 50.0,
            humidity_percent: humidity as f32 * (100.0 / 1_048_576.0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Reading;

    #[test]
    fn room_temperature_and_half_scale_humidity() {
        let r = Reading::from_frame([0x08, 0x80, 0, 0x06, 0, 0]);
        assert_eq!(r.temperature_c, 25.0);
        assert_eq!(r.humidity_percent, 50.0);
        assert_eq!(
            format!("{:.2} °C | {:.2} %", r.temperature_c, r.humidity_percent),
            "25.00 °C | 50.00 %"
        );
    }

    #[test]
    fn negative_temperature() {
        let r = Reading::from_frame([0x08, 0, 0, 0, 0, 0]);
        assert_eq!(r.temperature_c, -50.0);
        assert_eq!(r.humidity_percent, 0.0);
    }

    #[test]
    fn shared_nibbles_are_independent() {
        let r = Reading::from_frame([0x08, 0, 0, 0x1F, 0, 0]);
        assert_eq!(r.temperature_c, 137.5);
        assert!((r.humidity_percent - 0.00009536743).abs() < 1e-10);
    }

    #[test]
    fn maximum_twenty_bit_values() {
        let r = Reading::from_frame([0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]);
        assert!(r.humidity_percent < 100.0 && r.humidity_percent > 99.99);
        assert!(r.temperature_c < 150.0 && r.temperature_c > 149.99);
    }
}
