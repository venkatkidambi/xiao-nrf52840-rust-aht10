#[derive(Clone, Copy)]
pub struct Reading {
    temperature_raw: u32,
    humidity_raw: u32,
}

impl Reading {
    #[cfg(any(test, not(feature = "usb-serial")))]
    pub fn temperature_centidegrees(self) -> i16 {
        // raw * 20000 / 2^20 - 5000; reduced to fit in 32 bits.
        (((self.temperature_raw * 1250 + 32768) >> 16) as i16) - 5000
    }

    #[cfg(any(test, not(feature = "usb-serial")))]
    pub fn humidity_centipercent(self) -> u16 {
        ((self.humidity_raw * 625 + 32768) >> 16) as u16
    }

    #[cfg(any(test, feature = "usb-serial"))]
    pub fn temperature_c(self) -> f32 {
        self.temperature_raw as f32 * (200.0 / 1_048_576.0) - 50.0
    }

    #[cfg(any(test, feature = "usb-serial"))]
    pub fn humidity_percent(self) -> f32 {
        self.humidity_raw as f32 * (100.0 / 1_048_576.0)
    }

    /// Decode after the driver has checked the busy and calibration bits.
    pub fn from_frame(bytes: [u8; 6]) -> Self {
        // Six-byte AHT10 frame; no AHT20-style seventh CRC byte.
        let humidity =
            ((bytes[1] as u32) << 12) | ((bytes[2] as u32) << 4) | (bytes[3] as u32 >> 4);
        let temperature =
            (((bytes[3] & 0x0F) as u32) << 16) | ((bytes[4] as u32) << 8) | bytes[5] as u32;
        Self {
            temperature_raw: temperature,
            humidity_raw: humidity,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Reading;

    #[test]
    fn room_temperature_and_half_scale_humidity() {
        let r = Reading::from_frame([0x08, 0x80, 0, 0x06, 0, 0]);
        assert_eq!(r.temperature_c(), 25.0);
        assert_eq!(r.humidity_percent(), 50.0);
        assert_eq!(r.temperature_centidegrees(), 2500);
        assert_eq!(r.humidity_centipercent(), 5000);
        assert_eq!(
            format!(
                "{:.2} °C | {:.2} %",
                r.temperature_c(),
                r.humidity_percent()
            ),
            "25.00 °C | 50.00 %"
        );
    }

    #[test]
    fn negative_temperature() {
        let r = Reading::from_frame([0x08, 0, 0, 0, 0, 0]);
        assert_eq!(r.temperature_c(), -50.0);
        assert_eq!(r.humidity_percent(), 0.0);
    }

    #[test]
    fn shared_nibbles_are_independent() {
        let r = Reading::from_frame([0x08, 0, 0, 0x1F, 0, 0]);
        assert_eq!(r.temperature_c(), 137.5);
        assert!((r.humidity_percent() - 0.00009536743).abs() < 1e-10);
    }

    #[test]
    fn maximum_twenty_bit_values() {
        let r = Reading::from_frame([0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]);
        assert!(r.humidity_percent() < 100.0 && r.humidity_percent() > 99.99);
        assert!(r.temperature_c() < 150.0 && r.temperature_c() > 149.99);
    }

    #[test]
    fn fixed_point_rounding_and_full_sensor_range() {
        // Compare every 20-bit input against the unreduced, 64-bit formulas.
        for raw in 0..1_048_576u32 {
            let reading = Reading {
                temperature_raw: raw,
                humidity_raw: raw,
            };
            assert_eq!(
                i64::from(reading.temperature_centidegrees()),
                ((i64::from(raw) * 20_000 + 524_288) / 1_048_576) - 5000
            );
            assert_eq!(
                u64::from(reading.humidity_centipercent()),
                (u64::from(raw) * 10_000 + 524_288) / 1_048_576
            );
        }
    }
}
