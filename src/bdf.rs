//! BDF+ (24-bit EDF+) writer for a recording session.
//!
//! Layout: one data record per second, so the sample rate must be a whole
//! number of Hz (see [`check_config`]); the EEG channels (volts scaled to
//! microvolts in the header), the accelerometer channels as raw counts, and a
//! `BDF Annotations` signal of 40 samples (120 bytes) per record. The first
//! annotation in every record is the empty time-keeping one, as EDF+ requires.
//!
//! Notes on behaviour:
//! * Lost samples repeat the previous sample (zeros if there is none) and are
//!   annotated `lost <n>` at the first lost sample.
//! * A trailing partial record is padded by repeating the last sample and
//!   annotated `padding`.
//! * An annotation that does not fit in its record, or whose sample was already
//!   written, goes into the next record with room (onsets are absolute). Any
//!   still queued at `finish` get extra padded records.
//! * Labels are limited to [`MAX_LABEL_BYTES`] bytes (cut at a character
//!   boundary) and the control bytes 0x00, 0x14 and 0x15 become spaces.
//! * The header start time is the whole second of `start_unix` in UTC; the
//!   fractional second is not stored in the BDF (it is in `meta.json`).
//! * The first I/O error is kept; later writes are skipped and `finish`
//!   returns it.

use crate::chunk::StreamEvent;
use crate::config::DeviceConfig;
use std::fs::File;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

/// Samples (3 bytes each) in the annotation signal of every record.
const ANNOTATION_SAMPLES: usize = 40;
const ANNOTATION_BYTES: usize = ANNOTATION_SAMPLES * 3;
/// Longest annotation text written; leaves room for the time-keeping TAL and onset.
pub const MAX_LABEL_BYTES: usize = 80;
const DIGITAL_MIN: i32 = -8_388_608;
const DIGITAL_MAX: i32 = 8_388_607;
/// Offset of the "number of data records" field in the main header.
const RECORDS_FIELD: u64 = 236;

/// Writes a BDF+ file as samples arrive.
pub struct BdfWriter {
    out: BufWriter<File>,
    eeg: usize,
    ext: usize,
    acc: usize,
    /// Samples per signal per record (also the sample rate in Hz).
    spr: usize,
    /// One record of data, signal-major: `buf[signal * spr + i]`.
    buf: Vec<i32>,
    filled: usize,
    last: Vec<i32>,
    samples: u64,
    records: u64,
    pending: Vec<(u64, String)>,
    error: Option<io::Error>,
}

fn pad(s: &str, width: usize) -> Vec<u8> {
    let mut b: Vec<u8> = s
        .chars()
        .map(|c| {
            if c.is_ascii() && !c.is_ascii_control() {
                c
            } else {
                '_'
            }
        })
        .collect::<String>()
        .into_bytes();
    b.truncate(width);
    b.resize(width, b' ');
    b
}

/// A number in at most 8 ASCII characters with as many significant digits as fit.
fn fit8(x: f64) -> String {
    for decimals in (0..=6).rev() {
        let s = format!("{x:.decimals$}");
        if s.len() <= 8 {
            return s;
        }
    }
    format!("{x:.0}")
}

/// Civil date (year, month 1..=12, day) of a day count since 1970-01-01.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = yoe + era * 400 + i64::from(m <= 2);
    (y, m, d)
}

fn clean_label(label: &str) -> String {
    let mut s: String = label
        .chars()
        .map(|c| {
            if matches!(c, '\0' | '\u{14}' | '\u{15}') {
                ' '
            } else {
                c
            }
        })
        .collect();
    while s.len() > MAX_LABEL_BYTES {
        s.pop();
    }
    s
}

fn onset_text(onset: f64) -> String {
    let mut s = format!("{onset:.6}");
    while s.ends_with('0') {
        s.pop();
    }
    if s.ends_with('.') {
        s.pop();
    }
    format!("+{s}")
}

fn put24(out: &mut Vec<u8>, v: i32) {
    out.extend_from_slice(&v.clamp(DIGITAL_MIN, DIGITAL_MAX).to_le_bytes()[..3]);
}

/// Check that `config` can be written as BDF+ with one-second records;
/// returns the samples per record. The sample rate must be a whole number
/// of Hz from 1 to 100000.
pub fn check_config(config: &DeviceConfig) -> io::Result<usize> {
    let rate = config.sample_rate;
    if !(1.0..=100_000.0).contains(&rate) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("sample rate {rate} Hz is out of range for BDF"),
        ));
    }
    if rate.fract() != 0.0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "sample rate {rate} Hz is not a whole number: BDF records of one second need one"
            ),
        ));
    }
    if config.labels.len() != config.layout.eeg {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "label count differs from EEG channel count",
        ));
    }
    Ok(rate as usize)
}

impl BdfWriter {
    /// Create the file and write the header (record count -1 until `finish`).
    /// Fails before creating anything if [`check_config`] refuses `config`.
    pub fn create(
        path: &Path,
        config: &DeviceConfig,
        participant: &str,
        start_unix: f64,
    ) -> io::Result<BdfWriter> {
        let spr = check_config(config)?;
        let layout = config.layout;
        let names = config.channel_names();
        let mut labels: Vec<String> = config.labels.clone();
        for i in 0..layout.acc {
            labels.push(match names.get(layout.eeg + i) {
                Some(n) if layout.acc == 3 => n.clone(),
                _ => format!("ACC_{i}"),
            });
        }
        let data_signals = layout.eeg + layout.acc;
        let signals = data_signals + 1;

        let scale = config.eeg_volts_per_count() * 1e6;
        let eeg_min = fit8(f64::from(DIGITAL_MIN) * scale);
        let eeg_max = fit8(f64::from(DIGITAL_MAX) * scale);

        let secs = start_unix.floor() as i64;
        let (y, mo, d) = civil_from_days(secs.div_euclid(86_400));
        let tod = secs.rem_euclid(86_400);
        let months = [
            "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
        ];
        let yy = if (1985..=2084).contains(&y) {
            y % 100
        } else {
            0
        };
        let patient = format!("{} X X X", participant.replace(' ', "_"));
        let recording = format!(
            "Startdate {:02}-{}-{:04} X X Cognionics",
            d,
            months[(mo - 1) as usize],
            y
        );

        let mut h: Vec<u8> = Vec::with_capacity(256 * (signals + 1));
        h.push(0xFF);
        h.extend_from_slice(b"BIOSEMI");
        h.extend(pad(&patient, 80));
        h.extend(pad(&recording, 80));
        h.extend(pad(&format!("{d:02}.{mo:02}.{yy:02}"), 8));
        h.extend(pad(
            &format!("{:02}.{:02}.{:02}", tod / 3600, tod / 60 % 60, tod % 60),
            8,
        ));
        h.extend(pad(&(256 * (signals + 1)).to_string(), 8));
        h.extend(pad("BDF+C", 44));
        h.extend(pad("-1", 8));
        h.extend(pad("1", 8));
        h.extend(pad(&signals.to_string(), 4));
        debug_assert_eq!(h.len(), 256);

        let int_min = DIGITAL_MIN.to_string();
        let int_max = DIGITAL_MAX.to_string();
        let mut fields: Vec<Vec<Vec<u8>>> = vec![Vec::new(); 10];
        for (i, label) in labels.iter().enumerate() {
            let is_eeg = i < layout.eeg;
            fields[0].push(pad(label, 16));
            fields[1].push(pad("", 80));
            fields[2].push(pad(if is_eeg { "uV" } else { "count" }, 8));
            fields[3].push(pad(if is_eeg { &eeg_min } else { &int_min }, 8));
            fields[4].push(pad(if is_eeg { &eeg_max } else { &int_max }, 8));
            fields[5].push(pad(&int_min, 8));
            fields[6].push(pad(&int_max, 8));
            fields[7].push(pad("", 80));
            fields[8].push(pad(&spr.to_string(), 8));
            fields[9].push(pad("", 32));
        }
        fields[0].push(pad("BDF Annotations", 16));
        fields[1].push(pad("", 80));
        fields[2].push(pad("", 8));
        fields[3].push(pad("-1", 8));
        fields[4].push(pad("1", 8));
        fields[5].push(pad(&int_min, 8));
        fields[6].push(pad(&int_max, 8));
        fields[7].push(pad("", 80));
        fields[8].push(pad(&ANNOTATION_SAMPLES.to_string(), 8));
        fields[9].push(pad("", 32));
        for field in fields {
            for f in field {
                h.extend(f);
            }
        }
        debug_assert_eq!(h.len(), 256 * (signals + 1));

        let mut out = BufWriter::new(File::create(path)?);
        out.write_all(&h)?;
        out.flush()?;
        Ok(BdfWriter {
            out,
            eeg: layout.eeg,
            ext: layout.ext,
            acc: layout.acc,
            spr,
            buf: vec![0; data_signals * spr],
            filled: 0,
            last: vec![0; data_signals],
            samples: 0,
            records: 0,
            pending: Vec::new(),
            error: None,
        })
    }

    /// The first I/O or data error, if any write has failed.
    pub fn error(&self) -> Option<&io::Error> {
        self.error.as_ref()
    }

    fn fail(&mut self, e: io::Error) {
        if self.error.is_none() {
            self.error = Some(e);
        }
    }

    /// Add events from a stream.
    pub fn write(&mut self, event: &StreamEvent) {
        if self.error.is_some() {
            return;
        }
        match event {
            StreamEvent::Chunk(c) => {
                let width = self.eeg + self.ext + self.acc;
                if c.channels != width || c.counts.len() != c.samples * width {
                    self.fail(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("chunk has {} channels, expected {width}", c.channels),
                    ));
                    return;
                }
                for r in 0..c.samples {
                    let row = &c.counts[r * width..(r + 1) * width];
                    let mut sample: Vec<i32> = row[..self.eeg].to_vec();
                    sample.extend_from_slice(&row[self.eeg + self.ext..]);
                    self.push_sample(&sample);
                }
            }
            StreamEvent::Lost { count, .. } => {
                if *count == 0 {
                    return;
                }
                self.annotate(self.samples, &format!("lost {count}"));
                let sample = self.last.clone();
                for _ in 0..*count {
                    self.push_sample(&sample);
                }
            }
            StreamEvent::Disconnected(reason) => {
                self.annotate(self.samples, &format!("disconnected: {reason}"));
            }
        }
    }

    /// Annotate the time of `sample` (since the stream started).
    pub fn annotate(&mut self, sample: u64, label: &str) {
        self.pending.push((sample, clean_label(label)));
    }

    fn push_sample(&mut self, sample: &[i32]) {
        for (s, v) in sample.iter().enumerate() {
            self.buf[s * self.spr + self.filled] = *v;
        }
        self.last.copy_from_slice(sample);
        self.filled += 1;
        self.samples += 1;
        if self.filled == self.spr {
            self.flush_record(false);
        }
    }

    /// Write the current record. With `drain`, annotations are taken
    /// whatever their time (used after the data has ended).
    fn flush_record(&mut self, drain: bool) {
        if self.error.is_some() {
            return;
        }
        let k = self.records;
        let mut ann = format!("+{k}\u{14}\u{14}\0").into_bytes();
        let spr = self.spr as u64;
        let mut kept = Vec::new();
        for (sample, label) in std::mem::take(&mut self.pending) {
            let tal = format!(
                "{}\u{14}{}\u{14}\0",
                onset_text(sample as f64 / spr as f64),
                label
            );
            if (drain || sample / spr <= k) && ann.len() + tal.len() <= ANNOTATION_BYTES {
                ann.extend_from_slice(tal.as_bytes());
            } else {
                kept.push((sample, label));
            }
        }
        self.pending = kept;
        ann.resize(ANNOTATION_BYTES, 0);

        let mut bytes = Vec::with_capacity(self.buf.len() * 3 + ANNOTATION_BYTES);
        for v in &self.buf {
            put24(&mut bytes, *v);
        }
        bytes.extend_from_slice(&ann);
        let result = self.out.write_all(&bytes).and_then(|_| self.out.flush());
        match result {
            Ok(()) => self.records += 1,
            Err(e) => self.fail(e),
        }
        self.filled = 0;
    }

    /// Pad and write what remains, patch the record count and flush.
    pub fn finish(mut self) -> io::Result<()> {
        if self.error.is_none() && self.filled > 0 {
            let at = self.samples;
            self.annotate(at, "padding");
            for s in 0..self.last.len() {
                for i in self.filled..self.spr {
                    self.buf[s * self.spr + i] = self.last[s];
                }
            }
            self.flush_record(true);
        }
        while self.error.is_none() && !self.pending.is_empty() {
            for s in 0..self.last.len() {
                for i in 0..self.spr {
                    self.buf[s * self.spr + i] = self.last[s];
                }
            }
            self.flush_record(true);
        }
        if let Some(e) = self.error.take() {
            return Err(e);
        }
        self.out.seek(SeekFrom::Start(RECORDS_FIELD))?;
        self.out.write_all(&pad(&self.records.to_string(), 8))?;
        self.out.flush()?;
        Ok(())
    }
}
