//! Tests for the recorder and the BDF+ writer, using the 10 s recording from a real Quick-20.

#[allow(dead_code)] // not every test uses every helper
mod common;

use common::bdf::{all_labels, read_bdf, replay_rows};
use common::temp::TempDir;
use open_cognionics::bdf::BdfWriter;
use open_cognionics::chunk::{Chunk, Chunker, StreamEvent};
use open_cognionics::config::{extract_words, DeviceConfig};
use open_cognionics::record::{Recorder, Recording, SessionMeta, SESSION_FILES};
use std::path::Path;

fn fixture() -> Vec<u8> {
    std::fs::read("tests/fixtures/onhead_10s.bin").unwrap()
}

fn config() -> DeviceConfig {
    let bytes = std::fs::read("tests/fixtures/config_reply.bin").unwrap();
    let block = extract_words(&bytes).unwrap();
    DeviceConfig::from_words(&block.words).unwrap()
}

fn meta() -> SessionMeta {
    SessionMeta {
        participant: "P001".into(),
        participant_uuid: None,
        start_unix: 1_795_000_000.25,
        config: config(),
        port: "fixture".into(),
        software_version: "test".into(),
    }
}

fn events_for(bytes: &[u8]) -> Vec<StreamEvent> {
    let cfg = config();
    let mut chunker = Chunker::new(cfg.layout, cfg.sample_rate);
    let mut out = Vec::new();
    for (i, part) in bytes.chunks(4096).enumerate() {
        chunker.push(part, 1000.0 + i as f64 * 0.1, &mut out);
    }
    out
}

fn record(dir: &Path, bytes: &[u8], finish: bool) {
    let mut rec = Recorder::start(dir, meta()).unwrap();
    let cfg = config();
    let mut chunker = Chunker::new(cfg.layout, cfg.sample_rate);
    // Marks for samples that have not arrived yet are placed when their record is written.
    rec.mark("first", 500, 1001.0);
    rec.mark("second", 2500, 1005.0);
    for (i, part) in bytes.chunks(4096).enumerate() {
        rec.raw(part);
        let mut out = Vec::new();
        chunker.push(part, 1000.0 + i as f64 * 0.1, &mut out);
        for e in &out {
            rec.event(e);
        }
    }
    if finish {
        rec.finish().unwrap();
    } else {
        drop(rec);
    }
}

fn tempdir(name: &str) -> TempDir {
    TempDir::new(&format!("record-{name}"))
}

#[test]
fn recorded_fixture_is_byte_identical_and_complete() {
    let input = fixture();
    let dir = tempdir("full");
    record(&dir, &input, true);

    assert_eq!(std::fs::read(dir.join("raw.bin")).unwrap(), input);

    let markers = std::fs::read_to_string(dir.join("markers.jsonl")).unwrap();
    let rows: Vec<serde_json::Value> = markers
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["label"], "first");
    assert_eq!(rows[0]["sample"], 500);
    assert_eq!(rows[0]["unix_time"], 1001.0);
    assert_eq!(rows[1]["label"], "second");
    assert_eq!(rows[1]["sample"], 2500);

    let meta: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.join("meta.json")).unwrap()).unwrap();
    assert_eq!(meta["finished"], true);
    assert_eq!(meta["participant"], "P001");
    assert_eq!(meta["eeg_volts_per_count"], 5.0 / (3.0 * 16_777_216.0));
    let back: DeviceConfig = serde_json::from_value(meta["config"].clone()).unwrap();
    assert_eq!(back, config());

    // 20 EEG + 3 ACC + annotations.
    let bdf = read_bdf(&dir.join("recording.bdf"));
    assert_eq!(bdf.signals, 24);
    assert_eq!(bdf.spr, 500);
    assert_eq!(bdf.records, 10);
    let len = std::fs::metadata(dir.join("recording.bdf")).unwrap().len() as usize;
    assert_eq!(len, 256 * 25 + 10 * (23 * 500 * 3 + 120));
    assert_eq!(&bdf.header[..8], b"\xffBIOSEMI");
    assert_eq!(&bdf.header[192..197], b"BDF+C");
    let labels = all_labels(&bdf);
    assert!(labels.contains(&(1.0, "first".to_string())));
    assert!(labels.contains(&(5.0, "second".to_string())));
    assert_eq!(labels.len(), 2);

    // Every one of the 23 signals (20 EEG, then ACC) equals the raw counts in
    // the decoded events: EEG are columns 0..20 and ACC columns 20..23 (no EXT).
    let events = events_for(&input);
    let mut expect: Vec<Vec<i32>> = vec![Vec::new(); 23];
    for e in &events {
        if let StreamEvent::Chunk(c) = e {
            assert_eq!(c.channels, 23);
            for row in 0..c.samples {
                for (ch, col) in expect.iter_mut().enumerate() {
                    col.push(c.counts[row * c.channels + ch]);
                }
            }
        }
    }
    assert_eq!(expect[0].len(), 5000);
    for (ch, column) in expect.iter().enumerate() {
        assert_eq!(&bdf.data[ch], column, "signal {ch}");
    }
    assert!(
        expect[20].iter().any(|v| *v != expect[21][0]) && expect[21] != expect[22],
        "fixture accelerometer channels are distinguishable"
    );
}

#[test]
fn dropped_recorder_leaves_raw_and_markers_complete() {
    let input = fixture();
    let dir = tempdir("dropped");
    record(&dir, &input, false);
    assert_eq!(std::fs::read(dir.join("raw.bin")).unwrap(), input);
    let markers = std::fs::read_to_string(dir.join("markers.jsonl")).unwrap();
    assert_eq!(markers.lines().count(), 2);
    let meta: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.join("meta.json")).unwrap()).unwrap();
    assert_eq!(meta["finished"], false);
}

fn chunk(index: u64, rows: &[[i32; 23]]) -> StreamEvent {
    StreamEvent::Chunk(Chunk {
        index,
        samples: rows.len(),
        channels: 23,
        counts: rows.iter().flatten().copied().collect(),
        time: 0.0,
        battery_volts: 4.0,
        impedance_on: false,
        triggers: vec![0; rows.len()],
    })
}

/// A row whose 23 channels all differ: `v * 100 + channel`.
fn row(v: i32) -> [i32; 23] {
    std::array::from_fn(|c| v * 100 + c as i32)
}

#[test]
fn lost_samples_repeat_previous_and_are_annotated() {
    let dir = tempdir("lost");
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.join("a.bdf");
    let mut w = BdfWriter::create(&path, &config(), "P001", 1_795_000_000.0).unwrap();
    // 100 zeros-before-any is covered by starting with a loss.
    w.write(&StreamEvent::Lost { index: 0, count: 2 });
    w.write(&chunk(2, &[row(7), row(-9)]));
    w.write(&StreamEvent::Lost { index: 4, count: 3 });
    w.write(&chunk(7, &[row(5)]));
    w.finish().unwrap();

    let bdf = read_bdf(&path);
    assert_eq!(bdf.records, 1);
    // Sample 0..8: two lost (zeros, none before), 7, -9, three lost (repeat -9), 5.
    let values = [0, 0, 7, -9, -9, -9, -9, 5];
    for ch in 0..23 {
        let c = ch as i32;
        let expect: Vec<i32> = values
            .iter()
            .enumerate()
            .map(|(i, v)| if i < 2 { 0 } else { v * 100 + c })
            .collect();
        assert_eq!(&bdf.data[ch][..8], &expect[..], "signal {ch}");
        // Last sample repeated as padding for the rest of the record.
        assert_eq!(bdf.data[ch][8], 500 + c);
        assert_eq!(bdf.data[ch][499], 500 + c);
    }
    let labels = all_labels(&bdf);
    assert!(labels.contains(&(0.0, "lost 2".to_string())));
    assert!(labels.contains(&(4.0 / 500.0, "lost 3".to_string())));
    assert!(labels.contains(&(8.0 / 500.0, "padding".to_string())));
}

#[test]
fn many_annotations_overflow_into_later_records_and_none_are_dropped() {
    let dir = tempdir("overflow");
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.join("a.bdf");
    let mut w = BdfWriter::create(&path, &config(), "P001", 1_795_000_000.0).unwrap();
    let rows: Vec<[i32; 23]> = (0..1000).map(row).collect();
    w.write(&chunk(0, &rows));
    // 30 annotations at one sample cannot fit in one 120-byte annotation signal.
    for i in 0..30 {
        w.annotate(100, &format!("m{i:02}"));
    }
    // An annotation for a sample already written goes into a later record.
    w.annotate(5, "late");
    w.finish().unwrap();

    let bdf = read_bdf(&path);
    let labels = all_labels(&bdf);
    for i in 0..30 {
        assert!(
            labels.contains(&(0.2, format!("m{i:02}"))),
            "m{i:02} missing"
        );
    }
    assert!(labels.contains(&(0.01, "late".to_string())));
    assert!(bdf.records > 2, "extra padded records were written");
}

#[test]
fn error_is_reported_by_finish_not_swallowed() {
    let dir = tempdir("err");
    let mut rec = Recorder::start(&dir, meta()).unwrap();
    // A chunk with the wrong channel count cannot be written.
    rec.event(&chunk(0, &[row(1)]));
    rec.event(&StreamEvent::Chunk(Chunk {
        index: 1,
        samples: 1,
        channels: 5,
        counts: vec![0; 5],
        time: 0.0,
        battery_volts: 0.0,
        impedance_on: false,
        triggers: vec![0],
    }));
    assert!(rec.finish().is_err());
}

#[test]
fn recording_that_ends_in_a_disconnect_is_complete() {
    let input = fixture();
    let fed = &input[..1200 * 75]; // 2.4 s: a partial third record
    let dir = tempdir("disconnect");
    let mut rec = Recorder::start(&dir, meta()).unwrap();
    let cfg = config();
    let mut chunker = Chunker::new(cfg.layout, cfg.sample_rate);
    for (i, part) in fed.chunks(4096).enumerate() {
        rec.raw(part);
        let mut out = Vec::new();
        chunker.push(part, 1000.0 + i as f64 * 0.1, &mut out);
        for e in &out {
            rec.event(e);
        }
    }
    rec.event(&StreamEvent::Disconnected("link lost".into()));
    rec.finish().unwrap();

    assert_eq!(std::fs::read(dir.join("raw.bin")).unwrap(), fed);
    let meta: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.join("meta.json")).unwrap()).unwrap();
    assert_eq!(meta["finished"], true);
    // read_bdf asserts header + whole records == file length and the patched count.
    let bdf = read_bdf(&dir.join("recording.bdf"));
    assert_eq!(bdf.records, 3);
    let labels = all_labels(&bdf);
    assert!(labels.contains(&(1200.0 / 500.0, "disconnected: link lost".to_string())));
    assert!(labels.contains(&(1200.0 / 500.0, "padding".to_string())));
}

/// Name and bytes of every file in `dir`, sorted by name.
fn snapshot(dir: &Path) -> Vec<(String, Vec<u8>)> {
    let mut files: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| {
            let e = e.unwrap();
            (
                e.file_name().to_string_lossy().into_owned(),
                std::fs::read(e.path()).unwrap(),
            )
        })
        .collect();
    files.sort();
    files
}

#[test]
fn starting_where_a_recording_exists_is_refused_and_changes_nothing() {
    let input = fixture();
    let dir = tempdir("exists");
    record(&dir, &input[..100 * 75], true);
    let before = snapshot(&dir);
    let names: Vec<&str> = before.iter().map(|(n, _)| n.as_str()).collect();
    let mut expected = SESSION_FILES.to_vec();
    expected.sort();
    assert_eq!(names, expected);

    let err = Recorder::start(&dir, meta())
        .err()
        .expect("a second start is refused");
    assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists, "{err}");
    let err = Recording::start(&dir, meta())
        .err()
        .expect("a second start is refused");
    assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists, "{err}");
    assert_eq!(snapshot(&dir), before);

    // Any one of the session files is enough to refuse.
    let partial = tempdir("exists-partial");
    std::fs::create_dir_all(partial.path()).unwrap();
    std::fs::write(partial.join("markers.jsonl"), b"{}\n").unwrap();
    let err = Recording::start(&partial, meta()).err().expect("refused");
    assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists, "{err}");
    assert_eq!(
        snapshot(&partial),
        vec![("markers.jsonl".to_string(), b"{}\n".to_vec())]
    );
}

/// Counts of packet `k` of the fixture.
fn fixture_packet(k: usize) -> Vec<i32> {
    let cfg = config();
    let mut decoder = open_cognionics::legacy::LegacyDecoder::new(cfg.layout);
    let mut out = Vec::new();
    // The decoder finds the first packet once the next one's sync byte is there.
    decoder.feed(&fixture()[k * 75..(k + 2) * 75], &mut out);
    match &out[..] {
        [open_cognionics::legacy::Event::Packet(p), ..] => p.raw.clone(),
        other => panic!("packet {k}: {other:?}"),
    }
}

#[test]
fn a_recording_that_starts_mid_packet_counts_from_its_first_whole_packet() {
    let input = fixture();
    let start = 200 * 75 + 37;
    let mut bytes = input[start..3000 * 75].to_vec();
    // Packet 1000 is lost: recording sample 799 (sample 0 is packet 201).
    let at = 1000 * 75 - start;
    bytes.drain(at..at + 75);
    let dir = tempdir("midpacket");
    let mut rec = Recording::start(&dir, meta()).unwrap();
    assert_eq!(rec.position(), 0);
    // Up to the end of packet 600: packets 201 to 600 are whole.
    let first = 601 * 75 - start;
    for part in bytes[..first].chunks(1000) {
        rec.take(part, 1_795_000_000.0);
    }
    assert_eq!(rec.position(), 400);
    // The marker's sample is returned, so a caller never has to read it back from the file.
    assert_eq!(rec.mark("m", 1_795_000_001.5), 400);
    for part in bytes[first..].chunks(1000) {
        rec.take(part, 1_795_000_002.0);
    }
    assert_eq!(rec.position(), 2799);
    rec.finish().unwrap();

    assert_eq!(std::fs::read(dir.join("raw.bin")).unwrap(), bytes);
    let markers = std::fs::read_to_string(dir.join("markers.jsonl")).unwrap();
    let marker: serde_json::Value = serde_json::from_str(markers.lines().next().unwrap()).unwrap();
    assert_eq!(marker["label"], "m");
    assert_eq!(marker["sample"], 400);
    assert_eq!(marker["unix_time"], 1_795_000_001.5);

    let rows = replay_rows(&dir);
    assert_eq!(rows.len(), 2799);
    assert_eq!(rows[400].as_deref(), Some(&fixture_packet(601)[..]));
    assert_eq!(rows[0].as_deref(), Some(&fixture_packet(201)[..]));
    assert!(rows[799].is_none());

    // The BDF sample at each annotation's onset is the replayed sample with
    // the same index: EEG columns 0..20 and ACC columns 20..23.
    let bdf = read_bdf(&dir.join("recording.bdf"));
    let labels = all_labels(&bdf);
    assert!(
        labels.contains(&(400.0 / 500.0, "m".to_string())),
        "{labels:?}"
    );
    assert!(
        labels.contains(&(799.0 / 500.0, "lost 1".to_string())),
        "{labels:?}"
    );
    for (onset, _) in labels.iter().filter(|(_, l)| l == "m") {
        let sample = (onset * 500.0).round() as usize;
        assert_eq!(bdf.sample(sample), rows[sample].clone().unwrap());
    }
    for sample in [0, 1, 399, 400, 401, 798, 800, 2798] {
        assert_eq!(
            bdf.sample(sample),
            rows[sample].clone().unwrap(),
            "sample {sample}"
        );
    }
}

fn clock_lines(dir: &Path) -> Vec<(u64, f64)> {
    std::fs::read_to_string(dir.join("clock.jsonl"))
        .unwrap()
        .lines()
        .map(|line| {
            let v: serde_json::Value = serde_json::from_str(line).unwrap();
            (
                v["sample"].as_u64().unwrap(),
                v["unix_time"].as_f64().unwrap(),
            )
        })
        .collect()
}

#[test]
fn clock_lines_hold_the_fitted_time_of_samples_about_once_a_second() {
    const T0: f64 = 1.8e9;
    let input = fixture();
    let dir = tempdir("clock");
    let mut rec = Recording::start(&dir, meta()).unwrap();
    assert!(clock_lines(&dir).is_empty());
    // Reads of 25 packets, each 4 to 13 ms after its newest sample was digitised.
    for (k, part) in input.chunks(25 * 75).enumerate() {
        let newest = (k * 25 + 24) as f64;
        rec.take(part, T0 + newest / 500.0 + 0.004 + 0.003 * (k % 4) as f64);
        if k == 160 {
            // Written while recording, not only at the end.
            let so_far = clock_lines(&dir);
            assert!(so_far.len() >= 8, "{so_far:?}");
        }
    }
    rec.finish().unwrap();
    let lines = clock_lines(&dir);
    assert_eq!(lines.first().unwrap().0, 0);
    assert_eq!(lines.last().unwrap().0, 5000);
    for w in lines.windows(2) {
        assert!(w[1].0 > w[0].0, "{w:?}");
        assert!(w[1].1 >= w[0].1, "{w:?}");
    }
    for w in lines[..lines.len() - 1].windows(2) {
        assert_eq!(w[1].0 - w[0].0, 500, "{w:?}");
        assert!((w[1].1 - w[0].1 - 1.0).abs() < 1e-3, "{w:?}");
    }
    // The delay is never below 4 ms, so the model puts sample n at T0 + 4 ms + n / 500.
    for (sample, time) in &lines {
        let expected = T0 + 0.004 + *sample as f64 / 500.0;
        assert!(
            (time - expected).abs() < 1e-6,
            "sample {sample}: {time} vs {expected}"
        );
    }
}

#[test]
fn a_recording_with_no_arrival_times_writes_no_clock_lines() {
    let input = fixture();
    let dir = tempdir("noclock");
    let mut rec = Recording::start(&dir, meta()).unwrap();
    for part in input[..1000 * 75].chunks(4096) {
        rec.take(part, f64::NAN);
    }
    rec.finish().unwrap();
    assert!(clock_lines(&dir).is_empty());
    assert_eq!(replay_rows(&dir).len(), 1000);
}

#[test]
fn a_sample_rate_that_is_not_a_whole_number_is_refused_before_anything_is_written() {
    let dir = tempdir("rate");
    std::fs::create_dir_all(dir.path()).unwrap();
    let mut odd = meta();
    odd.config.sample_rate = 500.5;
    let err = BdfWriter::create(&dir.join("a.bdf"), &odd.config, "P001", 0.0)
        .err()
        .expect("refused");
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput, "{err}");
    assert!(err.to_string().contains("whole number"), "{err}");
    let err = Recording::start(&dir, odd).err().expect("refused");
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput, "{err}");
    // Nothing was created, so the same directory can be used again.
    assert!(std::fs::read_dir(dir.path())
        .map(|d| d.count() == 0)
        .unwrap_or(true));
}
