//! Read a saved session back as the stream of events a live headset produces.
//!
//! [`Replay::open`] takes a session directory (see [`crate::record`]) or a
//! bare `raw.bin`. The bytes are read 4096 at a time and fed to a
//! [`Chunker`]. A recording holds no arrival times, so sample `n` is stamped
//! the session start (`meta.start_unix`, or 0 when unknown) plus
//! `n / sample_rate`; these are not the timestamps the live stream gave. The iterator never yields
//! [`StreamEvent::Disconnected`]; it ends at the end of the file or at a read error (see [`Replay::error`]).

use crate::chunk::{Chunker, StreamEvent};
use crate::config::DeviceConfig;
use crate::record::SessionMeta;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::Path;
use std::time::{Duration, Instant};

const READ_SIZE: usize = 4096;

/// A marker saved in `markers.jsonl`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Marker {
    pub label: String,
    /// Samples in the recording when the marker was placed (0 is the first
    /// sample of the recording; lost samples count).
    pub sample: u64,
    /// Host time (seconds since the Unix epoch) when it was placed.
    pub unix_time: f64,
}

/// A line of `clock.jsonl`: the clock model's host time of one sample.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ClockPoint {
    /// Sample index in the recording.
    pub sample: u64,
    /// Host time of that sample, seconds since the Unix epoch.
    pub unix_time: f64,
}

#[derive(Debug)]
pub enum ReplayError {
    /// A bare `raw.bin` (or a directory without a usable `meta.json`) needs a configuration.
    NeedsConfig,
    Io(io::Error),
    BadMeta(String),
    BadMarkers(String),
    BadClock(String),
}

impl fmt::Display for ReplayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReplayError::NeedsConfig => {
                write!(f, "no meta.json: the device configuration must be given")
            }
            ReplayError::Io(e) => write!(f, "I/O error: {e}"),
            ReplayError::BadMeta(e) => write!(f, "bad meta.json: {e}"),
            ReplayError::BadMarkers(e) => write!(f, "bad markers.jsonl: {e}"),
            ReplayError::BadClock(e) => write!(f, "bad clock.jsonl: {e}"),
        }
    }
}

impl std::error::Error for ReplayError {}

impl From<io::Error> for ReplayError {
    fn from(e: io::Error) -> Self {
        ReplayError::Io(e)
    }
}

/// A saved recording, iterated as [`StreamEvent`]s.
pub struct Replay {
    file: File,
    config: DeviceConfig,
    markers: Vec<Marker>,
    clock: Vec<ClockPoint>,
    clock_error: Option<String>,
    chunker: Chunker,
    start: f64,
    realtime: bool,
    began: Option<Instant>,
    /// Stream position (samples, counting lost ones) after the events so far.
    position: u64,
    queue: VecDeque<StreamEvent>,
    done: bool,
    error: Option<io::Error>,
}

fn read_markers(path: &Path) -> Result<Vec<Marker>, ReplayError> {
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    text.lines()
        .enumerate()
        .filter(|(_, line)| !line.trim().is_empty())
        .map(|(i, line)| {
            serde_json::from_str(line)
                .map_err(|e| ReplayError::BadMarkers(format!("line {}: {e}", i + 1)))
        })
        .collect()
}

/// The lines of `clock.jsonl`; none if the file does not exist (recordings
/// made before it was written). Sample numbers must increase and times must
/// be finite and never decrease. A last line that does not parse and is not
/// ended by a newline was cut short by a crash or a full disk: it is left
/// out and reported in the second value. Any other bad line is an error.
fn read_clock(path: &Path) -> Result<(Vec<ClockPoint>, Option<String>), ReplayError> {
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok((Vec::new(), None)),
        Err(e) => return Err(e.into()),
    };
    let mut points: Vec<ClockPoint> = Vec::new();
    let mut cut_short = None;
    let lines = text.lines().count();
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let bad = |what: String| ReplayError::BadClock(format!("line {}: {what}", i + 1));
        let point: ClockPoint = match serde_json::from_str(line) {
            Ok(p) => p,
            Err(_) if i + 1 == lines && !text.ends_with('\n') => {
                cut_short = Some(format!("line {}: cut short", i + 1));
                break;
            }
            Err(e) => return Err(bad(e.to_string())),
        };
        if !point.unix_time.is_finite() {
            return Err(bad("unix_time is not finite".into()));
        }
        if let Some(last) = points.last() {
            if point.sample <= last.sample || point.unix_time < last.unix_time {
                return Err(bad("not after the line before".into()));
            }
        }
        points.push(point);
    }
    Ok((points, cut_short))
}

impl Replay {
    /// Open a session directory or a bare `raw.bin`.
    ///
    /// A configuration passed in `config` takes precedence over the one in
    /// `meta.json`. With `realtime`, events are released no earlier than
    /// their arrival time relative to the first call to `next`.
    pub fn open(
        path: &Path,
        config: Option<DeviceConfig>,
        realtime: bool,
    ) -> Result<Replay, ReplayError> {
        fs::metadata(path)?;
        let (raw_path, meta, markers, (clock, clock_error)) = if path.is_dir() {
            let meta = match fs::read(path.join("meta.json")) {
                Ok(bytes) => Some(
                    serde_json::from_slice::<SessionMeta>(&bytes)
                        .map_err(|e| ReplayError::BadMeta(e.to_string()))?,
                ),
                Err(_) => None,
            };
            let markers = read_markers(&path.join("markers.jsonl"))?;
            let clock = read_clock(&path.join("clock.jsonl"))?;
            (path.join("raw.bin"), meta, markers, clock)
        } else {
            (path.to_path_buf(), None, Vec::new(), (Vec::new(), None))
        };
        let start = meta.as_ref().map_or(0.0, |m| m.start_unix);
        if !start.is_finite() {
            return Err(ReplayError::BadMeta("start_unix is not finite".into()));
        }
        let config = match (config, meta) {
            (Some(c), _) => c,
            (None, Some(m)) => m.config,
            (None, None) => return Err(ReplayError::NeedsConfig),
        };
        config
            .validate()
            .map_err(|e| ReplayError::BadMeta(e.to_string()))?;
        let file = File::open(&raw_path)?;
        let chunker = Chunker::nominal(config.layout, config.sample_rate, start);
        Ok(Replay {
            file,
            config,
            markers,
            clock,
            clock_error,
            chunker,
            start,
            realtime,
            began: None,
            position: 0,
            queue: VecDeque::new(),
            done: false,
            error: None,
        })
    }

    pub fn config(&self) -> &DeviceConfig {
        &self.config
    }

    pub fn markers(&self) -> &[Marker] {
        &self.markers
    }

    /// The lines of `clock.jsonl` (empty without the file); the whole lines
    /// before a last line that was cut short. The events this iterator
    /// yields do not use them: their times stay nominal.
    pub fn clock(&self) -> &[ClockPoint] {
        &self.clock
    }

    /// What was left out of `clock.jsonl`, for example `line 3: cut short`
    /// when the last line was cut short; `None` when the file was whole or
    /// absent.
    pub fn clock_error(&self) -> Option<&str> {
        self.clock_error.as_deref()
    }

    /// The read error that ended the replay early, if any. `None` after a
    /// clean replay; check it once the iterator has returned `None` to tell a
    /// complete recording from one that could not be read to the end.
    pub fn error(&self) -> Option<&io::Error> {
        self.error.as_ref()
    }

    fn wait_until(&mut self, seconds: f64) {
        if !self.realtime {
            return;
        }
        let began = *self.began.get_or_insert_with(Instant::now);
        let Ok(offset) = Duration::try_from_secs_f64(seconds) else {
            return;
        };
        let Some(due) = began.checked_add(offset) else {
            return;
        };
        let now = Instant::now();
        if due > now {
            std::thread::sleep(due - now);
        }
    }

    /// Read the next block and queue its events; false at end of file.
    fn refill(&mut self) -> bool {
        let mut buf = [0u8; READ_SIZE];
        let n = loop {
            match self.file.read(&mut buf) {
                Ok(n) => break n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    self.error = Some(e);
                    break 0;
                }
            }
        };
        let offset = self.position as f64 / self.config.sample_rate;
        // At the end of the file, hold until the last sample's time has passed.
        self.wait_until(offset);
        if n == 0 {
            return false;
        }
        let mut out = Vec::new();
        self.chunker.push(&buf[..n], self.start + offset, &mut out);
        for event in &out {
            match event {
                StreamEvent::Chunk(c) => self.position = c.index + c.samples as u64,
                StreamEvent::Lost { index, count } => self.position = index + u64::from(*count),
                StreamEvent::Disconnected(_) => {}
            }
        }
        self.queue.extend(out);
        true
    }
}

impl Iterator for Replay {
    type Item = StreamEvent;

    fn next(&mut self) -> Option<StreamEvent> {
        while self.queue.is_empty() && !self.done {
            if !self.refill() {
                self.done = true;
            }
        }
        self.queue.pop_front()
    }
}
