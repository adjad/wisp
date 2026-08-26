import Foundation
import SQLite3
import Compression

// Reads Notes straight from its own on-disk store
// (~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite) instead
// of scripting Notes.app — so syncing notes never has to launch the app. Same
// Full Disk Access grant BrowserHistoryReader/MessagesReader already use.
//
// Verified against the real database on this machine before writing any of
// this (dumped sqlite_master + sampled real rows):
//   - Z_ENT for the "this row is a note" entity is looked up by name
//     ('ICNote') rather than hardcoded — Z_ENT numbering is assigned by
//     Core Data's model and isn't guaranteed stable across app/OS versions.
//   - A note's body lives in ZICNOTEDATA.ZDATA, one row per note (joined via
//     ZICCLOUDSYNCINGOBJECT.ZNOTEDATA), NOT inline on the note row itself.
//   - ZDATA is usually gzip-compressed (magic bytes 1F 8B) but NOT always —
//     small notes on this machine (~160 bytes) were stored as raw,
//     uncompressed protobuf. Both cases must be handled.
//   - Either way the payload is a protobuf blob. Rather than hardcode the
//     exact field-number path to the text (Apple's internal schema has
//     shifted across Notes versions and isn't documented), this walks the
//     whole message tree recursively and takes the LONGEST decodable UTF-8
//     string found anywhere in it. A note's actual body text is reliably far
//     longer than every other string field in the structure (titles, UUIDs,
//     style/attachment metadata), which makes this heuristic robust to
//     schema drift in a way a hardcoded field path would not be.
final class NotesDBReader {
    private let noteLimit = 500   // matches NotesReader's AppleScript `lim`

    private func storePath() -> String? {
        let p = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Group Containers/group.com.apple.notes/NoteStore.sqlite")
        return FileManager.default.fileExists(atPath: p) ? p : nil
    }

    /// Same FS/RS-delimited raw format as NotesReader's AppleScript path
    /// ("epochSecs FS name FS folder FS plaintext RS"), so the backend's
    /// existing parser needs no changes regardless of which reader produced
    /// a given sync. Returns nil only when the store itself couldn't be
    /// opened (no FDA, or Notes not set up).
    func readNotes() -> String? {
        guard let path = storePath() else { return nil }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        guard let noteEnt = entityID(db, name: "ICNote") else { return "" }

        let sql = """
        SELECT n.ZTITLE1, f.ZTITLE2, n.ZMODIFICATIONDATE1, d.ZDATA
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON d.ZNOTE = n.Z_PK
        LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
        WHERE n.Z_ENT = \(noteEnt)
          AND (n.ZMARKEDFORDELETION IS NULL OR n.ZMARKEDFORDELETION = 0)
        ORDER BY n.ZMODIFICATIONDATE1 DESC
        LIMIT \(noteLimit)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return "" }
        defer { sqlite3_finalize(stmt) }

        let FS = "\u{01}", RS = "\u{02}"
        var out = ""
        while sqlite3_step(stmt) == SQLITE_ROW {
            let title = text(stmt, 0) ?? "(untitled)"
            let folder = text(stmt, 1) ?? ""
            // Core Data epoch (2001-01-01) — same constant used across this
            // codebase (MessagesReader, BrowserHistoryReader) for every other
            // Apple store EXCEPT Mail's Envelope Index, which is plain Unix
            // epoch instead (confirmed separately — see MailDBReader).
            let modified = sqlite3_column_double(stmt, 2)
            guard modified > 0 else { continue }
            let epoch = Int64(modified + 978307200)

            guard let blob = sqlite3_column_blob(stmt, 3) else { continue }
            let len = Int(sqlite3_column_bytes(stmt, 3))
            guard len > 0 else { continue }
            let raw = Data(bytes: blob, count: len)
            let payload = gunzipIfNeeded(raw) ?? raw
            let body = ProtobufTextExtractor.longestString(in: payload) ?? ""
            guard !body.isEmpty else { continue }

            out += epoch.description + FS + title + FS + folder + FS + body + RS
        }
        return out
    }

    private func entityID(_ db: OpaquePointer?, name: String) -> Int64? {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = ?", -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, name, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
        guard sqlite3_step(stmt) == SQLITE_ROW else { return nil }
        return sqlite3_column_int64(stmt, 0)
    }

    private func text(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let c = sqlite3_column_text(stmt, idx) else { return nil }
        return String(cString: c)
    }

    /// Decompresses a gzip-wrapped blob, or returns nil if `data` isn't
    /// gzip (no 1F 8B magic) — callers fall back to treating it as raw
    /// protobuf directly, which is what small, uncompressed notes actually
    /// are on this machine.
    private func gunzipIfNeeded(_ data: Data) -> Data? {
        guard data.count > 18, data[data.startIndex] == 0x1f, data[data.startIndex + 1] == 0x8b else { return nil }
        var offset = data.startIndex + 10
        let flags = data[data.startIndex + 3]
        if flags & 0x04 != 0, offset + 2 <= data.endIndex {   // FEXTRA
            let xlen = Int(data[offset]) | (Int(data[offset + 1]) << 8)
            offset += 2 + xlen
        }
        if flags & 0x08 != 0 {   // FNAME
            while offset < data.endIndex, data[offset] != 0 { offset += 1 }
            offset += 1
        }
        if flags & 0x10 != 0 {   // FCOMMENT
            while offset < data.endIndex, data[offset] != 0 { offset += 1 }
            offset += 1
        }
        if flags & 0x02 != 0 { offset += 2 }   // FHCRC
        guard offset < data.endIndex - 8 else { return nil }
        let deflate = data.subdata(in: offset..<(data.endIndex - 8))

        // libcompression's COMPRESSION_ZLIB algorithm decodes raw DEFLATE
        // (no zlib/gzip framing) despite the name — that's exactly what's
        // left once the gzip header/trailer above are stripped.
        var capacity = max(deflate.count * 8, 4096)
        while capacity <= 32 * 1024 * 1024 {
            let decoded = deflate.withUnsafeBytes { srcPtr -> Data? in
                guard let src = srcPtr.bindMemory(to: UInt8.self).baseAddress else { return nil }
                var dst = [UInt8](repeating: 0, count: capacity)
                let n = compression_decode_buffer(&dst, capacity, src, deflate.count, nil, COMPRESSION_ZLIB)
                guard n > 0 else { return nil }
                if n == capacity { return nil }   // likely truncated — grow and retry
                return Data(dst[0..<n])
            }
            if let decoded { return decoded }
            capacity *= 4
        }
        return nil
    }
}

/// Generic, schema-agnostic protobuf reader used only to pull the single
/// longest embedded string out of an arbitrary message — see NotesDBReader's
/// header comment for why "longest string anywhere in the tree" stands in
/// for "the note body" without needing Apple's exact (undocumented,
/// version-shifting) field layout.
enum ProtobufTextExtractor {
    static func longestString(in data: Data) -> String? {
        var best: String?
        walk(data, into: &best)
        return best
    }

    private static func walk(_ data: Data, into best: inout String?) {
        var i = data.startIndex
        while i < data.endIndex {
            guard let (tag, next) = readVarint(data, i) else { return }
            i = next
            let wireType = tag & 0x7
            switch wireType {
            case 0:   // varint field — no string content, just skip it
                guard let (_, n) = readVarint(data, i) else { return }
                i = n
            case 1:   // fixed64
                guard i + 8 <= data.endIndex else { return }
                i += 8
            case 5:   // fixed32
                guard i + 4 <= data.endIndex else { return }
                i += 4
            case 2:   // length-delimited — could be a string OR a nested message
                guard let (len, n) = readVarint(data, i) else { return }
                let end = n + Int(len)
                guard len >= 0, end <= data.endIndex, end >= n else { return }
                let chunk = data.subdata(in: n..<end)
                if let s = decodePlausibleText(chunk), s.count > (best?.count ?? 0) {
                    best = s
                }
                // Recurse regardless — a valid UTF-8 string can also happen
                // to parse as a (nonsensical) nested message, so decoding
                // both directions and keeping whichever wins on length is
                // safer than picking one interpretation up front.
                walk(chunk, into: &best)
                i = end
            default:
                return   // wire type 3/4 (deprecated group markers) — bail
            }
        }
    }

    /// Only treat a length-delimited field as candidate body text when it
    /// looks like real prose: valid UTF-8, no NUL/control bytes (those
    /// appear constantly in nested submessages, never in plain text), and
    /// long enough that a UUID or short metadata string can't win by luck.
    private static func decodePlausibleText(_ chunk: Data) -> String? {
        guard chunk.count >= 4, let s = String(data: chunk, encoding: .utf8) else { return nil }
        for scalar in s.unicodeScalars {
            if scalar.value < 0x20 && scalar != "\n" && scalar != "\t" { return nil }
        }
        return s
    }

    private static func readVarint(_ data: Data, _ start: Data.Index) -> (UInt64, Data.Index)? {
        var result: UInt64 = 0
        var shift: UInt64 = 0
        var i = start
        while i < data.endIndex {
            let byte = data[i]
            result |= UInt64(byte & 0x7f) << shift
            i += 1
            if byte & 0x80 == 0 { return (result, i) }
            shift += 7
            if shift >= 64 { return nil }
        }
        return nil
    }
}
