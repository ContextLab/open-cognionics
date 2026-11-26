//! Tests for the legacy stream decoder.
//!
//! Expected values in `decode_*` tests are worked out by hand from the
//! vendor formula `((b0 << 24) | (b1 << 17) | (b2 << 10)) >> 8`, not produced
//! by this crate. No test here uses a recording from a real headset yet.

use open_cognionics::legacy::{decode_sample, lost_between, Event, Layout, LegacyDecoder, Packet};
use open_cognionics::scale;

const QUICK20: Layout = Layout {
    eeg: 20,
    ext: 4,
    acc: 0,
};

/// Build one packet from explicit channel byte triples.
fn packet_bytes(
    counter: u8,
    channels: &[[u8; 3]],
    status: u8,
    battery: u8,
    trigger: u16,
) -> Vec<u8> {
    let mut p = vec![0xFF, counter];
    for c in channels {
        p.extend_from_slice(c);
    }
    p.extend_from_slice(&[status, battery, (trigger >> 8) as u8, trigger as u8]);
    p
}

fn stream(counters: &[u8], layout: Layout) -> Vec<u8> {
    counters
        .iter()
        .flat_map(|&c| packet_bytes(c, &vec![[0, c, 1]; layout.channels()], 0x10, 100, 0))
        .collect()
}

fn packets(events: &[Event]) -> Vec<&Packet> {
    events
        .iter()
        .filter_map(|e| match e {
            Event::Packet(p) => Some(p),
            _ => None,
        })
        .collect()
}

#[test]
fn decode_hand_computed_values() {
    assert_eq!(decode_sample(0x00, 0x00, 0x00), 0);
    assert_eq!(decode_sample(0x00, 0x00, 0x01), 4);
    assert_eq!(decode_sample(0x00, 0x01, 0x00), 512);
    assert_eq!(decode_sample(0x01, 0x00, 0x00), 65_536);
    assert_eq!(decode_sample(0x7F, 0x7F, 0x7F), 8_388_604);
    assert_eq!(decode_sample(0x80, 0x00, 0x00), -8_388_608);
    assert_eq!(decode_sample(0xFE, 0x7F, 0x7F), -65_540);
    assert_eq!(decode_sample(0xFF, 0x7F, 0x7F), -4);
}

#[test]
fn packet_length_matches_vendor_formula() {
    assert_eq!(QUICK20.channels(), 24);
    assert_eq!(QUICK20.packet_len(), 3 * 24 + 6);
}

#[test]
fn counter_gaps() {
    assert_eq!(lost_between(5, 6), 0);
    assert_eq!(lost_between(127, 0), 0);
    assert_eq!(lost_between(5, 8), 2);
    assert_eq!(lost_between(126, 1), 2);
    assert_eq!(lost_between(5, 5), 127);
}

#[test]
fn decodes_fields_of_a_packet() {
    let layout = Layout {
        eeg: 2,
        ext: 1,
        acc: 0,
    };
    let mut bytes = packet_bytes(
        7,
        &[[0x00, 0x00, 0x01], [0x80, 0x00, 0x00], [0x01, 0x00, 0x00]],
        0x11,
        96,
        0x1234,
    );
    bytes.extend(packet_bytes(8, &[[0; 3]; 3], 0x10, 96, 0));
    bytes.push(0xFF);

    let mut events = Vec::new();
    LegacyDecoder::new(layout).feed(&bytes, &mut events);

    let got = packets(&events);
    assert_eq!(got.len(), 2);
    assert_eq!(got[0].counter, 7);
    assert_eq!(got[0].raw, vec![4, -8_388_608, 65_536]);
    assert!(got[0].impedance_on());
    assert_eq!(scale::battery_volts(got[0].battery), 3.75);
    assert_eq!(got[0].trigger, 0x1234);
    assert!(!got[1].impedance_on());
}

#[test]
fn same_events_whatever_the_chunking() {
    let bytes = stream(&[10, 11, 12, 13, 14, 15], QUICK20);
    let mut whole = Vec::new();
    LegacyDecoder::new(QUICK20).feed(&bytes, &mut whole);

    for chunk in [1, 7, 77, 78, 79, 200] {
        let mut decoder = LegacyDecoder::new(QUICK20);
        let mut events = Vec::new();
        for piece in bytes.chunks(chunk) {
            decoder.feed(piece, &mut events);
        }
        assert_eq!(events, whole, "chunk size {chunk}");
    }
    // The last packet is held back until the stream continues or locks.
    assert_eq!(
        packets(&whole)
            .iter()
            .map(|p| p.counter)
            .collect::<Vec<_>>(),
        vec![10, 11, 12, 13, 14, 15]
    );
}

#[test]
fn starts_mid_packet_and_skips_to_a_boundary() {
    let bytes = stream(&[3, 4, 5, 6], QUICK20);
    let mut events = Vec::new();
    LegacyDecoder::new(QUICK20).feed(&bytes[30..], &mut events);

    assert_eq!(events[0], Event::Skipped(QUICK20.packet_len() - 30));
    assert_eq!(
        packets(&events)
            .iter()
            .map(|p| p.counter)
            .collect::<Vec<_>>(),
        vec![4, 5, 6]
    );
}

#[test]
fn sync_valued_data_byte_does_not_fool_initial_lock() {
    // Channel bytes of 0xFF (small negative samples) look like sync bytes.
    let layout = Layout {
        eeg: 4,
        ext: 0,
        acc: 0,
    };
    let chans = [[0xFF, 0x7F, 0x7F]; 4];
    let mut bytes = vec![0x00, 0x01];
    for c in [20u8, 21, 22, 23] {
        bytes.extend(packet_bytes(c, &chans, 0x10, 100, 0));
    }
    let mut events = Vec::new();
    LegacyDecoder::new(layout).feed(&bytes, &mut events);

    assert_eq!(events[0], Event::Skipped(2));
    let got = packets(&events);
    assert_eq!(
        got.iter().map(|p| p.counter).collect::<Vec<_>>(),
        vec![20, 21, 22, 23]
    );
    assert!(got.iter().all(|p| p.raw == vec![-4; 4]));
}

#[test]
fn reports_lost_packets_and_recovers_from_dropped_bytes() {
    let mut bytes = stream(&[100, 101], QUICK20);
    bytes.extend(stream(&[104, 105], QUICK20)); // 102, 103 never arrive
    let cut = bytes.len();
    bytes.extend(stream(&[106, 107, 108, 109], QUICK20));
    bytes.drain(cut + 10..cut + 25); // 15 bytes vanish from packet 106

    let mut events = Vec::new();
    LegacyDecoder::new(QUICK20).feed(&bytes, &mut events);

    assert!(events.contains(&Event::Lost(2)));
    assert!(
        events.contains(&Event::Lost(1)),
        "packet 106 was damaged: {events:?}"
    );
    assert_eq!(
        packets(&events)
            .iter()
            .map(|p| p.counter)
            .collect::<Vec<_>>(),
        vec![100, 101, 104, 105, 107, 108, 109]
    );
}

#[test]
fn eeg_scale_matches_vendor_default() {
    assert_eq!(scale::ADS_GAINS[3], 3);
    assert_eq!(scale::eeg_volts_per_count(3), 9.934107462565105e-08);
}

/// 200 consecutive packets recorded from a real Quick-20 through its USB
/// dongle on 2026-11-24 (headset idle, impedance check on). Expected values
/// for the first packet are computed by hand from its bytes:
/// channel 0 = (10, 214, 182) -> 10 * 65536 + 214 * 512 + 182 * 4 = 765656,
/// channel 20 = (0, 64, 0) -> 64 * 512 = 32768.
#[test]
fn decodes_real_quick20_recording() {
    let bytes = std::fs::read("tests/fixtures/quick20_dongle_200packets.bin").unwrap();
    let layout = Layout {
        eeg: 20,
        ext: 0,
        acc: 3,
    };
    assert_eq!(bytes.len(), 200 * layout.packet_len());

    let mut events = Vec::new();
    let mut decoder = LegacyDecoder::new(layout);
    for piece in bytes.chunks(512) {
        decoder.feed(piece, &mut events);
    }

    let got = packets(&events);
    assert_eq!(got.len(), events.len(), "no loss or skipped bytes expected");
    assert_eq!(got.len(), 200);
    assert_eq!(got[0].counter, 1);
    assert_eq!(got[0].raw[0], 765_656);
    assert_eq!(got[0].raw[20], 32_768);
    for pair in got.windows(2) {
        assert_eq!(lost_between(pair[0].counter, pair[1].counter), 0);
    }
    for p in &got {
        assert!(p.impedance_on());
        assert_eq!(scale::battery_volts(p.battery), 4.1015625);
        assert_eq!(p.trigger, 0);
        // Every channel byte on the wire is even, so counts are multiples of 8.
        assert!(p.raw.iter().all(|v| v % 8 == 0));
    }
}

#[test]
fn packet_len_cannot_overflow() {
    let big = Layout {
        eeg: usize::MAX,
        ext: usize::MAX,
        acc: usize::MAX,
    };
    assert_eq!(big.channels(), usize::MAX);
    assert_eq!(big.packet_len(), usize::MAX);
    let ok = Layout {
        eeg: 20,
        ext: 0,
        acc: 3,
    };
    assert_eq!(ok.packet_len(), 75);
}
