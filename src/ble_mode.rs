use crate::{Irqs, aht10, bus::Bus, delivery::Retry, packet};
use embassy_executor::Spawner;
use embassy_nrf::{
    config,
    gpio::{Level, Output, OutputDrive},
    pac,
    rng::Rng,
};
use embassy_time::{Duration, Timer, with_timeout};
use nrf_mpsl::MultiprotocolServiceLayer;
use static_cell::StaticCell;

include!(concat!(env!("OUT_DIR"), "/settings.rs"));

#[embassy_executor::task]
async fn mpsl_task(mpsl: &'static MultiprotocolServiceLayer<'static>) -> ! {
    mpsl.run().await
}

pub async fn run(spawner: Spawner) {
    let mut config = config::Config::default();
    config.hfclk_source = config::HfclkSource::Internal;
    config.lfclk_source = config::LfclkSource::ExternalXtal;
    config.dcdc.reg1 = true; // XIAO has the required DCC inductor.
    config.debug = config::Debug::NotConfigured;
    let mut p = embassy_nrf::init(config);
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
    let mut address = [0; 6];
    address[..4].copy_from_slice(&pac::FICR.deviceaddr(0).read().to_le_bytes());
    address[4..].copy_from_slice(&pac::FICR.deviceaddr(1).read().to_le_bytes()[..2]);
    address[5] |= 0xc0; // Random static, factory-programmed address.

    let mpsl_p =
        nrf_mpsl::Peripherals::new(p.RTC0, p.TIMER0, p.TEMP, p.PPI_CH19, p.PPI_CH30, p.PPI_CH31);
    let lfclk = nrf_mpsl::raw::mpsl_clock_lfclk_cfg_t {
        source: nrf_mpsl::raw::MPSL_CLOCK_LF_SRC_XTAL as u8,
        rc_ctiv: 0,
        rc_temp_ctiv: 0,
        accuracy_ppm: 20,
        skip_wait_lfclk_started: false,
    };
    static MPSL: StaticCell<MultiprotocolServiceLayer> = StaticCell::new();
    let mpsl = MPSL.init(MultiprotocolServiceLayer::new(mpsl_p, Irqs, lfclk).unwrap());
    spawner.spawn(mpsl_task(mpsl).unwrap());
    #[cfg(not(feature = "ble-persistent"))]
    let mut outbox = crate::delivery::Outbox::new();
    let mut retry = Retry::new();
    let mut controller_memory = nrf_sdc::Mem::<4720>::new();

    Timer::after_millis(40).await; // AHT10 power-up; only once per boot.
    #[cfg(feature = "ble-persistent")]
    {
        use crate::ble_persistent::{self, Shared};
        use embassy_futures::join::join;
        use embassy_time::Instant;

        let shared = Shared::new();
        let sampling = async {
            let mut initialized = false;
            let mut sequence = 0u32;
            loop {
                let started = Instant::now();
                let sample = sample(&mut bus, &mut initialized).await;
                shared.push(packet::encode(boot, sequence, SLEEP_SECONDS, sample));
                sequence = sequence.wrapping_add(1);
                // Independent start-to-start cadence, including during an outage
                // or a slow ACK. No catch-up bursts after a delayed conversion.
                embassy_time::Timer::at(started + Duration::from_secs(u64::from(SLEEP_SECONDS)))
                    .await;
            }
        };
        let radio = async {
            loop {
                shared.changed.wait().await;
                if !retry.due() {
                    continue;
                }
                let radio_peripherals = nrf_sdc::Peripherals::new(
                    p.PPI_CH17.reborrow(),
                    p.PPI_CH18.reborrow(),
                    p.PPI_CH20.reborrow(),
                    p.PPI_CH21.reborrow(),
                    p.PPI_CH22.reborrow(),
                    p.PPI_CH23.reborrow(),
                    p.PPI_CH24.reborrow(),
                    p.PPI_CH25.reborrow(),
                    p.PPI_CH26.reborrow(),
                    p.PPI_CH27.reborrow(),
                    p.PPI_CH28.reborrow(),
                    p.PPI_CH29.reborrow(),
                );
                let controller = nrf_sdc::Builder::new()
                    .and_then(|b| b.support_adv().support_peripheral().peripheral_count(1))
                    .and_then(|b| b.buffer_cfg(27, 27, 3, 3))
                    .and_then(|b| {
                        b.build(radio_peripherals, &mut rng, mpsl, &mut controller_memory)
                    });
                let progress = match controller {
                    Ok(controller) => {
                        ble_persistent::deliver(
                            controller,
                            address,
                            &shared,
                            DELIVERY_WINDOW_MS,
                            ACK_TIMEOUT_MS,
                        )
                        .await
                    }
                    Err(_) => false,
                };
                retry.finish(progress);
                // Retry on future samples, never spin on a signal left over
                // from a connection that just ended. Sampling never stops.
                shared.changed.reset();
            }
        };
        join(sampling, radio).await;
    }

    #[cfg(not(feature = "ble-persistent"))]
    {
        let mut initialized = false;
        let mut sequence = 0u32;
        loop {
            let sample = sample(&mut bus, &mut initialized).await;
            outbox.push(packet::encode(boot, sequence, SLEEP_SECONDS, sample));
            if retry.due() {
                let radio_peripherals = nrf_sdc::Peripherals::new(
                    p.PPI_CH17.reborrow(),
                    p.PPI_CH18.reborrow(),
                    p.PPI_CH20.reborrow(),
                    p.PPI_CH21.reborrow(),
                    p.PPI_CH22.reborrow(),
                    p.PPI_CH23.reborrow(),
                    p.PPI_CH24.reborrow(),
                    p.PPI_CH25.reborrow(),
                    p.PPI_CH26.reborrow(),
                    p.PPI_CH27.reborrow(),
                    p.PPI_CH28.reborrow(),
                    p.PPI_CH29.reborrow(),
                );
                let controller = nrf_sdc::Builder::new()
                    .and_then(|b| b.support_adv().support_peripheral().peripheral_count(1))
                    .and_then(|b| b.buffer_cfg(27, 27, 3, 3))
                    .and_then(|b| {
                        b.build(radio_peripherals, &mut rng, mpsl, &mut controller_memory)
                    });
                let progress = match controller {
                    Ok(controller) => {
                        crate::ble_link::deliver(
                            controller,
                            address,
                            &mut outbox,
                            DELIVERY_WINDOW_MS,
                        )
                        .await
                    }
                    Err(_) => false,
                };
                retry.finish(progress);
            }
            sequence = sequence.wrapping_add(1);
            Timer::after_secs(u64::from(SLEEP_SECONDS)).await;
        }
    }
}

async fn sample(bus: &mut Bus<'_>, initialized: &mut bool) -> Result<(i16, u16), u8> {
    let result = with_timeout(Duration::from_millis(600), async {
        if !*initialized {
            aht10::initialize(bus).await?;
            *initialized = true;
        }
        aht10::measure(bus).await
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
        *initialized = false;
        bus.recover().await;
    }
    sample
}
