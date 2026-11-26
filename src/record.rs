//! A recording session on disk.
//!
//! The directory holds `raw.bin` (the bytes exactly as received),
//! `recording.bdf` (see [`crate::bdf`]), `markers.jsonl` (one JSON object per
//! marker), `clock.jsonl` (one JSON object per clock point, see
//! [`Recording`]) and `meta.json` (the [`SessionMeta`] plus `finished` and
//! `eeg_volts_per_count`). `raw.bin`, `markers.jsonl` and `clock.jsonl` are
//! written to the file on every call, so a crash loses at most the call in
//! progress. `meta.json` says `"finished": false` from the start and is
//! rewritten by [`Recorder::finish`].
//!
//! A directory that already holds any of these files is refused with an
//! [`io::ErrorKind::AlreadyExists`] error, and nothing in it is changed.
//!
//! The methods that write return nothing; the first I/O error is kept, later
//! writes are skipped, and [`Recorder::finish`] returns it
//! ([`Recorder::error`] shows it earlier).
//!
//! [`Recording`] is what a live session uses: it owns a [`Recorder`], decodes
//! the bytes it saves and counts its own position, so every sample number it
//! saves (markers, BDF annotations, lost-sample notes, clock points) is in
//! the recording's own frame: sample 0 is the first whole packet in
//! `raw.bin`, the same index [`crate::replay::Replay`] gives.

use crate::bdf::BdfWriter;
use crate::chunk::{Chunker, StreamEvent};
use crate::config::DeviceConfig;
use crate::replay::{ClockPoint, Marker};
use crate::timing::ClockState;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

/// Every file a session directory holds.
pub const SESSION_FILES: [&str; 5] = [
    "raw.bin",
    "markers.jsonl",
    "clock.jsonl",
    "meta.json",
    "recording.bdf",
];

/// What is known about a session when it starts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionMeta {
    pub participant: String,
    pub participant_uuid: Option<String>,
    /// Seconds since the Unix epoch at the start of the session.
    pub start_unix: f64,
    pub config: DeviceConfig,
    pub port: String,
    pub software_version: String,
}

pub struct Recorder {
    dir: PathBuf,
    meta: SessionMeta,
    raw: File,
    markers: File,
    clock: File,
    bdf: BdfWriter,
    error: Option<io::Error>,
}

fn write_meta(dir: &Path, meta: &SessionMeta, finished: bool) -> io::Result<()> {
    let mut value = serde_json::to_value(meta).map_err(io::Error::other)?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| io::Error::other("meta is not an object"))?;
    object.insert(
        "eeg_volts_per_count".into(),
        serde_json::json!(meta.config.eeg_volts_per_count()),
    );
    object.insert("finished".into(), serde_json::json!(finished));
    let text = serde_json::to_vec_pretty(&value).map_err(io::Error::other)?;
    let tmp = dir.join("meta.json.tmp");
    fs::write(&tmp, text)?;
    fs::rename(&tmp, dir.join("meta.json"))
}

/// Create `path`, failing if it exists.
fn create_new(path: &Path) -> io::Result<File> {
    OpenOptions::new().write(true).create_new(true).open(path)
}

impl Recorder {
    /// Create `dir` (and parents) and the session files in it. Fails with
    /// [`io::ErrorKind::AlreadyExists`], changing nothing, if `dir` already
    /// holds any of [`SESSION_FILES`].
    pub fn start(dir: &Path, meta: SessionMeta) -> io::Result<Recorder> {
        // A configuration the BDF+ writer refuses is refused before any file
        // is created.
        crate::bdf::check_config(&meta.config)?;
        fs::create_dir_all(dir)?;
        for name in SESSION_FILES {
            let path = dir.join(name);
            if path.symlink_metadata().is_ok() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("{} already exists", path.display()),
                ));
            }
        }
        // `raw.bin` first, with create_new: of two recorders started at once
        // in one directory only one gets past this line.
        let raw = create_new(&dir.join("raw.bin"))?;
        let markers = create_new(&dir.join("markers.jsonl"))?;
        let clock = create_new(&dir.join("clock.jsonl"))?;
        let bdf = BdfWriter::create(
            &dir.join("recording.bdf"),
            &meta.config,
            &meta.participant,
            meta.start_unix,
        )?;
        write_meta(dir, &meta, false)?;
        Ok(Recorder {
            dir: dir.to_path_buf(),
            meta,
            raw,
            markers,
            clock,
            bdf,
            error: None,
        })
    }

    /// The first error from any write so far.
    pub fn error(&self) -> Option<&io::Error> {
        self.error.as_ref().or_else(|| self.bdf.error())
    }

    fn keep(&mut self, result: io::Result<()>) {
        if let Err(e) = result {
            if self.error.is_none() {
                self.error = Some(e);
            }
        }
    }

    /// Append bytes exactly as received.
    pub fn raw(&mut self, bytes: &[u8]) {
        if self.error.is_none() {
            let r = self.raw.write_all(bytes);
            self.keep(r);
        }
    }

    /// Add a decoded event to the BDF+ file.
    pub fn event(&mut self, e: &StreamEvent) {
        self.bdf.write(e);
    }

    /// Record a marker at sample `sample`, seen at host time `unix_time`.
    pub fn mark(&mut self, label: &str, sample: u64, unix_time: f64) {
        self.bdf.annotate(sample, label);
        if self.error.is_none() {
            let marker = Marker {
                label: label.to_string(),
                sample,
                unix_time,
            };
            let r = serde_json::to_string(&marker)
                .map_err(io::Error::other)
                .and_then(|line| writeln!(self.markers, "{line}"));
            self.keep(r);
        }
    }

    /// Record that sample `sample` is at host time `unix_time`.
    pub fn clock(&mut self, sample: u64, unix_time: f64) {
        if self.error.is_none() {
            let point = ClockPoint { sample, unix_time };
            let r = serde_json::to_string(&point)
                .map_err(io::Error::other)
                .and_then(|line| writeln!(self.clock, "{line}"));
            self.keep(r);
        }
    }

    /// Complete the BDF+ file and mark `meta.json` finished.
    pub fn finish(self) -> io::Result<()> {
        let Recorder {
            dir,
            meta,
            bdf,
            error,
            ..
        } = self;
        let bdf_result = bdf.finish();
        if let Some(e) = error {
            return Err(e);
        }
        bdf_result?;
        write_meta(&dir, &meta, true)
    }
}

/// A recording fed with the bytes of a live stream.
///
/// [`Recording::take`] saves the bytes, decodes them with its own
/// [`Chunker`] and counts the samples (lost ones included), so
/// [`Recording::position`] is the number of samples in the recording so far
/// and every sample number this type saves is in the recording's own frame.
///
/// The host arrival time of every read feeds a [`ClockState`] in that frame.
/// `clock.jsonl` gets a line `{"sample": n, "unix_time": t}`, where `t` is
/// the clock model's timestamp of sample `n` when the line is written: for
/// sample 0 once the first sample has been read, then for every whole second
/// of samples (`n` a multiple of the rounded sample rate), and at
/// [`Recording::finish`] for the position at the end. Reads given a
/// non-finite arrival time are recorded but not timed.
pub struct Recording {
    recorder: Recorder,
    chunker: Chunker,
    sample_rate: f64,
    position: u64,
    events: Vec<StreamEvent>,
    clock: ClockState,
    /// Sample of the next periodic clock line; `None` before the first.
    next_clock_line: Option<u64>,
    /// Sample of the last clock line written.
    last_clock_line: Option<u64>,
}

impl Recording {
    /// Start a recording in `dir`; see [`Recorder::start`].
    pub fn start(dir: &Path, meta: SessionMeta) -> io::Result<Recording> {
        let layout = meta.config.layout;
        let sample_rate = meta.config.sample_rate;
        let chunker = Chunker::nominal(layout, sample_rate, meta.start_unix);
        Ok(Recording {
            recorder: Recorder::start(dir, meta)?,
            chunker,
            sample_rate,
            position: 0,
            events: Vec::new(),
            clock: ClockState::default(),
            next_clock_line: None,
            last_clock_line: None,
        })
    }

    /// Samples in the recording so far, lost ones included: the index the
    /// next sample will get.
    pub fn position(&self) -> u64 {
        self.position
    }

    /// The first error from any write so far.
    pub fn error(&self) -> Option<&io::Error> {
        self.recorder.error()
    }

    /// Samples per clock line: one second, at least one sample.
    fn clock_step(&self) -> u64 {
        self.sample_rate.round().max(1.0) as u64
    }

    /// Save `bytes`, read at host time `arrival` (seconds since the Unix
    /// epoch), and their decoded samples.
    pub fn take(&mut self, bytes: &[u8], arrival: f64) {
        self.recorder.raw(bytes);
        self.events.clear();
        self.chunker.push(bytes, arrival, &mut self.events);
        for event in &self.events {
            self.recorder.event(event);
            match event {
                StreamEvent::Chunk(c) => {
                    // Every sample of the read is observed before any
                    // timestamp is taken (see crate::timing).
                    for i in c.index..c.index + c.samples as u64 {
                        self.clock.observe(arrival, i, self.sample_rate);
                    }
                    self.position = c.index + c.samples as u64;
                }
                StreamEvent::Lost { index, count } => self.position = index + u64::from(*count),
                StreamEvent::Disconnected(_) => {}
            }
        }
        if self.clock.offset().is_none() {
            return;
        }
        let step = self.clock_step();
        let mut next = match self.next_clock_line {
            Some(next) => next,
            None => {
                self.write_clock(0);
                step
            }
        };
        while next < self.position {
            self.write_clock(next);
            next += step;
        }
        self.next_clock_line = Some(next);
    }

    fn write_clock(&mut self, sample: u64) {
        let time = self.clock.timestamp(sample, self.sample_rate);
        self.recorder.clock(sample, time);
        self.last_clock_line = Some(sample);
    }

    /// Place a marker at the current position, seen at host time `unix_time`;
    /// returns its sample (the position).
    pub fn mark(&mut self, label: &str, unix_time: f64) -> u64 {
        let sample = self.position;
        self.recorder.mark(label, sample, unix_time);
        sample
    }

    /// Note in the BDF+ file, at the current position, that the link dropped.
    pub fn disconnected(&mut self, reason: &str) {
        self.recorder
            .event(&StreamEvent::Disconnected(reason.to_string()));
    }

    /// Write the last clock line and complete the files.
    pub fn finish(mut self) -> io::Result<()> {
        let end = self.position;
        if self.clock.offset().is_some() && self.last_clock_line.is_some_and(|last| last < end) {
            self.write_clock(end);
        }
        self.recorder.finish()
    }
}
