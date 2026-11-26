//! Record the 10 s fixture, minus one packet, into a session directory.
//!
//!     cargo run --example record_fixture -- <output-dir> [disconnect]
//!
//! Packet 100 is removed so that the recording contains one lost sample;
//! markers "first" and "second" are set at samples 500 and 2500. With the
//! `disconnect` argument the stream stops after 1200 samples (2.4 s) with a
//! `StreamEvent::Disconnected`, as when the link is lost mid-recording.

use open_cognionics::chunk::{Chunker, StreamEvent};
use open_cognionics::config::{extract_words, DeviceConfig};
use open_cognionics::record::{Recorder, SessionMeta};
use std::path::PathBuf;

const PACKET: usize = 75;
const REMOVED_PACKET: usize = 100;

fn main() {
    let dir = PathBuf::from(std::env::args().nth(1).expect("output directory"));
    let mut bytes = std::fs::read("tests/fixtures/onhead_10s.bin").expect("fixture");
    bytes.drain(REMOVED_PACKET * PACKET..(REMOVED_PACKET + 1) * PACKET);
    let disconnect = std::env::args().nth(2).as_deref() == Some("disconnect");
    if disconnect {
        // 1199 packets plus the removed one make 1200 samples.
        bytes.truncate(1199 * PACKET);
    }

    let reply = std::fs::read("tests/fixtures/config_reply.bin").expect("config fixture");
    let words = extract_words(&reply).expect("configuration block").words;
    let config = DeviceConfig::from_words(&words).expect("configuration");

    let mut chunker = Chunker::new(config.layout, config.sample_rate);
    let meta = SessionMeta {
        participant: "P001".into(),
        participant_uuid: None,
        start_unix: 1_795_000_000.0,
        config,
        port: "fixture".into(),
        software_version: env!("CARGO_PKG_VERSION").into(),
    };
    let mut recorder = Recorder::start(&dir, meta).expect("start recording");
    recorder.mark("first", 500, 1_795_000_001.0);
    recorder.mark("second", 2500, 1_795_000_005.0);
    for (i, part) in bytes.chunks(4096).enumerate() {
        recorder.raw(part);
        let mut events = Vec::new();
        chunker.push(part, 1_795_000_000.0 + i as f64 * 0.1, &mut events);
        for event in &events {
            recorder.event(event);
        }
    }
    if disconnect {
        recorder.event(&StreamEvent::Disconnected("link lost".into()));
    }
    recorder.finish().expect("finish recording");
}
