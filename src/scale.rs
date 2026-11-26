//! Conversion from raw device values to physical units.
//!
//! Constants are the vendor acquisition app's; see `docs/protocol.md`.

/// ADC reference span in volts.
pub const ADC_SPAN_VOLTS: f64 = 5.0;
/// Full scale of the 24-bit converter.
pub const ADC_FULL_SCALE: f64 = 16_777_216.0;
/// Amplifier gain for each gain index reported in the device configuration.
pub const ADS_GAINS: [u32; 7] = [6, 1, 2, 3, 4, 8, 12];
/// Volts per count on the extension (ExG) channels.
pub const EXT_VOLTS_PER_COUNT: f64 = 5.662_441_253_662_109e-7;
/// Volts per battery-byte step.
pub const BATTERY_VOLTS_PER_COUNT: f64 = 0.039_062_5;

/// Volts per count on the EEG channels at the given amplifier gain.
pub fn eeg_volts_per_count(gain: u32) -> f64 {
    ADC_SPAN_VOLTS / (gain as f64 * ADC_FULL_SCALE)
}

/// Accelerometer reading from raw counts (units as produced by the vendor app).
pub fn acc_units(raw: i32) -> f64 {
    (raw as f64 - 32_768.0) * 0.000_108_991_350_446_428_58
}

/// Battery voltage from the packet's battery byte.
pub fn battery_volts(byte: u8) -> f64 {
    byte as f64 * BATTERY_VOLTS_PER_COUNT
}
