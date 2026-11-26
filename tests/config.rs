//! Tests for the label table and the configuration parser, using the
//! configuration reply recorded from a real Quick-20.

use open_cognionics::config::{extract_words, ConfigError, DeviceConfig};
use open_cognionics::labels::label_name;
use open_cognionics::legacy::{Event, Layout, LegacyDecoder};

const CONFIG_END: u32 = 1_234_567_890;

fn fixture() -> Vec<u8> {
    std::fs::read("tests/fixtures/config_reply.bin").unwrap()
}

fn recorded_words() -> Vec<u32> {
    extract_words(&fixture()).unwrap().words
}

#[test]
fn label_codes_from_the_headset_map_to_names() {
    let codes = [
        19, 4, 6, 27, 21, 23, 25, 43, 45, 71, 63, 67, 69, 341, 65, 84, 86, 47, 343, 348,
    ];
    let names: Vec<_> = codes.iter().map(|&c| label_name(c).unwrap()).collect();
    assert_eq!(
        names,
        [
            "F7", "Fp1", "Fp2", "F8", "F3", "Fz", "F4", "C3", "Cz", "P8", "P7", "Pz", "P4", "T3",
            "P3", "O1", "O2", "C4", "T4", "A2"
        ]
    );
    assert_eq!(label_name(348), Some("A2"));
    assert_eq!(label_name(349), None);
    assert_eq!(label_name(364), None);
}

#[test]
fn parses_the_recorded_configuration_reply() {
    let bytes = fixture();
    let block = extract_words(&bytes).unwrap();
    assert_eq!(*block.words.last().unwrap(), CONFIG_END);
    let cfg = DeviceConfig::from_words(&block.words).unwrap();
    assert_eq!(
        cfg.layout,
        Layout {
            eeg: 20,
            ext: 0,
            acc: 3
        }
    );
    assert_eq!(cfg.sample_rate, 500.0);
    assert_eq!(cfg.gain, 3);
    assert_eq!(cfg.labels[..3], ["F7", "Fp1", "Fp2"]);
    assert_eq!(cfg.labels.len(), 20);
    assert!(cfg.impedance_at_power_on);
    assert!(!cfg.compressed);
    assert!((cfg.eeg_volts_per_count() - 9.934107462565105e-08).abs() < 1e-20);
}

#[test]
fn channel_names_are_labels_then_accelerometer_axes() {
    let cfg = DeviceConfig::from_words(&recorded_words()).unwrap();
    let names = cfg.channel_names();
    assert_eq!(names.len(), 23);
    assert_eq!(names[0], "F7");
    assert_eq!(names[19], "A2");
    assert_eq!(names[20..], ["ACC_X", "ACC_Y", "ACC_Z"]);
}

#[test]
fn config_round_trips_through_json_serde() {
    let cfg = DeviceConfig::from_words(&recorded_words()).unwrap();
    let text = serde_json::to_string(&cfg).unwrap();
    let back: DeviceConfig = serde_json::from_str(&text).unwrap();
    assert_eq!(back, cfg);
}

#[test]
fn block_offsets_bracket_the_marker_and_the_end_word() {
    let bytes = fixture();
    let block = extract_words(&bytes).unwrap();
    assert_eq!(bytes[block.start..block.start + 4], [0xFF; 4]);
    assert_eq!(block.start, 9 * 75);
    assert_eq!(block.end, bytes.len() - 150 * 75);
    assert_eq!(block.words.len() * 4 + 4, block.end - block.start);
}

#[test]
fn incomplete_or_absent_configuration_is_none() {
    let bytes = fixture();
    let block = extract_words(&bytes).unwrap();
    let half = block.start + (block.end - block.start) / 2;
    assert!(extract_words(&bytes[..half]).is_none());
    assert!(extract_words(&bytes[..block.end - 1]).is_none());
    assert!(extract_words(&bytes[..block.start]).is_none());
    assert!(extract_words(&bytes[..block.start + 3]).is_none());
    assert!(extract_words(&[]).is_none());
}

#[test]
fn stream_resumes_after_the_block() {
    let bytes = fixture();
    let block = extract_words(&bytes).unwrap();
    let layout = DeviceConfig::from_words(&block.words).unwrap().layout;
    let mut decoder = LegacyDecoder::new(layout);
    let mut events = Vec::new();
    decoder.feed(&bytes[block.end..], &mut events);
    let packets = events
        .iter()
        .filter(|e| matches!(e, Event::Packet(_)))
        .count();
    assert_eq!(packets, 150);
    assert!(!events.iter().any(|e| matches!(e, Event::Lost(_))));
}

#[test]
fn unknown_layouts_are_errors_not_guesses() {
    let words = recorded_words();

    let mut w = words.clone();
    w[7] = 0;
    assert!(matches!(
        DeviceConfig::from_words(&w),
        Err(ConfigError::UnsupportedLayout(_))
    ));

    let mut w = words.clone();
    w[107] = 7;
    assert_eq!(
        DeviceConfig::from_words(&w),
        Err(ConfigError::UnknownRateIndex(7))
    );

    let mut w = words.clone();
    w[105] = 7;
    assert_eq!(
        DeviceConfig::from_words(&w),
        Err(ConfigError::UnknownGainIndex(7))
    );

    // A reply that stops short of the label codes is an error, not a panic.
    assert!(matches!(
        DeviceConfig::from_words(&words[..2030]),
        Err(ConfigError::TooShort(_))
    ));
    assert!(matches!(
        DeviceConfig::from_words(&[]),
        Err(ConfigError::TooShort(_))
    ));

    // A label code outside the table is an error.
    let mut w = words;
    w[2024] = 9999;
    assert_eq!(
        DeviceConfig::from_words(&w),
        Err(ConfigError::UnknownLabelCode(9999))
    );
}

#[test]
fn rate_is_halved_without_high_resolution() {
    let mut w = recorded_words();
    w[106] = 0;
    assert_eq!(DeviceConfig::from_words(&w).unwrap().sample_rate, 250.0);
}

#[test]
fn nonsense_header_values_are_errors_not_panics() {
    let words = recorded_words();
    let s = words[2] as usize;

    let mut w = words.clone();
    w[2] = 0;
    assert_eq!(
        DeviceConfig::from_words(&w),
        Err(ConfigError::Invalid("section sizes"))
    );

    for offset in [0, 2, 4] {
        let mut w = words.clone();
        w[6 * s + offset] = u32::MAX;
        assert_eq!(
            DeviceConfig::from_words(&w),
            Err(ConfigError::Invalid("channel counts"))
        );
    }
}

#[test]
fn a_compressed_stream_format_is_refused_with_a_clear_error() {
    let mut w = recorded_words();
    let s = w[2] as usize;
    // Data mode word: 0 is the compressed (delta) format.
    w[2 * s] = 0;
    let err = DeviceConfig::from_words(&w).unwrap_err();
    assert_eq!(err, ConfigError::CompressedFormat);
    assert!(err.to_string().contains("compressed"), "{err}");
    let mut cfg = DeviceConfig::from_words(&recorded_words()).unwrap();
    cfg.compressed = true;
    assert_eq!(cfg.validate(), Err(ConfigError::CompressedFormat));
}

#[test]
fn sample_rates_below_one_hertz_are_refused() {
    let cfg = DeviceConfig::from_words(&recorded_words()).unwrap();
    for rate in [1e-6, 0.5, 0.999, 0.0, -1.0] {
        let bad = DeviceConfig {
            sample_rate: rate,
            ..cfg.clone()
        };
        assert_eq!(
            bad.validate(),
            Err(ConfigError::Invalid("sample rate")),
            "{rate}"
        );
    }
    let one = DeviceConfig {
        sample_rate: 1.0,
        ..cfg
    };
    assert_eq!(one.validate(), Ok(()));
}
