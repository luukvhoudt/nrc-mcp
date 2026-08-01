//! Numbers that remember how they were written.
//!
//! Round-trip fidelity (§3.2) is the gate for everything else in this project, and
//! the thing that breaks it first is not geometry — it is float formatting. A map
//! saved by Radiant contains `0`, `-64`, `0.5`, `1.0000001` and `8.00000000000000e+00`
//! side by side, because different code paths wrote them. Re-deriving those strings
//! from an `f64` is impossible in general.
//!
//! So we don't try. A [`Num`] keeps the original token text and reprints it verbatim
//! until something actually changes the value, at which point the text is dropped and
//! the number is formatted canonically. An untouched map therefore round-trips
//! byte-for-byte, and a mutated one differs only where it was mutated — which is
//! exactly the diff a reviewer wants to see.

use std::fmt;

/// A numeric literal from a `.map` file, plus the text it was parsed from.
///
/// `PartialEq`/`Hash` are deliberately *semantic*: two `Num`s comparing equal may
/// still serialize differently. Use [`Num::text`] when you care about the bytes.
#[derive(Clone, Debug)]
pub struct Num {
    value: f64,
    /// `None` for values we synthesized or mutated, which format canonically.
    text: Option<Box<str>>,
}

impl Num {
    /// A number parsed from source, preserving `text` for output.
    pub fn parsed(text: &str, value: f64) -> Self {
        Self {
            value,
            text: Some(text.into()),
        }
    }

    /// A synthesized number, formatted canonically on output.
    pub fn new(value: f64) -> Self {
        Self { value, text: None }
    }

    pub fn value(&self) -> f64 {
        self.value
    }

    /// The original token text, if this number came from a file and is unmodified.
    pub fn text(&self) -> Option<&str> {
        self.text.as_deref()
    }

    /// True if this number still reprints as its original bytes.
    pub fn is_verbatim(&self) -> bool {
        self.text.is_some()
    }

    /// Overwrite the value, dropping verbatim text unless the value is unchanged.
    ///
    /// The no-op guard matters: transforms routinely rewrite every coordinate of a
    /// brush while moving only some of them, and we do not want a translate along X
    /// to reformat every Y and Z in the file.
    pub fn set(&mut self, value: f64) {
        if self.value.to_bits() != value.to_bits() {
            self.value = value;
            self.text = None;
        }
    }

    /// True if the value is an exact integer, which is what on-grid geometry means.
    pub fn is_integral(&self) -> bool {
        self.value.is_finite() && self.value.fract() == 0.0
    }
}

impl Default for Num {
    /// A synthesized zero, which formats as `0` rather than reproducing any source text.
    fn default() -> Self {
        Num::new(0.0)
    }
}

impl From<f64> for Num {
    fn from(v: f64) -> Self {
        Num::new(v)
    }
}

impl From<i32> for Num {
    fn from(v: i32) -> Self {
        Num::new(v as f64)
    }
}

impl PartialEq for Num {
    fn eq(&self, other: &Self) -> bool {
        self.value == other.value
    }
}

impl fmt::Display for Num {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.text {
            Some(t) => f.write_str(t),
            None => f.write_str(&fmt_f64(self.value)),
        }
    }
}

/// Canonical formatting for synthesized numbers, matching NetRadiant-custom exactly.
///
/// Upstream's writer is `libs/stream/textstream.h`'s `Decimal`: `snprintf("%10.10lf")`,
/// strip leading spaces, strip trailing `'0'`s, then strip a trailing `'.'`. We
/// reproduce that algorithm rather than using Rust's shortest-round-trip `Display`,
/// because the goal is that a file we write is indistinguishable from one Radiant wrote.
/// Any deviation is a diff that appears the next time a human opens the map and saves it.
///
/// Two consequences are worth knowing, both inherited deliberately:
///
/// - **Negative zero prints as `-0`.** Upstream does this, real maps contain it (see the
///   brush-primitives matrices in `corpus/real/garden_couch.map`), so we do too.
/// - **Precision is capped at 10 decimal places.** Values needing more do not round-trip
///   bit-exactly. This never affects parsed numbers, which keep their source text; it
///   affects only values we compute, and everything on an authoring grid — every power
///   of two down to 2^-10 — is exact.
///
/// Non-finite values have no meaning in a `.map` and would produce a file the compiler
/// rejects with an unhelpful diagnostic, so they are clamped to `0` rather than written.
pub fn fmt_f64(v: f64) -> String {
    if !v.is_finite() {
        return "0".to_string();
    }
    let s = format!("{v:.10}");
    let t = s.trim_end_matches('0');
    let t = t.strip_suffix('.').unwrap_or(t);
    t.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verbatim_text_survives_output() {
        // The whole point: these all parse to the same f64 and must print differently.
        for t in ["0", "0.0", "0.000000", "-0"] {
            let n = Num::parsed(t, 0.0);
            assert_eq!(n.to_string(), t);
        }
    }

    #[test]
    fn canonical_formatting_prefers_integers() {
        assert_eq!(fmt_f64(0.0), "0");
        assert_eq!(fmt_f64(1.0), "1");
        assert_eq!(
            fmt_f64(10.0),
            "10",
            "stripping zeroes must stop at the decimal point"
        );
        assert_eq!(fmt_f64(-64.0), "-64");
        assert_eq!(fmt_f64(0.5), "0.5");
        assert_eq!(fmt_f64(0.25), "0.25");
        assert_eq!(fmt_f64(1024.0), "1024");
        assert_eq!(fmt_f64(100.0), "100");
    }

    #[test]
    fn negative_zero_prints_as_upstream_writes_it() {
        // Upstream's Decimal writer emits "-0", and real maps contain it. Normalizing it
        // to "0" would be tidier and would put us out of step with the editor.
        assert_eq!(fmt_f64(-0.0), "-0");
    }

    #[test]
    fn never_uses_exponent_notation() {
        // Upstream's format string is %f, never %g, so a tiny value spells out its zeroes.
        assert_eq!(fmt_f64(1e-9), "0.000000001");
        assert!(!fmt_f64(1e18).contains('e'));
    }

    #[test]
    fn precision_is_capped_at_ten_decimals_like_upstream() {
        // 2^-10 is exact; 2^-11 needs an eleventh place and is rounded, exactly as the
        // editor would round it. Documented, not accidental.
        assert_eq!(fmt_f64(0.0009765625), "0.0009765625");
        // Ties round to even, matching C's snprintf and therefore upstream: the exact
        // value 0.00048828125 truncates to ...812, not ...813. Verified against
        // `printf("%10.10lf")` rather than assumed.
        assert_eq!(fmt_f64(0.00048828125), "0.0004882812");
        assert_eq!(fmt_f64(1.0 / 3.0), "0.3333333333");
    }

    #[test]
    fn non_finite_is_never_written() {
        assert_eq!(fmt_f64(f64::NAN), "0");
        assert_eq!(fmt_f64(f64::INFINITY), "0");
        assert_eq!(fmt_f64(f64::NEG_INFINITY), "0");
    }

    #[test]
    fn set_to_same_value_keeps_verbatim_text() {
        let mut n = Num::parsed("0.000000", 0.0);
        n.set(0.0);
        assert_eq!(n.to_string(), "0.000000", "a no-op write must not reformat");
    }

    #[test]
    fn set_to_new_value_drops_verbatim_text() {
        let mut n = Num::parsed("0.000000", 0.0);
        n.set(8.0);
        assert_eq!(n.to_string(), "8");
        assert!(!n.is_verbatim());
    }

    #[test]
    fn negative_zero_is_distinguished_by_bits_not_value() {
        // -0.0 == 0.0 in IEEE, so a bitwise guard is required or `set(-0.0)` on a
        // `0` would silently keep the old text while claiming a new value.
        let mut n = Num::parsed("0", 0.0);
        n.set(-0.0);
        assert!(!n.is_verbatim());
    }

    #[test]
    fn values_within_ten_decimals_round_trip_exactly() {
        // Everything an authoring grid produces, plus the texture-matrix scales that
        // appear in real maps (2^-7 and friends).
        for v in [
            0.1,
            0.5,
            1e-5,
            123456.789,
            -0.0078125,
            0.0009765625,
            -64.0,
            0.0,
        ] {
            let s = fmt_f64(v);
            let back: f64 = s.parse().unwrap();
            assert_eq!(back, v, "{s} did not round-trip");
        }
    }
}
