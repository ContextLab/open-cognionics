//! The headset's description of itself: the configuration dump.
//!
//! Word positions and tables are from `docs/protocol.md`, sections 0 and 5.
//! Only the layout confirmed on hardware (`W[7] == 1`) is understood.

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::labels::label_name;
use crate::legacy::Layout;
use crate::scale::{eeg_volts_per_count, ADS_GAINS};

/// Four `0xFF` bytes announce the configuration words.
pub(crate) const MARKER: [u8; 4] = [0xFF; 4];
/// The last word of the configuration.
pub(crate) const END_WORD: u32 = 1_234_567_890;
/// Sample rate for each rate index, in Hz, at high resolution.
const RATES_HZ: [f64; 7] = [32000.0, 16000.0, 8000.0, 4000.0, 2000.0, 1000.0, 500.0];

/// Largest plausible number of EEG, EXT or ACC channels.
pub const MAX_CHANNELS_PER_KIND: usize = 1000;
/// Largest plausible sample rate in Hz (the hardware tops out at 32000).
pub const MAX_SAMPLE_RATE: f64 = 1_000_000.0;
/// Smallest sample rate accepted, in Hz (the hardware's lowest is 250).
pub const MIN_SAMPLE_RATE: f64 = 1.0;

/// The configuration words found in a byte stream.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigBlock {
    /// Big-endian words after the marker, up to and including the end word.
    pub words: Vec<u32>,
    /// Byte offset of the marker.
    pub start: usize,
    /// Byte offset of the first byte after the end word.
    pub end: usize,
}

/// Find a complete configuration reply in `bytes`.
///
/// Returns `None` if there is no marker or the end word has not arrived yet.
/// On this hardware `0xFF` appears in a stream packet only as its sync byte,
/// so four consecutive `0xFF` bytes are the marker.
pub fn extract_words(bytes: &[u8]) -> Option<ConfigBlock> {
    let start = bytes.windows(MARKER.len()).position(|w| w == MARKER)?;
    let body = start + MARKER.len();
    let mut words = Vec::new();
    for chunk in bytes[body..].chunks_exact(4) {
        let word = u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
        words.push(word);
        if word == END_WORD {
            let end = body + 4 * words.len();
            return Some(ConfigBlock { words, start, end });
        }
    }
    None
}

/// Why a configuration could not be understood.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// The layout version word is not the one confirmed on hardware.
    UnsupportedLayout(u32),
    /// The reply ends before this word index.
    TooShort(usize),
    /// A header or section value is inconsistent or out of range.
    Invalid(&'static str),
    /// The gain index is not in the gain table.
    UnknownGainIndex(u32),
    /// The sample-rate index is not in the rate table.
    UnknownRateIndex(u32),
    /// A label code is not in the label table.
    UnknownLabelCode(u32),
    /// The headset sends the compressed (delta) stream format, which this
    /// library does not decode.
    CompressedFormat,
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedLayout(v) => write!(f, "unsupported configuration layout {v}"),
            Self::TooShort(i) => write!(f, "configuration ends before word {i}"),
            Self::Invalid(what) => write!(f, "invalid configuration: {what}"),
            Self::UnknownGainIndex(i) => write!(f, "unknown gain index {i}"),
            Self::UnknownRateIndex(i) => write!(f, "unknown sample-rate index {i}"),
            Self::UnknownLabelCode(c) => write!(f, "unknown electrode label code {c}"),
            Self::CompressedFormat => write!(
                f,
                "the headset is set to the compressed (delta) stream format, \
                 which this library does not decode"
            ),
        }
    }
}

impl std::error::Error for ConfigError {}

/// What the headset reports about itself.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeviceConfig {
    pub layout: Layout,
    /// Samples per second.
    pub sample_rate: f64,
    /// Amplifier gain (not the index).
    pub gain: u32,
    /// Electrode labels of the EEG channels, in wire order.
    pub labels: Vec<String>,
    pub impedance_at_power_on: bool,
    /// True if the headset sends the delta ("high-speed") format.
    pub compressed: bool,
}

impl DeviceConfig {
    /// Interpret the words of a configuration reply (see [`extract_words`]).
    pub fn from_words(words: &[u32]) -> Result<Self, ConfigError> {
        let get = |i: usize| words.get(i).copied().ok_or(ConfigError::TooShort(i));
        let index = |i: usize| -> Result<usize, ConfigError> {
            usize::try_from(get(i)?).map_err(|_| ConfigError::Invalid("word too large"))
        };

        let version = get(7)?;
        if version != 1 {
            return Err(ConfigError::UnsupportedLayout(version));
        }
        let sc = index(1)?;
        let s = index(2)?;
        let cs = index(4)?;
        // Keep every derived position small enough to be plausible.
        if s == 0 || s > 1000 || sc > 1000 || cs > 100_000 {
            return Err(ConfigError::Invalid("section sizes"));
        }

        let eeg = index(6 * s)?;
        let ext = index(6 * s + 2)?;
        let acc = index(6 * s + 4)?;
        if eeg > MAX_CHANNELS_PER_KIND || ext > MAX_CHANNELS_PER_KIND || acc > MAX_CHANNELS_PER_KIND
        {
            return Err(ConfigError::Invalid("channel counts"));
        }

        let gain_index = get(s + 5)?;
        let gain = *ADS_GAINS
            .get(gain_index as usize)
            .ok_or(ConfigError::UnknownGainIndex(gain_index))?;
        let high_resolution = get(s + 6)? != 0;
        let rate_index = get(s + 7)?;
        let base_rate = *RATES_HZ
            .get(rate_index as usize)
            .ok_or(ConfigError::UnknownRateIndex(rate_index))?;
        let sample_rate = if high_resolution {
            base_rate
        } else {
            base_rate / 2.0
        };

        let compressed = get(2 * s)? == 0;
        let impedance_at_power_on = get(2 * s + 2)? != 0;

        let label_start = sc * s + 2 * cs;
        let labels = (0..eeg)
            .map(|k| {
                let code = get(label_start + k)?;
                label_name(code)
                    .map(str::to_owned)
                    .ok_or(ConfigError::UnknownLabelCode(code))
            })
            .collect::<Result<Vec<_>, _>>()?;

        let config = Self {
            layout: Layout { eeg, ext, acc },
            sample_rate,
            gain,
            labels,
            impedance_at_power_on,
            compressed,
        };
        config.validate()?;
        Ok(config)
    }

    /// Check that the values are plausible and safe to use: every channel
    /// count at most [`MAX_CHANNELS_PER_KIND`], at least one channel, a finite
    /// sample rate in `[MIN_SAMPLE_RATE, MAX_SAMPLE_RATE]`, a non-zero gain,
    /// one label per EEG channel, and the uncompressed stream format (the
    /// only one decoded). [`DeviceConfig::from_words`] applies it; use it on
    /// any configuration that did not come from there (for instance one read
    /// from a file).
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.compressed {
            return Err(ConfigError::CompressedFormat);
        }
        let l = self.layout;
        if l.eeg > MAX_CHANNELS_PER_KIND
            || l.ext > MAX_CHANNELS_PER_KIND
            || l.acc > MAX_CHANNELS_PER_KIND
        {
            return Err(ConfigError::Invalid("channel counts"));
        }
        if l.channels() == 0 {
            return Err(ConfigError::Invalid("no channels"));
        }
        if !(self.sample_rate.is_finite()
            && self.sample_rate >= MIN_SAMPLE_RATE
            && self.sample_rate <= MAX_SAMPLE_RATE)
        {
            return Err(ConfigError::Invalid("sample rate"));
        }
        if self.gain == 0 {
            return Err(ConfigError::Invalid("gain"));
        }
        if self.labels.len() != l.eeg {
            return Err(ConfigError::Invalid("label count"));
        }
        Ok(())
    }

    /// EEG labels followed by `ACC_X`, `ACC_Y`, `ACC_Z`.
    pub fn channel_names(&self) -> Vec<String> {
        let mut names = self.labels.clone();
        names.extend(["ACC_X", "ACC_Y", "ACC_Z"].map(String::from));
        names
    }

    /// Volts per count on the EEG channels at this configuration's gain.
    pub fn eeg_volts_per_count(&self) -> f64 {
        eeg_volts_per_count(self.gain)
    }
}
