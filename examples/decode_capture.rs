//! Decode a raw capture and print per-channel statistics.
//!
//!     cargo run --example decode_capture -- captures/<name>.bin <channels> [chunk-bytes]

use open_cognionics::legacy::{Event, Layout, LegacyDecoder};
use open_cognionics::scale;
use std::collections::BTreeMap;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let path = args.get(1).expect("path to a .bin capture");
    let channels: usize = args
        .get(2)
        .expect("channel count")
        .parse()
        .expect("a number");
    let chunk: usize = args.get(3).map_or(4096, |s| s.parse().expect("a number"));
    let data = std::fs::read(path).expect("readable capture");

    let layout = Layout {
        eeg: channels,
        ext: 0,
        acc: 0,
    };
    let mut decoder = LegacyDecoder::new(layout);
    let mut events = Vec::new();
    for piece in data.chunks(chunk) {
        decoder.feed(piece, &mut events);
    }

    let (mut packets, mut lost, mut skipped) = (0u64, 0u64, 0usize);
    let mut sum = vec![0f64; channels];
    let mut sum_sq = vec![0f64; channels];
    let mut min = vec![i32::MAX; channels];
    let mut max = vec![i32::MIN; channels];
    let mut status: BTreeMap<u8, u64> = BTreeMap::new();
    let mut battery: BTreeMap<u8, u64> = BTreeMap::new();
    let mut trigger: BTreeMap<u16, u64> = BTreeMap::new();
    for event in &events {
        match event {
            Event::Lost(n) => lost += *n as u64,
            Event::Skipped(n) => skipped += n,
            Event::Packet(p) => {
                packets += 1;
                *status.entry(p.impedance_status).or_default() += 1;
                *battery.entry(p.battery).or_default() += 1;
                *trigger.entry(p.trigger).or_default() += 1;
                for (c, &v) in p.raw.iter().enumerate() {
                    sum[c] += v as f64;
                    sum_sq[c] += (v as f64) * (v as f64);
                    min[c] = min[c].min(v);
                    max[c] = max[c].max(v);
                }
            }
        }
    }

    println!(
        "bytes {}  packets {packets}  lost {lost}  skipped bytes {skipped}",
        data.len()
    );
    println!("impedance status bytes: {status:?}");
    println!(
        "battery bytes: {:?}",
        battery
            .iter()
            .map(|(b, n)| format!("{b} ({:.2} V) x{n}", scale::battery_volts(*b)))
            .collect::<Vec<_>>()
    );
    println!("trigger values: {trigger:?}");
    let uv = scale::eeg_volts_per_count(3) * 1e6;
    println!("ch       mean_counts    sd_counts         min         max   sd_uV(gain 3)");
    for c in 0..channels {
        let n = packets as f64;
        let mean = sum[c] / n;
        let sd = (sum_sq[c] / n - mean * mean).max(0.0).sqrt();
        println!(
            "{c:2} {mean:16.1} {sd:12.1} {:11} {:11} {:15.2}",
            min[c],
            max[c],
            sd * uv
        );
    }
}
