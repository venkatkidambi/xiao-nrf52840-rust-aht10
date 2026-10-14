//! Transmit-only BLE 1M advertising on channels 37/38/39, with hardware CRC
//! and whitening. No SoftDevice, connection, receive window, or background radio.
use core::{
    future::poll_fn,
    sync::atomic::{Ordering, compiler_fence},
    task::Poll,
};
use embassy_nrf::{
    Peri,
    interrupt::{self, typelevel::Interrupt},
    pac,
    peripherals::RADIO,
};
use embassy_sync::waitqueue::AtomicWaker;
use embassy_time::{Duration, with_timeout};

static WAKER: AtomicWaker = AtomicWaker::new();
static CLOCK_WAKER: AtomicWaker = AtomicWaker::new();

pub struct ClockInterruptHandler;
impl interrupt::typelevel::Handler<interrupt::typelevel::CLOCK_POWER> for ClockInterruptHandler {
    unsafe fn on_interrupt() {
        pac::CLOCK.intenclr().write(|w| w.set_hfclkstarted(true));
        CLOCK_WAKER.wake();
    }
}

pub struct InterruptHandler;
impl interrupt::typelevel::Handler<interrupt::typelevel::RADIO> for InterruptHandler {
    unsafe fn on_interrupt() {
        pac::RADIO.intenclr().write(|w| w.set_disabled(true));
        WAKER.wake();
    }
}

pub struct Beacon<'d> {
    _radio: Peri<'d, RADIO>,
}

// Own the DMA buffer for the entire transfer. Dropping a cancelled/timed-out
// event resets the peripheral BEFORE its buffer is released or moved.
struct Event {
    packet: [u8; crate::packet::PACKET_LEN],
}

impl Drop for Event {
    fn drop(&mut self) {
        pac::RADIO.intenclr().write(|w| w.0 = u32::MAX);
        pac::RADIO.power().write(|w| w.set_power(false));
        cortex_m::asm::dsb();
        compiler_fence(Ordering::SeqCst);
        pac::CLOCK.intenclr().write(|w| w.set_hfclkstarted(true));
        pac::CLOCK.tasks_hfclkstop().write_value(1);
    }
}

impl<'d> Beacon<'d> {
    pub fn new(
        radio: Peri<'d, RADIO>,
        _irq: impl interrupt::typelevel::Binding<interrupt::typelevel::RADIO, InterruptHandler>
        + interrupt::typelevel::Binding<
            interrupt::typelevel::CLOCK_POWER,
            ClockInterruptHandler,
        >,
    ) -> Self {
        interrupt::typelevel::RADIO::unpend();
        unsafe { interrupt::typelevel::RADIO::enable() };
        interrupt::typelevel::CLOCK_POWER::unpend();
        unsafe { interrupt::typelevel::CLOCK_POWER::enable() };
        Self { _radio: radio }
    }

    pub async fn send(&mut self, packet: [u8; crate::packet::PACKET_LEN]) -> Result<(), ()> {
        let event = Event { packet };
        let r = pac::RADIO;
        r.power().write(|w| w.set_power(false));
        r.power().write(|w| w.set_power(true));
        pac::CLOCK.events_hfclkstarted().write_value(0);
        pac::CLOCK.intenset().write(|w| w.set_hfclkstarted(true));
        pac::CLOCK.tasks_hfclkstart().write_value(1);
        with_timeout(
            Duration::from_millis(10),
            poll_fn(|cx| {
                CLOCK_WAKER.register(cx.waker());
                if pac::CLOCK.events_hfclkstarted().read() != 0 {
                    Poll::Ready(())
                } else {
                    Poll::Pending
                }
            }),
        )
        .await
        .map_err(|_| ())?;
        use pac::radio::vals;
        r.mode().write(|w| w.set_mode(vals::Mode::Ble1mbit));
        r.txpower().write(|w| w.set_txpower(vals::Txpower::_0dBm));
        r.modecnf0().write(|w| w.set_ru(vals::Ru::Fast));
        r.pcnf0().write(|w| {
            w.set_s0len(true);
            w.set_lflen(8);
            w.set_plen(vals::Plen::_8bit);
        });
        r.pcnf1().write(|w| {
            w.set_maxlen(37);
            w.set_balen(3);
            w.set_endian(vals::Endian::Little);
            w.set_whiteen(true);
        });
        // The high 24 bits of BASE0 precede PREFIX0 on air (LSB first).
        r.base0().write_value(0x89be_d600);
        r.prefix0().write(|w| w.set_ap0(0x8e));
        r.txaddress().write(|w| w.set_txaddress(0));
        r.crccnf().write(|w| {
            w.set_len(vals::Len::Three);
            w.set_skipaddr(vals::Skipaddr::Skip);
        });
        r.crcpoly().write(|w| w.set_crcpoly(0x0000_065b));
        r.crcinit().write(|w| w.set_crcinit(0x0055_5555));
        r.shorts().write(|w| {
            w.set_ready_start(true);
            w.set_end_disable(true);
        });
        // event is an async-frame local, pinned across every DMA await.
        r.packetptr().write_value(event.packet.as_ptr() as u32);
        compiler_fence(Ordering::SeqCst);
        for (channel, frequency) in [(37, 2), (38, 26), (39, 80)] {
            r.frequency().write(|w| w.set_frequency(frequency));
            r.datawhiteiv().write(|w| w.set_datawhiteiv(channel));
            r.events_disabled().write_value(0);
            r.intenset().write(|w| w.set_disabled(true));
            r.tasks_txen().write_value(1);
            with_timeout(
                Duration::from_millis(5),
                poll_fn(|cx| {
                    WAKER.register(cx.waker());
                    if r.events_disabled().read() != 0 {
                        Poll::Ready(())
                    } else {
                        Poll::Pending
                    }
                }),
            )
            .await
            .map_err(|_| ())?;
        }
        drop(event); // Radio and HFXO off even between advertising events.
        Ok(())
    }
}
