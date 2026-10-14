//! One owned TWIM transaction at a time, with bounded waits and bus clearing.
use embassy_nrf::{
    Peri,
    gpio::{Flex, OutputDrive, Pull},
    peripherals::{P0_04, P0_05, TWISPI0},
    twim::{self, Twim},
};
use embassy_time::{Duration, Timer, with_timeout};
use embedded_hal::i2c::Operation;

pub struct Bus<'d> {
    twim: Peri<'d, TWISPI0>,
    sda: Peri<'d, P0_04>,
    scl: Peri<'d, P0_05>,
}

struct Transfer<'d>(Option<Twim<'d>>);

impl Drop for Transfer<'_> {
    fn drop(&mut self) {
        // Also runs when the outer sample deadline cancels this future.
        drop(self.0.take());
        cortex_m::asm::dsb();
        core::sync::atomic::compiler_fence(core::sync::atomic::Ordering::SeqCst);
    }
}

impl<'d> Bus<'d> {
    pub fn new(twim: Peri<'d, TWISPI0>, sda: Peri<'d, P0_04>, scl: Peri<'d, P0_05>) -> Self {
        Self { twim, sda, scl }
    }

    async fn transfer(&mut self, op: Operation<'_>) -> Result<(), twim::Error> {
        let mut config = twim::Config::default();
        config.frequency = twim::Frequency::K100;
        config.sda_pullup = true;
        config.scl_pullup = true;
        // EasyDMA cannot read command constants from flash.
        let mut tx_ram = [0; 3];
        let mut twim = Transfer(Some(Twim::new(
            self.twim.reborrow(),
            crate::Irqs,
            self.sda.reborrow(),
            self.scl.reborrow(),
            config,
            &mut tx_ram,
        )));
        let result = with_timeout(
            Duration::from_millis(25),
            twim.0.as_mut().unwrap().transaction(0x38, &mut [op]),
        )
        .await;
        // REQUIRED: embassy-nrf 0.11's transfer future is not cancellation
        // safe by itself. Twim::drop disables the peripheral (and EasyDMA)
        // before tx_ram or the caller's RX buffer can go out of scope.
        // Reconstructing also clears stale events before the next transfer.
        drop(twim);
        result.unwrap_or(Err(twim::Error::Timeout))
    }

    pub async fn write(&mut self, bytes: &[u8]) -> Result<(), twim::Error> {
        self.transfer(Operation::Write(bytes)).await
    }

    pub async fn read(&mut self, bytes: &mut [u8]) -> Result<(), twim::Error> {
        self.transfer(Operation::Read(bytes)).await
    }

    pub async fn recover(&mut self) {
        // TWIM has already been dropped. Open drain: never drive a line high.
        // Nine clocks and a STOP can release a slave left mid-byte. A physical
        // short cannot be repaired in software, but this loop always exits.
        let mut sda = Flex::new(self.sda.reborrow());
        let mut scl = Flex::new(self.scl.reborrow());
        sda.set_high();
        scl.set_high();
        sda.set_as_input_output(Pull::Up, OutputDrive::Standard0Disconnect1);
        scl.set_as_input_output(Pull::Up, OutputDrive::Standard0Disconnect1);
        Timer::after_micros(10).await;
        for _ in 0..9 {
            if sda.is_high() || scl.is_low() {
                break;
            }
            scl.set_low();
            Timer::after_micros(10).await;
            scl.set_high();
            Timer::after_micros(10).await;
        }
        scl.set_low();
        sda.set_low();
        Timer::after_micros(10).await;
        scl.set_high();
        Timer::after_micros(10).await;
        sda.set_high();
        Timer::after_micros(10).await;
    }
}
