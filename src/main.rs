#![no_std]
#![no_main]

mod aht10;
mod bus;
mod reading;
mod serial;

use embassy_executor::Spawner;
use embassy_futures::join::join3;
use embassy_nrf::{bind_interrupts, config, peripherals, twim, usb};
use embassy_sync::{blocking_mutex::raw::NoopRawMutex, signal::Signal};
use embassy_time::{Duration, Instant, Timer};
use embassy_usb::class::cdc_acm::{CdcAcmClass, State};

bind_interrupts!(pub struct Irqs {
    USBD => usb::InterruptHandler<peripherals::USBD>;
    CLOCK_POWER => usb::vbus_detect::InterruptHandler;
    TWISPI0 => twim::InterruptHandler<peripherals::TWISPI0>;
});

type Reports = Signal<NoopRawMutex, Result<aht10::Reading, aht10::Error>>;

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    // cortex-m-rt's set-vtor runs before this entry point: IRQs go directly
    // to this image, not through the disabled SoftDevice's forwarding table.
    let mut config = config::Config::default();
    config.hfclk_source = config::HfclkSource::ExternalXtal;
    config.lfclk_source = config::LfclkSource::ExternalXtal;
    config.debug = config::Debug::NotConfigured;
    let p = embassy_nrf::init(config);

    let driver = usb::Driver::new(
        p.USBD,
        Irqs,
        usb::vbus_detect::HardwareVbusDetect::new(Irqs),
    );
    // Development-only IDs, as in Embassy's USB example; obtain assigned
    // VID/PID before distributing a product.
    let mut config = embassy_usb::Config::new(0xc0de, 0xcafe);
    config.manufacturer = Some("Rust firmware");
    config.product = Some("XIAO AHT10 Serial");
    let mut serial_number = heapless::String::<16>::new();
    use core::fmt::Write;
    let _ = write!(
        serial_number,
        "{:08X}{:08X}",
        embassy_nrf::pac::FICR.deviceid(1).read(),
        embassy_nrf::pac::FICR.deviceid(0).read()
    );
    config.serial_number = Some(serial_number.as_str());
    config.max_power = 100;
    config.max_packet_size_0 = 64;

    let mut config_descriptor = [0; 256];
    let mut bos_descriptor = [0; 256];
    let mut msos_descriptor = [0; 128];
    let mut control_buf = [0; 64];
    let mut state = State::new();
    let mut builder = embassy_usb::Builder::new(
        driver,
        config,
        &mut config_descriptor,
        &mut bos_descriptor,
        &mut msos_descriptor,
        &mut control_buf,
    );
    let cdc = CdcAcmClass::new(&mut builder, &mut state, 64);
    let mut usb = builder.build();
    let bus = bus::Bus::new(p.TWISPI0, p.P0_04, p.P0_05);
    let reports = Reports::new();

    // All futures are polled independently. Neither an absent terminal nor
    // a faulty sensor prevents USB control requests from being serviced.
    join3(usb.run(), sample(bus, &reports), serial::run(cdc, &reports)).await;
}

async fn sample(mut bus: bus::Bus<'_>, reports: &Reports) {
    Timer::after_millis(40).await;
    let mut initialized = false;
    loop {
        let next = Instant::now() + Duration::from_secs(1);
        let result = async {
            if !initialized {
                aht10::initialize(&mut bus).await?;
                initialized = true;
            }
            aht10::measure(&mut bus).await
        }
        .await;
        reports.signal(result);
        if result.is_err() {
            initialized = false;
            bus.recover().await;
        }
        // Normal measurement starts are one second apart; never burst to
        // catch up after errors. All sensor waits have bounded durations.
        Timer::at(next).await;
    }
}

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    // No debugger, semihosting, or RTT required. Unexpected software faults
    // reset the application; ordinary sensor errors never use this path.
    cortex_m::peripheral::SCB::sys_reset()
}
