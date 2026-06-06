import Foundation

/// Strip ANSI escape sequences (CSI … final byte) from a log line so the
/// monospaced renderer shows clean text. We don't render color in v1
/// (DESIGN §3.2: "plain monospaced text with ANSI-color stripping").
///
/// Matches ESC '[' then any number of parameter/intermediate bytes
/// (0x30–0x3F, 0x20–0x2F) then a final byte (0x40–0x7E). Also drops a bare
/// trailing ESC. Implemented by hand to avoid an NSRegularExpression dependency
/// on the hot log path.
enum Ansi {
    static func strip(_ s: String) -> String {
        guard s.contains("\u{1B}") else { return s }
        var out = String.UnicodeScalarView()
        let scalars = Array(s.unicodeScalars)
        var i = 0
        while i < scalars.count {
            let c = scalars[i]
            if c == "\u{1B}" {
                // ESC: try to consume a CSI sequence ESC '[' params final.
                if i + 1 < scalars.count, scalars[i + 1] == "[" {
                    i += 2
                    // parameter + intermediate bytes
                    while i < scalars.count {
                        let v = scalars[i].value
                        if (0x30...0x3F).contains(v) || (0x20...0x2F).contains(v) {
                            i += 1
                        } else {
                            break
                        }
                    }
                    // final byte
                    if i < scalars.count, (0x40...0x7E).contains(scalars[i].value) {
                        i += 1
                    }
                    continue
                } else {
                    // lone ESC or other escape (e.g. ESC ] OSC) — drop ESC and
                    // skip to a BEL or ST if it's an OSC; conservatively just
                    // drop the ESC itself.
                    i += 1
                    continue
                }
            }
            out.append(c)
            i += 1
        }
        return String(out)
    }
}
