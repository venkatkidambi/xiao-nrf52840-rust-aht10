#![no_std]
#![no_main]

mod aht10;
#[cfg(not(feature = "usb-serial"))]
mod beacon;
#[cfg(not(feature = "usb-serial"))]
mod ble_mode;
mod bus;
#[cfg(not(feature = "usb-serial"))]
mod packet;
mod reading;
#[cfg(feature = "usb-serial")]
mod serial;
#[cfg(feature = "usb-serial")]
mod usb_mode;

use embassy_executor::Spawner;
#[cfg(feature = "usb-serial")]
use embassy_nrf::usb;
use embassy_nrf::{bind_interrupts, peripherals, twim};

#[cfg(feature = "usb-serial")]
type Reports = embassy_sync::signal::Signal<
    embassy_sync::blocking_mutex::raw::NoopRawMutex,
    Result<aht10::Reading, aht10::Error>,
>;

#[cfg(feature = "usb-serial")]
bind_interrupts!(pub struct Irqs {
    USBD => usb::InterruptHandler<peripherals::USBD>;
    CLOCK_POWER => usb::vbus_detect::InterruptHandler;
    TWISPI0 => twim::InterruptHandler<peripherals::TWISPI0>;
});
#[cfg(not(feature = "usb-serial"))]
bind_interrupts!(pub struct Irqs {
    TWISPI0 => twim::InterruptHandler<peripherals::TWISPI0>;
    RADIO => beacon::InterruptHandler;
    CLOCK_POWER => beacon::ClockInterruptHandler;
    RNG => embassy_nrf::rng::InterruptHandler<peripherals::RNG>;
});

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    #[cfg(feature = "usb-serial")]
    usb_mode::run(spawner).await;
    #[cfg(not(feature = "usb-serial"))]
    ble_mode::run(spawner).await;
}

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    cortex_m::peripheral::SCB::sys_reset()
}
