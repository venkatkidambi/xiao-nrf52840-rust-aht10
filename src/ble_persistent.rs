//! Notification delivery on a retained connection, with no polling or heap.
use core::cell::{Cell, RefCell};
use embassy_futures::select::{Either, select};
use embassy_sync::{blocking_mutex::raw::NoopRawMutex, signal::Signal};
use embassy_time::{Duration, Instant, Timer, with_timeout};
use trouble_host::prelude::*;

use crate::{
    ble_link::ADVERTISEMENT,
    delivery::{Outbox, Session},
    packet::Packet as Sample,
};

// Accessed only by futures in the same executor task, never by an ISR. Every
// RefCell borrow ends before an await; sensor I/O never locks out BLE handling.
pub struct Shared {
    outbox: RefCell<Outbox>,
    connected: Cell<bool>,
    pub changed: Signal<NoopRawMutex, ()>,
    watchdog: Signal<NoopRawMutex, ()>,
}

impl Shared {
    pub const fn new() -> Self {
        Self {
            outbox: RefCell::new(Outbox::new()),
            connected: Cell::new(false),
            changed: Signal::new(),
            watchdog: Signal::new(),
        }
    }

    pub fn push(&self, packet: Sample) {
        if self.connected.get() {
            self.outbox.borrow_mut().push_preserving_head(packet);
        } else {
            self.outbox.borrow_mut().push(packet);
        }
        self.changed.signal(());
        self.watchdog.signal(());
    }

    fn front(&self) -> Option<Sample> {
        self.outbox.borrow().front().copied()
    }
}

struct Connected<'a>(&'a Cell<bool>);

impl Drop for Connected<'_> {
    fn drop(&mut self) {
        self.0.set(false);
    }
}

#[gatt_server]
struct Server {
    readings: ReadingService,
}

#[gatt_service(uuid = "a8170001-9c67-4f88-9b44-7d52e2b16a10")]
struct ReadingService {
    // Preserve the original read/ACK handles. The extra characteristics also
    // let receivers discover this mode without a CLI flag or an extra read.
    #[characteristic(uuid = "a8170002-9c67-4f88-9b44-7d52e2b16a10", read)]
    sample: [u8; 20],
    #[characteristic(uuid = "a8170003-9c67-4f88-9b44-7d52e2b16a10", write)]
    ack: [u8; 8],
    #[characteristic(uuid = "a8170004-9c67-4f88-9b44-7d52e2b16a10", notify)]
    stream: [u8; 20],
    #[characteristic(uuid = "a8170005-9c67-4f88-9b44-7d52e2b16a10", read)]
    link: [u8; 6],
}

pub async fn deliver(
    controller: nrf_sdc::SoftdeviceController<'_>,
    address: [u8; 6],
    shared: &Shared,
    window_ms: u32,
    ack_timeout_ms: u32,
) -> bool {
    let mut resources: HostResources<DefaultPacketPool, 1, 2> = HostResources::new();
    let stack = trouble_host::new(controller, &mut resources)
        .set_random_address(Address::random(address))
        .build();
    let mut runner = stack.runner();
    let mut peripheral = stack.peripheral();
    let server = Server::new_with_config(GapConfig::Peripheral(PeripheralConfig {
        name: "AHT10",
        appearance: &appearance::sensor::GENERIC_SENSOR,
    }))
    .unwrap();
    let mut progress = false;
    let attempt = async {
        let params = AdvertisementParameters {
            interval_min: Duration::from_millis(20),
            interval_max: Duration::from_millis(20),
            ..Default::default()
        };
        // Advertising/accept remains bounded; a healthy connection has no
        // lifetime deadline. SDC is dropped when this attempt ends for any reason.
        let connect = async {
            peripheral
                .advertise(
                    &params,
                    Advertisement::ConnectableScannableUndirected {
                        adv_data: &ADVERTISEMENT,
                        scan_data: &[],
                    },
                )
                .await?
                .accept()
                .await?
                .with_attribute_server(&server)
                .map_err(BleHostError::from)
        };
        let Ok(Ok(connection)) =
            with_timeout(Duration::from_millis(u64::from(window_ms)), connect).await
        else {
            return;
        };
        shared.connected.set(true);
        // Also clears on controller failure/cancellation of this whole future.
        let _connected = Connected(&shared.connected);

        let tune = async {
            // Allow service discovery and initial delivery at the central's
            // initial rate. One request per connection; rejection is harmless.
            // Keep TrouBLE's connection-params-update feature OFF: the pinned
            // peripheral-only SDC lacks the HCI LeConnUpdate symbol. Without
            // that feature this API sends the peripheral L2CAP update request.
            Timer::after_secs(5).await;
            let _ = connection
                .raw()
                .update_connection_params(
                    &stack,
                    &RequestedConnParams {
                        min_connection_interval: Duration::from_millis(30),
                        max_connection_interval: Duration::from_millis(45),
                        max_latency: 19,
                        supervision_timeout: Duration::from_secs(6),
                        min_event_length: Duration::from_millis(0),
                        max_event_length: Duration::from_millis(0),
                    },
                )
                .await;
            core::future::pending::<()>().await;
        };
        let session = RefCell::new(Session::new());
        let watchdog = async {
            loop {
                let deadline = session.borrow_mut().observe(
                    shared.front().is_some(),
                    Instant::now().as_millis(),
                    ack_timeout_ms,
                );
                match deadline {
                    Some(ms) => {
                        if matches!(
                            select(Timer::at(Instant::from_millis(ms)), shared.watchdog.wait())
                                .await,
                            Either::First(())
                        ) {
                            break;
                        }
                    }
                    None => shared.watchdog.wait().await,
                }
            }
        };
        let service = async {
            let mut notified: Option<Sample> = None;
            loop {
                let head = shared.front();
                let work = async {
                    if let Some(packet) = head {
                        if notified != Some(packet)
                            && server.readings.stream.should_notify(&connection)
                        {
                            server
                                .readings
                                .stream
                                .notify(&connection, &packet, false)
                                .await?;
                            session.borrow_mut().offer(&packet);
                            notified = Some(packet);
                        }
                    }
                    match select(connection.next(), shared.changed.wait()).await {
                        Either::Second(()) => {}
                        Either::First(GattConnectionEvent::Disconnected { .. }) => {
                            return Ok(false);
                        }
                        Either::First(GattConnectionEvent::Gatt { event }) => {
                            let reply = match event {
                                GattEvent::Read(event)
                                    if event.handle() == server.readings.sample.handle =>
                                {
                                    let packet = shared.front().unwrap_or([0; 20]);
                                    if packet != [0; 20] {
                                        session.borrow_mut().offer(&packet);
                                    }
                                    event.accept_unprocessed(&packet)
                                }
                                GattEvent::Read(event)
                                    if event.handle() == server.readings.link.handle =>
                                {
                                    let params = connection.raw().params();
                                    let mut value = [0; 6];
                                    value[..2].copy_from_slice(
                                        &((params.conn_interval.as_micros() / 1250) as u16)
                                            .to_le_bytes(),
                                    );
                                    value[2..4]
                                        .copy_from_slice(&params.peripheral_latency.to_le_bytes());
                                    value[4..].copy_from_slice(
                                        &((params.supervision_timeout.as_millis() / 10) as u16)
                                            .to_le_bytes(),
                                    );
                                    event.accept_unprocessed(&value)
                                }
                                GattEvent::Write(event)
                                    if event.handle() == server.readings.ack.handle =>
                                {
                                    let valid = event.with_data(|offset, data| {
                                        offset == 0
                                            && session
                                                .borrow_mut()
                                                .acknowledge(&mut shared.outbox.borrow_mut(), data)
                                    });
                                    if valid {
                                        progress = true;
                                        shared.watchdog.signal(());
                                        notified = None;
                                        event.accept_unprocessed()
                                    } else {
                                        event.reject(AttErrorCode::VALUE_NOT_ALLOWED)
                                    }
                                }
                                _ => event.accept(),
                            };
                            if let Ok(reply) = reply {
                                reply.send().await;
                            }
                        }
                        _ => {}
                    }
                    Ok::<bool, Error>(true)
                };
                if !matches!(work.await, Ok(true)) {
                    break;
                }
            }
        };
        // Independent watchdog also covers a blocked ATT reply while the queue
        // was empty: the next sensor sample starts its deadline regardless of
        // what the GATT service future is awaiting.
        select(watchdog, select(service, tune)).await;
        connection.raw().disconnect();
        let _ = with_timeout(Duration::from_millis(100), async {
            loop {
                if matches!(
                    connection.next().await,
                    GattConnectionEvent::Disconnected { .. }
                ) {
                    break;
                }
            }
        })
        .await;
    };
    select(runner.run(), attempt).await;
    progress
}
