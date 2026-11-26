//! Decoder for the legacy (uncompressed) stream format.
//!
//! One packet carries one sample and is `3 * N + 6` bytes long:
//!
//! ```text
//! 0xFF | counter | N x (b0 b1 b2) | impedance status | battery | trigger hi | trigger lo
//! ```
//!
//! The decoder is "sans-IO": bytes go in through [`LegacyDecoder::feed`] and
//! [`Event`]s come out, so the same code serves Bluetooth, FTDI and files.

/// Marks the start of every packet.
pub const SYNC: u8 = 0xFF;
/// The packet counter wraps at this value.
pub const COUNTER_MODULUS: u8 = 128;
/// Impedance-status byte value meaning "impedance check is on".
pub const IMPEDANCE_ON: u8 = 0x11;

const HEADER_LEN: usize = 2;
const TRAILER_LEN: usize = 4;

/// How many channels of each kind a packet carries, in wire order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Layout {
    pub eeg: usize,
    pub ext: usize,
    pub acc: usize,
}

impl Layout {
    /// Total number of 3-byte channel slots in a packet.
    pub fn channels(&self) -> usize {
        self.eeg.saturating_add(self.ext).saturating_add(self.acc)
    }

    /// Packet length in bytes, including sync byte and trailer.
    pub fn packet_len(&self) -> usize {
        self.channels()
            .saturating_mul(3)
            .saturating_add(HEADER_LEN + TRAILER_LEN)
    }
}

/// One decoded sample.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Packet {
    /// Packet counter, `0..COUNTER_MODULUS`.
    pub counter: u8,
    /// Signed ADC counts, one per channel in wire order (EEG, EXT, ACC).
    pub raw: Vec<i32>,
    /// Raw impedance-status byte; see [`Packet::impedance_on`].
    pub impedance_status: u8,
    /// Raw battery byte; see [`crate::scale::battery_volts`].
    pub battery: u8,
    /// 16-bit trigger value.
    pub trigger: u16,
}

impl Packet {
    pub fn impedance_on(&self) -> bool {
        self.impedance_status == IMPEDANCE_ON
    }
}

/// What the decoder reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A complete packet.
    Packet(Packet),
    /// The counter jumped: this many packets are missing before the next one.
    Lost(u32),
    /// This many bytes were discarded while searching for a packet boundary.
    Skipped(usize),
}

/// Combine the three bytes of one channel into signed ADC counts.
///
/// This is the vendor's formula. On a real Quick-20 every channel byte is
/// even (its low bit is clear, so it can never equal the sync byte); each
/// byte then carries 7 bits and the result is a multiple of 8.
pub fn decode_sample(b0: u8, b1: u8, b2: u8) -> i32 {
    let word = ((b0 as u32) << 24) | ((b1 as u32) << 17) | ((b2 as u32) << 10);
    (word as i32) >> 8
}

/// Number of packets missing between two consecutive counters.
pub fn lost_between(previous: u8, current: u8) -> u32 {
    let m = COUNTER_MODULUS as i32;
    let step = (current as i32 - previous as i32).rem_euclid(m);
    ((step - 1).rem_euclid(m)) as u32
}

/// Incremental decoder for the legacy stream format.
#[derive(Debug)]
pub struct LegacyDecoder {
    layout: Layout,
    buf: Vec<u8>,
    locked: bool,
    last_counter: Option<u8>,
}

impl LegacyDecoder {
    pub fn new(layout: Layout) -> Self {
        Self {
            layout,
            buf: Vec::new(),
            locked: false,
            last_counter: None,
        }
    }

    pub fn layout(&self) -> Layout {
        self.layout
    }

    /// Append received bytes and push every event they complete onto `out`.
    pub fn feed(&mut self, bytes: &[u8], out: &mut Vec<Event>) {
        self.buf.extend_from_slice(bytes);
        let len = self.layout.packet_len();
        let mut pos = 0;
        loop {
            if !self.locked {
                // A data byte can equal the sync byte, so a boundary only
                // counts once the byte one packet later is a sync byte too.
                match self.find_boundary(pos) {
                    Some(start) => {
                        if start > pos {
                            out.push(Event::Skipped(start - pos));
                        }
                        pos = start;
                        self.locked = true;
                    }
                    None => {
                        // Keep enough bytes to test every remaining candidate.
                        let keep_from = self.buf.len().saturating_sub(len).max(pos);
                        if keep_from > pos {
                            out.push(Event::Skipped(keep_from - pos));
                        }
                        pos = keep_from;
                        break;
                    }
                }
            }
            if self.buf.len() - pos < len {
                break;
            }
            // A damaged (short) packet runs into its successor. When the byte
            // after this packet has already arrived it must be a sync byte;
            // when it has not, the packet is emitted without waiting for it.
            let next_ok = self.buf.get(pos + len).is_none_or(|&b| b == SYNC);
            if self.buf[pos] != SYNC || !next_ok {
                self.locked = false;
                if self.buf[pos] == SYNC {
                    // Not a boundary after all: step past it before rescanning.
                    out.push(Event::Skipped(1));
                    pos += 1;
                }
                continue;
            }
            let packet = self.parse(&self.buf[pos..pos + len]);
            if let Some(previous) = self.last_counter {
                let lost = lost_between(previous, packet.counter);
                if lost > 0 {
                    out.push(Event::Lost(lost));
                }
            }
            self.last_counter = Some(packet.counter);
            out.push(Event::Packet(packet));
            pos += len;
        }
        self.buf.drain(..pos);
    }

    fn find_boundary(&self, from: usize) -> Option<usize> {
        let len = self.layout.packet_len();
        (from..self.buf.len().saturating_sub(len))
            .find(|&i| self.buf[i] == SYNC && self.buf[i + len] == SYNC)
    }

    fn parse(&self, p: &[u8]) -> Packet {
        let n = self.layout.channels();
        let raw = (0..n)
            .map(|c| {
                let o = HEADER_LEN + 3 * c;
                decode_sample(p[o], p[o + 1], p[o + 2])
            })
            .collect();
        let t = HEADER_LEN + 3 * n;
        Packet {
            counter: p[1],
            raw,
            impedance_status: p[t],
            battery: p[t + 1],
            trigger: ((p[t + 2] as u16) << 8) | p[t + 3] as u16,
        }
    }
}
