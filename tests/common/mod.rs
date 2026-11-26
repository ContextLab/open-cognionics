//! Shared helpers for integration tests.

pub mod bdf;
#[cfg(unix)]
pub mod pty_device;
pub mod temp;

use open_cognionics::config::extract_words;

/// The bytes rate of the recorded stream: 500 packets of 75 bytes a second.
pub const REAL_RATE: usize = 37_500;

/// 5000 whole packets recorded from a worn Quick-20, counters contiguous.
pub fn onhead_stream() -> Vec<u8> {
    std::fs::read("tests/fixtures/onhead_10s.bin").unwrap()
}

/// The configuration reply alone (marker through end word), cut out of the
/// recorded capture with the offsets `extract_words` reports.
pub fn config_reply() -> Vec<u8> {
    let bytes = std::fs::read("tests/fixtures/config_reply.bin").unwrap();
    let block = extract_words(&bytes).unwrap();
    bytes[block.start..block.end].to_vec()
}

/// Bytes that are not the headset's format, the same on every call (the
/// sequence of `garbage_stream` in `tests/python/pty_device.py`): a fixed
/// pseudo-random sequence in which every 25th byte starts a run of four
/// `0xFF` (a configuration marker out of place). The bytes of a
/// configuration's end word never occur in it. 500 packets long.
pub fn garbage_stream() -> Vec<u8> {
    let len = 500 * 75;
    let mut out = Vec::with_capacity(len + 4);
    let mut state: u64 = 0x2545_F491;
    while out.len() < len {
        state = (state * 1_103_515_245 + 12_345) & 0x7FFF_FFFF;
        out.push(((state >> 16) & 0xFF) as u8);
        if out.len() % 25 == 0 {
            out.extend_from_slice(&[0xFF; 4]);
        }
    }
    out.truncate(len);
    let end_word = 1_234_567_890u32.to_be_bytes();
    assert!(!out.windows(4).any(|w| w == end_word));
    out
}
