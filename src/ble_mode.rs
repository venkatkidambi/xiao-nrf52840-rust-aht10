use crate::{Irqs, aht10, beacon::Beacon, bus::Bus, packet};
use embassy_executor::Spawner;
use embassy_nrf::{
    config,
    gpio::{Level, Output, OutputDrive},
    pac,
    rng::Rng,
};
use embassy_time::{Duration, Instant, Timer, with_timeout};

include!(concat!(env!("OUT_DIR"), "/settings.rs"));

pub async fn run(_spawner: Spawner) {
    let mut config = config::Config::default();
    config.hfclk_source = config::HfclkSource::Internal;
    config.lfclk_source = config::LfclkSource::ExternalXtal;
    config.dcdc.reg1 = true; // XIAO has the required DCC inductor.
    config.debug = config::Debug::NotConfigured;
    let p = embassy_nrf::init(config);
    // Bootloader handoff may leave an explicitly requested HFXO running.
    pac::CLOCK.tasks_hfclkstop().write_value(1);
    pac::POWER.tasks_lowpwr().write_value(1);

    // Keep these outputs owned for the life of the application.
    let drive = OutputDrive::Standard;
    let _red = Output::new(p.P0_26, Level::High, drive);
    let _green = Output::new(p.P0_30, Level::High, drive);
    let _blue = Output::new(p.P0_06, Level::High, drive);
    let _battery_divider = Output::new(p.P0_14, Level::High, drive);
    let _imu_power = Output::new(p.P1_08, Level::Low, drive);
    let _mic_power = Output::new(p.P1_10, Level::Low, drive);

    // Put the onboard SPI NOR (when fitted) into deep power down (B9).
    // GPIO SPI mode 0 avoids leaving a QSPI clock/peripheral running.
    let mut cs = Output::new(p.P0_25, Level::High, drive);
    let mut sck = Output::new(p.P0_21, Level::Low, drive);
    let mut mosi = Output::new(p.P0_20, Level::Low, drive);
    let _wp = Output::new(p.P0_22, Level::High, drive);
    let _hold = Output::new(p.P0_23, Level::High, drive);
    cs.set_low();
    for bit in (0..8).rev() {
        mosi.set_level(if 0xb9 & (1 << bit) != 0 {
            Level::High
        } else {
            Level::Low
        });
        cortex_m::asm::delay(64);
        sck.set_high();
        cortex_m::asm::delay(64);
        sck.set_low();
    }
    cs.set_high();
    mosi.set_low();

    let mut bus = Bus::new(p.TWISPI0, p.P0_04, p.P0_05);
    let mut beacon = Beacon::new(p.RADIO, Irqs);
    let mut rng = Rng::new(p.RNG, Irqs);
    let mut seed = [0; 4];
    // RNG driver stops hardware on completion and on timeout cancellation.
    if with_timeout(Duration::from_millis(20), rng.fill_bytes(&mut seed))
        .await
        .is_err()
    {
        cortex_m::peripheral::SCB::sys_reset();
    }
    let boot = u32::from_le_bytes(seed);
    drop(rng);
    let mut random = boot.max(1);
    let mut address = [0; 6];
    address[..4].copy_from_slice(&pac::FICR.deviceaddr(0).read().to_le_bytes());
    address[4..].copy_from_slice(&pac::FICR.deviceaddr(1).read().to_le_bytes()[..2]);
    address[5] |= 0xc0; // Random static, factory-programmed address.

    Timer::after_millis(40).await; // AHT10 power-up; only once per boot.
    let mut initialized = false;
    let mut sequence = 0u32;
    loop {
        let result = with_timeout(Duration::from_millis(600), async {
            if !initialized {
                aht10::initialize(&mut bus).await?;
                initialized = true;
            }
            aht10::measure(&mut bus).await
        })
        .await;
        let sample = match result {
            Ok(Ok(r)) => Ok((r.temperature_centidegrees(), r.humidity_centipercent())),
            Ok(Err(aht10::Error::I2c(_))) => Err(1),
            Ok(Err(aht10::Error::BusyTimeout)) => Err(2),
            Ok(Err(aht10::Error::NotCalibrated)) => Err(3),
            Err(_) => Err(4),
        };
        if sample.is_err() {
            initialized = false;
            bus.recover().await;
        }
        let payload = packet::encode(address, boot, sequence, SLEEP_SECONDS, sample);
        for event in 0..ADV_EVENTS {
            let started = Instant::now();
            if beacon.send(payload).await.is_err() {
                break;
            }
            if event + 1 < ADV_EVENTS {
                // BLE advInterval = 20 ms, advDelay = pseudo-random 0..10 ms.
                random ^= random << 13;
                random ^= random >> 17;
                random ^= random << 5;
                Timer::at(started + Duration::from_micros(20_000 + u64::from(random % 10_001)))
                    .await;
            }
        }
        sequence = sequence.wrapping_add(1);
        // Interval is explicitly sleep AFTER the burst, not start-to-start.
        // Executor WFE + RTC1: no USB, radio, HFXO, TWIM, or polling loop.
        Timer::after_secs(u64::from(SLEEP_SECONDS)).await;
    }
}
