//! A minimal BDF+ reader for the tests: header fields, data and annotations.
#![allow(dead_code)] // not every test binary uses every helper

use std::path::Path;

/// (header bytes, record count, signals, samples per record, per-signal
/// samples, annotation bytes per record).
pub struct Bdf {
    pub header: Vec<u8>,
    pub records: usize,
    pub signals: usize,
    pub spr: usize,
    /// Every data signal (all but the annotation signal), as 24-bit values.
    pub data: Vec<Vec<i32>>,
    pub annotations: Vec<Vec<u8>>,
}

impl Bdf {
    /// The data signals' values at `sample`.
    pub fn sample(&self, sample: usize) -> Vec<i32> {
        self.data.iter().map(|column| column[sample]).collect()
    }
}

pub fn read_bdf(path: &Path) -> Bdf {
    let bytes = std::fs::read(path).unwrap();
    let text = |a: usize, b: usize| String::from_utf8_lossy(&bytes[a..b]).trim().to_string();
    let header_len: usize = text(184, 192).parse().unwrap();
    let records: i64 = text(236, 244).parse().unwrap();
    let signals: usize = text(252, 256).parse().unwrap();
    assert_eq!(header_len, 256 * (signals + 1));
    // "nr of samples" field of signal 0: after label 16, transducer 80, dim 8,
    // pmin 8, pmax 8, dmin 8, dmax 8, prefilter 80 = 216 bytes per signal.
    let nsamp = |s: usize| -> usize {
        let at = 256 + signals * 216 + s * 8;
        text(at, at + 8).parse().unwrap()
    };
    let spr = nsamp(0);
    let ann_len = nsamp(signals - 1) * 3;
    let record_len = (signals - 1) * spr * 3 + ann_len;
    assert_eq!((bytes.len() - header_len) % record_len, 0);
    let n = (bytes.len() - header_len) / record_len;
    assert_eq!(records, n as i64, "record count patched");
    let mut data = vec![Vec::new(); signals - 1];
    let mut annotations = Vec::new();
    for r in 0..n {
        let rec = &bytes[header_len + r * record_len..][..record_len];
        for (s, col) in data.iter_mut().enumerate() {
            for i in 0..spr {
                let b = &rec[(s * spr + i) * 3..][..3];
                let v =
                    i32::from_le_bytes([b[0], b[1], b[2], if b[2] & 0x80 != 0 { 255 } else { 0 }]);
                col.push(v);
            }
        }
        annotations.push(rec[(signals - 1) * spr * 3..].to_vec());
    }
    Bdf {
        header: bytes[..header_len].to_vec(),
        records: n,
        signals,
        spr,
        data,
        annotations,
    }
}

/// Parse the TALs of one record into (onset, text); the time-keeping TAL gives an empty text.
pub fn tals(block: &[u8]) -> Vec<(f64, String)> {
    let mut out = Vec::new();
    for tal in block.split(|&b| b == 0).filter(|t| !t.is_empty()) {
        let parts: Vec<&[u8]> = tal.split(|&b| b == 0x14).collect();
        let onset = String::from_utf8(parts[0].to_vec()).unwrap();
        let onset: f64 = onset.split('\x15').next().unwrap().parse().unwrap();
        for text in &parts[1..parts.len() - 1] {
            out.push((onset, String::from_utf8(text.to_vec()).unwrap()));
        }
    }
    out
}

/// Every annotation but the time-keeping ones, checking those come first.
pub fn all_labels(bdf: &Bdf) -> Vec<(f64, String)> {
    let mut v = Vec::new();
    for (r, block) in bdf.annotations.iter().enumerate() {
        let t = tals(block);
        assert_eq!(
            t[0],
            (r as f64, String::new()),
            "time-keeping first in record {r}"
        );
        v.extend(t.into_iter().skip(1));
    }
    v
}

/// Counts of every sample of a saved session, by recording sample index
/// (from `raw.bin` through `Replay`); `None` for lost samples.
pub fn replay_rows(dir: &Path) -> Vec<Option<Vec<i32>>> {
    use open_cognionics::chunk::StreamEvent;
    let mut rows = Vec::new();
    for event in open_cognionics::replay::Replay::open(dir, None, false).unwrap() {
        match event {
            StreamEvent::Chunk(c) => {
                assert_eq!(c.index as usize, rows.len());
                for row in c.counts.chunks(c.channels) {
                    rows.push(Some(row.to_vec()));
                }
            }
            StreamEvent::Lost { index, count } => {
                assert_eq!(index as usize, rows.len());
                rows.extend((0..count).map(|_| None));
            }
            StreamEvent::Disconnected(_) => {}
        }
    }
    rows
}
