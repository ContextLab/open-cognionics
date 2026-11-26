//! The one data type that both the live transport and file replay emit.

use crate::legacy::{Event, Layout, LegacyDecoder};
use crate::scale::battery_volts;
use crate::timing::ClockState;

/// A run of consecutive samples decoded from one read.
#[derive(Clone, Debug, PartialEq)]
pub struct Chunk {
    /// Index of the first sample since the stream started (lost samples count).
    pub index: u64,
    /// Number of samples (rows) in this chunk.
    pub samples: usize,
    /// Number of channels (columns): EEG, then EXT, then ACC.
    pub channels: usize,
    /// Raw ADC counts, row-major, `samples * channels` values.
    pub counts: Vec<i32>,
    /// Host time of the first sample, in seconds on the clock the arrival
    /// times came from (the Unix epoch for a live headset). Sample `k` of the
    /// chunk is at `time + k / sample_rate`. See [`crate::timing`].
    pub time: f64,
    /// Battery voltage, from the last packet in the chunk.
    pub battery_volts: f64,
    /// Whether impedance mode was on in the last packet in the chunk.
    pub impedance_on: bool,
    /// Trigger value of every sample.
    pub triggers: Vec<u16>,
}

/// What a stream produces.
#[derive(Clone, Debug, PartialEq)]
pub enum StreamEvent {
    Chunk(Chunk),
    /// `count` samples starting at `index` never arrived.
    Lost {
        index: u64,
        count: u32,
    },
    /// The transport went away. Never produced by [`Chunker`].
    Disconnected(String),
}

/// Turns raw bytes into [`StreamEvent`]s.
pub struct Chunker {
    decoder: LegacyDecoder,
    sample_rate: f64,
    clock: ClockState,
    /// When set, sample `n` is stamped `start + n / sample_rate` and arrival
    /// times are ignored.
    nominal_start: Option<f64>,
    next_index: u64,
}

impl Chunker {
    pub fn new(layout: Layout, sample_rate: f64) -> Chunker {
        Chunker {
            decoder: LegacyDecoder::new(layout),
            sample_rate,
            clock: ClockState::default(),
            nominal_start: None,
            next_index: 0,
        }
    }

    /// A chunker for bytes that carry no arrival times (a saved recording):
    /// sample `n` is stamped `start + n / sample_rate`, and the `arrival`
    /// given to [`Chunker::push`] is ignored.
    pub fn nominal(layout: Layout, sample_rate: f64, start: f64) -> Chunker {
        Chunker {
            nominal_start: Some(start),
            ..Chunker::new(layout, sample_rate)
        }
    }

    /// Feed bytes read at host time `arrival`; chunks and losses are appended to `out`.
    ///
    /// Every sample in the read is taken into the clock model before any
    /// chunk from it is stamped, because the newest sample of a read is the
    /// one that waited least (see [`crate::timing`]).
    pub fn push(&mut self, bytes: &[u8], arrival: f64, out: &mut Vec<StreamEvent>) {
        let first_new = out.len();
        let channels = self.decoder.layout().channels();
        let mut events = Vec::new();
        self.decoder.feed(bytes, &mut events);
        let mut pending: Option<Chunk> = None;
        for event in events {
            match event {
                Event::Packet(p) => {
                    if self.nominal_start.is_none() {
                        self.clock
                            .observe(arrival, self.next_index, self.sample_rate);
                    }
                    let chunk = pending.get_or_insert_with(|| Chunk {
                        index: self.next_index,
                        samples: 0,
                        channels,
                        counts: Vec::new(),
                        time: 0.0,
                        battery_volts: 0.0,
                        impedance_on: false,
                        triggers: Vec::new(),
                    });
                    chunk.samples += 1;
                    chunk.counts.extend_from_slice(&p.raw);
                    chunk.triggers.push(p.trigger);
                    chunk.battery_volts = battery_volts(p.battery);
                    chunk.impedance_on = p.impedance_on();
                    self.next_index += 1;
                }
                Event::Lost(n) => {
                    if let Some(chunk) = pending.take() {
                        out.push(StreamEvent::Chunk(chunk));
                    }
                    out.push(StreamEvent::Lost {
                        index: self.next_index,
                        count: n,
                    });
                    self.next_index += u64::from(n);
                }
                Event::Skipped(_) => {}
            }
        }
        if let Some(chunk) = pending {
            out.push(StreamEvent::Chunk(chunk));
        }
        for event in &mut out[first_new..] {
            if let StreamEvent::Chunk(chunk) = event {
                chunk.time = match self.nominal_start {
                    Some(start) => start + chunk.index as f64 / self.sample_rate,
                    None => self.clock.timestamp(chunk.index, self.sample_rate),
                };
            }
        }
    }
}
