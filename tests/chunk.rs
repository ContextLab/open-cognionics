//! Tests for `Chunker`, using the 10 s recording from a real Quick-20.

use open_cognionics::chunk::{Chunk, Chunker, StreamEvent};
use open_cognionics::legacy::{Event, Layout, LegacyDecoder};

const LAYOUT: Layout = Layout {
    eeg: 20,
    ext: 0,
    acc: 3,
};
const RATE: f64 = 500.0;
const PACKET: usize = 75;

fn fixture() -> Vec<u8> {
    std::fs::read("tests/fixtures/onhead_10s.bin").unwrap()
}

fn run(bytes: &[u8], piece: usize) -> Vec<StreamEvent> {
    let mut chunker = Chunker::new(LAYOUT, RATE);
    let mut out = Vec::new();
    for (i, part) in bytes.chunks(piece).enumerate() {
        chunker.push(part, 1000.0 + i as f64 * 0.1, &mut out);
    }
    out
}

fn chunks(events: &[StreamEvent]) -> Vec<&Chunk> {
    events
        .iter()
        .filter_map(|e| match e {
            StreamEvent::Chunk(c) => Some(c),
            _ => None,
        })
        .collect()
}

#[test]
fn fixture_chunks_are_contiguous_and_total_5000_samples() {
    let events = run(&fixture(), 4096);
    assert!(events.iter().all(|e| matches!(e, StreamEvent::Chunk(_))));
    let mut next = 0u64;
    for c in chunks(&events) {
        assert_eq!(c.index, next);
        assert_eq!(c.channels, 23);
        assert_eq!(c.counts.len(), c.samples * c.channels);
        assert_eq!(c.triggers.len(), c.samples);
        next += c.samples as u64;
    }
    assert_eq!(next, 5000);
}

#[test]
fn a_removed_packet_is_reported_and_advances_the_index() {
    let mut bytes = fixture();
    bytes.drain(100 * PACKET..101 * PACKET);
    let events = run(&bytes, 4096);
    let lost: Vec<_> = events
        .iter()
        .filter(|e| matches!(e, StreamEvent::Lost { .. }))
        .collect();
    assert_eq!(
        lost,
        [&StreamEvent::Lost {
            index: 100,
            count: 1
        }]
    );
    let total: usize = chunks(&events).iter().map(|c| c.samples).sum();
    assert_eq!(total, 4999);
    let last = chunks(&events).into_iter().last().unwrap();
    assert_eq!(last.index + last.samples as u64, 5000);
    // The chunk right after the loss starts one past the contiguous position.
    let pos = events
        .iter()
        .position(|e| matches!(e, StreamEvent::Lost { .. }))
        .unwrap();
    let before = match &events[pos - 1] {
        StreamEvent::Chunk(c) => c,
        other => panic!("expected chunk, got {other:?}"),
    };
    let after = match &events[pos + 1] {
        StreamEvent::Chunk(c) => c,
        other => panic!("expected chunk, got {other:?}"),
    };
    assert_eq!(after.index, before.index + before.samples as u64 + 1);
}

#[test]
fn first_sample_counts_match_the_decoder_directly() {
    let bytes = fixture();
    let mut decoder = LegacyDecoder::new(LAYOUT);
    let mut raw = Vec::new();
    decoder.feed(&bytes, &mut raw);
    let first = match &raw[0] {
        Event::Packet(p) => p.clone(),
        other => panic!("expected packet, got {other:?}"),
    };
    let events = run(&bytes, 4096);
    let c = chunks(&events)[0];
    assert_eq!(c.counts[..23], first.raw[..]);
    assert_eq!(c.triggers[0], first.trigger);
}

#[test]
fn chunk_times_from_logged_reads_keep_the_spacing_of_their_indices() {
    // The reads logged from the real headset (tests/fixtures/read_timing_10s.csv),
    // each delivering the fixture's bytes at the same offsets, on a host
    // clock near 1.8e9 s. The first reads drain about 0.6 s of backlog.
    let bytes = fixture();
    let text = std::fs::read_to_string("tests/fixtures/read_timing_10s.csv").unwrap();
    let mut chunker = Chunker::new(LAYOUT, RATE);
    let mut stamped: Vec<(Chunk, f64)> = Vec::new();
    for line in text.lines().skip(1) {
        let f: Vec<&str> = line.split(',').collect();
        let arrival = 1.8e9 + f[0].parse::<f64>().unwrap();
        let offset: usize = f[1].parse().unwrap();
        let n: usize = f[2].parse().unwrap();
        let end = (offset + n).min(bytes.len());
        let mut out = Vec::new();
        chunker.push(&bytes[offset.min(end)..end], arrival, &mut out);
        for event in out {
            if let StreamEvent::Chunk(c) = event {
                stamped.push((c, arrival));
            }
        }
    }
    assert!(stamped.len() > 150);
    // No sample is stamped after the read that delivered it.
    for (c, arrival) in &stamped {
        let newest = c.time + (c.samples - 1) as f64 / RATE;
        assert!(
            newest <= arrival + 1e-6,
            "chunk {}: {newest} > {arrival}",
            c.index
        );
    }
    // Once settled, consecutive chunks are as far apart in time as in
    // samples, to within the 1.5 ms corrections the clock model makes.
    let settled: Vec<_> = stamped.iter().filter(|(c, _)| c.index >= 500).collect();
    for w in settled.windows(2) {
        let (a, b) = (&w[0].0, &w[1].0);
        let expected = (b.index - a.index) as f64 / RATE;
        assert!(
            (b.time - a.time - expected).abs() < 1.5e-3,
            "chunks {} and {}: {} s apart, expected {expected}",
            a.index,
            b.index,
            b.time - a.time
        );
    }
}

#[test]
fn chunk_times_around_a_loss_inside_one_push_follow_the_index() {
    // Packets 0 to 19 with packet 10 removed, in one read at an epoch-scale time.
    let mut bytes = fixture()[..20 * PACKET].to_vec();
    bytes.drain(10 * PACKET..11 * PACKET);
    let arrival = 1.8e9 + 5.0;
    let mut chunker = Chunker::new(LAYOUT, RATE);
    let mut out = Vec::new();
    chunker.push(&bytes, arrival, &mut out);
    let (first, second) = match &out[..] {
        [StreamEvent::Chunk(a), StreamEvent::Lost {
            index: 10,
            count: 1,
        }, StreamEvent::Chunk(b)] => (a, b),
        other => panic!("{other:?}"),
    };
    assert_eq!((first.index, first.samples), (0, 10));
    assert_eq!((second.index, second.samples), (11, 9));
    // The newest sample (19) waited least: it is stamped at the arrival, and
    // the others one period apart before it, across the lost one.
    assert!((first.time - (arrival - 19.0 / RATE)).abs() < 1e-6);
    assert!((second.time - (arrival - 8.0 / RATE)).abs() < 1e-6);
}

#[test]
fn chunk_metadata_matches_the_decoder() {
    let bytes = &fixture()[..10 * PACKET];
    let mut decoder = LegacyDecoder::new(LAYOUT);
    let mut raw = Vec::new();
    decoder.feed(bytes, &mut raw);
    let packets: Vec<_> = raw
        .iter()
        .filter_map(|e| match e {
            Event::Packet(p) => Some(p.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(packets.len(), 10);
    let mut chunker = Chunker::new(LAYOUT, RATE);
    let mut out = Vec::new();
    chunker.push(bytes, 1234.5, &mut out);
    assert_eq!(out.len(), 1);
    let c = match &out[0] {
        StreamEvent::Chunk(c) => c,
        other => panic!("expected chunk, got {other:?}"),
    };
    let last = packets.last().unwrap();
    assert_eq!(
        c.battery_volts,
        open_cognionics::scale::battery_volts(last.battery)
    );
    assert_eq!(c.impedance_on, last.impedance_on());
    let triggers: Vec<u16> = packets.iter().map(|p| p.trigger).collect();
    assert_eq!(c.triggers, triggers);
    // Ten samples read together at 1234.5: the newest arrived then, so the
    // first was digitised nine sample periods earlier.
    assert!((c.time - (1234.5 - 9.0 / RATE)).abs() < 1e-9);
}

#[test]
fn small_pieces_give_the_same_samples_as_one_push() {
    let bytes = fixture();
    let whole = run(&bytes, bytes.len());
    let expected: Vec<i32> = chunks(&whole)
        .iter()
        .flat_map(|c| c.counts.clone())
        .collect();
    assert_eq!(expected.len(), 5000 * 23);
    for piece in [7, 100] {
        let events = run(&bytes, piece);
        let got: Vec<i32> = chunks(&events)
            .iter()
            .flat_map(|c| c.counts.clone())
            .collect();
        assert_eq!(got, expected, "piece size {piece}");
        let mut next = 0u64;
        for c in chunks(&events) {
            assert_eq!(c.index, next, "piece size {piece}");
            next += c.samples as u64;
        }
        assert_eq!(next, 5000);
    }
}

#[test]
fn a_loss_at_the_start_of_a_push_gives_lost_and_no_empty_chunk() {
    let bytes = fixture();
    let mut chunker = Chunker::new(LAYOUT, RATE);
    let mut out = Vec::new();
    chunker.push(&bytes[..100 * PACKET], 1000.0, &mut out);
    out.clear();
    // Packet 100 is skipped; the next push begins with packet 101.
    chunker.push(&bytes[101 * PACKET..102 * PACKET], 1001.0, &mut out);
    assert_eq!(out.len(), 2, "{out:?}");
    assert_eq!(
        out[0],
        StreamEvent::Lost {
            index: 100,
            count: 1
        }
    );
    match &out[1] {
        StreamEvent::Chunk(c) => {
            assert_eq!(c.index, 101);
            assert_eq!(c.samples, 1);
        }
        other => panic!("expected chunk, got {other:?}"),
    }
}

#[test]
fn two_adjacent_removed_packets_give_one_lost_event() {
    let mut bytes = fixture();
    bytes.drain(100 * PACKET..102 * PACKET);
    let events = run(&bytes, 4096);
    let lost: Vec<_> = events
        .iter()
        .filter(|e| matches!(e, StreamEvent::Lost { .. }))
        .collect();
    assert_eq!(
        lost,
        [&StreamEvent::Lost {
            index: 100,
            count: 2
        }]
    );
    let last = chunks(&events).into_iter().last().unwrap();
    assert_eq!(last.index + last.samples as u64, 5000);
}
