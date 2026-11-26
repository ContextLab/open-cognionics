//! With the `python` feature, tests, examples and binaries link to libpython
//! (maturin builds the extension module with `PYO3_BUILD_EXTENSION_MODULE`
//! set, which does not). This adds an rpath to the libpython directory so
//! those executables find it at run time; when libpython is not linked it
//! adds nothing.

fn main() {
    #[cfg(feature = "python")]
    pyo3_build_config::add_libpython_rpath_link_args();
}
