//! Tests for `Replay`, using the 10 s recording from a real Quick-20.

#[allow(dead_code)] // not every test uses every helper
mod common;

use common::temp::TempDir;
use open_cognionics::chunk::{Chunker, StreamEvent};
use open_cognionics::config::{extract_words, DeviceConfig};
use open_cognionics::record::{Recorder, SessionMeta};
use open_cognionics::replay::{Marker, Replay, ReplayError};
use std::path::{Path, PathBuf};
use std::time::Instant;

const PACKET: usize = 75;
const START: f64 = 1_795_000_000.0;

fn fixture() -> Vec<u8> {
    std::fs::read("tests/fixtures/onhead_10s.bin").unwrap()
}

fn config() -> DeviceConfig {
    let bytes = std::fs::read("tests/fixtures/config_reply.bin").unwrap();
    let block = extract_words(&bytes).unwrap();
    DeviceConfig::from_words(&block.words).unwrap()
}

fn tempdir(name: &str) -> TempDir {
    TempDir::new(&format!("replay-{name}"))
}

/// The chunker run the way replay runs it: no arrival times, so sample `n`
/// is stamped `start + n / sample_rate`.
fn expected(bytes: &[u8], start: f64) -> Vec<StreamEvent> {
    let cfg = config();
    let mut chunker = Chunker::nominal(cfg.layout, cfg.sample_rate, start);
    let mut out = Vec::new();
    let mut next = 0u64;
    for part in bytes.chunks(4096) {
        let before = out.len();
        chunker.push(part, start + next as f64 / cfg.sample_rate, &mut out);
        for e in &out[before..] {
            match e {
                StreamEvent::Chunk(c) => next = c.index + c.samples as u64,
                StreamEvent::Lost { index, count } => next = index + u64::from(*count),
                StreamEvent::Disconnected(_) => unreachable!(),
            }
        }
    }
    out
}

fn record(dir: &Path, bytes: &[u8]) {
    let meta = SessionMeta {
        participant: "P001".into(),
        participant_uuid: None,
        start_unix: START,
        config: config(),
        port: "fixture".into(),
        software_version: "test".into(),
    };
    let mut rec = Recorder::start(dir, meta).unwrap();
    rec.mark("first", 500, START + 1.0);
    rec.mark("second", 2500, START + 5.0);
    let cfg = config();
    let mut chunker = Chunker::new(cfg.layout, cfg.sample_rate);
    for (i, part) in bytes.chunks(4096).enumerate() {
        rec.raw(part);
        let mut out = Vec::new();
        chunker.push(part, START + i as f64 * 0.1, &mut out);
        for e in &out {
            rec.event(e);
        }
    }
    rec.finish().unwrap();
}

fn counts(events: &[StreamEvent]) -> Vec<i32> {
    events
        .iter()
        .filter_map(|e| match e {
            StreamEvent::Chunk(c) => Some(c.counts.clone()),
            _ => None,
        })
        .flatten()
        .collect()
}

/// A bare `raw.bin` in a new scratch directory; the directory goes when
/// the returned `TempDir` is dropped.
fn write_raw(name: &str, bytes: &[u8]) -> (TempDir, PathBuf) {
    let dir = tempdir(name);
    let path = dir.join("raw.bin");
    std::fs::write(&path, bytes).unwrap();
    (dir, path)
}

#[test]
fn directory_replay_gives_the_recorded_counts_and_exact_events() {
    let dir = tempdir("dir");
    let mut bytes = fixture();
    bytes.drain(100 * PACKET..101 * PACKET);
    record(&dir, &bytes);
    let replay = Replay::open(&dir, None, false).unwrap();
    assert_eq!(replay.config(), &config());
    let events: Vec<_> = replay.collect();
    assert_eq!(events, expected(&bytes, START));
    assert!(events.contains(&StreamEvent::Lost {
        index: 100,
        count: 1
    }));
    let total = counts(&events).len() / 23;
    assert_eq!(total, 4999);
}

#[test]
fn markers_are_read_from_the_session_directory() {
    let dir = tempdir("markers");
    record(&dir, &fixture());
    let replay = Replay::open(&dir, None, false).unwrap();
    assert_eq!(
        replay.markers(),
        [
            Marker {
                label: "first".into(),
                sample: 500,
                unix_time: START + 1.0
            },
            Marker {
                label: "second".into(),
                sample: 2500,
                unix_time: START + 5.0
            },
        ]
    );
}

#[test]
fn first_chunk_time_is_the_session_start() {
    let dir = tempdir("start");
    record(&dir, &fixture());
    let first = Replay::open(&dir, None, false).unwrap().next().unwrap();
    match first {
        StreamEvent::Chunk(c) => {
            assert_eq!(c.index, 0);
            assert_eq!(c.time, START);
        }
        other => panic!("expected chunk, got {other:?}"),
    }
}

#[test]
fn bare_raw_bin_without_config_needs_config() {
    let (_dir, path) = write_raw("needs", &fixture());
    match Replay::open(&path, None, false) {
        Err(ReplayError::NeedsConfig) => {}
        other => panic!("expected NeedsConfig, got {:?}", other.map(|_| ())),
    }
}

#[test]
fn bare_raw_bin_with_config_starts_at_zero() {
    let bytes = fixture();
    let (_dir, path) = write_raw("bare", &bytes);
    let replay = Replay::open(&path, Some(config()), false).unwrap();
    assert!(replay.markers().is_empty());
    let events: Vec<_> = replay.collect();
    assert_eq!(events, expected(&bytes, 0.0));
    assert_eq!(counts(&events).len(), 5000 * 23);
}

#[test]
fn directory_without_meta_behaves_like_bare_raw_bin() {
    let dir = tempdir("nometa");
    record(&dir, &fixture());
    std::fs::remove_file(dir.join("meta.json")).unwrap();
    assert!(matches!(
        Replay::open(&dir, None, false),
        Err(ReplayError::NeedsConfig)
    ));
    let events: Vec<_> = Replay::open(&dir, Some(config()), false).unwrap().collect();
    assert_eq!(events, expected(&fixture(), 0.0));
}

#[test]
fn malformed_meta_is_bad_meta() {
    let dir = tempdir("badmeta");
    record(&dir, &fixture());
    std::fs::write(dir.join("meta.json"), "{not json").unwrap();
    assert!(matches!(
        Replay::open(&dir, None, false),
        Err(ReplayError::BadMeta(_))
    ));
}

#[test]
fn missing_markers_file_means_no_markers() {
    let dir = tempdir("nomarkers");
    record(&dir, &fixture());
    std::fs::remove_file(dir.join("markers.jsonl")).unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert!(replay.markers().is_empty());
}

#[test]
fn malformed_marker_line_is_bad_markers() {
    let dir = tempdir("badmarkers");
    record(&dir, &fixture());
    std::fs::write(
        dir.join("markers.jsonl"),
        "{\"label\":\"a\",\"sample\":1,\"unix_time\":2.0}\nnot json\n",
    )
    .unwrap();
    assert!(matches!(
        Replay::open(&dir, None, false),
        Err(ReplayError::BadMarkers(_))
    ));
}

#[test]
fn missing_path_is_an_io_error() {
    let dir = tempdir("missing");
    assert!(matches!(
        Replay::open(&dir.join("nope"), Some(config()), false),
        Err(ReplayError::Io(_))
    ));
}

#[test]
fn realtime_replay_of_one_second_takes_about_one_second() {
    let bytes = fixture()[..500 * PACKET].to_vec();
    let (_dir, path) = write_raw("realtime", &bytes);
    let start = Instant::now();
    let replay = Replay::open(&path, Some(config()), true).unwrap();
    let events: Vec<_> = replay.collect();
    let elapsed = start.elapsed().as_secs_f64();
    assert_eq!(counts(&events).len(), 500 * 23);
    assert!((0.9..2.5).contains(&elapsed), "took {elapsed} s");
}

#[test]
fn non_realtime_replay_is_fast() {
    let (_dir, path) = write_raw("fast", &fixture());
    let start = Instant::now();
    let n = Replay::open(&path, Some(config()), false).unwrap().count();
    assert!(n > 0);
    assert!(start.elapsed().as_secs_f64() < 2.0);
}

#[test]
fn a_file_cut_mid_packet_delivers_every_whole_packet() {
    let bytes = fixture();
    let (_dir, path) = write_raw("cut", &bytes[..1000 * PACKET + 31]);
    let events: Vec<_> = Replay::open(&path, Some(config()), false)
        .unwrap()
        .collect();
    assert_eq!(counts(&events).len(), 1000 * 23);
    assert!(events.iter().all(|e| matches!(e, StreamEvent::Chunk(_))));
}

#[test]
fn overwritten_bytes_report_lost_and_replay_continues() {
    let mut bytes = fixture();
    for b in &mut bytes[2000 * PACKET - 15..2000 * PACKET + 15] {
        *b = 0x55;
    }
    let (_dir, path) = write_raw("corrupt", &bytes);
    let events: Vec<_> = Replay::open(&path, Some(config()), false)
        .unwrap()
        .collect();
    let lost: u32 = events
        .iter()
        .map(|e| match e {
            StreamEvent::Lost { count, .. } => *count,
            _ => 0,
        })
        .sum();
    assert!(lost >= 1, "expected a Lost event");
    let last = events
        .iter()
        .rev()
        .find_map(|e| match e {
            StreamEvent::Chunk(c) => Some(c),
            _ => None,
        })
        .unwrap();
    // The stream resumes and runs to the end: index counts lost samples.
    assert_eq!(last.index + last.samples as u64, 5000);
}

fn edit_meta(name: &str, edit: impl FnOnce(&mut serde_json::Value)) -> TempDir {
    let dir = tempdir(name);
    record(&dir, &fixture());
    let path = dir.join("meta.json");
    let mut value: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    edit(&mut value);
    std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
    dir
}

fn assert_bad_meta(dir: &Path, what: &str) {
    match Replay::open(dir, None, true) {
        Err(ReplayError::BadMeta(_)) => {}
        other => panic!("{what}: expected BadMeta, got {:?}", other.map(|_| ())),
    }
}

#[test]
fn hand_edited_meta_values_are_bad_meta() {
    use serde_json::json;
    type Edit = Box<dyn Fn(&mut serde_json::Value)>;
    let cases: Vec<(&str, Edit)> = vec![
        (
            "rate0",
            Box::new(|v| v["config"]["sample_rate"] = json!(0.0)),
        ),
        (
            "rate_negative",
            Box::new(|v| v["config"]["sample_rate"] = json!(-500.0)),
        ),
        (
            // Would make a realtime replay sleep for days.
            "rate_tiny",
            Box::new(|v| v["config"]["sample_rate"] = json!(1e-6)),
        ),
        (
            "compressed",
            Box::new(|v| v["config"]["compressed"] = json!(true)),
        ),
        (
            "rate_huge",
            Box::new(|v| *v.pointer_mut("/config/sample_rate").unwrap() = json!(1e300)),
        ),
        (
            "rate_string",
            Box::new(|v| v["config"]["sample_rate"] = json!("fast")),
        ),
        (
            "eeg_huge",
            Box::new(|v| v["config"]["layout"]["eeg"] = json!(4_000_000_000u64)),
        ),
        (
            "ext_huge",
            Box::new(|v| v["config"]["layout"]["ext"] = json!(u64::MAX)),
        ),
        (
            "labels_wrong_length",
            Box::new(|v| v["config"]["labels"] = json!(["Fp1"])),
        ),
        ("gain0", Box::new(|v| v["config"]["gain"] = json!(0))),
        (
            "start_string",
            Box::new(|v| v["start_unix"] = json!("yesterday")),
        ),
        ("start_null", Box::new(|v| v["start_unix"] = json!(null))),
    ];
    for (name, edit) in cases {
        let dir = edit_meta(name, |v| edit(v));
        assert_bad_meta(&dir, name);
    }
}

#[test]
fn non_finite_numbers_in_meta_are_bad_meta() {
    // JSON has no NaN or infinity; a huge exponent parses to infinity in some readers.
    for (name, field) in [("rate", "sample_rate"), ("start", "start_unix")] {
        let dir = edit_meta(&format!("inf-{name}"), |_| {});
        let path = dir.join("meta.json");
        let text = std::fs::read_to_string(&path).unwrap();
        let mut value: serde_json::Value = serde_json::from_str(&text).unwrap();
        // Replace the number by the text "1e999" after serialising.
        if field == "sample_rate" {
            value["config"][field] = serde_json::json!(-1.0);
        } else {
            value[field] = serde_json::json!(-1.0);
        }
        let edited = serde_json::to_string(&value)
            .unwrap()
            .replacen("-1.0", "1e999", 1);
        std::fs::write(&path, edited).unwrap();
        assert_bad_meta(&dir, name);
    }
}

#[test]
fn caller_config_with_bad_rate_is_an_error_not_a_panic() {
    let (_dir, path) = write_raw("badcfg", &fixture());
    for rate in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut cfg = config();
        cfg.sample_rate = rate;
        assert!(matches!(
            Replay::open(&path, Some(cfg), true),
            Err(ReplayError::BadMeta(_))
        ));
    }
    let mut cfg = config();
    cfg.layout.eeg = 4_000_000_000;
    assert!(matches!(
        Replay::open(&path, Some(cfg), false),
        Err(ReplayError::BadMeta(_))
    ));
}

#[test]
fn nonexistent_path_is_io_not_found_even_without_config() {
    let dir = tempdir("gone");
    match Replay::open(&dir.join("nope"), None, false) {
        Err(ReplayError::Io(e)) => assert_eq!(e.kind(), std::io::ErrorKind::NotFound),
        other => panic!("expected Io, got {:?}", other.map(|_| ())),
    }
}

#[test]
fn clean_replay_has_no_error() {
    let (_dir, path) = write_raw("clean", &fixture());
    let mut replay = Replay::open(&path, Some(config()), false).unwrap();
    assert!(replay.error().is_none());
    while replay.next().is_some() {}
    assert!(replay.error().is_none());
}

#[test]
fn a_read_error_is_kept_and_not_mistaken_for_end_of_file() {
    // A directory named raw.bin opens but cannot be read.
    let dir = tempdir("readerr");
    record(&dir, &fixture());
    std::fs::remove_file(dir.join("raw.bin")).unwrap();
    std::fs::create_dir(dir.join("raw.bin")).unwrap();
    let mut replay = Replay::open(&dir, None, false).unwrap();
    assert!(replay.next().is_none());
    assert!(replay.error().is_some(), "read error was swallowed");
}

#[test]
fn clock_lines_are_read_and_a_folder_without_them_still_reads() {
    let dir = tempdir("clock");
    record(&dir, &fixture());
    std::fs::write(
        dir.join("clock.jsonl"),
        "{\"sample\":0,\"unix_time\":1795000000.004}\n\
         {\"sample\":500,\"unix_time\":1795000001.0041}\n",
    )
    .unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert_eq!(replay.clock().len(), 2);
    assert_eq!(replay.clock()[1].sample, 500);
    assert_eq!(replay.clock()[1].unix_time, 1_795_000_001.004_1);
    let with_clock: Vec<StreamEvent> = replay.collect();

    // Recordings made before clock.jsonl existed: no clock, same events.
    std::fs::remove_file(dir.join("clock.jsonl")).unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert!(replay.clock().is_empty());
    assert_eq!(replay.collect::<Vec<_>>(), with_clock);
}

#[test]
fn clock_lines_out_of_order_are_bad_clock() {
    let dir = tempdir("badclock");
    record(&dir, &fixture());
    for text in [
        "{\"sample\":500,\"unix_time\":2.0}\n{\"sample\":0,\"unix_time\":1.0}\n",
        "{\"sample\":0,\"unix_time\":2.0}\n{\"sample\":500,\"unix_time\":1.0}\n",
        "{\"sample\":0}\n",
        "not json\n",
    ] {
        std::fs::write(dir.join("clock.jsonl"), text).unwrap();
        match Replay::open(&dir, None, false) {
            Err(ReplayError::BadClock(_)) => {}
            other => panic!("{text:?}: got {:?}", other.map(|_| ())),
        }
    }
}

#[test]
fn clock_times_read_back_exactly_as_written() {
    // Host times near 1.8e9 s with microsecond fractions, written in the
    // shortest form that reads back to the same double (as the recorder
    // writes them). Each must read back bit for bit.
    let dir = tempdir("clockexact");
    record(&dir, &fixture());
    let times: Vec<f64> = (0..2000)
        .map(|k| 1_795_567_568.0 + k as f64 * 1.000_123_457 + (k * 7919 % 1_000_003) as f64 * 1e-9)
        .collect();
    let text: String = times
        .iter()
        .enumerate()
        .map(|(k, t)| format!("{{\"sample\":{},\"unix_time\":{t}}}\n", k * 500))
        .collect();
    std::fs::write(dir.join("clock.jsonl"), text).unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    let read: Vec<f64> = replay.clock().iter().map(|p| p.unix_time).collect();
    let wrong = read
        .iter()
        .zip(&times)
        .filter(|(a, b)| a.to_bits() != b.to_bits())
        .count();
    assert_eq!(
        wrong,
        0,
        "{wrong} of {} times read back differently",
        times.len()
    );
}

const WHOLE_CLOCK_LINES: &str = "{\"sample\":0,\"unix_time\":1795000000.004}\n\
                                 {\"sample\":250,\"unix_time\":1795000000.5}\n";

#[test]
fn a_clock_line_cut_short_does_not_stop_the_replay() {
    let dir = tempdir("clockcut");
    record(&dir, &fixture());
    std::fs::write(
        dir.join("clock.jsonl"),
        format!("{WHOLE_CLOCK_LINES}{{\"sample\":500,\"unix_ti"),
    )
    .unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert_eq!(replay.clock().len(), 2);
    assert_eq!(replay.clock()[1].sample, 250);
    let error = replay.clock_error().expect("the cut line is reported");
    assert!(error.starts_with("line 3: "), "{error}");
    assert!(error.contains("cut short"), "{error}");
    let damaged: Vec<StreamEvent> = replay.collect();

    std::fs::remove_file(dir.join("clock.jsonl")).unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert!(replay.clock_error().is_none());
    assert_eq!(replay.collect::<Vec<_>>(), damaged);

    // A whole file reports no error.
    std::fs::write(dir.join("clock.jsonl"), WHOLE_CLOCK_LINES).unwrap();
    let replay = Replay::open(&dir, None, false).unwrap();
    assert!(replay.clock_error().is_none());
    assert_eq!(replay.clock().len(), 2);
}

#[test]
fn a_bad_clock_line_that_is_not_the_last_is_still_refused() {
    let dir = tempdir("clockmid");
    record(&dir, &fixture());
    std::fs::write(
        dir.join("clock.jsonl"),
        format!(
            "{WHOLE_CLOCK_LINES}{{\"sample\":500,\"unix_ti\n\
             {{\"sample\":750,\"unix_time\":1795000001.5}}"
        ),
    )
    .unwrap();
    assert!(matches!(
        Replay::open(&dir, None, false),
        Err(ReplayError::BadClock(_))
    ));
}

#[test]
fn a_bad_last_line_that_ends_in_a_newline_is_still_refused() {
    let dir = tempdir("clocknl");
    record(&dir, &fixture());
    std::fs::write(
        dir.join("clock.jsonl"),
        format!("{WHOLE_CLOCK_LINES}{{\"sample\":500,\"unix_ti\n"),
    )
    .unwrap();
    assert!(matches!(
        Replay::open(&dir, None, false),
        Err(ReplayError::BadClock(_))
    ));
}
