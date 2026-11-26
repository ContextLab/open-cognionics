//! Check the library against a real headset.
//!
//!     cargo run --features serial --example hardware_check -- <seconds> <out-dir> [port]
//!
//! Opens the headset (this sends the configuration-dump command, 0x14, and
//! nothing else), prints its configuration, records for the given time and
//! reports samples, lost samples and how the stream ended. `<out-dir>` must
//! not hold a recording already.

use open_cognionics::chunk::StreamEvent;
use open_cognionics::record::{Recording, SessionMeta};
use open_cognionics::transport::Headset;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage: hardware_check <seconds> <out-dir> [port]";

/// Lock a mutex, carrying on if a panic elsewhere poisoned it.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn main() {
    // Errors are printed with their Display text.
    if let Err(e) = run() {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let seconds: f64 = args.get(1).ok_or(USAGE)?.parse()?;
    let out = args.get(2).ok_or(USAGE)?;
    let port = args.get(3).map(String::as_str);

    let headset = Headset::open(port)?;
    let config = headset.config().clone();
    println!("port: {}", headset.port());
    println!(
        "layout: {:?}  rate: {} Hz  gain: {}  impedance at power-on: {}  compressed: {}",
        config.layout,
        config.sample_rate,
        config.gain,
        config.impedance_at_power_on,
        config.compressed
    );
    println!("labels: {}", config.labels.join(" "));

    let meta = SessionMeta {
        participant: "hardware-check".into(),
        participant_uuid: None,
        start_unix: SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64(),
        config,
        port: headset.port().to_string(),
        software_version: env!("CARGO_PKG_VERSION").into(),
    };
    // The recording is fed from the raw bytes on the reader thread; the
    // events below are only counted.
    let recording = Arc::new(Mutex::new(Some(Recording::start(Path::new(out), meta)?)));
    let events = headset.subscribe(4096);
    let sink_recording = recording.clone();
    let sink = headset.add_raw_sink(Box::new(move |bytes, _index, arrival| {
        if let Some(r) = lock(&sink_recording).as_mut() {
            r.take(bytes, arrival);
        }
    }));

    let (mut samples, mut lost, mut chunks) = (0u64, 0u64, 0u64);
    let mut ended = String::from("time elapsed");
    let (mut battery, mut impedance) = (0.0, false);
    let deadline = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < deadline {
        let event = match events.recv_timeout(Duration::from_millis(500)) {
            Ok(event) => event,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                ended = "event channel closed".into();
                break;
            }
        };
        match event {
            StreamEvent::Chunk(c) => {
                samples += c.samples as u64;
                chunks += 1;
                battery = c.battery_volts;
                impedance = c.impedance_on;
            }
            StreamEvent::Lost { count, .. } => lost += count as u64,
            StreamEvent::Disconnected(reason) => {
                ended = format!("disconnected: {reason}");
                break;
            }
        }
    }
    headset.remove_raw_sink(sink);
    let index = headset.sample_index();
    let taken = lock(&recording).take();
    let (recorded, finished) = match taken {
        Some(mut r) => {
            if let Some(reason) = headset.disconnected() {
                r.disconnected(&reason);
            }
            let position = r.position();
            (position, r.finish().map_err(|e| e.to_string()))
        }
        None => (0, Err("no recording".to_string())),
    };
    println!("samples: {samples}  chunks: {chunks}  lost: {lost}  headset sample index: {index}");
    println!("samples recorded (lost ones included): {recorded}");
    println!("battery: {battery:.2} V  impedance check on: {impedance}");
    println!("ended: {ended}");
    match finished {
        Ok(()) => println!("recording finished: {out}"),
        Err(e) => println!("recording finish failed: {e}"),
    }
    Ok(())
}
