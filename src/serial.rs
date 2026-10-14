use core::fmt::Write;
use embassy_futures::join::join;
use embassy_time::{Duration, Timer, with_timeout};
use embassy_usb::{
    class::cdc_acm::{CdcAcmClass, Sender},
    driver::Driver,
};

async fn send<'d, D: Driver<'d>>(sender: &mut Sender<'d, D>, bytes: &[u8]) -> Result<(), ()> {
    for packet in bytes.chunks(64) {
        if !sender.dtr() {
            return Err(());
        }
        with_timeout(Duration::from_millis(200), sender.write_packet(packet))
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
    }
    if bytes.len().is_multiple_of(64) {
        with_timeout(Duration::from_millis(200), sender.write_packet(&[]))
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
    }
    Ok(())
}

pub async fn run<'d, D: Driver<'d>>(cdc: CdcAcmClass<'d, D>, reports: &crate::Reports) {
    let (mut sender, mut receiver) = cdc.split();
    let tx = async {
        loop {
            sender.wait_connection().await;
            while !sender.dtr() {
                Timer::after_millis(20).await;
            }
            let mut announced = false;
            while sender.dtr() {
                // Bound this wait too, to notice a terminal close/reopen even
                // if the sensor stops producing reports.
                let Ok(report) = with_timeout(Duration::from_millis(100), reports.wait()).await
                else {
                    continue;
                };
                let mut line = heapless::String::<128>::new();
                match report {
                    Ok(reading) => {
                        if !announced {
                            if send(&mut sender, b"AHT10 initialized\r\n").await.is_err() {
                                break;
                            }
                            announced = true;
                        }
                        let _ = write!(
                            line,
                            "Temperature: {:.2} °C | Humidity: {:.2} %\r\n",
                            reading.temperature_c, reading.humidity_percent
                        );
                    }
                    Err(error) => {
                        announced = false;
                        let _ = write!(line, "AHT10 error: {error}; retrying\r\n");
                    }
                }
                if send(&mut sender, line.as_bytes()).await.is_err() {
                    break;
                }
            }
            Timer::after_millis(20).await;
        }
    };
    let rx = async {
        let mut buffer = [0; 64];
        loop {
            receiver.wait_connection().await;
            while receiver.read_packet(&mut buffer).await.is_ok() {}
        }
    };
    join(tx, rx).await;
}
