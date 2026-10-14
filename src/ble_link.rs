//! One connection per bounded delivery window. Controller ownership is scoped
//! to the attempt: cancellation drops SDC and disables all radio procedures.
#[cfg(not(feature = "ble-persistent"))]
use crate::delivery::Outbox;
#[cfg(not(feature = "ble-persistent"))]
use embassy_futures::select::select;
#[cfg(not(feature = "ble-persistent"))]
use embassy_time::{Duration, with_timeout};
use trouble_host::prelude::*;

// UUIDs are shared with tools/ble_receive.py. No pairing/CCCD/MTU negotiation
// is required: a 20-byte read and 8-byte ACK fit the default ATT MTU of 23.
#[gatt_server]
struct Server {
    readings: ReadingService,
}
#[gatt_service(uuid = "a8170001-9c67-4f88-9b44-7d52e2b16a10")]
struct ReadingService {
    #[characteristic(uuid = "a8170002-9c67-4f88-9b44-7d52e2b16a10", read)]
    sample: [u8; 20],
    #[characteristic(uuid = "a8170003-9c67-4f88-9b44-7d52e2b16a10", write)]
    ack: [u8; 8],
}

// Flags + complete 128-bit service UUID. No scan response/name needed.
pub(crate) const ADVERTISEMENT: [u8; 21] = [
    2, 1, 6, 17, 7, 0x10, 0x6a, 0xb1, 0xe2, 0x52, 0x7d, 0x44, 0x9b, 0x88, 0x4f, 0x67, 0x9c, 0x01,
    0x00, 0x17, 0xa8,
];

#[cfg(not(feature = "ble-persistent"))]
pub async fn deliver<C: Controller>(
    controller: C,
    address: [u8; 6],
    outbox: &mut Outbox,
    window_ms: u32,
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
        let advertiser = peripheral
            .advertise(
                &params,
                Advertisement::ConnectableScannableUndirected {
                    adv_data: &ADVERTISEMENT,
                    scan_data: &[],
                },
            )
            .await?;
        let connection = advertiser.accept().await?.with_attribute_server(&server)?;
        let mut served = false;
        loop {
            match connection.next().await {
                GattConnectionEvent::Disconnected { .. } => break,
                GattConnectionEvent::Gatt { event } => {
                    let reply = match event {
                        GattEvent::Read(event)
                            if event.handle() == server.readings.sample.handle =>
                        {
                            if let Some(packet) = outbox.front() {
                                served = true;
                                event.accept_unprocessed(packet)
                            } else {
                                event.accept_unprocessed(&[0u8; 20])
                            }
                        }
                        GattEvent::Write(event) if event.handle() == server.readings.ack.handle => {
                            let valid = event.with_data(|offset, data| {
                                offset == 0 && served && outbox.acknowledge(data)
                            });
                            if valid {
                                progress = true;
                                served = false;
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
                    if outbox.front().is_none() {
                        connection.raw().disconnect();
                        // Graceful termination is also within the original deadline.
                        // Never wait for the peer's supervision timeout.
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
                        break;
                    }
                }
                _ => {}
            }
        }
        Ok::<(), BleHostError<C::Error>>(())
    };
    // The outer deadline is never restarted by connections, reads or bad ACKs.
    // Dropping stack after cancellation drops SDC synchronously (sdc_disable),
    // rather than relying on an asynchronously queued advertising cancellation.
    let _ = with_timeout(
        Duration::from_millis(u64::from(window_ms)),
        select(runner.run(), attempt),
    )
    .await;
    progress
}
