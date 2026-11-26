//! Streaming and recording for Cognionics (CGX) EEG headsets.
//!
//! The wire format implemented here is described, with its sources, in
//! `docs/protocol.md`. The legacy stream decoder and the configuration
//! parser are tested against bytes recorded from a Quick-20 on 2026-11-24.

pub mod bdf;
pub mod chunk;
pub mod config;
pub mod labels;
pub mod legacy;
#[cfg(feature = "python")]
mod python;
pub mod record;
pub mod replay;
pub mod scale;
pub mod timing;
#[cfg(feature = "serial")]
pub mod transport;
