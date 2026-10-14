use crate::bus::Bus;
use embassy_nrf::twim;
use embassy_time::{Duration, Instant, Timer};

pub use crate::reading::Reading;

const BUSY: u8 = 0x80;
const CALIBRATED: u8 = 0x08;

#[derive(Clone, Copy, Debug)]
pub enum Error {
    I2c(twim::Error),
    BusyTimeout,
    NotCalibrated,
}

impl From<twim::Error> for Error {
    fn from(error: twim::Error) -> Self {
        Self::I2c(error)
    }
}

impl core::fmt::Display for Error {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::I2c(error) => write!(f, "I2C {error:?}"),
            Self::BusyTimeout => f.write_str("sensor busy timeout"),
            Self::NotCalibrated => f.write_str("calibration bit is clear"),
        }
    }
}

async fn ready(bus: &mut Bus<'_>) -> Result<u8, Error> {
    let deadline = Instant::now() + Duration::from_millis(250);
    loop {
        let mut status = [0];
        // AHT10 returns status as the first byte of a plain read. Do not
        // confuse the 8-bit I2C read address 0x71 with a register command.
        bus.read(&mut status).await?;
        if status[0] & BUSY == 0 {
            return Ok(status[0]);
        }
        if Instant::now() >= deadline {
            return Err(Error::BusyTimeout);
        }
        Timer::after_millis(10).await;
    }
}

pub async fn initialize(bus: &mut Bus<'_>) -> Result<(), Error> {
    bus.write(&[0xBA]).await?;
    Timer::after_millis(20).await;
    let status = ready(bus).await?;
    if status & CALIBRATED == 0 {
        // AHT10 initialization (AHT20 instead uses 0xBE).
        bus.write(&[0xE1, 0x08, 0x00]).await?;
        Timer::after_millis(10).await;
        if ready(bus).await? & CALIBRATED == 0 {
            return Err(Error::NotCalibrated);
        }
    }
    Ok(())
}

pub async fn measure(bus: &mut Bus<'_>) -> Result<Reading, Error> {
    if ready(bus).await? & CALIBRATED == 0 {
        return Err(Error::NotCalibrated);
    }
    bus.write(&[0xAC, 0x33, 0x00]).await?;
    Timer::after_millis(80).await;
    ready(bus).await?;
    let mut bytes = [0; 6];
    bus.read(&mut bytes).await?;
    if bytes[0] & BUSY != 0 {
        return Err(Error::BusyTimeout);
    }
    if bytes[0] & CALIBRATED == 0 {
        return Err(Error::NotCalibrated);
    }
    Ok(Reading::from_frame(bytes))
}
